// Unit tests for the wrapper-path policy tier.
//
// The load-bearing property: a wrapper-backed worker's park is judged by the SAME `decide()`
// the SDK hook used — allow and deny are answered without a human, escalate is left parked
// for the manager — and every judgement leaves one `permissions.jsonl` row naming who
// decided. The real `policy.mjs` and the bundled `policy.json` are used, not a stub, so a
// rule change that would silently stop allowing cwd edits fails here.

import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { WrapperError } from './wrapper-stream.mjs'
import { decide as decideWith } from './policy.mjs'
import { createWrapperPolicyPoller, isPermissionQuestion } from './wrapper-policy.mjs'

const bundled = JSON.parse(readFileSync(new URL('./policy.json', import.meta.url), 'utf8'))
const RULES = bundled.rules

const CWD = '/tmp/wrapper-policy-worker'
const WRAPPER_SESSION = 'w-0001'

function agentRecord(overrides = {}) {
  return {
    id: 'agent_1',
    label: 'probe',
    cwd: CWD,
    rules: RULES,
    policyPath: null,
    status: 'blocked-on-permission',
    wrapperSessionId: WRAPPER_SESSION,
    ...overrides,
  }
}

function permissionQuestion(requestId, toolName, input) {
  return { request_id: requestId, kind: 'permission', tool_name: toolName, input }
}

/** A poller wired to in-memory fakes; returns the fakes so a test can assert on them. */
function harness({ agents, sessions, answerImpl } = {}) {
  const answers = []
  const rows = []
  const lines = []
  let roster = sessions
  const poller = createWrapperPolicyPoller({
    agents: () => agents,
    listSessions: async () => {
      if (roster instanceof Error) throw roster
      return roster
    },
    answer: async (call) => {
      answers.push(call)
      if (answerImpl) return answerImpl(call)
      return { ok: true }
    },
    decide: (toolName, input, cwd, rules) => decideWith(rules, toolName, input, cwd),
    logPermission: (row) => rows.push(row),
    log: (...a) => lines.push(a.join(' ')),
    now: () => '2026-11-06T12:00:00.000Z',
  })
  return { poller, answers, rows, lines, setRoster: (r) => { roster = r } }
}

test('allow: an Edit inside the worker cwd is answered allow with a policy row', async () => {
  const agent = agentRecord()
  const q = permissionQuestion('req-allow', 'Edit', { file_path: `${CWD}/task.md`, old_string: 'a', new_string: 'b' })
  const { poller, answers, rows } = harness({ agents: [agent], sessions: [{ id: WRAPPER_SESSION, question: q }] })

  const result = await poller.tick()

  assert.deepEqual(answers, [{ sessionId: WRAPPER_SESSION, requestId: 'req-allow', behavior: 'allow' }])
  assert.equal(rows.length, 1)
  assert.equal(rows[0].decided_by, 'policy')
  assert.equal(rows[0].decision, 'allow')
  assert.equal(rows[0].request_id, 'req-allow')
  assert.equal(rows[0].agent, 'agent_1')
  assert.equal(rows[0].via, 'wrapper')
  assert.equal(agent.status, 'running')
  assert.deepEqual(result.results, [{ request_id: 'req-allow', action: 'allow', delivered: true }])
})

test('escalate: a command no rule allows stays parked and logs one escalated row', async () => {
  const agent = agentRecord()
  const q = permissionQuestion('req-esc', 'Bash', { command: 'git log --oneline -3' })
  const { poller, answers, rows } = harness({ agents: [agent], sessions: [{ id: WRAPPER_SESSION, question: q }] })

  await poller.tick()
  await poller.tick()

  assert.deepEqual(answers, [], 'an escalated park must never be answered by the policy tier')
  assert.equal(rows.length, 1, 'the escalation is logged once, not once per tick')
  assert.equal(rows[0].decided_by, 'escalated')
  assert.equal(rows[0].decision, null)
  assert.equal(rows[0].request_id, 'req-esc')
  assert.equal(agent.status, 'blocked-on-permission')
})

test('deny: the bundled rm -rf rule is answered deny without waiting', async () => {
  const agent = agentRecord()
  const q = permissionQuestion('req-deny', 'Bash', { command: 'rm -rf /tmp/scratch' })
  const { poller, answers, rows } = harness({ agents: [agent], sessions: [{ id: WRAPPER_SESSION, question: q }] })

  await poller.tick()

  assert.deepEqual(answers, [{ sessionId: WRAPPER_SESSION, requestId: 'req-deny', behavior: 'deny' }])
  assert.equal(rows.length, 1)
  assert.equal(rows[0].decided_by, 'policy')
  assert.equal(rows[0].decision, 'deny')
})

test('an Edit OUTSIDE the worker cwd escalates rather than allowing', async () => {
  const agent = agentRecord()
  const q = permissionQuestion('req-out', 'Edit', { file_path: '/etc/hosts', old_string: 'a', new_string: 'b' })
  const { poller, answers, rows } = harness({ agents: [agent], sessions: [{ id: WRAPPER_SESSION, question: q }] })

  await poller.tick()

  assert.deepEqual(answers, [])
  assert.equal(rows[0].decided_by, 'escalated')
})

