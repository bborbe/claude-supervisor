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
import {
  liveSessionIds,
  rehydratableAgents,
  rehydratedStatus,
  toAgentRecord,
} from './registry-rehydrate.mjs'

function fixture() {
  const root = mkdtempSync(join(tmpdir(), 'registry-rehydrate-'))
  const dirs = {
    root,
    registryDir: join(root, 'registry'),
    ledgerDir: join(root, 'ledger'),
    heartbeatDir: join(root, 'heartbeats'),
  }
  for (const key of ['registryDir', 'ledgerDir', 'heartbeatDir']) mkdirSync(dirs[key])
  return dirs
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
  writeFileSync(
    join(dir.ledgerDir, `${sessionId}.json`),
    JSON.stringify({
      session_id: sessionId,
      agent_id: agentId,
      label: `label-${sessionId}`,
      mode,
      spawned_at: '2026-10-06T00:00:00.000Z',
      status: 'running',
      ...extra,
    }),
  )
}

function beat(dir, sessionId, { mode = 'headless' } = {}) {
  stampRecord(dir.heartbeatDir, { sessionId, pid: process.pid, mode })
}

test('adopts a live worker the ledger carries — the roster rebuilds on a fresh process', (t) => {
  const dir = fixture()
  t.after(() => rmSync(dir.root, { recursive: true, force: true }))
  register(dir, 's-tab')
  ledger(dir, 's-tab', { agentId: 'agent_1' })

  const agents = rehydratableAgents(dir)

  assert.equal(agents.length, 1)
  assert.equal(agents[0].id, 'agent_1')
  assert.equal(agents[0].sessionId, 's-tab')
  assert.equal(agents[0].rehydrated, true)
})

test('a ledger record with no liveness in either channel is NOT adopted — dead stays unknown', (t) => {
  const dir = fixture()
  t.after(() => rmSync(dir.root, { recursive: true, force: true }))
  // The ledger's own `status: running` is the trap: nothing closes a record whose server
  // went away, so this row is exactly the one a naive rehydrate would resurrect.
  ledger(dir, 's-dead', { agentId: 'agent_dead' })

  assert.deepEqual(rehydratableAgents(dir), [])
})

test('a registry entry whose pid is DEAD is not adopted — presence is not liveness', (t) => {
  const dir = fixture()
  t.after(() => rmSync(dir.root, { recursive: true, force: true }))
  // The file is still on disk because the session crashed rather than exited, which is
  // precisely the case `workerSessions()` tolerates (over-counting a cap fails safe) and a
  // roster must not.
  register(dir, 's-crashed', { pid: DEAD_PID })
  ledger(dir, 's-crashed', { agentId: 'agent_crashed' })

  assert.deepEqual(rehydratableAgents(dir), [])
})

test('the heartbeat store alone is enough — a headless worker has no registry entry', (t) => {
  const dir = fixture()
  t.after(() => rmSync(dir.root, { recursive: true, force: true }))
  ledger(dir, 's-headless', { agentId: 'agent_h', mode: 'headless' })
  beat(dir, 's-headless', { mode: 'headless' })

  const agents = rehydratableAgents(dir)

  assert.equal(agents.length, 1)
  assert.equal(agents[0].id, 'agent_h')
  assert.equal(agents[0].status, 'running')
})

test('a resumed worker IS adopted, unlike the cap counter which filters it out', (t) => {
  const dir = fixture()
  t.after(() => rmSync(dir.root, { recursive: true, force: true }))
  register(dir, 's-resumed')
  ledger(dir, 's-resumed', { agentId: 'agent_r', resumed_from: 's-original' })

  const agents = rehydratableAgents(dir)

  assert.equal(agents.length, 1, 'a roster must not inherit workerSessions() resumed_from filter')
  assert.equal(agents[0].resumedFrom, 's-original')
})

test('a registry session with no ledger record is not a worker — the operator and managers stay out', (t) => {
  const dir = fixture()
  t.after(() => rmSync(dir.root, { recursive: true, force: true }))
  register(dir, 's-operator')

  assert.deepEqual(rehydratableAgents(dir), [])
})

test('a duplicate agent_id keeps BOTH workers addressable — the loser is disambiguated, not dropped', (t) => {
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

  const agents = rehydratableAgents({ ...dir, log: (line) => logged.push(line) })

  assert.equal(agents.length, 2, 'a live worker must not become unaddressable')
  assert.equal(agents[0].id, 'agent_1')
  assert.equal(agents[0].sessionId, 'aaaaaaaa-1111-4111-8111-111111111111', 'sorted order makes first-wins decidable')
  assert.equal(agents[1].id, 'agent_1~bbbbbbbb')
  assert.equal(agents[1].sessionId, 'bbbbbbbb-2222-4222-8222-222222222222')
  assert.equal(logged.length, 1)
  assert.match(logged[0], /share agent id agent_1/)
})

