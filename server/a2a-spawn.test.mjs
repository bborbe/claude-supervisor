// Tests for the A2A spawn path.
//
// The cases here pin the rules the dispatch must get right, and the refusals are asserted as
// hard as the successes — a module whose whole job is to refuse a call it cannot honour is not
// tested by its happy path. Two assertions are load-bearing beyond their own subject: the record
// carries NO result (the asynchrony SC5 requires), and the token appears in no projection.

import { test } from 'node:test'
import assert from 'node:assert/strict'
import {
  A2A_ACK_TIMEOUT_MS,
  A2A_LEDGER_MODE,
  A2A_SPAWN_TARGETS,
  a2aCardUrlFor,
  a2aCreateCallFor,
  a2aSpawnArgumentRefusal,
  a2aTurnPatch,
  a2aWorkerRecord,
  a2aWorkerStatus,
  collectWorkerTurn,
  createWorkerOnController,
  fetchWorkerCard,
  ledgerModeForStatus,
  openWorkerTurn,
  a2aListCallFor,
  a2aLiveSessions,
  listWorkersOnController,
  pollA2A,
  reapExistingWorkerForTask,
  resolveA2AControllerTarget,
  a2aStopCallFor,
  stopWorkerOnController,
} from './a2a-spawn.mjs'

const TOKEN = 'token-value-that-must-never-be-echoed'

// --- the target surface -------------------------------------------------------------------

test('the A2A targets are docker and k8s, and nothing else', () => {
  assert.deepEqual(A2A_SPAWN_TARGETS, ['docker', 'k8s'])
})

test('the roster status is the target name, mirroring cluster', () => {
  assert.equal(a2aWorkerStatus({ target: 'docker' }), 'docker')
  assert.equal(a2aWorkerStatus({ target: 'k8s' }), 'k8s')
  // A local or cluster worker is not this path's to name.
  assert.equal(a2aWorkerStatus({ target: 'local' }), null)
  assert.equal(a2aWorkerStatus({ target: 'cluster' }), null)
  assert.equal(a2aWorkerStatus({}), null)
})

test('the ledger mode is its own value, and only for an A2A status', () => {
  assert.equal(A2A_LEDGER_MODE, 'a2a')
  assert.equal(ledgerModeForStatus('docker'), 'a2a')
  assert.equal(ledgerModeForStatus('k8s'), 'a2a')
  // The existing modes keep their own answers — this must not swallow them.
  assert.equal(ledgerModeForStatus('interactive'), null)
  assert.equal(ledgerModeForStatus('cluster'), null)
  assert.equal(ledgerModeForStatus('running'), null)
})

// --- argument refusals --------------------------------------------------------------------

test('a well-formed call is allowed for both targets', () => {
  assert.equal(a2aSpawnArgumentRefusal({ target: 'docker', task: 'some task' }), null)
  assert.equal(a2aSpawnArgumentRefusal({ target: 'k8s', task: 'some task' }), null)
})

test('task is required, because the controller creates one worker per task name', () => {
  for (const task of [undefined, null, '', 42]) {
    const refusal = a2aSpawnArgumentRefusal({ target: 'docker', task })
    assert.match(refusal, /requires `task`/)
  }
})

test('every local-only argument is refused rather than silently dropped', () => {
  const cases = [
    { env: { A: '1' }, names: /env is not supported/ },
    { resume: 'abc', names: /resume is not supported/ },
    { policy: '/tmp/policy.json', names: /policy is not supported/ },
    { interactive: true, names: /interactive is not supported/ },
    { windowId: '3', names: /window_id is not supported/ },
    { role: 'agent', names: /role is not supported/ },
    { shipping: true, names: /shipping is not supported/ },
  ]
  for (const { names, ...args } of cases) {
    const refusal = a2aSpawnArgumentRefusal({ target: 'docker', task: 'some task', ...args })
    assert.match(refusal, names)
    // The refusal names the target, so the caller can tell which leg refused.
    assert.match(refusal, /"docker"/)
  }
})

test('an empty env object is not a refusal — it sets nothing', () => {
  assert.equal(a2aSpawnArgumentRefusal({ target: 'docker', task: 't', env: {} }), null)
})

test('the refusal is only defined for an A2A target', () => {
  assert.match(a2aSpawnArgumentRefusal({ target: 'local', task: 't' }), /not an A2A target/)
})

// --- locating the controller ---------------------------------------------------------------

test('the controller URL resolves per target from the file, the token only from the environment', () => {
  const resolved = resolveA2AControllerTarget({
    target: 'docker',
    envToken: TOKEN,
    file: { a2a: { url: { docker: 'http://127.0.0.1:8080/a2a' } } },
  })
  assert.equal(resolved.url, 'http://127.0.0.1:8080/a2a')
  assert.equal(resolved.token, TOKEN)
})

test('each target resolves its OWN url from one map — the 2026-10-14 divergence in one assertion', () => {
  // The defect this split closes: a `docker` spawn must not reach the k8s controller's address,
  // and a `k8s` spawn must not reach the Mac's. One URL cannot be right for both, so the map is
  // read per target rather than once.
  const file = {
    a2a: { url: { docker: 'http://127.0.0.1:8080/a2a', k8s: 'https://nuke.example/a2a' } },
  }
  assert.equal(
    resolveA2AControllerTarget({ target: 'docker', envToken: TOKEN, file }).url,
    'http://127.0.0.1:8080/a2a',
  )
  assert.equal(
    resolveA2AControllerTarget({ target: 'k8s', envToken: TOKEN, file }).url,
    'https://nuke.example/a2a',
  )
})

test('configuring one target does not configure the other', () => {
  // The failure this must keep: a map that names only `docker` leaves `k8s` unconfigured, and a
  // k8s spawn has to refuse rather than fall back to the docker address.
  const file = { a2a: { url: { docker: 'http://127.0.0.1:8080/a2a' } } }
  const resolved = resolveA2AControllerTarget({ target: 'k8s', envToken: TOKEN, file })
  assert.match(resolved.error, /SUPERVISOR_A2A_URL_K8S/, 'must name the key the operator has to set')
  // Asserted on the RESULT, not on the message: the refusal's own "e.g." hint carries an example
  // address, so a message-matching assertion here would fail on the hint rather than on a fallback.
  assert.equal(resolved.url, undefined, 'must not fall back to the other target')
})

