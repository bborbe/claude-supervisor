// Unit tests for the session-liveness probe.
//
// Two regressions these exist for, both found live rather than reasoned about:
//
// 1. `sessionIsLive` was `pgrep -fl <id>` alone, and a session started fresh carries
//    its id nowhere in argv, so the guard read a live session as closed and would have
//    allowed the resume it exists to refuse.
// 2. The probe added to fix (1) matched the FULL COMMAND LINE of any process, so a
//    finished worker whose id was merely MENTIONED — by a shell, a watcher, a grep —
//    read as live and became unresumable. The bystander test below fails against that
//    implementation, and it is the reason no argv probe survives in liveness.mjs.
//
// They do NOT replace the end-to-end run: that a refused resume leaves no second
// writer on the conversation is an integration fact these tests cannot see.

import { test } from 'node:test'
import assert from 'node:assert/strict'
import { existsSync, mkdtempSync, rmSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { HEARTBEAT_TTL_MS, stampPath, stampRecord } from './heartbeat.mjs'
import {
  checkLiveness,
  findRegisteredByName,
  liveSessionIds,
  pidIsAlive,
  readRegistry,
  registeredAsLive,
  sessionIdsNamed,
  uniqueTabName,
} from './liveness.mjs'

const SESSION = '63c613e0-7fd8-4dc3-b05e-c43bbf89be58'
const OTHER = '4ff785a3-c2b9-45cc-98d9-83720ff1883d'

// A pid that cannot be running: above macOS's maximum, so `kill -0` is ESRCH.
const DEAD_PID = 999999

function fixture(files) {
  const dir = mkdtempSync(join(tmpdir(), 'supervisor-liveness-'))
  for (const [name, body] of Object.entries(files)) {
    writeFileSync(join(dir, name), typeof body === 'string' ? body : JSON.stringify(body))
  }
  return dir
}

// The registry fixture in which the id is registered nowhere — the state a finished
// worker is in, and the state a bystander used to be able to override.
function registryWithout(sessionId) {
  return fixture({ '1.json': { pid: process.pid, sessionId } })
}

test('pidIsAlive confirms this process and rejects a pid that is gone', () => {
  assert.equal(pidIsAlive(process.pid), true)
  assert.equal(pidIsAlive(DEAD_PID), false)
  assert.equal(pidIsAlive(0), false)
  assert.equal(pidIsAlive('1234'), false)
  assert.equal(pidIsAlive(undefined), false)
})

test('readRegistry reports "no information" for a missing directory, not emptiness', () => {
  assert.equal(readRegistry('/nonexistent/supervisor/sessions'), null)
})

test('readRegistry skips files that are unparsable or not registry entries', () => {
  const dir = fixture({
    '1.json': { pid: 1, sessionId: SESSION },
    'broken.json': '{ not json',
    'foreign.json': { hello: 'world' },
    'notes.txt': 'ignored entirely',
  })
  try {
    const entries = readRegistry(dir)
    assert.equal(entries.length, 1)
    assert.equal(entries[0].sessionId, SESSION)
  } finally {
    rmSync(dir, { recursive: true, force: true })
  }
})

test('registeredAsLive matches the session id against a running pid', () => {
  const dir = fixture({ '1.json': { pid: process.pid, sessionId: SESSION } })
  try {
    assert.equal(registeredAsLive(SESSION, { dir }), true)
    assert.equal(registeredAsLive(OTHER, { dir }), false)
  } finally {
    rmSync(dir, { recursive: true, force: true })
  }
})

test('registeredAsLive ignores a stale entry whose pid is gone', () => {
  const dir = fixture({ '1.json': { pid: DEAD_PID, sessionId: SESSION } })
  try {
    assert.equal(registeredAsLive(SESSION, { dir }), false, 'a crashed session must not read as live forever')
  } finally {
    rmSync(dir, { recursive: true, force: true })
  }
})

test('checkLiveness finds a live session the registry knows and no process probe could see', () => {
  const dir = fixture({ '1.json': { pid: process.pid, sessionId: SESSION } })
  try {
    const verdict = checkLiveness(SESSION, { dir })
    assert.equal(verdict.live, true, 'this is the fresh-session case a pgrep-alone probe missed')
    assert.deepEqual(verdict.probes, ['registry'])
  } finally {
    rmSync(dir, { recursive: true, force: true })
  }
})

test('a session id mentioned in some other process command line does not read as live', () => {
  // The regression this file exists for, stated as the guard meets it: the registry
  // does not know this id, and something on the machine merely MENTIONS it.
  //
  // Against the `pgrep -fl <id>` probe this replaces, this test fails — the bystander
  // matches, the verdict is `live: true`, and a finished worker becomes unresumable.
  // Measured live 2026-09-20: a manager's own completion watcher held the id in its
  // argv, so the session that armed the watcher could not resume the worker it had
  // just watched finish.
  const dir = registryWithout(OTHER)
  try {
    const verdict = checkLiveness(SESSION, { dir })
    assert.equal(verdict.live, false, 'a bystander mention must not forbid a resume')
    assert.notEqual(verdict.live, true, 'the two-writers guard must not fire on a process that is not the session')
  } finally {
    rmSync(dir, { recursive: true, force: true })
  }
})

test('checkLiveness reports "could not tell" when the registry is unreadable', () => {
  // Hermetic: the heartbeat dir is a fixture too, so this asserts the unreadable-registry
  // path rather than whatever happens to be stamped on the machine running the suite.
  const beats = mkdtempSync(join(tmpdir(), 'hb-'))
  try {
    const verdict = checkLiveness(SESSION, { dir: '/nonexistent/supervisor/sessions', heartbeatDir: beats })
    assert.equal(verdict.live, null, 'unreadable must never be folded into "closed"')
    assert.deepEqual(verdict.probes, [], 'no probe produced a verdict, so the caller cannot read this as "confirmed closed"')
  } finally {
    rmSync(beats, { recursive: true, force: true })
  }
})

test('checkLiveness reports closed only when a probe ran and found nothing', () => {
  const dir = registryWithout(OTHER)
  const beats = mkdtempSync(join(tmpdir(), 'hb-'))
  try {
    const verdict = checkLiveness(SESSION, { dir, heartbeatDir: beats })
    assert.equal(verdict.live, false)
    assert.deepEqual(
      verdict.probes,
      ['registry', 'heartbeat'],
      'both probes ran and found nothing — this is a real "closed", not an unreadable store',
    )
  } finally {
    rmSync(dir, { recursive: true, force: true })
    rmSync(beats, { recursive: true, force: true })
  }
})

// ---------------------------------------------------------------------------
// The cross-server blind spot, CLOSED — [[A Non-Spawning Session Cannot Decide a
// Headless Worker's Liveness]].
//
// A headless worker is an in-process SDK `query()` owned by the supervisor server
// that spawned it. It writes no registry entry (it holds no socket), so the
// registry cannot see it at all — and the spawning server's `agents` Map, which
// can, is per-process, so the manager that did not spawn the worker has neither
// channel. It read the same decisive `false` for a worker genuinely mid-turn and
// for one that finished an hour ago, and the guard acts on `false` by ALLOWING the
// resume. Two writers landed on one conversation.
//
// These two tests used to CHARACTERISE that blind spot: both cases had to produce
// the same verdict, and the file said the first assertion would fail once a
// decisive cross-server probe landed — "that failure is the signal the fix works,
// so update it deliberately rather than deleting it". The heartbeat store
// (`heartbeat.mjs`) is that probe, so they are updated here rather than removed.
//
// What they now assert is the property the fix buys: the live case and the
// finished case READ DIFFERENTLY. That is the whole claim, and it is deliberately
// the assertion the old pair could not make.

test('a live cross-server headless worker reads as live from a server that did not spawn it', () => {
  // The registry still does not know this id — that is unchanged, and it is what a
  // headless worker looks like from any server that did not spawn it. What changed is
  // that the owning server's knowledge is now on disk: a fresh stamp is the worker
  // saying "still being worked" through a channel every process can read.
  const dir = registryWithout(OTHER)
  const beats = mkdtempSync(join(tmpdir(), 'hb-'))
  try {
    stampRecord(beats, { sessionId: SESSION, pid: process.pid, mode: 'headless' })
    const asSeenFromAnotherServer = checkLiveness(SESSION, { dir, heartbeatDir: beats })

    assert.equal(
      asSeenFromAnotherServer.live,
      true,
      'THE FIX: a genuinely live headless worker is visible to a non-spawning server',
    )
    assert.ok(
      asSeenFromAnotherServer.probes.includes('heartbeat'),
      'the heartbeat is the probe that found it — the registry has no entry to find',
    )
  } finally {
    rmSync(dir, { recursive: true, force: true })
    rmSync(beats, { recursive: true, force: true })
  }
})

test('the heartbeat separates a live headless worker from a finished one', () => {
  // The property the fix must establish, stated as the thing the registry could not do:
  // give this module both channels and ask it about a live headless worker and a finished
  // one. The two answers must now DIFFER, so a caller can branch on them.
  //
  // The registry holds an UNRELATED session, so the registry probe genuinely runs and finds
  // nothing for both ids — the decisive-negative case, not the unreadable one. The ONLY
  // difference between the two ids is the stamp, which is exactly the variable under test.
  const dir = fixture({ '1.json': { pid: process.pid, sessionId: 'a1b2c3d4-0000-4000-8000-000000000000' } })
  const beats = mkdtempSync(join(tmpdir(), 'hb-'))
  try {
    stampRecord(beats, { sessionId: SESSION, pid: process.pid, mode: 'headless' })

    const liveHeadlessWorker = checkLiveness(SESSION, { dir, heartbeatDir: beats })
    const finishedHeadlessWorker = checkLiveness(OTHER, { dir, heartbeatDir: beats })

    assert.equal(liveHeadlessWorker.live, true, 'the stamped worker is live')
    assert.equal(finishedHeadlessWorker.live, false, 'the unstamped worker is not')
    assert.notEqual(
      liveHeadlessWorker.live,
      finishedHeadlessWorker.live,
      'THE FIX: the two cases no longer read the same, so a non-spawning server can decide',
    )
  } finally {
    rmSync(dir, { recursive: true, force: true })
    rmSync(beats, { recursive: true, force: true })
  }
})

test('a stale stamp reads not-alive without anyone clearing it', () => {
  // The kill -9 case, and the reason the stamp is a heartbeat rather than a flag. Nothing
  // unlinks this file — the owning server was killed mid-turn and never ran its clear — so
  // the verdict has to come from the stamp's AGE. A write-at-spawn / delete-on-graceful-exit
  // design passes every graceful case and fails exactly here, which is why this test exists.
  const dir = registryWithout(OTHER)
  const beats = mkdtempSync(join(tmpdir(), 'hb-'))
  try {
    stampRecord(beats, { sessionId: SESSION, pid: process.pid, mode: 'headless' })

    // Read as of a moment past the TTL, with the file still on disk and nothing having
    // deleted it. `now` is injected rather than slept for: the property under test is the
    // age comparison, not the clock.
    const afterTtl = checkLiveness(SESSION, {
      dir,
      heartbeatDir: beats,
      now: Date.now() + HEARTBEAT_TTL_MS + 1000,
    })

    assert.equal(afterTtl.live, false, 'a stamp older than the TTL is not evidence of life')
    assert.ok(
      existsSync(stampPath(beats, SESSION)),
      'the file is still there — the verdict came from its age, not from its absence',
    )
  } finally {
    rmSync(dir, { recursive: true, force: true })
    rmSync(beats, { recursive: true, force: true })
  }
})

// The tab name is the join between a spawned session and its id, and a name is NOT
// unique. `findRegisteredByName` returned the FIRST entry matching the name, so a label
// reused while an earlier worker still held it resolved the new spawn to THAT worker —
// and its id, not the new session's, is what went into the ledger record. Nothing in the
// old implementation could tell "the session I just started" from "a session that was
// already there".
//
// Measured live 2026-09-22: a spawn whose label collided returned `sessionId: null` and
// wrote no ledger record, while the same call with a free label resolved and wrote one.
// The name is what differs, so the name is what the spawn has to make unambiguous.

test('findRegisteredByName will not hand back a holder the caller already knew about', () => {
  const dir = fixture({ '1.json': { pid: process.pid, sessionId: SESSION, name: '⚙ reused' } })
  try {
    // Pre-fix behaviour: the pre-existing holder IS the answer, indistinguishable from a
    // session that registered a moment ago.
    assert.equal(findRegisteredByName('⚙ reused', { dir }), SESSION)
    // Excluding it leaves nothing — "not there yet", which the caller polls on, rather
    // than a stranger's id it would otherwise record as its own.
    assert.equal(findRegisteredByName('⚙ reused', { dir, exclude: new Set([SESSION]) }), null)
  } finally {
    rmSync(dir, { recursive: true, force: true })
  }
})

test('findRegisteredByName with an exclusion rejects every holder, not just the first read', () => {
  // Two holders of one name: which one `find` returns is a readdir order the caller
  // cannot know, so the exclusion has to reject BOTH rather than whichever it saw.
  const dir = fixture({
    '1.json': { pid: process.pid, sessionId: SESSION, name: '⚙ reused' },
    '2.json': { pid: process.pid, sessionId: OTHER, name: '⚙ reused' },
  })
  try {
    assert.ok(
      [SESSION, OTHER].includes(findRegisteredByName('⚙ reused', { dir })),
      'pre-fix, one of the two holders is returned as if it were the session just started',
    )
    assert.equal(findRegisteredByName('⚙ reused', { dir, exclude: new Set([SESSION, OTHER]) }), null)
  } finally {
    rmSync(dir, { recursive: true, force: true })
  }
})

test('sessionIdsNamed reports every holder, and null when the registry is unreadable', () => {
  const dir = fixture({
    '1.json': { pid: process.pid, sessionId: SESSION, name: '⚙ reused' },
    '2.json': { pid: process.pid, sessionId: OTHER, name: '⚙ reused' },
    '3.json': { pid: process.pid, sessionId: 'a1b2c3d4-0000-4000-8000-000000000000', name: '⚙ other' },
  })
  try {
    assert.deepEqual(sessionIdsNamed('⚙ reused', { dir }).sort(), [SESSION, OTHER].sort())
    assert.deepEqual(sessionIdsNamed('⚙ nobody', { dir }), [])
    assert.equal(sessionIdsNamed('⚙ reused', { dir: '/nonexistent/supervisor/sessions' }), null)
  } finally {
    rmSync(dir, { recursive: true, force: true })
  }
})

test('uniqueTabName suffixes past every holder so the join can only be the new session', () => {
  const dir = fixture({
    '1.json': { pid: process.pid, sessionId: SESSION, name: '⚙ dup' },
    '2.json': { pid: process.pid, sessionId: OTHER, name: '⚙ dup (2)' },
  })
  try {
    assert.equal(uniqueTabName('⚙ dup', { dir }), '⚙ dup (3)')
    assert.equal(uniqueTabName('⚙ free', { dir }), '⚙ free')
  } finally {
    rmSync(dir, { recursive: true, force: true })
  }
})

test('uniqueTabName leaves the name alone when the registry cannot be read', () => {
  // An unreadable registry is "no information", not "nothing holds this name": renaming
  // on that answer would rename on every spawn whenever the directory is briefly gone.
  assert.equal(uniqueTabName('⚙ x', { dir: '/nonexistent/supervisor/sessions' }), '⚙ x')
})

test('liveSessionIds REQUIRES isAlive — a default would silently pick one caller\'s reading', () => {
  // This function is the one home for two callers that want OPPOSITE answers: the cap counter
  // wants presence (over-counting fails safe), a roster wants pid-checked liveness (presence
  // resurrects a crashed session's left-behind file). A default would answer one of them
  // wrongly and say nothing, which is the failure this throw exists to make loud.
  assert.throws(() => liveSessionIds({ registryDir: '/nonexistent' }), /needs an explicit isAlive/)
})

test('liveSessionIds treats a null heartbeatDir as unset, not as a directory named null', () => {
  const dir = mkdtempSync(join(tmpdir(), 'liveness-null-beats-'))
  try {
    // `checkLiveness` still passes `heartbeatDir: null` to mean "use the default". A default
    // PARAMETER fires only on `undefined`, so a null would reach `listLive({ dir: null })` —
    // where `readdirSync(null)` raises ERR_INVALID_ARG_TYPE (not ENOENT), so `listLive`'s own
    // catch answers `null` and the union returns null: a permissions-shaped answer for a
    // caller that meant "default".
    //
    // Asserted as EQUIVALENCE rather than `instanceof Map`, deliberately. The default is
    // module config and cannot be injected, so both spellings read the machine's real
    // heartbeat store; on a host where that store is unreadable both answer null and the
    // assertion still holds, because what is pinned is that `null` and `undefined` take the
    // SAME path. The defect this guards produces the opposite — null for one, a Map for the
    // other — so the comparison still fails on it.
    const viaNull = liveSessionIds({ registryDir: dir, heartbeatDir: null, isAlive: () => true })
    const viaUndefined = liveSessionIds({ registryDir: dir, isAlive: () => true })
    assert.deepEqual([...(viaNull ?? [])], [...(viaUndefined ?? [])])
  } finally {
    rmSync(dir, { recursive: true, force: true })
  }
})