test('an unreadable ledger answers null, never an empty roster', (t) => {
  const dir = fixture()
  t.after(() => rmSync(dir.root, { recursive: true, force: true }))
  register(dir, 's-tab')
  ledger(dir, 's-tab')
  // A ledger path that exists but is a FILE, not a directory — readLedger reports the error
  // rather than treating it as "nobody was ever spawned".
  writeFileSync(join(dir.root, 'not-a-dir'), 'x')

  assert.equal(rehydratableAgents({ ...dir, ledgerDir: join(dir.root, 'not-a-dir') }), null)
})

test('an unreadable registry answers null — the other store a permissions error takes', (t) => {
  const dir = fixture()
  t.after(() => rmSync(dir.root, { recursive: true, force: true }))
  ledger(dir, 's-tab')
  writeFileSync(join(dir.root, 'registry-file'), 'x')

  assert.equal(rehydratableAgents({ ...dir, registryDir: join(dir.root, 'registry-file') }), null)
})

test('an unreadable heartbeat store answers null — the third store, and the one headless needs', (t) => {
  const dir = fixture()
  t.after(() => rmSync(dir.root, { recursive: true, force: true }))
  register(dir, 's-tab')
  ledger(dir, 's-tab')
  writeFileSync(join(dir.root, 'beat-file'), 'x')

  assert.equal(rehydratableAgents({ ...dir, heartbeatDir: join(dir.root, 'beat-file') }), null)
})

test('a rehydrated record carries the arrays agentView dereferences unconditionally', (t) => {
  const dir = fixture()
  t.after(() => rmSync(dir.root, { recursive: true, force: true }))
  register(dir, 's-tab')
  ledger(dir, 's-tab', { agentId: 'agent_1' })

  const [agent] = rehydratableAgents(dir)

  // agentView does `a.transcript.length` and `a.permissions.filter(...)` with no guard, so an
  // absent field takes the WHOLE roster down rather than one row.
  assert.ok(Array.isArray(agent.transcript))
  assert.ok(Array.isArray(agent.permissions))
})

test('a record with no agent_id is skipped rather than adopted under a synthesised id', (t) => {
  const dir = fixture()
  t.after(() => rmSync(dir.root, { recursive: true, force: true }))
  register(dir, 's-noid')
  writeFileSync(
    join(dir.ledgerDir, 's-noid.json'),
    JSON.stringify({ session_id: 's-noid', label: 'no agent id', mode: 'interactive' }),
  )

  assert.deepEqual(rehydratableAgents(dir), [])
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
  assert.match(src, /rehydratableAgents\(\{ log \}\)/)
  assert.match(src, /for \(const agent of rehydrated\) agents\.set\(agent\.id, agent\)/)
  assert.match(src, /function pruneRehydratedAgents\(\)/)
  // Called before both readers answer, so a worker that died after boot leaves the roster.
  assert.match(src, /case 'list_agents':\n\s+pruneRehydratedAgents\(\)/)
  assert.match(src, /case 'agent_status': \{\n\s+pruneRehydratedAgents\(\)/)
  // ⚠️ The resume path must PRUNE and then guard on EVERY row — never filter rehydrated rows
  // out. Filtering fixed the dead-worker deadlock by breaking the live case: a rehydrated row
  // whose worker still runs must keep blocking a resume, or two writers land on one
  // conversation.
  assert.match(src, /pruneRehydratedAgents\(\)\n\s+const holder = findLiveHolder\(agents\.values\(\), resume\)/)
  assert.ok(!/findLiveHolder\(\[\.\.\.agents\.values\(\)\]\.filter/.test(src), 'the rehydrated filter must not come back')
  // Not stamped on shutdown — that record belongs to the server that spawned the worker.
  assert.match(src, /if \(agent\.rehydrated\) continue/)
  // ⚠️ The MINT must never land on an adopted id. `seq` restarts at 0 on every reconnect, so
  // without this loop the first spawn_agent after a reconnect overwrites a live worker's
  // rehydrated row — reproducing the exact `unknown agent` symptom this PR closes.
  assert.match(src, /do \{\n\s+id = `agent_\$\{\+\+seq\}`\n\s+\} while \(agents\.has\(id\)\)/)
  // And the marker must REACH a caller: `agentView` is the only surface an MCP client sees,
  // and without this field a boot-time row is indistinguishable from a live one.
  assert.match(src, /rehydrated: a\.rehydrated === true,/)
})