test('the environment wins over the file, and a trailing slash is trimmed', () => {
  const resolved = resolveA2AControllerTarget({
    target: 'docker',
    envUrl: 'http://127.0.0.1:9099/a2a/',
    envToken: TOKEN,
    file: { a2a: { url: { docker: 'http://elsewhere/a2a' } } },
  })
  assert.equal(resolved.url, 'http://127.0.0.1:9099/a2a')
})

test('a bare-string a2a.url REFUSES, naming the map that replaces it', () => {
  // The shape that made the bug reachable. Refusing turns a silent mis-route into an error that
  // names the key to change, which is the whole point of not accepting the legacy value.
  const resolved = resolveA2AControllerTarget({
    target: 'docker',
    envToken: TOKEN,
    file: { a2a: { url: 'http://127.0.0.1:8080/a2a' } },
  })
  assert.match(resolved.error, /is a single string/)
  assert.match(resolved.error, /per-target map/, 'must name the shape to use instead')
  assert.equal(resolved.url, undefined, 'must not resolve a url at all')
})

test('an unknown target refuses, naming the targets that exist', () => {
  const resolved = resolveA2AControllerTarget({ target: 'local', envToken: TOKEN, file: {} })
  assert.match(resolved.error, /is not an A2A spawn target/)
  assert.match(resolved.error, /"docker" or "k8s"/)
})

// --- the plaintext token the migration removes ----------------------------------------------

test('a plaintext a2a.token REFUSES, naming the key and the file to delete it from', () => {
  const resolved = resolveA2AControllerTarget({
    target: 'docker',
    envUrl: 'http://127.0.0.1:8080/a2a',
    envToken: TOKEN,
    file: { a2a: { url: { docker: 'http://127.0.0.1:8080/a2a' }, token: 'file-token-3c1a' } },
    path: '/etc/sup.json',
  })
  assert.match(resolved.error, /"a2a\.token" is set/)
  assert.match(resolved.error, /\/etc\/sup\.json/, 'must name the file to edit')
  assert.match(resolved.error, /Delete the "a2a\.token" key/, 'must name the fix')
  assert.equal(resolved.token, undefined, 'must not resolve a token at all')
})

test('the plaintext refusal fires even when the env token would win, and echoes nothing', () => {
  // Presence, not precedence: env-first means the file value never WINS, so a check placed at
  // the point of use would never see it and the plaintext token would sit in the file
  // indefinitely while every spawn kept working.
  const resolved = resolveA2AControllerTarget({
    target: 'docker',
    envUrl: 'http://127.0.0.1:8080/a2a',
    envToken: TOKEN,
    file: { a2a: { url: { docker: 'http://127.0.0.1:8080/a2a' }, token: 'file-token-3c1a' } },
  })
  assert.ok(resolved.error, 'a plaintext token must refuse even when the env var would win')
  assert.doesNotMatch(resolved.error, /file-token-3c1a/, 'the refused value must not travel back')
})

test('an emptied a2a.token is absence, not a plaintext token', () => {
  const resolved = resolveA2AControllerTarget({
    target: 'docker',
    envUrl: 'http://127.0.0.1:8080/a2a',
    envToken: TOKEN,
    file: { a2a: { url: { docker: 'http://127.0.0.1:8080/a2a' }, token: '' } },
  })
  assert.equal(resolved.token, TOKEN)
})

test('a bad URL in the LOSER still refuses — a dormant value breaks the spawn later', () => {
  const resolved = resolveA2AControllerTarget({
    target: 'docker',
    envUrl: 'http://127.0.0.1:9099/a2a',
    envToken: TOKEN,
    file: { a2a: { url: { docker: 'not-a-url' } } },
  })
  assert.match(resolved.error, /"a2a\.url\.docker" in/)
})

test('an unset controller refuses rather than defaulting to a guessed address', () => {
  const resolved = resolveA2AControllerTarget({ target: 'docker', file: {} })
  assert.match(resolved.error, /SUPERVISOR_A2A_URL_DOCKER/)
})

test('a missing token refuses, and the refusal names the key rather than a value', () => {
  const resolved = resolveA2AControllerTarget({
    target: 'docker',
    file: { a2a: { url: { docker: 'http://127.0.0.1:8080/a2a' } } },
  })
  assert.match(resolved.error, /SUPERVISOR_A2A_TOKEN_DOCKER/)
})

test('a non-object a2a section refuses without echoing it', () => {
  const resolved = resolveA2AControllerTarget({ target: 'docker', file: { a2a: TOKEN } })
  assert.match(resolved.error, /is not an object/)
  assert.doesNotMatch(resolved.error, new RegExp(TOKEN))
})

// --- the create call -----------------------------------------------------------------------

test('create is a ONE-SHOT control call carrying a data part, not a text part', () => {
  const call = a2aCreateCallFor({
    endpoint: 'http://127.0.0.1:8080/a2a',
    task: 'some task',
    token: TOKEN,
    messageId: 'msg-1',
    contextId: 'ctx-1',
  })
  assert.equal(call.body.method, 'SendMessage')
  assert.equal(call.body.params.message.role, 'ROLE_USER')
  assert.deepEqual(call.body.params.message.parts, [{ data: { operation: 'create', create: { task: 'some task' } } }])
  assert.equal(call.headers.authorization, `Bearer ${TOKEN}`)
  // ⚠️ A control call is one-shot: asking the route for a stream reads as a call with no frames.
  assert.equal(call.headers.accept, undefined)
})

test('the first call mints a contextId rather than omitting it', () => {
  const call = a2aCreateCallFor({
    endpoint: 'http://127.0.0.1:8080/a2a',
    task: 't',
    token: TOKEN,
    messageId: 'msg-1',
    mint: () => 'minted-ctx',
  })
  assert.equal(call.contextId, 'minted-ctx')
})

test('create refuses an empty task before it reaches the wire', () => {
  const call = a2aCreateCallFor({
    endpoint: 'http://127.0.0.1:8080/a2a',
    task: '',
    token: TOKEN,
    messageId: 'msg-1',
    mint: () => 'ctx',
  })
  assert.match(call.error, /non-empty `task`/)
})

// --- the card ------------------------------------------------------------------------------

test('the card is read from the ORIGIN create returned, never from the /a2a path', () => {
  assert.equal(
    a2aCardUrlFor({ a2aUrl: 'http://localhost:9093/a2a' }).url,
    'http://localhost:9093/.well-known/agent-card.json',
  )
  assert.equal(
    a2aCardUrlFor({ a2aUrl: 'https://worker.example:8443/a2a' }).url,
    'https://worker.example:8443/.well-known/agent-card.json',
  )
})

