// Unit tests for the fleet-wide worker-session counter.
//
// The property that matters is the JOIN, and the cases that pin it are the two a single-channel
// reading gets wrong: a worker with a ledger record and a live endpoint row and NO registry
// entry (every headless and every cluster worker), and a session live at the endpoint with no
// ledger record (a manager, or one of the operator's own). Until 2026-10-05 the counter read the
// registry alone, so it answered **0 for the first population while counting it dead** — and that
// regression is invisible from the other side, because the tab case it does count keeps the
// number plausible. Since 2026-10-09 liveness comes from the session-heartbeat endpoint, which
// holds both populations in one store; the tests below therefore drive a FIXTURE ENDPOINT rather
// than the filesystem, and the real store on this machine cannot turn an assertion green.
//
// The negatives matter as much as the positives, and they are the reason the fixture builds a
// ledger and an endpoint rather than one store. A check that counted every ledger record would
// pass the positive cases while counting every worker that has ever existed.

import { test } from 'node:test'
import assert from 'node:assert/strict'
import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'

import { workerSessions } from './worker-sessions.mjs'

// A fixed base URL: it is never dialled, because `fetchImpl` is always injected, but passing it
// explicitly keeps a forgotten argument from falling back to `config.attentionStoreUrl` and
// reading the machine's real store.
const FIXTURE = 'http://fixture.invalid'

function fixture() {
  const root = mkdtempSync(join(tmpdir(), 'worker-sessions-'))
  const dirs = { root, ledgerDir: join(root, 'ledger') }
  mkdirSync(dirs.ledgerDir)
  return dirs
}

// A store row in the wire shape attention-controller serves: snake_case `session_id`, and
// `live` as the ONLY field that grants liveness.
function row(sessionId, { live = true, source = 'mcp-timer' } = {}) {
  const out = { session_id: sessionId, source, at: '2026-10-09T00:00:00Z', age_seconds: 3 }
  if (live !== undefined) out.live = live
  return out
}

// An injected fetch. `readHeartbeat` is the only caller, and it treats any non-200, a throwing
// transport, or a body that is not an array as "could not read it".
function endpoint(rows, { status = 200 } = {}) {
  return async () => ({ status, json: async () => rows })
}

function deadEndpoint() {
  return async () => {
    throw new Error('connect ECONNREFUSED 127.0.0.1:18080')
  }
}

function ledger(dir, sessionId, label, extra = {}) {
  writeFileSync(
    join(dir.ledgerDir, `${sessionId}.json`),
    JSON.stringify({ session_id: sessionId, label, mode: 'interactive', ...extra }),
  )
}

function count(dir, rows, opts = {}) {
  return workerSessions({ endpoint: FIXTURE, fetchImpl: endpoint(rows), ...dir, ...opts })
}

test('a ledger record with a live endpoint row is a worker', async () => {
  const dir = fixture()
  const sid = 'ffffffff-0000-0000-0000-000000000001'
  ledger(dir, sid, 'cluster worker')

  const workers = await count(dir, [row(sid, { source: 'cluster' })])
  assert.equal(workers.length, 1)
  assert.equal(workers[0].sessionId, sid)
  assert.equal(workers[0].label, 'cluster worker')
})

test('the count is the size of the join, not of either store', async () => {
  const dir = fixture()
  const ids = ['a', 'b', 'c'].map((c) => `${c.repeat(8)}-0000-0000-0000-000000000001`)
  for (const [i, sid] of ids.entries()) ledger(dir, sid, `worker ${i}`)

  // Two of the three are live; the third holds a record and no row, which is a worker that has
  // exited. Counting the ledger would answer 3, and counting the endpoint would answer 2.
  const workers = await count(dir, [row(ids[0]), row(ids[1])])
  assert.equal(workers.length, 2)
})

test('a ledger record with no endpoint row has exited', async () => {
  // The ledger is the durable half and keeps a record forever, so a session it names and the
  // endpoint does not is one that is gone — the case that makes absence meaningful.
  const dir = fixture()
  ledger(dir, 'cccccccc-0000-0000-0000-000000000001', 'long gone')
  assert.equal((await count(dir, [])).length, 0)
})

test('a live endpoint row with no ledger record is not a worker', async () => {
  // THE MANAGER CASE. A manager is started by hand and holds no ledger record, so counting
  // every live session would count the operator's own sessions and every manager too.
  const dir = fixture()
  const sid = 'bbbbbbbb-0000-0000-0000-000000000001'
  assert.equal((await count(dir, [row(sid)])).length, 0)
})

test('a row the endpoint reports not live is not counted', async () => {
  // The store keeps a row for a session long after it dies. A counter that read row PRESENCE
  // rather than `live: true` would count a dead fleet as a live one.
  const dir = fixture()
  const sid = 'ffffffff-0000-0000-0000-000000000002'
  ledger(dir, sid, 'finished hours ago')
  assert.equal((await count(dir, [row(sid, { live: false })])).length, 0)
})

