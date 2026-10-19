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
import { mkdtempSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'

import { workerSessions } from './worker-sessions.mjs'

// A fixed base URL: it is never dialled, because `fetchImpl` is always injected, but passing it
// explicitly keeps a forgotten argument from falling back to `config.attentionStoreUrl` and
// reading the machine's real store.
const FIXTURE = 'http://fixture.invalid'

function fixture() {
  const root = mkdtempSync(join(tmpdir(), 'worker-sessions-'))
  // ⚠️ The ledger is the ENDPOINT now, so its fixture is a row array rather than a directory
  // of files. The suite still dials nothing — both transports are injected.
  return { root, ledgerRows: [] }
}

// A store row in the wire shape attention-controller serves: snake_case `session_id`, and
// `live` as the ONLY field that grants liveness.
function row(sessionId, { live = true, source = 'mcp-timer' } = {}) {
  const out = { session_id: sessionId, source, at: '2026-10-09T00:00:00Z', age_seconds: 3 }
  if (live !== undefined) out.live = live
  return out
}

// An injected fetch. TWO readers share this base — `readHeartbeat` and `readSessionLedger` —
// so the fixture routes by path. Both treat a non-200, a throwing transport, or a body that is
// not an array as "could not read it".
function endpoint(rows, { status = 200, ledgerRows } = {}) {
  return async (url) => ({
    status,
    json: async () => (String(url).includes('/session-ledger') ? ledgerRows ?? [] : rows),
  })
}

function deadEndpoint() {
  return async () => {
    // `code` is attached because a real socket failure carries it (Node's fetch puts it on
    // `cause`; a direct socket error has it at the top level) — the diagnostics name the code,
    // and a fixture that threw a bare Error would not exercise that.
    throw Object.assign(new Error('connect ECONNREFUSED 127.0.0.1:18080'), { code: 'ECONNREFUSED' })
  }
}

function ledger(dir, sessionId, label, extra = {}) {
  dir.ledgerRows.push({ session_id: sessionId, label, mode: 'interactive', ...extra })
}

// `workerSessions` answers `{ workers, error }`. Most cases here are about the join, so this
// unwraps the happy path; the failure cases below call `workerSessions` directly to read the
// cause that travels with a `null` count.
async function count(dir, rows, opts = {}) {
  const { workers } = await workerSessions({
    endpoint: FIXTURE,
    fetchImpl: endpoint(rows, { ledgerRows: dir.ledgerRows }),
    ...opts,
  })
  return workers
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

test('a manager record is not counted against the worker cap', async () => {
  // `spawn_agent(role="manager")` writes a ledger record exactly like a worker's, so presence
  // in the ledger alone counted a manager against `spawn.maxConcurrent` and pushed the fleet
  // toward refusing ordinary spawns. The record's own `role` is what separates them.
  const dir = fixture()
  const manager = 'ffffffff-0000-0000-0000-000000000010'
  const agent = 'ffffffff-0000-0000-0000-000000000011'
  ledger(dir, manager, 'topic manager', { role: 'manager' })
  ledger(dir, agent, 'worker', { role: 'agent' })

  const workers = await count(dir, [row(manager), row(agent)])
  assert.equal(workers.length, 1)
  assert.equal(workers[0].sessionId, agent)
})

test('two agent records are both counted — the control for the manager exclusion', async () => {
  // Without this the manager case above passes on a counter that returns 0 for everything.
  const dir = fixture()
  const first = 'ffffffff-0000-0000-0000-000000000012'
  const second = 'ffffffff-0000-0000-0000-000000000013'
  ledger(dir, first, 'worker a', { role: 'agent' })
  ledger(dir, second, 'worker b', { role: 'agent' })

  assert.equal((await count(dir, [row(first), row(second)])).length, 2)
})

test('a role-less record still reads as a worker', async () => {
  // ⚠️ The exclusion is `role === 'manager'`, never `role !== 'agent'`. A record with no
  // `role` was written before the field existed, and those are pre-field workers and managers
  // alike — so treating absence as "manager" would drop every pre-field worker out of the
  // count this cap exists to enforce.
  const dir = fixture()
  const sid = 'ffffffff-0000-0000-0000-000000000014'
  ledger(dir, sid, 'pre-field worker')

  assert.equal((await count(dir, [row(sid)])).length, 1)
})

test('status names the store row source', async () => {
  const dir = fixture()
  const sid = 'ffffffff-0000-0000-0000-000000000006'
  ledger(dir, sid, 'Some Worker')
  const workers = await count(dir, [row(sid, { source: 'cluster' })])
  assert.equal(workers[0].status, 'cluster')
})

test('an unreadable ledger store returns null, not an empty fleet', async () => {
  // `null` refuses the spawn and `[]` permits it, so collapsing a store failure into "nobody is
  // live" is the one direction that opens past a limit that cannot be counted. ⚠️ The heartbeat
  // answers normally here — the failure is the LEDGER's alone, so this spec discriminates
  // rather than passing because both stores happened to be down.
  const sid = 'aaaaaaaa-0000-0000-0000-000000000009'
  const fetchImpl = async (url) => {
    if (String(url).includes('/session-ledger')) {
      throw Object.assign(new Error('connect ECONNREFUSED'), { code: 'ECONNREFUSED' })
    }
    return { status: 200, json: async () => [row(sid)] }
  }
  const counted = await workerSessions({ endpoint: FIXTURE, fetchImpl })
  assert.equal(counted.workers, null)
  // ⚠️ The cause travels with the null. Without it the refusal can say only "the count could not
  // be taken", which is what made 2026-10-19's lost spawns undiagnosable — and this case pins
  // that the LEDGER is named, not the heartbeat, since only the ledger failed.
  assert.match(counted.error, /session-ledger read failed after \d+ ms: ECONNREFUSED/)
})

test('an empty ledger store is an empty fleet, not an error', async () => {
  // A store that answers an empty array means nobody has ever been spawned — a different answer
  // from "could not read it", and it must not be reported as unknown. The endpoint may hold any
  // number of live rows; with no ledger record they are managers and the operator's own.
  const dir = fixture()
  const sid = 'aaaaaaaa-0000-0000-0000-000000000003'
  const workers = await count(dir, [row(sid)])
  assert.deepEqual(workers, [])
})

test('an unreachable endpoint returns null, not an empty fleet', async () => {
  // The endpoint is the liveness source now, so failing to read it is UNKNOWN for the same
  // reason the ledger is: the unread store may be hiding the very session being counted.
  const dir = fixture()
  ledger(dir, 'ffffffff-0000-0000-0000-000000000007', 'worker')
  const counted = await workerSessions({ endpoint: FIXTURE, fetchImpl: deadEndpoint(), ...dir })
  assert.equal(counted.workers, null)
  assert.match(counted.error, /session-ledger read failed after \d+ ms: ECONNREFUSED/)
})

test('a heartbeat failure names the heartbeat, not the ledger', async () => {
  // The complement of the case above: a readable ledger and a failing heartbeat must report the
  // SECOND reader. A message that named whichever failed first would be right by accident here
  // and wrong there, so the two cases together pin the routing.
  const dir = fixture()
  ledger(dir, 'ffffffff-0000-0000-0000-00000000000a', 'worker')
  const fetchImpl = async (url) => {
    if (String(url).includes('/session-ledger')) {
      return { status: 200, json: async () => dir.ledgerRows }
    }
    throw Object.assign(new Error('fetch failed'), { cause: { code: 'ECONNREFUSED' } })
  }
  const counted = await workerSessions({ endpoint: FIXTURE, fetchImpl })
  assert.equal(counted.workers, null)
  assert.match(counted.error, /session-heartbeat read failed after \d+ ms: ECONNREFUSED/)
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