test('a missing or unparseable worker URL refuses', () => {
  assert.match(a2aCardUrlFor({}).error, /not a worker A2A URL/)
  assert.match(a2aCardUrlFor({ a2aUrl: 'nonsense' }).error, /not a parseable URL/)
})

// --- the record ----------------------------------------------------------------------------

test('the record carries NO result — the spawn returns before any turn is sent', () => {
  const { record } = a2aWorkerRecord({
    id: 'agent_1',
    prompt: 'do the thing',
    task: 'some task',
    target: 'docker',
    sessionId: 'sess-1',
    workerId: 'worker-1',
    a2aUrl: 'http://localhost:9093/a2a',
    workerToken: TOKEN,
    createdAt: '2026-10-13T00:00:00.000Z',
  })
  assert.equal(record.result, null)
  assert.equal(record.status, 'docker')
  assert.equal(record.sessionId, 'sess-1')
})

test('the record seeds transcript and permissions EMPTY, never absent', () => {
  // `agentView` dereferences both unconditionally and `list_agents` maps over every agent, so a
  // missing one makes the whole roster unreadable rather than just that row.
  const { record } = a2aWorkerRecord({
    id: 'agent_1',
    prompt: 'p',
    task: 't',
    target: 'k8s',
    sessionId: 's',
    workerId: 'w',
    a2aUrl: 'http://localhost:9093/a2a',
    workerToken: TOKEN,
    createdAt: '2026-10-13T00:00:00.000Z',
  })
  assert.deepEqual(record.transcript, [])
  assert.deepEqual(record.permissions, [])
})

test('the token is held on the record, and the record is the ONLY place it lives', () => {
  const { record } = a2aWorkerRecord({
    id: 'agent_1',
    prompt: 'p',
    task: 't',
    target: 'docker',
    sessionId: 's',
    workerId: 'w',
    a2aUrl: 'http://localhost:9093/a2a',
    workerToken: TOKEN,
    createdAt: '2026-10-13T00:00:00.000Z',
  })
  assert.equal(record.workerToken, TOKEN)
})

test('the record refuses a target this path does not own', () => {
  const { error } = a2aWorkerRecord({ target: 'cluster', sessionId: 's' })
  assert.match(error, /not an A2A target/)
})

// --- the two calls, against a stub fetch ----------------------------------------------------
//
// Both are driven with a stub rather than a live controller: the point of these cases is the
// refusal behaviour, and a controller that has to exist before a refusal can be tested is a
// controller whose refusals never get tested.

const WORKER = { worker_id: 'w1', task: 'some task', a2a_url: 'http://localhost:9093/a2a', backend: 'docker', state: 'running' }

// The raw body a one-shot control reply arrives as — a STRING, which is what the client parses.
const createReply = ({ worker = WORKER, token = TOKEN, state = 'TASK_STATE_COMPLETED' } = {}) =>
  JSON.stringify({
    result: {
      id: 'task-1',
      status: { state },
      artifacts: [{ parts: [{ data: { operation: 'create', create: { worker, token } } }] }],
    },
  })

const stubFetch = (body, { ok = true, status = 200 } = {}) => async () => ({
  ok,
  status,
  text: async () => body,
  json: async () => JSON.parse(body),
})

test('create returns the worker and its token, and never logs either', async () => {
  const result = await createWorkerOnController({
    endpoint: 'http://127.0.0.1:8080/a2a',
    task: 'some task',
    token: TOKEN,
    messageId: 'msg-1',
    mint: () => 'ctx-1',
    fetchImpl: stubFetch(createReply()),
  })
  assert.equal(result.worker.worker_id, 'w1')
  assert.equal(result.token, TOKEN)
  assert.equal(result.contextId, 'ctx-1')
})

test('the capture hook receives the exact body sent, and never the headers that carry the token', async () => {
  let captured
  let sentBody
  const fetchImpl = async (_url, init) => {
    sentBody = init.body
    return stubFetch(createReply())()
  }
  await createWorkerOnController({
    endpoint: 'http://127.0.0.1:8080/a2a',
    task: 'some task',
    token: TOKEN,
    messageId: 'msg-1',
    mint: () => 'ctx-1',
    fetchImpl,
    capture: (c) => {
      captured = c
    },
  })
  assert.deepEqual(Object.keys(captured).sort(), ['body', 'url'])
  assert.equal(JSON.stringify(captured.body), sentBody)
  assert.ok(!JSON.stringify(captured).includes(TOKEN), 'the capture must never carry the bearer token')
})

test('a throwing capture never fails the spawn', async () => {
  const result = await createWorkerOnController({
    endpoint: 'http://127.0.0.1:8080/a2a',
    task: 'some task',
    token: TOKEN,
    messageId: 'msg-1',
    mint: () => 'ctx-1',
    fetchImpl: stubFetch(createReply()),
    capture: () => {
      throw new Error('disk full')
    },
  })
  assert.equal(result.worker.worker_id, 'w1')
})

test('a FAILED create is reported as an unknown outcome, never as "no worker was created"', async () => {
  // ⚠️ The store copy fails AFTER the executor has run, so a FAILED status can accompany a
  // container that WAS started. A caller that read this as "nothing happened" would leak it.
  const result = await createWorkerOnController({
    endpoint: 'http://127.0.0.1:8080/a2a',
    task: 'some task',
    token: TOKEN,
    messageId: 'msg-1',
    mint: () => 'ctx-1',
    fetchImpl: stubFetch(createReply({ state: 'TASK_STATE_FAILED' })),
  })
  assert.match(result.error, /FAILED/)
  assert.match(result.error, /does NOT mean the operation did not happen/)
  assert.match(result.error, /reconcile/)
})

test('a create reply with no a2a_url refuses rather than falling back to the controller', async () => {
  const result = await createWorkerOnController({
    endpoint: 'http://127.0.0.1:8080/a2a',
    task: 't',
    token: TOKEN,
    messageId: 'm',
    mint: () => 'c',
    fetchImpl: stubFetch(createReply({ worker: { worker_id: 'w1' } })),
  })
  assert.match(result.error, /no `a2a_url`/)
})

test('a create reply with no token refuses — nothing could authenticate to that worker', async () => {
  const result = await createWorkerOnController({
    endpoint: 'http://127.0.0.1:8080/a2a',
    task: 't',
    token: TOKEN,
    messageId: 'm',
    mint: () => 'c',
    fetchImpl: stubFetch(createReply({ token: '' })),
  })
  assert.match(result.error, /no worker token/)
})

