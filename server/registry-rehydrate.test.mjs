// Unit tests for the roster a fresh server process reads back from disk.
//
// The defect: `agents` in `supervisor.mjs` is populated only by `spawn_agent`, so a
// reconnected MCP client — which gets a NEW server process — answers `list_agents` with `[]`
// and `agent_status` with `unknown agent <id>` while the workers themselves are still alive.
// The regression this pins is therefore "a fresh process can rebuild the roster at all".
//
// ⚠️ **The negative case is the load-bearing one, and it is why the fixture is not just one
// live worker.** The ledger holds 1110 records still asserting `status: running` against a
// much smaller live population, because nothing closes an entry whose server went away. A
// rehydrate that trusted the ledger would pass every positive case here while resurrecting
// every dead worker the fleet has ever opened — and `agent_status` would then answer for ids
// that must read `unknown agent`, breaking the discriminating pair the fix is required to
// keep. So a ledger record with no liveness in either channel must be ABSENT from the result.
//
// ⚠️ **And a registry entry whose pid is dead must be absent too**, which is a deliberate
// divergence from `workerSessions()`: that reader takes registry presence as liveness because
// over-counting a cap fails safe, while adopting a row into a roster is the opposite
// direction — a file left behind by a crashed session would otherwise resurrect the very
// worker this module exists to exclude.
//
// The resumed-worker case pins the one deliberate difference from `workerSessions()`: that
// function drops `resumed_from` records for the cap counter's sake, and a roster that
// inherited the filter would silently hide exactly the workers a manager most needs to see.
//
// The wiring case at the bottom exists because `supervisor.mjs` starts an MCP server at
// import and so cannot be exercised behaviourally — the remedy this repo already uses in
// `cluster-spawn.test.mjs` and `attention-poll.test.mjs`. Without it, deleting the adoption
// block would leave this whole suite green while the fix silently did nothing.

