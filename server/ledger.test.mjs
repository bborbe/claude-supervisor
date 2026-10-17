// Unit tests for the durable spawn ledger's record shape.
//
// The load-bearing one is the merge: a completion patch must not erase the spawn fields, because
// they arrive minutes apart from different call sites and the caller that knows the outcome does
// not know the edge. That property used to be exercised through the FILE (`writeRecord` then
// `updateRecord`); the file store is gone, so it is exercised against `mergeRecord`, which is what
// `writeLedger` now calls — the same property, one layer in, and still behavioural rather than a
// regex over source text.
//
// These do NOT replace the end-to-end check: that a row survives its session — with the live
// registry entry gone — is a fact about two stores, which a fixture cannot see.

import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { MODES, buildRecord, ledgerWriteRoute, mergeRecord, parentSessionId, stoppedPatch, UNOBSERVED_STATUS, unobservedPatch } from './ledger.mjs'

const SESSION = 'ea363bb5-123a-4d03-bc89-1087a3114bbd'

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
  //
  // ⚠️ Anchored to a whole non-comment LINE, deliberately. An unanchored
  // `/role: agent\.role \?\? null,/` is satisfied by a commented-out
  // `// role: agent.role ?? null,` — a guard that stays green through exactly the
  // edit it exists to catch, which is the failure mode this whole test is about.
  // `^[ \t]*role` cannot match a line whose first non-blank characters are `//`.
  const src = readFileSync(new URL('./supervisor.mjs', import.meta.url), 'utf8')
  assert.match(src, /^[ \t]*role: agent\.role \?\? null,[ \t]*$/m)
  assert.match(src, /^[ \t]*const record = buildRecord\(\{$/m)
})

test('the shipping claim and the code agree', () => {
  // SC4. `server/supervisor.mjs` claimed the flag was recorded "so a resume can see the
  // worker was shipping", while `buildRecord` has no `shipping` parameter and
  // `registry-rehydrate.mjs` hardcodes `shipping: false` and calls it unrecoverable. Two
  // sites agreed and one did not; the fix corrected the outlier rather than the code, so
  // this test pins BOTH halves of the agreement — the record carries no `shipping` key, AND
  // no comment in supervisor.mjs claims it is recorded.
  //
  // ⚠️ If someone later persists the flag, the first assertion fails — deliberately. That
  // failure is the reminder that the comment and `registry-rehydrate.mjs` must move in the
  // same change, which is exactly the drift this pins.
  assert.equal('shipping' in spawnRecord({ shipping: true }), false, 'buildRecord does not persist shipping')
  const src = readFileSync(new URL('./supervisor.mjs', import.meta.url), 'utf8')
  assert.doesNotMatch(src, /Recorded so a resume can see the worker was shipping/)
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
  // The key IS the resume handle, which is why the row is useful to the resume path and not only
  // to a process table. Pinned on the row's own field now that no path derives it from a filename.
  assert.equal(spawnRecord().session_id, SESSION)
  assert.equal(buildRecord({ sessionId: SESSION, mode: 'headless' }).session_id, SESSION)
})