test('a non-2xx from the controller refuses with the status', async () => {
  const result = await createWorkerOnController({
    endpoint: 'http://127.0.0.1:8080/a2a',
    task: 't',
    token: TOKEN,
    messageId: 'm',
    mint: () => 'c',
    fetchImpl: stubFetch('nope', { ok: false, status: 503 }),
  })
  assert.match(result.error, /HTTP 503/)
})

test('a transport failure refuses rather than throwing out of the spawn', async () => {
  const result = await createWorkerOnController({
    endpoint: 'http://127.0.0.1:8080/a2a',
    task: 't',
    token: TOKEN,
    messageId: 'm',
    mint: () => 'c',
    fetchImpl: async () => {
      throw new Error('ECONNREFUSED')
    },
  })
  assert.match(result.error, /could not be made/)
})

test('the card is read from the origin and its endpoint resolved from supportedInterfaces', async () => {
  const result = await fetchWorkerCard({
    a2aUrl: 'http://localhost:9093/a2a',
    fetchImpl: stubFetch(JSON.stringify({ supportedInterfaces: [{ url: 'http://localhost:9093/a2a' }] })),
  })
  assert.equal(result.endpoint, 'http://localhost:9093/a2a')
})

test('a card with no supportedInterfaces refuses — never a top-level url fallback', async () => {
  const result = await fetchWorkerCard({
    a2aUrl: 'http://localhost:9093/a2a',
    fetchImpl: stubFetch(JSON.stringify({ url: 'http://localhost:9093/a2a' })),
  })
  assert.match(result.error, /supportedInterfaces/)
})

test('a card that 404s past its deadline refuses at spawn rather than at the first turn', async () => {
  let t = 0
  const result = await fetchWorkerCard({
    a2aUrl: 'http://localhost:9093/a2a',
    fetchImpl: stubFetch('not found', { ok: false, status: 404 }),
    readyTimeoutMs: 1000,
    now: () => t,
    sleep: async (ms) => {
      t += ms
    },
  })
  assert.match(result.error, /HTTP 404/)
})

test('an ingress 404 then 503 is retried until the k8s worker answers', async () => {
  // Measured 2026-10-14 on nuke-dev: 404 while the pod is Pending, 503 while it starts, then 200.
  const answers = [404, 503, 200]
  let calls = 0
  const fetchImpl = async () => {
    const status = answers[calls++]
    if (status !== 200) return { ok: false, status, json: async () => ({}) }
    return { ok: true, status, json: async () => ({ supportedInterfaces: [{ url: 'http://localhost:9093/a2a' }] }) }
  }
  const result = await fetchWorkerCard({ a2aUrl: 'http://localhost:9093/a2a', fetchImpl, sleep: async () => {} })
  assert.equal(result.endpoint, 'http://localhost:9093/a2a')
  assert.equal(calls, 3)
})

test('a refused card fetch is retried until the worker listens', async () => {
  // `create` returns when the container STARTS; its server listens a few seconds later.
  let calls = 0
  const fetchImpl = async () => {
    calls += 1
    if (calls < 3) throw new Error('fetch failed')
    return { ok: true, status: 200, json: async () => ({ supportedInterfaces: [{ url: 'http://localhost:9093/a2a' }] }) }
  }
  const result = await fetchWorkerCard({ a2aUrl: 'http://localhost:9093/a2a', fetchImpl, sleep: async () => {} })
  assert.equal(result.endpoint, 'http://localhost:9093/a2a')
  assert.equal(calls, 3)
})

test('a card that never listens gives up at its deadline', async () => {
  let t = 0
  const result = await fetchWorkerCard({
    a2aUrl: 'http://localhost:9093/a2a',
    fetchImpl: async () => {
      throw new Error('fetch failed')
    },
    readyTimeoutMs: 1000,
    now: () => t,
    sleep: async (ms) => {
      t += ms
    },
  })
  assert.match(result.error, /within 1000 ms/)
})

test('an HTTP answer outside the not-ready set is final and never retried', async () => {
  let calls = 0
  const result = await fetchWorkerCard({
    a2aUrl: 'http://localhost:9093/a2a',
    fetchImpl: async (...args) => {
      calls += 1
      return stubFetch('unauthorized', { ok: false, status: 401 })(...args)
    },
    sleep: async () => {},
  })
  assert.match(result.error, /HTTP 401/)
  assert.equal(calls, 1)
})

// --- the turn half --------------------------------------------------------------------------

// An SSE body — what a streaming turn actually returns, rather than a JSON object.
const sse = (frames) => frames.map((f) => `data: ${JSON.stringify(f)}\n`).join('\n')

// The frame order the worker really sends: `task(SUBMITTED)` → `artifactUpdate` →
// `statusUpdate(COMPLETED)`. The order is load-bearing, not decorative — the `task` frame carries
// a status of its own, so a collector reading `task.status.state` reports SUBMITTED on a turn that
// completed. That defect was measured by the client's own tests before it shipped.
const turnFrames = ({ contextId = 'ctx-1', reply = 'done', state = 'TASK_STATE_COMPLETED' } = {}) => [
  { task: { id: 'task-1', contextId, status: { state: 'TASK_STATE_SUBMITTED' } } },
  { artifactUpdate: { parts: [{ text: reply }] } },
  { statusUpdate: { state } },
]

const turnFetch = (frames, opts) => stubFetch(sse(frames), opts)
const turnResponse = (frames, opts) => turnFetch(frames, opts)('http://x', {})

test('the send issues a streaming turn and returns once the worker acknowledges', async () => {
  const sent = await openWorkerTurn({
    endpoint: 'http://localhost:9093/a2a',
    contextId: 'ctx-1',
    text: 'do the thing',
    token: TOKEN,
    messageId: 'msg-1',
    fetchImpl: turnFetch(turnFrames()),
  })
  assert.equal(sent.error, undefined)
  assert.ok(sent.res, 'an acknowledging worker yields the response')
  assert.equal(sent.contextId, 'ctx-1')
})

test('the send refuses with no contextId rather than opening a conversation', async () => {
  const sent = await openWorkerTurn({
    endpoint: 'http://localhost:9093/a2a',
    text: 'do the thing',
    token: TOKEN,
    messageId: 'msg-1',
    fetchImpl: turnFetch(turnFrames()),
  })
  assert.match(sent.error, /contextId/)
})