import { test } from 'node:test'
import assert from 'node:assert/strict'
import { mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'

import { stampRecord } from './heartbeat.mjs'
import { isFinished } from './resume-guard.mjs'
import { liveSessionIds, rehydratableAgents, rehydratedStatus, toAgentRecord, unionRoster } from './registry-rehydrate.mjs'

function fixture() {
  const root = mkdtempSync(join(tmpdir(), 'registry-rehydrate-'))
  const dirs = {
    root,
    registryDir: join(root, 'registry'),
    heartbeatDir: join(root, 'heartbeats'),
    // ⚠️ The ledger is now the ENDPOINT, so its fixture is a row array served in-process
    // rather than a directory of files. The suite still dials nothing — the transport is
    // injected — and `rows` is the whole store, so a spec that wants an unreadable ledger
    // fails the transport instead of pointing at a missing directory.
    rows: [],
  }
  for (const key of ['registryDir', 'heartbeatDir']) mkdirSync(dirs[key])
  return dirs
}

// The endpoint as the suite sees it: one row array, served from memory.
function serving(dir) {
  return async () => ({ status: 200, json: async () => dir.rows })
}

// The roster, read the way `supervisor.mjs` reads it — from the endpoint, with the registry and
// heartbeat channels still supplying liveness. Every spec goes through here so the read has ONE
// spelling and a change to its shape cannot leave half the suite asserting the old one.
function roster(dir, overrides = {}) {
  return rehydratableAgents({
    endpoint: 'http://fixture.invalid',
    fetchImpl: serving(dir),
    registryDir: dir.registryDir,
    heartbeatDir: dir.heartbeatDir,
    ...overrides,
  })
}

// A pid that cannot be running. macOS caps pids well below this, so `pidIsAlive` answers
// false rather than throwing — the same reading a crashed session's left-behind file gets.
const DEAD_PID = 999999

// The registry's own shape, written the way Claude Code writes it: keyed by pid, deleted on
// exit. The default pid is this process's, so an entry here is unambiguously live.
function register(dir, sessionId, { pid = process.pid, status = 'busy' } = {}) {
  writeFileSync(
    join(dir.registryDir, `${sessionId}.json`),
    JSON.stringify({ sessionId, pid, status }),
  )
}

// A durable ledger record. `status: 'running'` is the default because that is what a record
// reads when its server died without observing the outcome — the whole hazard.
function ledger(dir, sessionId, { agentId = `agent_${sessionId}`, mode = 'interactive', ...extra } = {}) {
  dir.rows.push({
    session_id: sessionId,
    agent_id: agentId,
    label: `label-${sessionId}`,
    mode,
    spawned_at: '2026-10-06T00:00:00.000Z',
    status: 'running',
    ...extra,
  })
}

function beat(dir, sessionId, { mode = 'headless' } = {}) {
  stampRecord(dir.heartbeatDir, { sessionId, pid: process.pid, mode })
}

test('adopts a live worker the ledger carries — the roster rebuilds on a fresh process', async (t) => {
  const dir = fixture()
  t.after(() => rmSync(dir.root, { recursive: true, force: true }))
  register(dir, 's-tab')
  ledger(dir, 's-tab', { agentId: 'agent_1' })

  const agents = await roster(dir)

  assert.equal(agents.length, 1)
  assert.equal(agents[0].id, 'agent_1')
  assert.equal(agents[0].sessionId, 's-tab')
  assert.equal(agents[0].rehydrated, true)
})

test('a ledger record with no liveness in either channel is NOT adopted — dead stays unknown', async (t) => {
  const dir = fixture()
  t.after(() => rmSync(dir.root, { recursive: true, force: true }))
  // The ledger's own `status: running` is the trap: nothing closes a record whose server
  // went away, so this row is exactly the one a naive rehydrate would resurrect.
  ledger(dir, 's-dead', { agentId: 'agent_dead' })

  assert.deepEqual(await roster(dir), [])
})

test('a registry entry whose pid is DEAD is not adopted — presence is not liveness', async (t) => {
  const dir = fixture()
  t.after(() => rmSync(dir.root, { recursive: true, force: true }))
  // The file is still on disk because the session crashed rather than exited, which is
  // precisely the case `workerSessions()` tolerates (over-counting a cap fails safe) and a
  // roster must not.
  register(dir, 's-crashed', { pid: DEAD_PID })
  ledger(dir, 's-crashed', { agentId: 'agent_crashed' })

  assert.deepEqual(await roster(dir), [])
})

test('the heartbeat store alone is enough — a headless worker has no registry entry', async (t) => {
  const dir = fixture()
  t.after(() => rmSync(dir.root, { recursive: true, force: true }))
  ledger(dir, 's-headless', { agentId: 'agent_h', mode: 'headless' })
  beat(dir, 's-headless', { mode: 'headless' })

  const agents = await roster(dir)

  assert.equal(agents.length, 1)
  assert.equal(agents[0].id, 'agent_h')
  assert.equal(agents[0].status, 'running')
})

test('a resumed worker IS adopted, unlike the cap counter which filters it out', async (t) => {
  const dir = fixture()
  t.after(() => rmSync(dir.root, { recursive: true, force: true }))
  register(dir, 's-resumed')
  ledger(dir, 's-resumed', { agentId: 'agent_r', resumed_from: 's-original' })

  const agents = await roster(dir)

  assert.equal(agents.length, 1, 'a roster must not inherit workerSessions() resumed_from filter')
  assert.equal(agents[0].resumedFrom, 's-original')
})

test('a registry session with no ledger record is not a worker — the operator and managers stay out', async (t) => {
  const dir = fixture()
  t.after(() => rmSync(dir.root, { recursive: true, force: true }))
  register(dir, 's-operator')

  assert.deepEqual(await roster(dir), [])
})

test('a duplicate agent_id keeps BOTH workers addressable — the loser is disambiguated, not dropped', async (t) => {
  const dir = fixture()
  t.after(() => rmSync(dir.root, { recursive: true, force: true }))
  // `supervisor.mjs` mints ids from a per-process counter, so a reconnect restarts it and two
  // live records can share one id. Dropping the loser would leave a genuinely live worker
  // invisible to list_agents / agent_status / send_agent_message for this process's whole
  // life — the availability outcome this module exists to remove, only deterministic.
  register(dir, 'aaaaaaaa-1111-4111-8111-111111111111')
  register(dir, 'bbbbbbbb-2222-4222-8222-222222222222')
  ledger(dir, 'aaaaaaaa-1111-4111-8111-111111111111', { agentId: 'agent_1' })
  ledger(dir, 'bbbbbbbb-2222-4222-8222-222222222222', { agentId: 'agent_1' })
  const logged = []

  const agents = await roster(dir, { log: (line) => logged.push(line) })

  assert.equal(agents.length, 2, 'a live worker must not become unaddressable')
  assert.equal(agents[0].id, 'agent_1')
  assert.equal(agents[0].sessionId, 'aaaaaaaa-1111-4111-8111-111111111111', 'sorted order makes first-wins decidable')
  assert.equal(agents[1].id, 'agent_1~bbbbbbbb')
  assert.equal(agents[1].sessionId, 'bbbbbbbb-2222-4222-8222-222222222222')
  assert.equal(logged.length, 1)
  assert.match(logged[0], /share agent id agent_1/)
})

test('an unreadable ledger endpoint answers null, never an empty roster', async (t) => {
  const dir = fixture()
  t.after(() => rmSync(dir.root, { recursive: true, force: true }))
  register(dir, 's-tab')
  ledger(dir, 's-tab')
  // ⚠️ A store OUTAGE, not an empty store: the endpoint refuses to answer, which is a
  // different fact from "nobody was ever spawned" — `[]` — and collapsing the two would let a
  // permissions error read as a fleet with no workers.
  const fetchImpl = async () => {
    throw new Error('connect ECONNREFUSED')
  }

  assert.equal(await roster(dir, { fetchImpl }), null)
})

test('an unreadable registry answers null — the other store a permissions error takes', async (t) => {
  const dir = fixture()
  t.after(() => rmSync(dir.root, { recursive: true, force: true }))
  ledger(dir, 's-tab')
  writeFileSync(join(dir.root, 'registry-file'), 'x')

  assert.equal(await roster(dir, { registryDir: join(dir.root, 'registry-file') }), null)
})

test('an unreadable heartbeat store answers null — the third store, and the one headless needs', async (t) => {
  const dir = fixture()
  t.after(() => rmSync(dir.root, { recursive: true, force: true }))
  register(dir, 's-tab')
  ledger(dir, 's-tab')
  writeFileSync(join(dir.root, 'beat-file'), 'x')

  assert.equal(await roster(dir, { heartbeatDir: join(dir.root, 'beat-file') }), null)
})

test('a rehydrated record carries the arrays agentView dereferences unconditionally', async (t) => {
  const dir = fixture()
  t.after(() => rmSync(dir.root, { recursive: true, force: true }))
  register(dir, 's-tab')
  ledger(dir, 's-tab', { agentId: 'agent_1' })

  const [agent] = await roster(dir)

  // agentView does `a.transcript.length` and `a.permissions.filter(...)` with no guard, so an
  // absent field takes the WHOLE roster down rather than one row.
  assert.ok(Array.isArray(agent.transcript))
  assert.ok(Array.isArray(agent.permissions))
})

test('a record with no agent_id is skipped rather than adopted under a synthesised id', async (t) => {
  const dir = fixture()
  t.after(() => rmSync(dir.root, { recursive: true, force: true }))
  register(dir, 's-noid')
  dir.rows.push({ session_id: 's-noid', label: 'no agent id', mode: 'interactive' })

  assert.deepEqual(await roster(dir), [])
})

test('liveSessionIds pid-checks the registry half, and unions in the heartbeat store', (t) => {
  const dir = fixture()
  t.after(() => rmSync(dir.root, { recursive: true, force: true }))
  register(dir, 's-alive')
  register(dir, 's-crashed', { pid: DEAD_PID })
  beat(dir, 's-headless')

  const live = liveSessionIds(dir)

  assert.ok(live.has('s-alive'))
  assert.ok(!live.has('s-crashed'))
  assert.ok(live.has('s-headless'), 'the union must keep the channel with no registry entry')
})

test('the ledger maps onto the agent shape without inventing data', () => {
  const record = {
    session_id: 's-1',
    agent_id: 'agent_1',
    label: 'my worker',
    mode: 'interactive',
    cwd: '/tmp/x',
    policy: '/tmp/policy.json',
    mode_source: 'argument',
    pane_id: '12',
    launcher: '/tmp/launch',
    parent_session: 's-parent',
    spawned_at: '2026-10-06T00:00:00.000Z',
    resumed_from: null,
    result: null,
  }

  const agent = toAgentRecord(record)

  assert.equal(agent.id, 'agent_1')
  assert.equal(agent.label, 'my worker')
  assert.equal(agent.cwd, '/tmp/x')
  assert.equal(agent.policyPath, '/tmp/policy.json')
  assert.equal(agent.modeSource, 'argument')
  assert.equal(agent.paneId, '12')
  assert.equal(agent.launcher, '/tmp/launch')
  assert.equal(agent.parentSession, 's-parent')
  assert.equal(agent.createdAt, '2026-10-06T00:00:00.000Z')
  assert.equal(agent.status, 'interactive')
  assert.equal(agent.carriedDecision, null)
  assert.equal(agent.error, null)
})

test('status maps from the ledger mode, so a rehydrated row reads like a spawned one', () => {
  assert.equal(rehydratedStatus({ mode: 'interactive' }), 'interactive')
  assert.equal(rehydratedStatus({ mode: 'cluster' }), 'cluster')
  assert.equal(rehydratedStatus({ mode: 'headless' }), 'running')
  // An A2A row re-adopts as its target's name, mirroring cluster; a pre-field record falls back.
  assert.equal(rehydratedStatus({ mode: 'a2a', target: 'docker' }), 'docker')
  assert.equal(rehydratedStatus({ mode: 'a2a', target: 'k8s' }), 'k8s')
  assert.equal(rehydratedStatus({ mode: 'a2a' }), 'running')
})

test('a re-adopted A2A row keeps its worker id, so the liveness mirror still stamps it', () => {
  const agent = toAgentRecord({ session_id: 's-1', agent_id: 'agent_1', mode: 'a2a', target: 'docker', worker_id: 'w-1' })
  assert.equal(agent.status, 'docker')
  assert.equal(agent.workerId, 'w-1')
})

test('none of the three rehydrated statuses is terminal — which is why liveness, not the status, decides', () => {
  // `resume-guard.mjs` treats every non-terminal status as a live in-process holder, and this
  // pins the premise that makes liveness the ONLY thing that may decide a rehydrated row's
  // fate: a dead worker's row would refuse its own resume, so such rows are pruned rather than
  // excluded by the `rehydrated` marker — and a row that survives the prune holds the guard.
  for (const mode of ['interactive', 'cluster', 'headless']) {
    assert.equal(isFinished(rehydratedStatus({ mode })), false, `${mode} must not read as finished`)
  }
})

test('supervisor.mjs actually adopts the roster and guards the rehydrated rows', () => {
  // The WIRING is the feature. Every other test here calls the module directly, so reverting
  // the call site would leave the whole suite green with the fix dead. Pinned by reading
  // supervisor.mjs's text — the remedy this repo already uses in cluster-spawn.test.mjs and
  // attention-poll.test.mjs, because supervisor.mjs starts an MCP server at import.
  const src = readFileSync(new URL('./supervisor.mjs', import.meta.url), 'utf8')
  assert.match(src, /await rehydratableAgents\(\{ log \}\)/)
  assert.match(src, /for \(const agent of rehydrated\) agents\.set\(agent\.id, agent\)/)
  assert.match(src, /function pruneRehydratedAgents\(\)/)
  // ⚠️ **Both readers answer from the REQUEST-TIME roster, not the in-process Map.** The boot
  // adoption above is a snapshot that can never gain a row, so a reader that went back to
  // `agents.values()`/`agents.get()` would restore the exact defect this change closes while
  // every other assertion here stayed green. The Map-only shape is asserted ABSENT for that
  // reason, not for tidiness.
  assert.match(src, /case 'list_agents':[\s\S]{0,400}?return reply\(\[\.\.\.\(await currentRoster\(\)\)\.values\(\)\]\.map\(agentView\)\)/)
  assert.match(src, /case 'agent_status': \{[\s\S]{0,400}?const agent = \(await currentRoster\(\)\)\.get\(args\.agent_id\)/)
  assert.match(src, /case 'send_agent_message': \{[\s\S]{0,900}?const agent = \(await currentRoster\(\)\)\.get\(args\.agent_id\)/)
  assert.ok(!/case 'list_agents':\n\s+pruneRehydratedAgents\(\)/.test(src), 'the Map-only list_agents must not come back')
  assert.ok(!/case 'agent_status': \{\n\s+pruneRehydratedAgents\(\)/.test(src), 'the Map-only agent_status must not come back')
  // And the union itself: a request-time read, deduped on `session_id`, with this process's own
  // row winning — an endpoint row carries identity alone and must never displace a live one.
  assert.match(src, /async function currentRoster\(\)/)
  assert.match(src, /const fresh = await rehydratableAgents\(\{ log \}\)/)
  // ⚠️ The store-outage branch must BOTH keep the roster AND say so. A bare `return roster` is
  // indistinguishable from a healthy answer to the manager reading `list_agents`, and it threw
  // away the `null`-vs-`[]` distinction `readSessionLedger` deliberately preserves.
  assert.match(src, /if \(fresh === null\) \{[\s\S]{0,700}?log\('WARNING: the ledger endpoint could not be read[\s\S]{0,250}?return new Map\(agents\)/)
  // ⚠️ **The union itself lives in `registry-rehydrate.mjs`, because it can be TESTED there and
  // `supervisor.mjs` cannot be imported.** Pinning it by regex over THIS file's text is what let
  // the id-shadowing defect through: a regex asserts the source's SHAPE, never its behaviour.
  // The behavioural cases are the `unionRoster` tests at the end of this file; what is asserted
  // HERE is only that this call site still goes through it rather than re-inlining the loop.
  assert.match(src, /return unionRoster\(agents, fresh\)/)
  assert.ok(!/takenIds/.test(src), 'the union must not be re-inlined in supervisor.mjs')
  const rehydrateSrc = readFileSync(new URL('./registry-rehydrate.mjs', import.meta.url), 'utf8')
  assert.match(rehydrateSrc, /export function unionRoster\(inProcess, fresh\)/)
  // ⚠️ **A foreign row's id is a function of the ROW ALONE.** Deriving it from `takenIds` —
  // seeded from this process's own Map — made an endpoint row answer as bare `agent_1` only
  // while this process held no such id, so a spawn between a manager's `list_agents` and its
  // `send_agent_message` flipped that row to `agent_1~<sid8>` and handed `agent_1` to the NEW
  // local worker: the message reached the WRONG pane, silently. `agent_1` is the common case —
  // 215 of the 1891 ledger rows claim it.
  assert.match(rehydrateSrc, /let id = `\$\{agent\.id\}~\$\{agent\.sessionId\.slice\(0, 8\)\}`/)
  assert.ok(!/if \(takenIds\.has\(id\)\) id = /.test(rehydrateSrc), 'the Map-derived id must not come back')
  // ⚠️ The resume path must PRUNE and then guard on EVERY row — never filter rehydrated rows
  // out. Filtering fixed the dead-worker deadlock by breaking the live case: a rehydrated row
  // whose worker still runs must keep blocking a resume, or two writers land on one
  // conversation.
  assert.match(src, /pruneRehydratedAgents\(\)\n\s+const holder = findLiveHolder\(agents\.values\(\), resume\)/)
  assert.ok(!/findLiveHolder\(\[\.\.\.agents\.values\(\)\]\.filter/.test(src), 'the rehydrated filter must not come back')
  // Not stamped on shutdown — that record belongs to the server that spawned the worker.
  assert.match(src, /if \(agent\.rehydrated\) continue/)
  // ⚠️ And the WRITER refuses an adopted row outright, so the single-writer guarantee is a
  // property of `writeLedger` rather than a `continue` each call site has to remember. This is
  // load-bearing now that `send_agent_message` looks its agent up in `currentRoster()`: the
  // cluster arm writes `{ result }` onto whatever it found, and the endpoint's `Post` REPLACES
  // a row rather than merging one, so an unguarded write from a second server would blank the
  // owner's row rather than lose a single field.
  assert.match(src, /if \(agent\.rehydrated\) \{\n\s+log\(`WARNING: refusing to write the ledger row/)
  // ⚠️ The MINT must never land on an adopted id. `seq` restarts at 0 on every reconnect, so
  // without this loop the first spawn_agent after a reconnect overwrites a live worker's
  // rehydrated row — reproducing the exact `unknown agent` symptom this PR closes.
  assert.match(src, /do \{\n\s+id = `agent_\$\{\+\+seq\}`\n\s+\} while \(agents\.has\(id\)\)/)
  // And the marker must REACH a caller: `agentView` is the only surface an MCP client sees,
  // and without this field a boot-time row is indistinguishable from a live one.
  assert.match(src, /rehydrated: a\.rehydrated === true,/)
})

// The union is the heart of this change and it had no behavioural test at all: it lived inline
// in `supervisor.mjs`'s `currentRoster`, which cannot be imported (it starts an MCP server at
// module load), so it was pinned only by regexes over the source text. A regex cannot catch a
// behavioural regression, and one got through — see the first case below.
const foreign = (id, sessionId) => ({ id, sessionId, label: id, rehydrated: true })
const OWN_A1 = { id: 'agent_1', sessionId: 'bbbb2222-0000-0000-0000-000000000000', label: 'mine' }
const FAR_A1 = 'aaaa1111-0000-0000-0000-000000000000'

test('a foreign row keeps ONE id, whatever this process happens to hold', () => {
  // ⚠️ THE regression this pins. With the id derived from a set seeded by the LOCAL Map, the
  // same endpoint row answered as bare `agent_1` on one call and `agent_1~<sid8>` on the next,
  // so a manager that read `list_agents` and then called `send_agent_message` with an id from
  // it reached whichever worker happened to hold that id — silently, and in the wrong pane.
  // `agent_1` is the common case: 215 of the 1891 ledger rows claim it.
  const alone = unionRoster(new Map(), [foreign('agent_1', FAR_A1)])
  const afterLocalSpawn = unionRoster(new Map([['agent_1', OWN_A1]]), [foreign('agent_1', FAR_A1)])
  const idAlone = [...alone.keys()].find((k) => k.startsWith('agent_1'))
  const idAfter = [...afterLocalSpawn.keys()].find((k) => k.startsWith('agent_1~'))
  assert.equal(idAlone, 'agent_1~aaaa1111')
  assert.equal(idAfter, 'agent_1~aaaa1111')
  // And the local worker still answers under its own bare id.
  assert.equal(afterLocalSpawn.get('agent_1').sessionId, OWN_A1.sessionId)
})

test("this process's own row wins on a session_id collision", () => {
  // An in-process agent carries the live transcript, the park queue and the permission rules;
  // an endpoint row carries identity alone. The fresh row must never displace it.
  const roster = unionRoster(new Map([['agent_1', OWN_A1]]), [foreign('agent_9', OWN_A1.sessionId)])
  assert.equal(roster.size, 1)
  assert.equal(roster.get('agent_1').label, 'mine')
})

test('two foreign rows sharing an agent_id are both kept, under distinct ids', () => {
  // The collapse `agentsFromRecords` refuses within one read, refused here too: dropping a
  // collision drops live workers.
  const roster = unionRoster(new Map(), [
    foreign('agent_1', FAR_A1),
    foreign('agent_1', 'dddd4444-0000-0000-0000-000000000000'),
  ])
  assert.equal(roster.size, 2)
  assert.deepEqual(
    [...roster.values()].map((a) => a.sessionId).sort(),
    [FAR_A1, 'dddd4444-0000-0000-0000-000000000000'],
  )
})

test('the in-process map is not mutated', () => {
  const inProcess = new Map([['agent_1', OWN_A1]])
  unionRoster(inProcess, [foreign('agent_1', FAR_A1)])
  assert.equal(inProcess.size, 1)
  assert.equal(inProcess.get('agent_1').label, 'mine')
})
