// Unit tests for the durable spawn ledger.
//
// The load-bearing ones are the merge (a completion patch must not erase the spawn
// fields — they arrive minutes apart from different call sites) and the atomic write
// (a reader must never catch a half-written record and read it as a session with no
// fields, which is the same "reports success while being wrong" shape as the bugs this
// file's neighbours exist to prevent).
//
// These do NOT replace the end-to-end check: that a record survives its session — with
// the live registry entry gone — is a fact about two stores, which a fixture cannot see.

import { test } from 'node:test'
import assert from 'node:assert/strict'
import { existsSync, mkdtempSync, readFileSync, readdirSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { MODES, buildRecord, listRecords, parentSessionId, readRecord, recordPath, UNOBSERVED_STATUS, unobservedPatch, updateRecord, writeRecord } from './ledger.mjs'

const SESSION = 'ea363bb5-123a-4d03-bc89-1087a3114bbd'

function withDir(fn) {
  const dir = mkdtempSync(join(tmpdir(), 'supervisor-ledger-'))
  try {
    return fn(dir)
  } finally {
    rmSync(dir, { recursive: true, force: true })
  }
}

const spawnRecord = (over = {}) =>
  buildRecord({
    sessionId: SESSION, agentId: 'agent_1', label: 'drill', mode: 'interactive',
    cwd: '/tmp', launcher: 'cc-private-deepseek', paneId: '1711', ...over,
  })

test('buildRecord carries the spawn edge and starts with no outcome', () => {
  const record = spawnRecord({ resumedFrom: 'aaaa1111-0000-0000-0000-000000000000', parentSession: 'bbbb2222-0000-0000-0000-000000000000' })
  assert.equal(record.session_id, SESSION)
  assert.equal(record.mode, 'interactive')
  assert.equal(record.pane_id, '1711')
  assert.equal(record.resumed_from, 'aaaa1111-0000-0000-0000-000000000000')
  assert.equal(record.parent_session, 'bbbb2222-0000-0000-0000-000000000000')
  assert.equal(record.status, 'running')
  assert.equal(record.ended_at, null, 'a running worker has no end time yet')
  assert.equal(record.result, null)
})

test('buildRecord records which policy the worker ran under', () => {
  // Absence means the server policy — the same reading agentView and the permission log
  // use, so the three cannot disagree about what "no policy" means.
  assert.equal(spawnRecord().policy, null, 'no per-spawn policy reads as the server policy, not as unknown')
  assert.equal(spawnRecord({ policy: '/etc/strict.json' }).policy, '/etc/strict.json')
})

test('buildRecord records the role the spawn declared', () => {
  // The spawn edge alone cannot say whether a spawned session is a worker or a manager,
  // and `gate-owner-filter.py` used to read any record as proof of "worker" — so a manager
  // opened with `spawn_agent(role="manager")` was read as a worker by every peer manager
  // and its workers' gates were escalated twice. `spawn_agent` defaults the role to `agent`
  // before this point, so a null here is a pre-field record, not an undeclared role.
  assert.equal(spawnRecord().role, null, 'a spawn that declared no role records none')
  assert.equal(spawnRecord({ role: 'manager' }).role, 'manager')
  assert.equal(spawnRecord({ role: 'agent' }).role, 'agent')
})

test('writeLedger actually passes the agent role into the record', () => {
  // The WIRING is the fix. Every other test here reaches `buildRecord` directly, so deleting
  // `role: agent.role` from `writeLedger` would leave this whole suite green while silently
  // reinstating the reported defect — a manager opened through the worker path filed as a
  // worker, its workers' gates re-escalated by every peer. Pinned by reading supervisor.mjs's
  // text, the remedy this repo already uses for the same reason in cluster-spawn.test.mjs and
  // attention-poll.test.mjs: that module starts an MCP server at import, so it cannot be
  // imported behaviourally.
  const src = readFileSync(new URL('./supervisor.mjs', import.meta.url), 'utf8')
  assert.match(src, /role: agent\.role \?\? null,/)
  assert.match(src, /const record = buildRecord\(\{/)
})

test('buildRecord refuses a record it could not file', () => {
  assert.throws(() => buildRecord({ mode: 'interactive' }), /needs a sessionId/)
  assert.throws(() => buildRecord({ sessionId: SESSION, mode: 'telepathy' }), /unknown mode/)
})

test('buildRecord accepts the third mode, cluster', () => {
  // A cluster worker is neither a tab nor an in-process query, and the mode it is filed under
  // is what the fleet reads to answer "who started this, and how". Omitting `cluster` from
  // MODES failed SILENTLY rather than loudly: `buildRecord` throws, `writeLedger` catches that
  // throw and only logs a warning, and the spawn still returns a working-looking response — so
  // every cluster worker ran with no ledger record at all. This test is the guard, because it
  // fails at the throw where the spawn path could not.
  assert.ok(MODES.includes('cluster'))
  assert.equal(buildRecord({ sessionId: SESSION, mode: 'cluster' }).mode, 'cluster')
})

test('the record is keyed by the session uuid, which is the resume handle', () => {
  assert.equal(recordPath('/state/sessions', SESSION), `/state/sessions/${SESSION}.json`)
})

test('write then read round-trips, creating the directory', () => {
  withDir((dir) => {
    const nested = join(dir, 'sessions')
    writeRecord(nested, spawnRecord())
    assert.equal(readRecord(nested, SESSION).label, 'drill')
  })
})

test('writing leaves no temporary file behind', () => {
  withDir((dir) => {
    writeRecord(dir, spawnRecord())
    assert.deepEqual(readdirSync(dir).filter((f) => f.endsWith('.tmp')), [], 'a stray .tmp is a half-written record waiting to be read')
  })
})

test('a completion patch merges rather than replacing the spawn fields', () => {
  withDir((dir) => {
    writeRecord(dir, spawnRecord())
    const done = updateRecord(dir, SESSION, {
      status: 'done',
      ended_at: '2026-09-14T08:00:00.000Z',
      result: { subtype: 'success', permission_denials: 0 },
    })
    assert.equal(done.status, 'done')
    assert.equal(done.result.subtype, 'success')
    // The spawn edge must survive: it is what nothing else records, and the caller
    // that knows the outcome does not know it.
    assert.equal(done.parent_session, spawnRecord().parent_session)
    assert.equal(done.mode, 'interactive')
    assert.equal(done.label, 'drill')
    assert.equal(done.pane_id, '1711')
    assert.equal(readRecord(dir, SESSION).status, 'done', 'the merge is persisted, not just returned')
  })
})

test('updating an unknown session reports nothing rather than inventing a record', () => {
  withDir((dir) => {
    assert.equal(updateRecord(dir, 'no-such-session', { status: 'done' }), null)
    assert.equal(existsSync(recordPath(dir, 'no-such-session')), false)
  })
})

test('listRecords tolerates a missing directory and skips what it cannot parse', () => {
  assert.deepEqual(listRecords('/nonexistent/supervisor/ledger'), [])
  withDir((dir) => {
    writeRecord(dir, spawnRecord())
    writeRecord(dir, spawnRecord({ sessionId: 'other-session' }))
    assert.equal(listRecords(dir).length, 2)
  })
})

test('parentSessionId resolves the manager through the live registry', () => {
  const registry = () => [{ pid: 58436, sessionId: 'manager-session' }, { pid: 999, sessionId: 'someone-else' }]
  assert.equal(parentSessionId({ ppid: 58436, registry }), 'manager-session')
  assert.equal(parentSessionId({ ppid: 1234, registry }), null, 'an unknown parent is null, never a guess')
  assert.equal(parentSessionId({ ppid: 58436, registry: () => null }), null, 'an unreadable registry is null too')
})

test('parentSessionId walks the launch path to the manager, past the wrapper', () => {
  // The shape that produced a null in every record ever written, and the one a literal
  // ppid cannot see: `.mcp.json` starts the server through a `bun run` wrapper, so the
  // pid handed in is the wrapper and the manager session is one level above it.
  // Measured 2026-09-22: 0 of 42 live servers had their own ppid in the registry, and
  // 42 of 42 had their grandparent in it.
  //
  // Injected rather than read from this machine, deliberately. A test that passed a
  // literal pid which happened to be a registered session would have passed before this
  // fix too, so it would prove nothing; and the real pids here are this machine's, which
  // makes the assertion a fact about the machine rather than about the resolver.
  const registry = () => [{ pid: 74590, sessionId: 'manager-session' }]
  const parentOf = (pid) => ({ 76174: 76172, 76172: 74590, 74590: 1 })[pid] ?? null
  assert.equal(
    parentSessionId({ ppid: 76174, registry, parentOf }),
    'manager-session',
    'the manager is the nearest registered ancestor, not the direct parent',
  )
})

test('the nearest registered ancestor is the spawn edge when two are registered', () => {
  // The first session up the chain started this process tree; anything above it merely
  // launched that session, so it is not the edge this field records. Nearest-wins is the
  // whole rule, so it is pinned rather than left to whichever the map happens to yield.
  const registry = () => [
    { pid: 700, sessionId: 'the-manager' },
    { pid: 500, sessionId: 'an-outer-session' },
  ]
  const parentOf = (pid) => ({ 900: 800, 800: 700, 700: 500, 500: 1 })[pid] ?? null
  assert.equal(parentSessionId({ ppid: 900, registry, parentOf }), 'the-manager')
})

test('no registered ancestor is null, never a guess', () => {
  // The honest-null case, asserted against an injected chain so it does not depend on
  // this machine's process table. A walk that reaches the end without a hit must answer
  // null — the registry is the only authority, and a manager that exited has no id.
  const registry = () => [{ pid: 111, sessionId: 'unrelated' }]
  const parentOf = (pid) => ({ 900: 800, 800: 700, 700: 1 })[pid] ?? null
  assert.equal(parentSessionId({ ppid: 900, registry, parentOf }), null)
})

test('a chain that loops or ends stops rather than spinning', () => {
  const registry = () => [{ pid: 111, sessionId: 'unrelated' }]
  assert.equal(parentSessionId({ ppid: 900, registry, parentOf: (pid) => pid }), null, 'a pid that is its own parent')
  assert.equal(parentSessionId({ ppid: 900, registry, parentOf: () => null }), null, 'a pid with no parent')
  assert.equal(parentSessionId({ ppid: 900, registry, parentOf: (pid) => pid - 1 }), null, 'a walk capped by maxDepth')
})

test('the record carries which source decided the mode', () => {
  // The mode alone cannot say whether a headless worker was asked for or merely
  // inherited from a config nobody remembered editing — which is the question actually
  // asked when a fleet turns out to be running the wrong way.
  const record = buildRecord({ sessionId: SESSION, mode: 'headless', modeSource: 'config' })
  assert.equal(record.mode_source, 'config')

  // Absent rather than invented for a record built before the field existed.
  assert.equal(buildRecord({ sessionId: SESSION, mode: 'headless' }).mode_source, null)
})

test('an unobserved worker is stamped unknown, never done or error', () => {
  // The defect this closes: a record whose server went away kept asserting `running`
  // indefinitely, which reads as an affirmative claim about a worker nobody watches.
  const patch = unobservedPatch({ mode: 'headless', at: '2026-09-20T00:00:00.000Z' })
  assert.equal(patch.status, UNOBSERVED_STATUS)
  assert.equal(patch.status, 'unknown')
  assert.equal(patch.supervisor_exited_at, '2026-09-20T00:00:00.000Z')
  // No outcome may be claimed: nothing observed this worker finish.
  assert.equal('ended_at' in patch, false, 'an unobserved worker has no end time')
  assert.equal('result' in patch, false, 'an unobserved worker has no result to report')
})

test('the two worker kinds carry why they differ, in the record', () => {
  const tab = unobservedPatch({ mode: 'interactive' })
  const head = unobservedPatch({ mode: 'headless' })
  assert.match(tab.unknown_reason, /outlives its supervisor/)
  assert.match(head.unknown_reason, /terminated with its supervisor/)
  assert.notEqual(tab.unknown_reason, head.unknown_reason, 'one status, two reasons — the reason is what distinguishes them')
})

test('unobservedPatch refuses a mode it cannot explain', () => {
  assert.throws(() => unobservedPatch({ mode: 'telepathy' }), /unknown mode/)
})

test('stamping an unobserved worker keeps the spawn fields and drops the claim', () => {
  withDir((dir) => {
    writeRecord(dir, spawnRecord())
    const stamped = updateRecord(dir, SESSION, unobservedPatch({ mode: 'interactive' }))
    assert.equal(stamped.status, 'unknown', 'running must not survive the server that asserted it')
    assert.equal(stamped.mode, 'interactive')
    assert.equal(stamped.label, 'drill')
    assert.equal(stamped.pane_id, '1711')
    assert.equal(stamped.parent_session, spawnRecord().parent_session)
    assert.equal(readRecord(dir, SESSION).status, 'unknown', 'the stamp is persisted, not just returned')
  })
})