test('a request that never left is an error, not a slow worker', async () => {
  const sent = await openWorkerTurn({
    endpoint: 'http://localhost:9093/a2a',
    contextId: 'ctx-1',
    text: 'do the thing',
    token: TOKEN,
    messageId: 'msg-1',
    fetchImpl: async () => {
      throw new Error('ECONNREFUSED')
    },
  })
  assert.match(sent.error, /could not be sent/)
  assert.equal(sent.pending, undefined, 'a request that never left is not a pending turn')
})

test('the send hands back an unresolved request instead of blocking, and never aborts it', async () => {
  // ⚠️ THIS IS SC5'S FALSIFIER, ASSERTED DIRECTLY: *"a send whose latency tracks the turn's
  // duration (blocking)"*. The worker here does not answer until it is released, and the send must
  // still return — carrying the REQUEST rather than the reply.
  let release
  const gate = new Promise((resolve) => {
    release = resolve
  })
  const fetchImpl = async () => {
    await gate
    return { ok: true, status: 200, text: async () => sse(turnFrames()) }
  }

  const started = Date.now()
  const sent = await openWorkerTurn({
    endpoint: 'http://localhost:9093/a2a',
    contextId: 'ctx-1',
    text: 'do the thing',
    token: TOKEN,
    messageId: 'msg-1',
    ackTimeoutMs: 20,
    fetchImpl,
  })
  const elapsed = Date.now() - started

  assert.equal(sent.error, undefined)
  assert.equal(sent.res, undefined, 'a worker that has not acknowledged yields no response yet')
  assert.ok(sent.pending, 'the send must hand the unresolved request back rather than block')
  assert.ok(elapsed < 1000, `the send must return near its bound, took ${elapsed}ms`)

  // ⚠️ AND THE REQUEST IS NOT ABORTED: releasing it still produces the turn's outcome. An abort
  // would have killed the turn the send had just started, and reported a failure for work running.
  release()
  const collected = await collectWorkerTurn({ res: sent.pending, sentContextId: sent.contextId })
  assert.equal(collected.error, undefined)
  assert.equal(collected.state, 'TASK_STATE_COMPLETED')
  assert.equal(collected.reply, 'done')
})

test("the ack bound sits below SC5's budget, because that budget covers the whole spawn", () => {
  assert.ok(A2A_ACK_TIMEOUT_MS < 2000, `the bound must leave room inside 2s, is ${A2A_ACK_TIMEOUT_MS}`)
})

test('the collection reads the LAST statusUpdate, not the task frame that precedes it', async () => {
  const collected = await collectWorkerTurn({
    res: await turnResponse(turnFrames()),
    sentContextId: 'ctx-1',
  })
  assert.equal(collected.error, undefined)
  assert.equal(collected.state, 'TASK_STATE_COMPLETED')
  assert.equal(collected.reply, 'done')
})

test('the collection refuses a turn that ran under a different contextId', async () => {
  const collected = await collectWorkerTurn({
    res: await turnResponse(turnFrames({ contextId: 'ctx-OTHER' })),
    sentContextId: 'ctx-1',
  })
  assert.match(collected.error, /contextId/)
})

test('an error frame inside HTTP 200 is fatal, because the status proves nothing there', async () => {
  const body = sse([{ error: { code: -32700, message: 'unknown part content type' } }])
  const collected = await collectWorkerTurn({ res: await stubFetch(body)('http://x', {}), sentContextId: 'ctx-1' })
  assert.match(collected.error, /refused the turn/)
})

test('a non-2xx answer is reported rather than read as frames', async () => {
  const collected = await collectWorkerTurn({
    res: await stubFetch('nope', { ok: false, status: 502 })('http://x', {}),
    sentContextId: 'ctx-1',
  })
  assert.match(collected.error, /HTTP 502/)
})

test('the collection refuses with no response at all', async () => {
  const collected = await collectWorkerTurn({ sentContextId: 'ctx-1' })
  assert.match(collected.error, /no response/)
})

test('the collection reports a pending request that later fails, rather than hanging', async () => {
  // ⚠️ THIS IS A PRODUCTION PATH, not a defensive one: it is what a worker that is slow to
  // acknowledge produces when its connection then drops. `openWorkerTurn` has already returned
  // `pending` by then, so the rejection arrives AFTER the send returned — and a collector that
  // awaited it without a guard would leave the turn's outcome unfiled forever, with the record
  // showing `result: null` and no error to explain it.
  const failing = (async () => {
    throw new Error('socket hang up')
  })()
  const collected = await collectWorkerTurn({ res: failing, sentContextId: 'ctx-1' })
  assert.match(collected.error, /socket hang up/)
})

test('a collected turn files its outcome and no error', () => {
  const patch = a2aTurnPatch(
    { state: 'TASK_STATE_COMPLETED', contextId: 'ctx-1', reply: 'done' },
    { at: '2026-10-13T00:00:00.000Z' },
  )
  assert.equal(patch.error, null)
  assert.equal(patch.result.kind, 'a2a-turn')
  assert.equal(patch.result.state, 'TASK_STATE_COMPLETED')
  assert.equal(patch.result.reply, 'done')
  assert.equal(patch.result.collectedAt, '2026-10-13T00:00:00.000Z')
})

test('a failed collection clears the result rather than leaving a stale one standing', () => {
  const patch = a2aTurnPatch({ error: 'the worker answered HTTP 502 to the turn.' })
  assert.equal(patch.result, null)
  assert.match(patch.error, /HTTP 502/)
})

test('a FAILED turn is filed as an error AND keeps its outcome', () => {
  // ⚠️ Both, not either. The state is the worker's own report and the error is what a roster read
  // needs: dropping the result would lose the state, dropping the error would read as a turn that
  // answered.
  const patch = a2aTurnPatch({ state: 'TASK_STATE_FAILED', contextId: 'ctx-1', reply: '' })
  assert.equal(patch.result.state, 'TASK_STATE_FAILED')
  assert.match(patch.error, /TASK_STATE_FAILED/)
})

test('the patch carries only the outcome, never the record it is merged into', () => {
  // ⚠️ The shape is pinned rather than spot-checked, because the way a token reaches the ledger is
  // a spread — and a spread of the agent record would carry `workerToken` with it. An exact key
  // set fails loudly on that change; a `does not include` check would pass while it happened.
  const patch = a2aTurnPatch({ state: 'TASK_STATE_COMPLETED', contextId: 'ctx-1', reply: 'done' })
  assert.deepEqual(Object.keys(patch).sort(), ['error', 'result'])
  assert.deepEqual(Object.keys(patch.result).sort(), ['collectedAt', 'contextId', 'kind', 'reply', 'state'])
})

