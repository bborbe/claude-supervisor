// Unit tests for the headless-worker heartbeat store.
//
// The property that matters is not "the functions work" — it is that the WRITE and the READ
// can happen in different processes, because that is the entire reason this module exists.
// The spawning server knows its workers from an in-process Map; the manager that must decide
// whether to resume one has no access to that Map. So a same-process round trip proves
// nothing here: it would pass just as well against an in-memory variable, which is the
// design that already failed. The cross-process test below is therefore the load-bearing one,
// and it spawns a real child rather than mocking the boundary.
//
// The second property is the kill -9 case. A marker written at spawn and unlinked on a
// graceful exit passes every test that only ever ends its worker politely; it leaves a
// permanent "running" behind when the server is SIGKILLed mid-turn, which is the defect the
// task exists to fix. The staleness tests inject `now` rather than sleeping, so the age
// comparison is exercised without a 60-second test.

import { test } from 'node:test'
import assert from 'node:assert/strict'
import { execFileSync } from 'node:child_process'
import { mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import {
  HEARTBEAT_INTERVAL_MS,
  HEARTBEAT_TTL_MS,
  clearStamp,
  listLive,
  readLive,
  readState,
  stampPath,
  stampRecord,
  startSelfStamp,
  sweepStale,
} from './heartbeat.mjs'

const SESSION = 'fa942d7a-c190-46dd-93f3-8dfe3280046b'
const OTHER = '11111111-2222-4333-8444-555555555555'

const withDir = (fn) => {
  const dir = mkdtempSync(join(tmpdir(), 'hb-test-'))
  try {
    return fn(dir)
  } finally {
    rmSync(dir, { recursive: true, force: true })
  }
}

test('a stamp is readable by the process that wrote it', () => {
  withDir((dir) => {
    stampRecord(dir, { sessionId: SESSION, pid: process.pid, mode: 'headless' })
    assert.equal(readLive(SESSION, { dir }).live, true)
  })
})

test('a stamp written by ANOTHER process is readable here — the whole point', () => {
  // Spawned, not mocked. If this module's read path depended on anything process-local —
  // an in-memory Map, a module-level cache — this test fails and the ones above still pass,
  // which is exactly the asymmetry that makes it the one worth having.
  withDir((dir) => {
    const script = `
      import { stampRecord } from ${JSON.stringify(new URL('./heartbeat.mjs', import.meta.url).href)}
      stampRecord(${JSON.stringify(dir)}, { sessionId: ${JSON.stringify(SESSION)}, pid: process.pid, mode: 'headless' })
      console.log('stamped by', process.pid)
    `
    const childPid = Number(
      execFileSync(process.execPath, ['--input-type=module', '-e', script], { encoding: 'utf8' }).trim().replace('stamped by ', ''),
    )

    assert.notEqual(childPid, process.pid, 'the write must have happened in a different process')

    const verdict = readLive(SESSION, { dir })
    assert.equal(verdict.live, true, 'a non-spawning process reads a worker the child stamped')

    const [entry] = listLive({ dir })
    assert.equal(entry.sessionId, SESSION, 'the reader names the worker by session id')
    assert.equal(entry.pid, childPid, 'and reports the pid that stamped it')
  })
})

test('a worker with no stamp reads not-alive, and does so decisively', () => {
  withDir((dir) => {
    const verdict = readLive(OTHER, { dir })
    assert.equal(verdict.live, false)
    assert.notEqual(verdict.live, null, 'an absent stamp is a negative, not an unreadable channel')
  })
})

test('a stamp older than the TTL reads not-alive while the file is still on disk', () => {
  // The SIGKILL case: nothing cleared this file, because the server that would have was
  // killed mid-turn. The verdict must come from the age, not from the absence.
  withDir((dir) => {
    stampRecord(dir, { sessionId: SESSION, pid: process.pid, mode: 'headless' })
    assert.equal(readLive(SESSION, { dir, now: Date.now() + HEARTBEAT_TTL_MS + 1 }).live, false)
  })
})

test('the TTL leaves a whole missed tick of slack over the interval', () => {
  // With TTL === INTERVAL a live worker reads dead for up to 2x, because the newest stamp is
  // already one full interval old when the next is due. The bound is only meaningful if the
  // two constants are related, so the relationship is asserted rather than assumed.
  assert.ok(
    HEARTBEAT_TTL_MS >= HEARTBEAT_INTERVAL_MS * 2,
    'the TTL must cover one missed interval, or a live worker reads dead between stamps',
  )
})

test('clearStamp removes the stamp, so the next reader does not wait out a TTL', () => {
  withDir((dir) => {
    stampRecord(dir, { sessionId: SESSION, pid: process.pid, mode: 'headless' })
    clearStamp(SESSION, { dir })
    assert.equal(readLive(SESSION, { dir }).live, false)
  })
})

test('listLive reports only the fresh stamps', () => {
  withDir((dir) => {
    stampRecord(dir, { sessionId: SESSION, pid: process.pid, mode: 'headless' })
    // A stale sibling, written directly so its mtime is set explicitly.
    writeFileSync(stampPath(dir, OTHER), '{"sessionId":"' + OTHER + '"}\n', { flag: 'w' })
    const past = Date.now() - HEARTBEAT_TTL_MS - 60_000
    execFileSync('touch', ['-t', new Date(past).toISOString().replace(/[-:T]/g, '').slice(0, 12), stampPath(dir, OTHER)])

    const live = listLive({ dir })
    assert.deepEqual(
      live.map((entry) => entry.sessionId),
      [SESSION],
      'the stale stamp is not a live worker, and the fresh one is',
    )
  })
})

test('sweepStale removes exactly the stale stamps', () => {
  withDir((dir) => {
    stampRecord(dir, { sessionId: SESSION, pid: process.pid, mode: 'headless' })
    writeFileSync(stampPath(dir, OTHER), '{"sessionId":"' + OTHER + '"}\n')
    const past = Date.now() - HEARTBEAT_TTL_MS - 60_000
    execFileSync('touch', ['-t', new Date(past).toISOString().replace(/[-:T]/g, '').slice(0, 12), stampPath(dir, OTHER)])

    assert.deepEqual(sweepStale({ dir }), [OTHER], 'only the stale one is swept')
    assert.equal(readLive(SESSION, { dir }).live, true, 'the live worker survives the sweep')
  })
})

test('an unreadable directory is "could not tell", never a confident not-alive', () => {
  // The caller acts on `false` by ALLOWING a resume, so an unreadable store must not be
  // folded into a negative — that turns an I/O error into permission to start a second
  // writer on one conversation. A path that exists but is not a directory is the cheap
  // stand-in for a permissions failure, and it is a different error from ENOENT.
  withDir((dir) => {
    const notADir = join(dir, 'a-file')
    mkdirSync(notADir)
    writeFileSync(join(notADir, 'x'), '')
    const verdict = readLive(SESSION, { dir: join(notADir, 'x') })
    assert.equal(verdict.live, null, 'an unreadable store is not evidence of death')
    assert.equal(listLive({ dir: join(notADir, 'x') }), null)
  })
})

test('an absent directory is a real negative, not "could not tell"', () => {
  // ENOENT means no worker has ever been stamped here, which is an answer. Folding it into
  // null would make a fresh machine's first read unreadable rather than empty.
  withDir((dir) => {
    assert.equal(readLive(SESSION, { dir: join(dir, 'nope') }).live, false)
    assert.deepEqual(listLive({ dir: join(dir, 'nope') }), [])
  })
})

// The session-liveness fields, added so the attention store can answer "which session is
// alive, doing what, on which task" without reading the supervisor's in-process state.
//
// ⚠️ The load-bearing property here is ADDITIVE COMPATIBILITY, not "the fields appear". The
// store directory is shared: a reader keyed on the old shape must keep working, so a stamp
// from a writer that knows none of these fields has to be byte-for-byte what it was before.
// A test that only checked the new fields would pass on a writer that had started emitting
// `"task": null` into every headless stamp.
test('the session-liveness fields are carried when the writer sets them', () => {
  withDir((dir) => {
    stampRecord(dir, {
      sessionId: SESSION,
      pid: process.pid,
      mode: 'local',
      source: 'mcp-timer',
      task: 'Session Liveness Comes From a Heartbeat Store in attention-controller',
      vault: 'private-personal',
      location: 'local',
      activity: 'idle',
    })
    const row = JSON.parse(readFileSync(stampPath(dir, SESSION), 'utf8'))
    assert.equal(row.source, 'mcp-timer')
    assert.equal(row.task, 'Session Liveness Comes From a Heartbeat Store in attention-controller')
    assert.equal(row.vault, 'private-personal')
    assert.equal(row.location, 'local')
    assert.equal(row.activity, 'idle')
  })
})

// ⚠️ THE GUARD. `state` on a stamp is the name the liveness VERDICT uses: four readers gate on
// `stamp.get("state", "live") != "live"`, where a MISSING key must read as `live` rather than
// vanish.
//
// ⚠️ **This comment first claimed the collision was LIVE — that an activity written into that
// key "reads as NOT-live", so a live session would draw no fleet-board row and free a duplicate
// auto-resume. That was wrong**, and it was verified wrong by running the reader: `live-workers.py`'s
// `readLive` is the single chokepoint that parses a raw stamp, and it builds a fresh object
// carrying its own verdict without ever reading the file's — so NO reader consumes a raw
// stamp's `state` at all. The key is write-only today, which is what makes the rename cheap now
// and expensive once a raw-stamp reader exists.
//
// A key name is exactly the kind of thing a later refactor restores by accident, so it is
// pinned rather than described — the assertion below is what the comment above it cannot be.
test("a stamp never carries the activity under the readers' `state` key", () => {
  withDir((dir) => {
    stampRecord(dir, { sessionId: SESSION, pid: process.pid, mode: 'local', activity: 'busy' })
    const row = JSON.parse(readFileSync(stampPath(dir, SESSION), 'utf8'))
    assert.equal(row.activity, 'busy')
    assert.equal(row.state, undefined, 'an activity under `state` collides with the verdict vocabulary')
    // The exact key set too, so a future field cannot land on the verdict key unnoticed.
    assert.deepEqual(Object.keys(row).sort(), ['activity', 'at', 'mode', 'pid', 'sessionId'])
  })
})

test('a writer that knows none of the new fields emits exactly the old shape', () => {
  withDir((dir) => {
    stampRecord(dir, { sessionId: SESSION, pid: process.pid, mode: 'headless' })
    const row = JSON.parse(readFileSync(stampPath(dir, SESSION), 'utf8'))
    assert.deepEqual(Object.keys(row).sort(), ['at', 'mode', 'pid', 'sessionId'])
  })
})

test('task and vault are written together or not at all', () => {
  // ⚠️ A task name collides across vaults, so half an anchor cannot be resolved later — the
  // attention store rejects one without the other, so the writer must not be the thing that
  // produces an unresolvable row.
  withDir((dir) => {
    stampRecord(dir, { sessionId: SESSION, pid: process.pid, mode: 'local', task: 'orphan-task' })
    const row = JSON.parse(readFileSync(stampPath(dir, SESSION), 'utf8'))
    assert.equal(row.task, undefined, 'a task with no vault must not be written')
    assert.equal(row.vault, undefined)
  })
})

// What the session is doing — the event half of the heartbeat, recorded by a hook and read
// by the timer on its next tick.
//
// ⚠️ The load-bearing property is that a MISSING record reads as `null` and never as a
// guessed state. A wrong state reads as knowledge; an absent one reads as absence, and the
// caller turns it into the documented `idle`.
test('readState returns the recorded state, and null when no hook has fired', () => {
  withDir((dir) => {
    assert.equal(readState(SESSION, { dir }), null, 'no record must read as null, never a guess')
    writeFileSync(join(dir, `${SESSION}.json`), JSON.stringify({ session_id: SESSION, state: 'busy' }))
    assert.equal(readState(SESSION, { dir }), 'busy')
  })
})

test('readState refuses a state outside the vocabulary', () => {
  // ⚠️ The state directory is shared, so a hand-edited or older record must not put a value on
  // the wire that no reader's vocabulary contains — the store passes enums through verbatim.
  withDir((dir) => {
    writeFileSync(join(dir, `${SESSION}.json`), JSON.stringify({ state: 'napping' }))
    assert.equal(readState(SESSION, { dir }), null)
  })
})

test('readState tolerates an unreadable record', () => {
  withDir((dir) => {
    writeFileSync(join(dir, `${SESSION}.json`), '{')
    assert.equal(readState(SESSION, { dir }), null)
  })
})

test('readState refuses an id that could escape the state directory', () => {
  // ⚠️ The id becomes a FILENAME. The write side refuses a separator or a `..`; a reader that
  // joined an unchecked id would be the other half of the same traversal.
  withDir((dir) => {
    for (const bad of ['../escaped', '..', '.', 'a/b', 'a\\b', '']) {
      assert.equal(readState(bad, { dir }), null, `readState must refuse ${JSON.stringify(bad)}`)
    }
  })
})

// The self-stamp lifecycle — the session this server runs INSIDE.
//
// ⚠️ These are reachable ONLY because the lifecycle lives in `heartbeat.mjs`. It used to sit in
// `supervisor.mjs`, which has no export surface (a test importing it would start an MCP
// server), so none of this could be asserted at all — and `stop` clearing the stamp shipped
// untested for exactly that reason.
test('startSelfStamp stamps the local mode and the current activity', () => {
  withDir((dir) => {
    const self = startSelfStamp(SESSION, { dir, intervalMs: 60_000, readActivity: () => 'busy' })
    assert.ok(self, 'a session id must produce a stamp')
    const row = JSON.parse(readFileSync(stampPath(dir, SESSION), 'utf8'))
    assert.equal(row.mode, 'local')
    assert.equal(row.source, 'mcp-timer')
    assert.equal(row.location, 'local')
    assert.equal(row.activity, 'busy')
    // ⚠️ No anchor: the store accepts an unanchored row by design, and resolving one would put
    // a vault read on the liveness path.
    assert.equal(row.task, undefined)
    assert.equal(row.vault, undefined)
    self.stop()
  })
})

test('startSelfStamp reports idle when no hook has fired', () => {
  // ⚠️ `idle`, never a guessed `busy`: a wrong state reads as knowledge while an absent one
  // reads as absence.
  withDir((dir) => {
    const self = startSelfStamp(SESSION, { dir, intervalMs: 60_000, readActivity: () => null })
    assert.equal(JSON.parse(readFileSync(stampPath(dir, SESSION), 'utf8')).activity, 'idle')
    self.stop()
  })
})

test('startSelfStamp is a no-op with no session id', () => {
  // ⚠️ A server started outside a session must stamp NOTHING rather than stamping under an
  // invented key — a row under a made-up id would be a permanent phantom reading Live until
  // its TTL, with no session able to clear it.
  withDir((dir) => {
    assert.equal(startSelfStamp(null, { dir }), null)
    assert.equal(startSelfStamp('', { dir }), null)
    assert.deepEqual(listLive({ dir }), [], 'no id must write no file')
  })
})

test('stop clears the stamp, so the next reader does not wait out a TTL', () => {
  // ⚠️ The change this test exists for: `stopSelf` used to clear only the timer, so a restarted
  // server left its own row reading live for a full TTL — and the self-stamp is the one row an
  // operator is most likely to be looking at.
  withDir((dir) => {
    const self = startSelfStamp(SESSION, { dir, intervalMs: 60_000 })
    assert.equal(readLive(SESSION, { dir }).live, true, 'stamped, so live')
    self.stop()
    assert.equal(readLive(SESSION, { dir }).live, false, 'stop must clear the stamp, not just the timer')
  })
})

test('the self-stamp is re-written on each tick, picking up a changed activity', async () => {
  // ⚠️ Read per tick rather than captured once: a value captured at start would report the
  // state the session had when the server came up for the rest of its life.
  const dir = mkdtempSync(join(tmpdir(), 'hb-test-'))
  try {
    let activity = 'idle'
    const self = startSelfStamp(SESSION, { dir, intervalMs: 10, readActivity: () => activity })
    assert.equal(JSON.parse(readFileSync(stampPath(dir, SESSION), 'utf8')).activity, 'idle')
    activity = 'busy'
    await new Promise((resolve) => setTimeout(resolve, 60))
    assert.equal(
      JSON.parse(readFileSync(stampPath(dir, SESSION), 'utf8')).activity,
      'busy',
      'the next tick must carry the new activity',
    )
    self.stop()
  } finally {
    rmSync(dir, { recursive: true, force: true })
  }
})