test('a row missing its live flag is not counted', async () => {
  // A renamed or absent key must never be read as liveness. The Python twin carries the same
  // rule (`live is True`), and the two must not drift on the one field that grants it.
  // ⚠️ Built inline rather than through `row()`: that builder's `live = true` default fires on
  // an explicit `undefined`, so `row(sid, { live: undefined })` produces a row WITH the flag —
  // the test would then pass for the wrong reason, or fail for one.
  const dir = fixture()
  const sid = 'ffffffff-0000-0000-0000-000000000003'
  ledger(dir, sid, 'malformed row')
  const noFlag = { session_id: sid, source: 'mcp-timer', age_seconds: 3 }
  assert.equal(Object.hasOwn(noFlag, 'live'), false)
  assert.equal((await count(dir, [noFlag])).length, 0)
})

test('a malformed row does not crash the count', async () => {
  // This runs on the spawn path, where an uncaught throw is a failed spawn rather than a wrong
  // number. A row with a non-string `session_id` is skipped, and the good rows still count.
  const dir = fixture()
  const sid = 'ffffffff-0000-0000-0000-000000000005'
  ledger(dir, sid, 'good')
  const rows = [null, 'nope', { live: true }, { session_id: 42, live: true }, { session_id: '', live: true }, row(sid)]
  assert.equal((await count(dir, rows)).length, 1)
})

test('an auto-resumed worker is not counted', async () => {
  // Auto-resumes answer to the auto-resume gate's own 30-min crash-loop cap, not this one:
  // counting them would leave a sweep that revived two dead workers unable to open a new one.
  // ⚠️ The marker is the ledger's `resumed_from`, and this exclusion used to hold by ACCIDENT —
  // every auto-resume is headless, and a headless worker held no registry entry under the old
  // registry-only count. Reading a channel that CAN see headless workers removes that accident.
  const dir = fixture()
  const sid = 'ffffffff-0000-0000-0000-000000000004'
  ledger(dir, sid, 'auto-resumed', { resumed_from: 'aaaaaaaa-0000-0000-0000-000000000009' })
  assert.equal((await count(dir, [row(sid)])).length, 0)
})

test('status names the store row source', async () => {
  const dir = fixture()
  const sid = 'ffffffff-0000-0000-0000-000000000006'
  ledger(dir, sid, 'Some Worker')
  const workers = await count(dir, [row(sid, { source: 'cluster' })])
  assert.equal(workers[0].status, 'cluster')
})

// ⚠️ The unreadable-ledger case below points the store at a REGULAR FILE rather than calling
// `chmod 0o000` on it. A 0o000 directory is still readable by root (`CAP_DAC_OVERRIDE`), so the
// chmod spelling passes or fails on the euid of whoever runs it — green on CI, red under root.
// `readdirSync` on a file throws ENOTDIR, which is not ENOENT, so the store reads as unreadable
// under both.
function unreadable(path) {
  rmSync(path, { recursive: true })
  writeFileSync(path, '')
}

test('an unreadable ledger returns null, not an empty fleet', async () => {
  // `null` refuses the spawn and `[]` permits it, so collapsing an I/O error into "nobody is
  // live" is the one direction that opens past a limit that cannot be counted.
  const dir = fixture()
  unreadable(dir.ledgerDir)
  assert.equal(await count(dir, []), null)
})

test('an absent ledger directory is an empty fleet, not an error', async () => {
  // A directory that was never created means nobody has ever been spawned — a different answer
  // from "could not read it", and it must not be reported as unknown. The endpoint may hold any
  // number of live rows; with no ledger record they are managers and the operator's own.
  const dir = fixture()
  rmSync(dir.ledgerDir, { recursive: true })
  const sid = 'aaaaaaaa-0000-0000-0000-000000000003'
  const workers = await count(dir, [row(sid)])
  assert.deepEqual(workers, [])
})

test('an unreachable endpoint returns null, not an empty fleet', async () => {
  // The endpoint is the liveness source now, so failing to read it is UNKNOWN for the same
  // reason the ledger is: the unread store may be hiding the very session being counted.
  const dir = fixture()
  ledger(dir, 'ffffffff-0000-0000-0000-000000000007', 'worker')
  assert.equal(await workerSessions({ endpoint: FIXTURE, fetchImpl: deadEndpoint(), ...dir }), null)
})

test('a non-200 endpoint returns null', async () => {
  const dir = fixture()
  ledger(dir, 'ffffffff-0000-0000-0000-000000000008', 'worker')
  assert.equal(await count(dir, [], { fetchImpl: endpoint([], { status: 503 }) }), null)
})

test('a body that is not a row array returns null', async () => {
  const dir = fixture()
  ledger(dir, 'ffffffff-0000-0000-0000-000000000009', 'worker')
  assert.equal(await count(dir, { error: 'nope' }), null)
})