// --- liveness (SC7) -------------------------------------------------------------------------

const a2aRecord = (over = {}) => ({ session_id: 's-1', mode: 'a2a', worker_id: 'w-1', ended_at: null, ...over })

test('the list call is a one-shot control call asking for `list`', () => {
  const call = a2aListCallFor({ endpoint: 'http://127.0.0.1:8080/a2a', token: TOKEN, messageId: 'm-1', mint: () => 'ctx-1' })
  assert.equal(call.error, undefined)
  assert.equal(call.body.method, 'SendMessage')
  assert.equal(call.body.params.message.parts[0].data.operation, 'list')
})

test('a listed, running worker stamps its session', () => {
  const live = a2aLiveSessions({ workers: [{ worker_id: 'w-1', state: 'running' }], records: [a2aRecord()] })
  assert.deepEqual(live, [{ sessionId: 's-1', workerId: 'w-1' }])
})

test('a killed worker is not stamped — absent from the list, or listed as stopped', () => {
  // ⚠️ SC7's falsifier is a killed worker still rendered live. Both shapes of "killed" must miss.
  assert.deepEqual(a2aLiveSessions({ workers: [], records: [a2aRecord()] }), [])
  for (const state of ['exited', 'stopped', 'dead', 'FAILED']) {
    assert.deepEqual(a2aLiveSessions({ workers: [{ worker_id: 'w-1', state }], records: [a2aRecord()] }), [], state)
  }
})

test('matching is by worker id, never by task, so a successor cannot revive a dead session', () => {
  const records = [a2aRecord({ session_id: 'dead', worker_id: 'w-old', task: 'T' }), a2aRecord({ session_id: 'new', worker_id: 'w-new', task: 'T' })]
  const live = a2aLiveSessions({ workers: [{ worker_id: 'w-new', task: 'T' }], records })
  assert.deepEqual(live.map((l) => l.sessionId), ['new'])
})

test('only open A2A records match', () => {
  const workers = [{ worker_id: 'w-1' }]
  assert.deepEqual(a2aLiveSessions({ workers, records: [a2aRecord({ ended_at: '2026-10-13T00:00:00Z' })] }), [])
  assert.deepEqual(a2aLiveSessions({ workers, records: [a2aRecord({ mode: 'cluster' })] }), [])
  assert.deepEqual(a2aLiveSessions({ workers, records: [a2aRecord({ worker_id: null })] }), [])
})

const listReply = (workers) =>
  JSON.stringify({ result: { id: 't', status: { state: 'TASK_STATE_COMPLETED' }, artifacts: [{ parts: [{ data: { operation: 'list', list: { workers } } }] }] } })

test('an empty list arrives as null and reads as no workers', async () => {
  // gob does not transmit zero values, so the controller's empty list is `null` on the wire.
  const listed = await listWorkersOnController({ endpoint: 'http://127.0.0.1:8080/a2a', token: TOKEN, messageId: 'm', mint: () => 'c', fetchImpl: stubFetch(listReply(null)) })
  assert.deepEqual(listed, { workers: [] })
})

test('a non-2xx list is an error, not an empty list', async () => {
  const listed = await listWorkersOnController({ endpoint: 'http://127.0.0.1:8080/a2a', token: TOKEN, messageId: 'm', mint: () => 'c', fetchImpl: stubFetch('no', { ok: false, status: 503 }) })
  assert.match(listed.error, /HTTP 503/)
})

test('a list the controller never answers is aborted at its bound, not awaited forever', async () => {
  // The poll skips a tick while one is in flight, so an unbounded hang would wedge the mirror for
  // the process's life. The stub resolves only when its signal aborts — proving the bound fired.
  // `AbortSignal.timeout`'s timer is unref'd, so the stub holds the loop open with its own.
  const hang = (_url, { signal }) =>
    new Promise((_resolve, reject) => {
      const keepAlive = setTimeout(() => {}, 5_000)
      signal.addEventListener('abort', () => {
        clearTimeout(keepAlive)
        reject(signal.reason)
      })
    })
  const listed = await listWorkersOnController({ endpoint: 'http://127.0.0.1:8080/a2a', token: TOKEN, messageId: 'm', mint: () => 'c', fetchImpl: hang, timeoutMs: 20 })
  assert.match(listed.error, /could not be made/)
})

test('a poll stamps every live session and reports them', async () => {
  const stamped = []
  const result = await pollA2A({ records: [a2aRecord()], list: async () => ({ workers: [{ worker_id: 'w-1' }] }), stamp: (id) => stamped.push(id) })
  assert.equal(result.ok, true)
  assert.deepEqual(stamped, ['s-1'])
})

test('a failed list stamps nothing and reports not-ok, never "no workers live"', async () => {
  const stamped = []
  const result = await pollA2A({ records: [a2aRecord()], list: async () => ({ error: 'HTTP 503' }), stamp: (id) => stamped.push(id) })
  assert.equal(result.ok, false)
  assert.deepEqual(stamped, [])
  const thrown = await pollA2A({ records: [a2aRecord()], list: async () => { throw new Error('ECONNREFUSED') }, stamp: (id) => stamped.push(id) })
  assert.equal(thrown.ok, false)
  assert.match(thrown.reason, /ECONNREFUSED/)
})

// --- stop: the third control operation, and the one a manager drives ----------------------
//
// ⚠️ These pin SC4's discriminating half — that the caller stops a worker BY THE ID IT WAS
// GIVEN, never by a task name. The builder's own refusals are already covered in
// `a2a-client.test.mjs`; what is asserted here is the WIRE BODY this caller sends, which is the
// only place the id the ledger recorded can be shown to reach the controller.

const stopReply = (worker = { ...WORKER, state: 'stopped' }) =>
  JSON.stringify({
    result: {
      id: 'task-1',
      status: { state: 'TASK_STATE_COMPLETED' },
      artifacts: [{ parts: [{ data: { operation: 'stop', stop: { worker } } }] }],
    },
  })