test('a manager that answered first (409) is not overridden and writes no policy row', async () => {
  const agent = agentRecord()
  const q = permissionQuestion('req-race', 'Write', { file_path: `${CWD}/out.txt`, content: 'x' })
  const { poller, answers, rows } = harness({
    agents: [agent],
    sessions: [{ id: WRAPPER_SESSION, question: q }],
    answerImpl: () => { throw new WrapperError('wrapper answer failed: HTTP 409', 409) },
  })

  const first = await poller.tick()
  await poller.tick()

  assert.equal(answers.length, 1, 'a 409 is terminal — the id is not retried')
  assert.equal(rows.filter((r) => r.decided_by === 'policy').length, 0)
  assert.equal(first.results[0].delivered, false)
  assert.equal(first.results[0].status, 409)
})

test('a transient answer failure (503) is retried on the next tick', async () => {
  const agent = agentRecord()
  const q = permissionQuestion('req-503', 'Edit', { file_path: `${CWD}/a.md`, old_string: 'a', new_string: 'b' })
  let calls = 0
  const { poller, answers, rows } = harness({
    agents: [agent],
    sessions: [{ id: WRAPPER_SESSION, question: q }],
    answerImpl: () => {
      calls += 1
      if (calls === 1) throw new WrapperError('wrapper answer failed: HTTP 503', 503)
      return { ok: true }
    },
  })

  await poller.tick()
  await poller.tick()

  assert.equal(answers.length, 2)
  assert.equal(rows.length, 1)
  assert.equal(rows[0].decided_by, 'policy')
})

test('wrapper down: nothing is answered, and the outage is logged once, not per tick', async () => {
  const agent = agentRecord()
  const { poller, answers, rows, lines, setRoster } = harness({ agents: [agent], sessions: new Error('connect ECONNREFUSED') })

  const r1 = await poller.tick()
  await poller.tick()
  await poller.tick()

  assert.equal(r1.ok, false)
  assert.deepEqual(answers, [])
  assert.deepEqual(rows, [])
  assert.equal(lines.filter((l) => l.includes('wrapper unreadable')).length, 1)

  setRoster([])
  await poller.tick()
  assert.equal(lines.filter((l) => l.includes('readable again')).length, 1)
})

test('an AskUserQuestion park is never judged by the policy', async () => {
  const agent = agentRecord()
  const q = { request_id: 'req-ask', kind: 'question', tool_name: 'AskUserQuestion', questions: [] }
  const { poller, answers, rows } = harness({ agents: [agent], sessions: [{ id: WRAPPER_SESSION, question: q }] })

  await poller.tick()

  assert.deepEqual(answers, [])
  assert.deepEqual(rows, [])
})

test('only this server’s own wrapper-backed agents are judged', async () => {
  const tab = agentRecord({ id: 'agent_tab', wrapperSessionId: null })
  const q = permissionQuestion('req-foreign', 'Edit', { file_path: `${CWD}/x.md`, old_string: 'a', new_string: 'b' })
  const { poller, answers } = harness({ agents: [tab], sessions: [{ id: 'someone-elses-session', question: q }] })

  const result = await poller.tick()

  assert.deepEqual(answers, [])
  assert.equal(result.skipped, 'no wrapper-backed agents')
})

test('a per-spawn policy overlay is what decides, not the bundled rules', async () => {
  const strict = [{ tool: '*', match: '*', action: 'escalate' }]
  const agent = agentRecord({ rules: strict, policyPath: '/tmp/strict.json' })
  const q = permissionQuestion('req-overlay', 'Edit', { file_path: `${CWD}/a.md`, old_string: 'a', new_string: 'b' })
  const { poller, answers, rows } = harness({ agents: [agent], sessions: [{ id: WRAPPER_SESSION, question: q }] })

  await poller.tick()

  assert.deepEqual(answers, [])
  assert.equal(rows[0].decided_by, 'escalated')
  assert.equal(rows[0].policy, '/tmp/strict.json')
})

test('an adopted row whose per-spawn policy was not persisted is left to the manager', async () => {
  const adopted = agentRecord({ rules: undefined, policyPath: '/tmp/strict.json', rehydrated: true })
  const q = permissionQuestion('req-adopted', 'Edit', { file_path: `${CWD}/a.md`, old_string: 'a', new_string: 'b' })
  const { poller, answers, rows } = harness({ agents: [adopted], sessions: [{ id: WRAPPER_SESSION, question: q }] })

  await poller.tick()

  assert.deepEqual(answers, [])
  assert.deepEqual(rows, [])
})

test('run() never stacks a second poll behind a slow one', async () => {
  let release
  const gate = new Promise((r) => { release = r })
  const poller = createWrapperPolicyPoller({
    agents: () => [agentRecord()],
    listSessions: async () => { await gate; return [] },
    answer: async () => ({}),
    decide: () => ({ action: 'escalate', key: '', rule: null }),
    logPermission: () => {},
    log: () => {},
  })

  const first = poller.run()
  const second = await poller.run()
  release()
  await first

  assert.equal(second.skipped, 'in flight')
})

test('isPermissionQuestion: kind-less records from an older wrapper still count', () => {
  assert.equal(isPermissionQuestion({ request_id: 'r', tool_name: 'Edit' }), true)
  assert.equal(isPermissionQuestion({ request_id: 'r', tool_name: 'Edit', kind: 'permission' }), true)
  assert.equal(isPermissionQuestion({ request_id: 'r', tool_name: 'Edit', kind: 'question' }), false)
  assert.equal(isPermissionQuestion({ request_id: '', tool_name: 'Edit' }), false)
  assert.equal(isPermissionQuestion(null), false)
})
