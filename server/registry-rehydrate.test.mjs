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
// The resumed-worker case pins the one deliberate difference from `workerSessions()`: that
// function drops `resumed_from` records for the cap counter's sake, and a roster that
// inherited the filter would silently hide exactly the workers a manager most needs to see.

import { test } from 'node:test'
import assert from 'node:assert/strict'
import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'

import { stampRecord } from './heartbeat.mjs'
import { rehydratableAgents, rehydratedStatus, toAgentRecord } from './registry-rehydrate.mjs'

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

// The registry's own shape, written the way Claude Code writes it: keyed by pid, deleted on
// exit. The pid is this process's, so an entry here is unambiguously live.
function register(dir, sessionId, status = 'busy') {
  writeFileSync(
    join(dir.registryDir, `${sessionId}.json`),
    JSON.stringify({ sessionId, pid: process.pid, status }),
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

  const agents = rehydratableAgents(dir)

  assert.deepEqual(agents, [])
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

test('an unreadable store answers null, never an empty roster', (t) => {
  const dir = fixture()
  t.after(() => rmSync(dir.root, { recursive: true, force: true }))
  register(dir, 's-tab')
  ledger(dir, 's-tab')
  // A ledger path that exists but is a FILE, not a directory — readLedger reports the error
  // rather than treating it as "nobody was ever spawned".
  writeFileSync(join(dir.root, 'not-a-dir'), 'x')

  assert.equal(rehydratableAgents({ ...dir, ledgerDir: join(dir.root, 'not-a-dir') }), null)
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