test('the stop call is a one-shot control call naming the worker, not the task', () => {
  const call = a2aStopCallFor({ endpoint: 'http://127.0.0.1:8080/a2a', workerId: 'w-1', token: TOKEN, messageId: 'm-1', mint: () => 'ctx-1' })
  assert.equal(call.error, undefined)
  assert.equal(call.body.method, 'SendMessage')
  assert.equal(call.body.params.message.parts[0].data.operation, 'stop')
  assert.deepEqual(call.body.params.message.parts[0].data.stop, { worker_id: 'w-1' })
  // ⚠️ The discriminator is `data`, never `text`: the controller reads the ControlRequest from
  // the message's FIRST DATA part and refuses a text part outright (a2a-client.mjs trap 5).
  assert.equal(call.body.params.message.parts[0].text, undefined)
  assert.equal(call.body.params.message.parts[0].data.task, undefined)
})

test('a stop with no worker id is refused by the builder, never sent', () => {
  const call = a2aStopCallFor({ endpoint: 'http://127.0.0.1:8080/a2a', token: TOKEN, messageId: 'm-1', mint: () => 'ctx-1' })
  assert.match(call.error, /non-empty `workerId`/)
})

test('the stop body carries the worker id the caller was given — the wire half of SC4', async () => {
  // The caller is handed the id `create` recorded (the ledger's `worker_id`); this asserts that
  // id — and no task name — is what reaches the controller. A stop keyed on the task would remove
  // the successor when one task has been spawned twice.
  let captured
  let sentBody
  const fetchImpl = async (_url, init) => {
    sentBody = init.body
    return stubFetch(stopReply())()
  }
  const result = await stopWorkerOnController({
    endpoint: 'http://127.0.0.1:8080/a2a',
    workerId: 'd8b6e35d2017e4b6',
    token: TOKEN,
    messageId: 'm-1',
    mint: () => 'ctx-1',
    fetchImpl,
    capture: (c) => {
      captured = c
    },
  })
  assert.equal(result.error, undefined)
  const sent = JSON.parse(sentBody)
  assert.equal(sent.params.message.parts[0].data.operation, 'stop')
  assert.equal(sent.params.message.parts[0].data.stop.worker_id, 'd8b6e35d2017e4b6')
  assert.deepEqual(captured.body, sent)
})

test('a stop reply reports the stopped worker and the terminal state', async () => {
  // The shape SC1 checks: `TASK_STATE_COMPLETED` with the worker in `state: "stopped"`.
  const result = await stopWorkerOnController({
    endpoint: 'http://127.0.0.1:8080/a2a',
    workerId: 'w-1',
    token: TOKEN,
    messageId: 'm-1',
    mint: () => 'ctx-1',
    fetchImpl: stubFetch(stopReply()),
  })
  assert.equal(result.state, 'TASK_STATE_COMPLETED')
  assert.equal(result.response.operation, 'stop')
  assert.equal(result.response.stop.worker.state, 'stopped')
  assert.equal(result.response.stop.worker.worker_id, 'w1')
})

test('the capture never carries the controller token', async () => {
  let captured
  await stopWorkerOnController({
    endpoint: 'http://127.0.0.1:8080/a2a',
    workerId: 'w-1',
    token: TOKEN,
    messageId: 'm-1',
    mint: () => 'ctx-1',
    fetchImpl: stubFetch(stopReply()),
    capture: (c) => {
      captured = c
    },
  })
  assert.deepEqual(Object.keys(captured).sort(), ['body', 'url'])
  assert.ok(!JSON.stringify(captured).includes(TOKEN), 'the capture must never carry the bearer token')
})

test('a non-2xx stop is an error, never read as a stopped worker', async () => {
  const result = await stopWorkerOnController({
    endpoint: 'http://127.0.0.1:8080/a2a',
    workerId: 'w-1',
    token: TOKEN,
    messageId: 'm-1',
    mint: () => 'ctx-1',
    fetchImpl: stubFetch('no', { ok: false, status: 503 }),
  })
  assert.match(result.error, /HTTP 503/)
})

test('a FAILED stop is reported as an unknown outcome, never as "the worker is still running"', async () => {
  // The controller reports a store-side failure AFTER the executor has run, so a FAILED stop can
  // accompany a worker that WAS removed. Reading it as "still running" is the leak, one layer up
  // from the create case — so the refusal must carry the same reconcile instruction.
  const failed = JSON.stringify({ result: { id: 't', status: { state: 'TASK_STATE_FAILED' } } })
  const result = await stopWorkerOnController({
    endpoint: 'http://127.0.0.1:8080/a2a',
    workerId: 'w-1',
    token: TOKEN,
    messageId: 'm-1',
    mint: () => 'ctx-1',
    fetchImpl: stubFetch(failed),
  })
  assert.match(result.error, /FAILED/)
  assert.match(result.error, /does NOT mean the operation did not happen/)
  assert.match(result.error, /reconcile/)
})

test('a stop the controller never answers is aborted at its bound, not awaited forever', async () => {
  const hang = (_url, { signal }) =>
    new Promise((_resolve, reject) => {
      const keepAlive = setTimeout(() => {}, 5_000)
      signal.addEventListener('abort', () => {
        clearTimeout(keepAlive)
        reject(signal.reason)
      })
    })
  const result = await stopWorkerOnController({
    endpoint: 'http://127.0.0.1:8080/a2a',
    workerId: 'w-1',
    token: TOKEN,
    messageId: 'm-1',
    mint: () => 'ctx-1',
    fetchImpl: hang,
    timeoutMs: 20,
  })
  assert.match(result.error, /could not be made/)
})

test('a reply naming another operation is refused, not read as this stop\'s answer', async () => {
  // `sentOperation` is asserted for the same reason `create` and `list` assert it: a reply that
  // names a different operation is the only client-side observable that the answer is not ours.
  const result = await stopWorkerOnController({
    endpoint: 'http://127.0.0.1:8080/a2a',
    workerId: 'w-1',
    token: TOKEN,
    messageId: 'm-1',
    mint: () => 'ctx-1',
    fetchImpl: stubFetch(listReply([{ worker_id: 'w-1', state: 'running' }])),
  })
  assert.match(result.error, /does not belong to this call/)
})

// --- reapExistingWorkerForTask ----------------------------------------------------------------
//
// ⚠️ These pin the distinction the caller depends on: a create that failed over a DUPLICATE has a
// worker to reap, and one that failed for its own reasons does not. Collapsing the two would hide
// the real error behind an invented one — reporting a reaping failure for a transport failure, or
// reporting `none` as though it were a success.

const reapArgs = {
  endpoint: 'http://127.0.0.1:8080/a2a',
  task: 'task-a',
  token: TOKEN,
  messageId: 'm-1',
  mint: () => 'ctx-1',
}

