// Unit tests for the fleet-wide worker-session counter.
//
// The property that matters is the UNION, and the case that pins it is a worker with a ledger
// record and a heartbeat stamp and NO registry entry. That is not a corner: it is every
// headless worker (an in-process SDK `query()` with no pid of its own) and every cluster
// worker (a process on another machine). Until 2026-10-05 the counter read the registry
// alone, so it answered **0 for both populations while counting them as dead** — and the
// regression is invisible from the other side, because the tab case it does count keeps the
// number plausible. A test that only exercised a registry-backed worker would pass on the
// broken version, which is why the heartbeat-only case below is the load-bearing one.
//
// The negatives matter as much as the positives, and they are the reason the fixture builds
// three stores rather than one. A check that counted every ledger record would pass the
// positive cases while counting every worker that has ever existed; a check that counted
// every registry session would count the operator's own sessions and the managers.

import { test } from 'node:test'
import assert from 'node:assert/strict'
import { mkdirSync, mkdtempSync, rmSync, utimesSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'

import { HEARTBEAT_TTL_MS, stampRecord } from './heartbeat.mjs'
import { workerSessions } from './worker-sessions.mjs'

function fixture() {
  const root = mkdtempSync(join(tmpdir(), 'worker-sessions-'))
  const dirs = {
    root,
    registryDir: join(root, 'registry'),
    ledgerDir: join(root, 'ledger'),
    heartbeatDir: join(root, 'heartbeats'),
  }
  for (const key of ['registryDir', 'ledgerDir', 'heartbeatDir']) mkdirSync(dirs[key])
  return dirs
}

// The registry's own shape, written the way Claude Code writes it: keyed by pid, deleted on
// exit. The pid is this process's, so an entry here is unambiguously a live one.
function register(dir, sessionId, status = 'busy') {
  writeFileSync(
    join(dir.registryDir, `${sessionId}.json`),
    JSON.stringify({ sessionId, pid: process.pid, status }),
  )
}

function ledger(dir, sessionId, label, extra = {}) {
  writeFileSync(
    join(dir.ledgerDir, `${sessionId}.json`),
    JSON.stringify({ session_id: sessionId, label, mode: 'interactive', ...extra }),
  )
}

// A stamp whose AGE is what the reader decides on — the file's existence proves nothing,
// because a store whose writer was killed keeps its files.
function beat(dir, sessionId, { mode = 'headless', source, ageMs = 0 } = {}) {
  const path = stampRecord(dir.heartbeatDir, { sessionId, pid: process.pid, mode, source })
  if (ageMs) {
    const when = new Date(Date.now() - ageMs)
    utimesSync(path, when, when)
  }
}

test('a ledger record with a fresh heartbeat stamp is a worker', () => {
  const dir = fixture()
  const sid = 'ffffffff-0000-0000-0000-000000000001'
  ledger(dir, sid, 'cluster worker')
  beat(dir, sid, { mode: 'cluster', source: 'cluster' })

  const workers = workerSessions(dir)
  assert.equal(workers.length, 1)
  assert.equal(workers[0].sessionId, sid)
  assert.equal(workers[0].label, 'cluster worker')
})

test('a ledger record with a registry entry is a worker', () => {
  const dir = fixture()
  const sid = 'aaaaaaaa-0000-0000-0000-000000000001'
  register(dir, sid, 'idle')
  ledger(dir, sid, 'tab worker')

  const workers = workerSessions(dir)
  assert.equal(workers.length, 1)
  assert.equal(workers[0].status, 'idle')
})

test('a session in both channels is counted once', () => {
  const dir = fixture()
  const sid = 'aaaaaaaa-0000-0000-0000-000000000002'
  register(dir, sid)
  beat(dir, sid)
  ledger(dir, sid, 'both')

  assert.equal(workerSessions(dir).length, 1)
})

test('a ledger record with neither channel has exited', () => {
  const dir = fixture()
  ledger(dir, 'cccccccc-0000-0000-0000-000000000001', 'long gone')
  assert.equal(workerSessions(dir).length, 0)
})

test('a registry session with no ledger record is not a worker', () => {
  // The manager case: a manager is started by hand and holds no ledger record, so counting
  // every registry session would count the operator's own sessions too.
  const dir = fixture()
  register(dir, 'bbbbbbbb-0000-0000-0000-000000000001')
  assert.equal(workerSessions(dir).length, 0)
})

test('an auto-resumed worker is not counted', () => {
  // Auto-resumes answer to the auto-resume gate's own 30-min crash-loop cap, not this one:
  // counting them would leave a sweep that revived two dead workers unable to open a new one.
  // ⚠️ The marker is the ledger's `resumed_from`, and this exclusion used to hold by ACCIDENT
  // — every auto-resume is headless, and a headless worker held no registry entry under the
  // old registry-only count. Reading the heartbeat store is what removes that accident, so
  // the rule is written down here for the first time.
  const dir = fixture()
  const sid = 'ffffffff-0000-0000-0000-000000000004'
  ledger(dir, sid, 'auto-resumed', { resumed_from: 'aaaaaaaa-0000-0000-0000-000000000009' })
  beat(dir, sid)

  assert.equal(workerSessions(dir).length, 0)
})

test('a manager record is not counted against the worker cap', () => {
  // `spawn_agent(role="manager")` writes a ledger record exactly like a worker's, so presence
  // in the ledger alone counted a manager against `spawn.maxConcurrent` and pushed the fleet
  // toward refusing ordinary spawns. The record's own `role` is what separates them.
  const dir = fixture()
  const manager = 'ffffffff-0000-0000-0000-000000000010'
  const agent = 'ffffffff-0000-0000-0000-000000000011'
  ledger(dir, manager, 'topic manager', { role: 'manager' })
  ledger(dir, agent, 'worker', { role: 'agent' })
  beat(dir, manager)
  beat(dir, agent)

  const workers = workerSessions(dir)
  assert.equal(workers.length, 1)
  assert.equal(workers[0].sessionId, agent)
})

test('two agent records are both counted — the control for the manager exclusion', () => {
  // Without this the manager case above passes on a counter that returns 0 for everything.
  const dir = fixture()
  const first = 'ffffffff-0000-0000-0000-000000000012'
  const second = 'ffffffff-0000-0000-0000-000000000013'
  ledger(dir, first, 'worker a', { role: 'agent' })
  ledger(dir, second, 'worker b', { role: 'agent' })
  beat(dir, first)
  beat(dir, second)

  assert.equal(workerSessions(dir).length, 2)
})

test('a role-less record still reads as a worker', () => {
  // ⚠️ The exclusion is `role === 'manager'`, never `role !== 'agent'`. A record with no
  // `role` was written before the field existed, and those are pre-field workers and managers
  // alike — so treating absence as "manager" would drop every pre-field worker out of the
  // count this cap exists to enforce.
  const dir = fixture()
  const sid = 'ffffffff-0000-0000-0000-000000000014'
  ledger(dir, sid, 'pre-field worker')
  beat(dir, sid)

  assert.equal(workerSessions(dir).length, 1)
})

test('a stale heartbeat stamp is not a worker', () => {
  const dir = fixture()
  const sid = 'ffffffff-0000-0000-0000-000000000002'
  ledger(dir, sid, 'finished hours ago')
  beat(dir, sid, { ageMs: HEARTBEAT_TTL_MS * 10 })

  assert.equal(workerSessions(dir).length, 0)
})

// ⚠️ Both unreadable-store cases below point the store at a REGULAR FILE rather than calling
// `chmod 0o000` on it. A 0o000 directory is still readable by root (`CAP_DAC_OVERRIDE`), so the
// chmod spelling passes or fails on the euid of whoever runs it — green on CI, red under root.
// `readdirSync` on a file throws ENOTDIR, which is not ENOENT, so the store reads as unreadable
// under both.
function unreadable(path) {
  rmSync(path, { recursive: true })
  writeFileSync(path, '')
}

test('an unreadable ledger returns null, not an empty fleet', () => {
  // `null` refuses the spawn and `[]` permits it, so collapsing an I/O error into "nobody is
  // live" is the one direction that opens past a limit that cannot be counted.
  const dir = fixture()
  unreadable(dir.ledgerDir)
  assert.equal(workerSessions(dir), null)
})

test('an unreadable heartbeat store returns null, not an empty fleet', () => {
  // The heartbeat is a liveness channel now, so failing to read it is UNKNOWN for the same
  // reason the registry is: the unread channel may be hiding the very session being counted.
  const dir = fixture()
  unreadable(dir.heartbeatDir)
  assert.equal(workerSessions(dir), null)
})

test('an absent heartbeat store is an empty fleet, not an error', () => {
  // A directory that was never created means nothing has ever stamped — a different answer
  // from "could not read it", and it must not be reported as unknown.
  const dir = fixture()
  rmSync(dir.heartbeatDir, { recursive: true })
  const sid = 'aaaaaaaa-0000-0000-0000-000000000003'
  register(dir, sid)
  ledger(dir, sid, 'tab only')

  assert.equal(workerSessions(dir).length, 1)
})