test('a completion patch merges rather than replacing the spawn fields', () => {
  const done = mergeRecord(spawnRecord(), {
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
})

test('a park survives a later result patch — the merge carries the whole row', () => {
  // ⚠️ This is the property that decides whether a resumed worker carries an operator's answer.
  // The park is written when the gate opens and the result when the turn ends, so a replace rather
  // than a merge would drop the park in between and nothing else would notice: the resume path
  // reads the park off the row, and an absent one refuses the decision silently.
  const parked = mergeRecord(spawnRecord(), { parked_permission: { request_id: 'perm_1', tool: 'Bash' } })
  const done = mergeRecord(parked, { status: 'done', result: { subtype: 'success' } })
  assert.equal(done.parked_permission.tool, 'Bash', 'the park must outlive the terminal patch')
  assert.equal(done.label, 'drill', 'and so must the spawn edge')
})

test('clearing a park is an explicit null, not an omitted key', () => {
  // `clearParkPatch` sends `{ parked_permission: null }` because a spread cannot express a
  // deletion — an omitted key would leave the row advertising a park nobody holds.
  const cleared = mergeRecord(mergeRecord(spawnRecord(), { parked_permission: { tool: 'Bash' } }), { parked_permission: null })
  assert.equal(cleared.parked_permission, null)
  assert.ok('parked_permission' in cleared, 'the key must still be present — that is what makes it a clear')
})

test('merging into no record reports nothing rather than inventing one', () => {
  // A patch that finds no record is a session id that resolved with no spawn behind it. Inventing
  // a row from the patch alone would file one missing the spawn edge — worse than none, because it
  // is addressable and wrong.
  assert.equal(mergeRecord(undefined, { status: 'done' }), null)
  assert.equal(mergeRecord(null, { status: 'done' }), null)
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
  const stamped = mergeRecord(spawnRecord(), unobservedPatch({ mode: 'interactive' }))
  assert.equal(stamped.status, 'unknown', 'running must not survive the server that asserted it')
  assert.equal(stamped.mode, 'interactive')
  assert.equal(stamped.label, 'drill')
  assert.equal(stamped.pane_id, '1711')
  assert.equal(stamped.parent_session, spawnRecord().parent_session)
})

test('an A2A record carries its worker id and target, and never a token', () => {
  const record = buildRecord({ sessionId: SESSION, agentId: 'agent_1', mode: 'a2a', cwd: null, workerId: 'w-1', target: 'docker', spawnedAt: '2026-10-13T00:00:00.000Z' })
  assert.equal(record.worker_id, 'w-1')
  assert.equal(record.target, 'docker')
  // The token authenticates turns; the ledger is a world-readable file. No key may carry it.
  assert.ok(!Object.keys(record).some((k) => /token/i.test(k)), `record keys: ${Object.keys(record).join(',')}`)
})

test('a local record leaves the A2A fields null', () => {
  const record = buildRecord({ sessionId: SESSION, agentId: 'agent_1', mode: 'headless', cwd: '/tmp', spawnedAt: '2026-10-13T00:00:00.000Z' })
  assert.equal(record.worker_id, null)
  assert.equal(record.target, null)
})

// ---- ledgerWriteRoute: the single-writer guarantee, asserted rather than grepped -------------

// ⚠️ **This is the assertion the whole feature turns on, and it is why the decision is a pure
// function.** The supervisor that stops a worker is usually NOT the one that spawned it —
// `stop_agent` resolves its agent from the request-time roster, so an adopted row is the normal
// case rather than an edge one. While the endpoint had no merge, every write to such a row was
// refused, so the stop was recorded nowhere durable and the sweep read a deliberately stopped
// worker exactly as it reads a dead one. The route below is what lets the stop land.
test('an adopted row takes a MERGE, never the replacing route', () => {
  const adopted = { id: 'agent_7', sessionId: SESSION, rehydrated: true }
  assert.equal(ledgerWriteRoute(adopted, stoppedPatch()), 'patch')
  assert.notEqual(ledgerWriteRoute(adopted, stoppedPatch()), 'post')
})

test('an adopted row refuses a CREATE — that is the write that would blank the owner', () => {
  // No patch means the create arm: a record built from identity alone and POSTed over a row whose
  // fields belong to another server. That refusal is the original guarantee and it stays.
  const adopted = { id: 'agent_7', sessionId: SESSION, rehydrated: true }
  assert.equal(ledgerWriteRoute(adopted, undefined), 'refuse')
  assert.equal(ledgerWriteRoute(adopted, null), 'refuse')
})

test('an owned row is replaced, with or without a patch', () => {
  // A row this server spawned is held in full in memory, so a replace loses nothing — and the
  // patch arm merges locally (`mergeRecord`) before publishing the whole record.
  const owned = { id: 'agent_1', sessionId: SESSION }
  assert.equal(ledgerWriteRoute(owned, undefined), 'post')
  assert.equal(ledgerWriteRoute(owned, stoppedPatch()), 'post')
})

test('a session id that never resolved has no route at all', () => {
  // Not even an adopted row can be written without a key: the per-id route needs the id, and a
  // row filed under a synthesised one is addressable but meaningless.
  assert.equal(ledgerWriteRoute({ id: 'agent_1' }, stoppedPatch()), 'no-session-id')
  assert.equal(ledgerWriteRoute({ id: 'agent_1', sessionId: '', rehydrated: true }, stoppedPatch()), 'no-session-id')
  assert.equal(ledgerWriteRoute(undefined, stoppedPatch()), 'no-session-id')
})

test('an adopted stop carries only the stop field, because the merge is the store job', () => {
  // ⚠️ The patch is what crosses the wire, so it must name nothing but the field being changed —
  // anything else would be a claim about fields this server has never read.
  assert.deepEqual(stoppedPatch({ at: '2026-10-17T14:05:00.000Z' }), { stopped_at: '2026-10-17T14:05:00.000Z' })
  assert.deepEqual(Object.keys(stoppedPatch()), ['stopped_at'])
})