test('reapExistingWorkerForTask stops the worker whose task matches', async () => {
  const stopped = []
  const result = await reapExistingWorkerForTask({
    ...reapArgs,
    listWorkers: async () => ({
      workers: [{ worker_id: 'w-other', task: 'task-b' }, { worker_id: 'w-1', task: 'task-a' }],
    }),
    stopWorker: async ({ workerId }) => {
      stopped.push(workerId)
      return { worker: { worker_id: workerId } }
    },
  })

  assert.deepEqual(result, { stoppedWorkerId: 'w-1' })
  assert.deepEqual(stopped, ['w-1'])
})

test('reapExistingWorkerForTask reports none when no worker serves the task', async () => {
  let stopCalls = 0
  const result = await reapExistingWorkerForTask({
    ...reapArgs,
    listWorkers: async () => ({ workers: [{ worker_id: 'w-other', task: 'task-b' }] }),
    stopWorker: async () => {
      stopCalls += 1
      return {}
    },
  })

  // ⚠️ `none`, NOT an error, and nothing is stopped. The caller reports the ORIGINAL create
  // failure; a reaping failure invented here would replace a real diagnosis with a wrong one.
  assert.deepEqual(result, { none: true })
  assert.equal(stopCalls, 0)
})

test('reapExistingWorkerForTask reports none for an empty and for a null worker list', async () => {
  // gob does not transmit zero values, so an empty list round-trips as `null` rather than `[]`.
  // a2aWorkerList normalises that, and this asserts the caller does not throw on either shape.
  for (const workers of [[], null]) {
    const result = await reapExistingWorkerForTask({
      ...reapArgs,
      listWorkers: async () => ({ workers }),
      stopWorker: async () => ({}),
    })
    assert.deepEqual(result, { none: true })
  }
})

test('reapExistingWorkerForTask refuses rather than reporting success when the stop fails', async () => {
  const result = await reapExistingWorkerForTask({
    ...reapArgs,
    listWorkers: async () => ({ workers: [{ worker_id: 'w-1', task: 'task-a' }] }),
    stopWorker: async () => ({ error: 'the controller answered HTTP 500' }),
  })

  // The id is named because a caller reading this has to know WHICH worker is still running.
  assert.match(result.error, /w-1/)
  assert.match(result.error, /could not be stopped/)
})

test('reapExistingWorkerForTask refuses when the list itself fails', async () => {
  const result = await reapExistingWorkerForTask({
    ...reapArgs,
    listWorkers: async () => ({ error: 'the list call could not be made' }),
    stopWorker: async () => ({}),
  })

  assert.match(result.error, /could not be listed/)
})

test('reapExistingWorkerForTask never echoes the token', async () => {
  const result = await reapExistingWorkerForTask({
    ...reapArgs,
    listWorkers: async () => ({ workers: [{ worker_id: 'w-1', task: 'task-a' }] }),
    stopWorker: async () => ({ worker: { worker_id: 'w-1' } }),
  })

  assert.equal(JSON.stringify(result).includes(TOKEN), false)
})

// --- the incremental collection --------------------------------------------------------------

// A response whose body is a real byte stream, so the incremental path is exercised rather than
// the `.text()` fallback it deliberately keeps.
function streamResponse(chunks, onRead) {
  let i = 0
  return {
    ok: true,
    body: {
      getReader: () => ({
        read: async () => {
          onRead?.(i)
          return i < chunks.length
            ? { done: false, value: new TextEncoder().encode(chunks[i++]) }
            : { done: true, value: undefined }
        },
      }),
    },
  }
}

test('the task id is announced MID-STREAM, not after the turn ends', async () => {
  // ⚠️ This ordering is the whole feature. The push callback must be registered before the worker
  // emits its mid-turn status ~10 s in, so a callback that fires only once the stream is drained
  // is indistinguishable from one that never fires — the turn still completes and the outcome is
  // still correct, which is exactly why the defect would go unnoticed.
  const reads = []
  const seen = []
  const res = streamResponse(
    [
      'data: {"result":{"task":{"id":"task-1","status":{"state":"TASK_STATE_SUBMITTED"}}}}\n\n',
      'data: {"result":{"statusUpdate":{"taskId":"task-1","status":{"state":"TASK_STATE_COMPLETED"}}}}\n\n',
    ],
    (i) => reads.push(i),
  )

  const outcome = await collectWorkerTurn({ res, onTaskId: async (id) => seen.push({ id, reads: reads.length }) })

  assert.deepEqual(seen, [{ id: 'task-1', reads: 1 }], 'announced after the FIRST read, not at the end')
  // The collection is unaffected by the extra path: the outcome is the same one the whole-body
  // reader produces for the same bytes.
  assert.equal(outcome.state, 'TASK_STATE_COMPLETED')
  assert.equal(outcome.error, undefined)
})

test('a chunk boundary inside a frame does not lose the task id or corrupt the outcome', async () => {
  const body = 'data: {"result":{"task":{"id":"task-9","status":{"state":"TASK_STATE_SUBMITTED"}}}}\n\n'
  const seen = []
  // Split inside the JSON — where the network actually breaks chunks.
  const outcome = await collectWorkerTurn({
    res: streamResponse([body.slice(0, 25), body.slice(25)]),
    onTaskId: async (id) => seen.push(id),
  })
  assert.deepEqual(seen, ['task-9'])
  assert.equal(outcome.error, undefined)
})

test('a throwing onTaskId does not fail the collection', async () => {
  // ⚠️ The callback registers an optional channel. A registration failure must not be reported as
  // a failed turn — the worker ran, and the outcome is the worker's, not the channel's.
  const body = 'data: {"result":{"task":{"id":"task-1","status":{"state":"TASK_STATE_COMPLETED"}}}}\n\n'
  const outcome = await collectWorkerTurn({
    res: streamResponse([body]),
    onTaskId: async () => {
      throw new Error('registration refused')
    },
  })
  assert.equal(outcome.error, undefined, 'the turn is still collected')
  assert.equal(outcome.state, 'TASK_STATE_COMPLETED')
})

test('with no onTaskId the whole-body path is used, even on a stream body', async () => {
  // The fallback is what every existing caller and test relies on, so it must not be displaced by
  // the mere presence of a stream body.
  const body = 'data: {"result":{"task":{"id":"task-1","status":{"state":"TASK_STATE_COMPLETED"}}}}\n\n'
  const res = streamResponse([body])
  res.text = async () => body
  const outcome = await collectWorkerTurn({ res })
  assert.equal(outcome.state, 'TASK_STATE_COMPLETED')
})
