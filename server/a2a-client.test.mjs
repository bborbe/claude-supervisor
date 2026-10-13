// Tests for the supervisor's A2A client.
//
// Every case here pins a trap that was MEASURED on the real wire rather than reasoned about,
// because each one produced a request that looked correct and was not. A test that only asserted
// the happy path would pass against every one of the defects this module exists to prevent, so
// the refusals are asserted as hard as the successes.

import { test } from 'node:test'
import assert from 'node:assert/strict'
import {
  A2A_AGENT_CARD_PATH,
  A2A_CONTROL_OPERATIONS,
  A2A_SEND_METHOD,
  A2A_STREAM_METHOD,
  a2aContextIdFor,
  a2aControlRequestFor,
  a2aControlResponseFor,
  a2aDataPart,
  a2aFrames,
  a2aOutcome,
  a2aSendRequestFor,
  a2aStreamRequestFor,
  a2aWorkerList,
  resolveA2AEndpoint,
} from './a2a-client.mjs'

const CARD = {
  name: 'claude-interactive-local',
  supportedInterfaces: [{ url: 'http://localhost:9091/a2a', protocolBinding: 'JSONRPC' }],
}

const REQUEST = {
  endpoint: 'http://localhost:9091/a2a',
  contextId: 'ctx-1',
  text: 'say hello',
  token: 'not-a-real-token',
  messageId: 'msg-1',
}

const sse = (...results) => results.map((r) => `data: ${JSON.stringify({ jsonrpc: '2.0', id: 1, result: r })}`).join('\n\n') + '\n\n'

// --- the card's endpoint ---------------------------------------------------------------------

test('the endpoint is read from supportedInterfaces[0].url', () => {
  assert.equal(A2A_AGENT_CARD_PATH, '/.well-known/agent-card.json')
  assert.deepEqual(resolveA2AEndpoint(CARD), { url: 'http://localhost:9091/a2a' })
})

test('a card with no supportedInterfaces refuses rather than reading a top-level url', () => {
  // ⚠️ The v2 SDK's AgentCard carries NO top-level `url` field, so a card shaped the obvious way
  // is exactly what this refusal is for. A fallback would send every turn to `undefined`.
  const { error } = resolveA2AEndpoint({ name: 'x', url: 'http://wrong.example/a2a' })
  assert.match(error, /no `supportedInterfaces` array/)
  assert.deepEqual(resolveA2AEndpoint({ supportedInterfaces: [] }).error !== undefined, true)
  assert.deepEqual(resolveA2AEndpoint({}).error !== undefined, true)
  assert.deepEqual(resolveA2AEndpoint(undefined).error !== undefined, true)
})

test('an interface carrying no url refuses and names the top-level-url trap', () => {
  const { error } = resolveA2AEndpoint({ supportedInterfaces: [{ protocolBinding: 'JSONRPC' }] })
  assert.match(error, /carries no `url`/)
  assert.match(error, /top-level `url`/)
})

// --- the contextId ---------------------------------------------------------------------------

test('an existing contextId is reused unchanged', () => {
  assert.deepEqual(a2aContextIdFor({ previous: 'ctx-1' }), { contextId: 'ctx-1' })
})

test('a first turn mints a contextId rather than omitting it', () => {
  // ⚠️ Omitting it on the first call runs the turn under the server's DEFAULT session, and the
  // follow-up then opens a different conversation — the worker correctly reports no memory of
  // turn 1, and nothing on the client side reads as wrong.
  assert.deepEqual(a2aContextIdFor({ previous: '', mint: () => 'ctx-new' }), { contextId: 'ctx-new' })
  assert.deepEqual(a2aContextIdFor({ mint: () => 'ctx-new' }), { contextId: 'ctx-new' })
  assert.deepEqual(a2aContextIdFor({ previous: undefined, mint: () => 'ctx-new' }), { contextId: 'ctx-new' })
})

test('a first turn with no way to mint refuses rather than sending contextless', () => {
  const { error } = a2aContextIdFor({})
  assert.match(error, /refusing to send a first turn without one/)
  assert.deepEqual(a2aContextIdFor({ mint: () => '' }).error !== undefined, true)
  assert.deepEqual(a2aContextIdFor({ mint: () => 42 }).error !== undefined, true)
})

// --- the request -----------------------------------------------------------------------------

test('the request carries the bare method, the ROLE_USER enum, and a FLATTENED part', () => {
  const { url, headers, body } = a2aStreamRequestFor(REQUEST)
  assert.equal(url, 'http://localhost:9091/a2a')
  assert.equal(headers.authorization, 'Bearer not-a-real-token')
  assert.equal(headers.accept, 'text/event-stream')
  // ⚠️ Bare, not `message/stream`: the slash spelling answers `-32601 method not found`.
  assert.equal(body.method, A2A_STREAM_METHOD)
  assert.equal(body.method, 'SendStreamingMessage')
  assert.equal(body.params.message.role, 'ROLE_USER')
  assert.equal(body.params.message.contextId, 'ctx-1')
  assert.equal(body.params.message.messageId, 'msg-1')
  // ⚠️ `{ text }` and never `{ content: … }` — a `content` part is refused by the service with
  // `-32700 parse error: unknown part content type`, INSIDE a well-formed HTTP 200 SSE envelope.
  assert.deepEqual(body.params.message.parts, [{ text: 'say hello' }])
  assert.equal('content' in body.params.message.parts[0], false)
})

test('every field the service validates is required, and a missing one refuses', () => {
  // The SDK rejects a nil message, an empty parts array, and an empty message id or role with
  // ErrInvalidParams BEFORE the executor runs, so a missing field fails the turn for a reason
  // that never reaches the agent.
  for (const field of ['endpoint', 'contextId', 'text', 'token', 'messageId']) {
    const { error } = a2aStreamRequestFor({ ...REQUEST, [field]: '' })
    assert.match(error, new RegExp(`\`${field}\` is required`), `${field} should be required`)
  }
})

// --- the frames ------------------------------------------------------------------------------

test('frames are parsed from the data lines and non-data lines are ignored', () => {
  const body = 'event: message\n' + sse({ task: { id: 't-1', contextId: 'ctx-1' } }) + 'data: [DONE]\n\n'
  const { frames } = a2aFrames(body)
  assert.equal(frames.length, 1)
  assert.equal(frames[0].task.id, 't-1')
})

test('an error frame is FATAL rather than returned as data', () => {
  // ⚠️ The measured defect this guards: a refused request arrives inside a well-formed
  // `HTTP 200 text/event-stream` envelope, so every transport check passes while nothing ran.
  const body = sse({ task: { id: 't-1' } }) + `data: ${JSON.stringify({ jsonrpc: '2.0', id: 1, error: { code: -32700, message: 'parse error: unknown part content type: [content]' } })}\n\n`
  const { error, frames } = a2aFrames(body)
  assert.equal(frames, undefined)
  assert.match(error, /refused the turn/)
  assert.match(error, /unknown part content type/)
})

test('an unparseable frame refuses rather than being skipped', () => {
  const { error } = a2aFrames('data: {not json}\n\n')
  assert.match(error, /is not JSON/)
})

// --- the outcome -----------------------------------------------------------------------------

test('the outcome carries the state, the contextId and the reply text', () => {
  const body = sse(
    { task: { id: 't-1', contextId: 'ctx-1', status: { state: 'TASK_STATE_SUBMITTED' } } },
    { artifactUpdate: { parts: [{ text: 'stream-probe-ok' }] } },
    { statusUpdate: { state: 'TASK_STATE_COMPLETED' } },
  )
  const { frames } = a2aFrames(body)
  assert.deepEqual(a2aOutcome(frames, { sentContextId: 'ctx-1' }), {
    state: 'TASK_STATE_COMPLETED',
    contextId: 'ctx-1',
    reply: 'stream-probe-ok',
  })
})

test('a contextId mismatch is the error, because it is the only client-side observable', () => {
  // ⚠️ A turn that ran under the server's default session returns a contextId just as happily as
  // one that ran where it was asked to, so nothing else on this side reads as wrong.
  const { frames } = a2aFrames(sse({ task: { id: 't-1', contextId: 'identity', status: { state: 'TASK_STATE_COMPLETED' } } }))
  const { error } = a2aOutcome(frames, { sentContextId: 'ctx-1' })
  assert.match(error, /ran under contextId "identity"/)
  assert.match(error, /"ctx-1"/)
})

test('a turn with no frames refuses rather than reporting an empty success', () => {
  assert.match(a2aOutcome([], { sentContextId: 'ctx-1' }).error, /no frames/)
  assert.match(a2aOutcome(undefined, {}).error, /no frames/)
})

// --- the controller-facing half: the control request -----------------------------------------

test('each operation builds its own ControlRequest, and exactly one field', () => {
  assert.deepEqual(a2aControlRequestFor({ operation: 'list' }), { request: { operation: 'list' } })
  assert.deepEqual(a2aControlRequestFor({ operation: 'create', task: 'Some Task' }), {
    request: { operation: 'create', create: { task: 'Some Task' } },
  })
  assert.deepEqual(a2aControlRequestFor({ operation: 'stop', workerId: 'w-1' }), {
    request: { operation: 'stop', stop: { worker_id: 'w-1' } },
  })
  // The controller refuses a request setting none or more than one operation-specific field
  // rather than guessing, so a builder that dropped a stray field would silently ask for a
  // DIFFERENT operation than the caller named.
  for (const args of [
    { operation: 'create' },
    { operation: 'create', task: '' },
    { operation: 'create', task: 'T', workerId: 'w-1' },
    { operation: 'list', task: 'T' },
    { operation: 'list', workerId: 'w-1' },
    { operation: 'stop' },
    { operation: 'stop', workerId: '', },
    { operation: 'stop', workerId: 'w-1', task: 'T' },
    { operation: 'delete', task: 'T' },
    { operation: undefined },
  ]) {
    assert.equal(a2aControlRequestFor(args).error !== undefined, true, JSON.stringify(args))
  }
  assert.deepEqual(A2A_CONTROL_OPERATIONS, ['create', 'list', 'stop'])
})

// --- the controller-facing half: the data part -----------------------------------------------

test('a data part is FLATTENED as { data }, never { content } and never { text }', () => {
  // ⚠️ THE CONTROLLER'S OWN WIRE RULE. `controlRequestFromMessage` reads the ControlRequest from
  // the message's FIRST data part and refuses a message carrying none, so a TEXT part — the shape
  // the worker half sends — is not a near miss: the controller answers ErrInvalidParams and no
  // operation runs at all.
  assert.deepEqual(a2aDataPart({ operation: 'list' }), { part: { data: { operation: 'list' } } })
  assert.equal('content' in a2aDataPart({ operation: 'list' }).part, false)
  assert.equal('text' in a2aDataPart({ operation: 'list' }).part, false)
})

test('a null or non-object data payload refuses, because null hydrates to no discriminator', () => {
  // ⚠️ The SDK hydrates `data` into a `*any`, so a JSON `null` leaves that pointer nil and counts
  // as NO discriminator at all — refused as `unknown part content type`, exactly as a missing part
  // is. It fails quietly enough to be worth refusing here instead.
  for (const value of [null, undefined, 'list', 42, ['list'], true]) {
    const { error, part } = a2aDataPart(value)
    assert.equal(part, undefined, JSON.stringify(value))
    assert.match(error, /non-null object/, JSON.stringify(value))
  }
})

// --- the controller-facing half: the call ----------------------------------------------------

test('the control call is ONE-SHOT — bare SendMessage, a data part, and no SSE accept', () => {
  const { url, headers, body } = a2aSendRequestFor({
    endpoint: 'http://localhost:8080/a2a',
    contextId: 'ctx-1',
    request: { operation: 'list' },
    token: 'not-a-real-token',
    messageId: 'msg-1',
  })
  assert.equal(url, 'http://localhost:8080/a2a')
  assert.equal(headers.authorization, 'Bearer not-a-real-token')
  assert.equal(body.method, A2A_SEND_METHOD)
  // ⚠️ Bare, and NOT the streaming spelling: a control call is one request and response with no
  // live turn to interrupt, so a client that opened a stream would wait on frames never sent.
  assert.equal(body.method, 'SendMessage')
  assert.notEqual(body.method, A2A_STREAM_METHOD)
  assert.equal(headers.accept, undefined)
  assert.equal(body.params.message.role, 'ROLE_USER')
  assert.equal(body.params.message.contextId, 'ctx-1')
  assert.deepEqual(body.params.message.parts, [{ data: { operation: 'list' } }])
})

test('every field the controller validates is required, and a missing one refuses', () => {
  const CALL = {
    endpoint: 'http://localhost:8080/a2a',
    contextId: 'ctx-1',
    request: { operation: 'list' },
    token: 'not-a-real-token',
    messageId: 'msg-1',
  }
  for (const field of ['endpoint', 'contextId', 'token', 'messageId']) {
    const { error } = a2aSendRequestFor({ ...CALL, [field]: '' })
    assert.match(error, new RegExp(`\`${field}\` is required`), `${field} should be required`)
  }
  assert.match(a2aSendRequestFor({ ...CALL, request: null }).error, /non-null object/)
})

// --- the controller-facing half: the reply ---------------------------------------------------

const CONTROL_REPLY = (response) => ({
  id: 't-1',
  contextId: 'ctx-1',
  status: { state: 'TASK_STATE_COMPLETED' },
  artifacts: [{ artifactId: 'a-1', parts: [{ data: response }] }],
})

test('the ControlResponse is read from the artifact data part, in the one-shot JSON framing', () => {
  const body = JSON.stringify({
    jsonrpc: '2.0',
    id: 1,
    result: CONTROL_REPLY({ operation: 'list', list: { workers: [] } }),
  })
  assert.deepEqual(a2aControlResponseFor(body, { sentOperation: 'list' }), {
    response: { operation: 'list', list: { workers: [] } },
    state: 'TASK_STATE_COMPLETED',
  })
})

test('the same reply parses from the streaming framing, because one handler serves both', () => {
  const body = sse(
    { task: { id: 't-1', contextId: 'ctx-1', status: { state: 'TASK_STATE_SUBMITTED' } } },
    { artifactUpdate: { artifact: { artifactId: 'a-1', parts: [{ data: { operation: 'create', create: { worker: { worker_id: 'w-1' }, token: 'not-a-real-token' } } }] } } },
    { statusUpdate: { state: 'TASK_STATE_COMPLETED' } },
  )
  const { response, state } = a2aControlResponseFor(body, { sentOperation: 'create' })
  assert.equal(state, 'TASK_STATE_COMPLETED')
  assert.equal(response.create.worker.worker_id, 'w-1')
})

test('a one-shot reply wrapped as SendMessageResponse { task } is unwrapped', () => {
  // The shape a2a-go v2 actually sends (measured against a2a-worker-controller v0.4.4). Reading
  // `result` as the task found no status and no artifact, and orphaned a worker it had created.
  const body = JSON.stringify({
    jsonrpc: '2.0',
    id: 1,
    result: { task: { id: 't-1', contextId: 'ctx-1', status: { state: 'TASK_STATE_COMPLETED' }, artifacts: [{ artifactId: 'a-1', parts: [{ data: { operation: 'create', create: { worker: { worker_id: 'w-1' }, token: 'not-a-real-token' } } }] }] } },
  })
  const { response, state } = a2aControlResponseFor(body, { sentOperation: 'create' })
  assert.equal(state, 'TASK_STATE_COMPLETED')
  assert.equal(response.create.worker.worker_id, 'w-1')
})

test('a FAILED status is the error, and it is read BEFORE the missing payload', () => {
  // ⚠️ A backend failure yields TASK_STATE_FAILED with NO artifact, and the controller
  // deliberately withholds the backend's error text. A client that hunted for the data part first
  // would find none and have to invent a reason, while the status states the real one.
  const body = JSON.stringify({
    jsonrpc: '2.0',
    id: 1,
    result: { id: 't-1', contextId: 'ctx-1', status: { state: 'TASK_STATE_FAILED' } },
  })
  const { error } = a2aControlResponseFor(body, { sentOperation: 'create' })
  assert.match(error, /reported the control call FAILED/)
  assert.match(error, /never sent/)
})

test('a completed call with no data part refuses, naming the text-part trap', () => {
  const body = JSON.stringify({
    jsonrpc: '2.0',
    id: 1,
    result: {
      id: 't-1',
      contextId: 'ctx-1',
      status: { state: 'TASK_STATE_COMPLETED' },
      artifacts: [{ artifactId: 'a-1', parts: [{ text: 'ok' }] }],
    },
  })
  const { error } = a2aControlResponseFor(body, { sentOperation: 'list' })
  assert.match(error, /no data part/)
  assert.match(error, /not a near miss/)
})

test('a reply naming a different operation refuses — the only client-side observable', () => {
  // ⚠️ Exactly as `sentContextId` is in `a2aOutcome`: nothing else on this side reads as wrong.
  const body = JSON.stringify({
    jsonrpc: '2.0',
    id: 1,
    result: CONTROL_REPLY({ operation: 'stop', stop: { worker: { worker_id: 'w-1' } } }),
  })
  const { error } = a2aControlResponseFor(body, { sentOperation: 'list' })
  assert.match(error, /asked for "list"/)
  assert.match(error, /"stop"/)
})

test('a JSON-RPC error is refused as a refusal, not read as a FAILED task', () => {
  const body = JSON.stringify({ jsonrpc: '2.0', id: 1, error: { code: -32602, message: 'invalid params' } })
  const { error } = a2aControlResponseFor(body, { sentOperation: 'list' })
  assert.match(error, /refused the control call/)
  assert.match(error, /never reached a backend/)
})

test('a reply that is neither SSE nor JSON refuses rather than being skipped', () => {
  assert.match(a2aControlResponseFor('<html>502</html>', { sentOperation: 'list' }).error, /neither SSE nor JSON/)
  assert.match(a2aControlResponseFor(undefined, {}).error, /must be a string/)
})

test('a FAILED status is an UNKNOWN outcome, never "nothing happened"', () => {
  // ⚠️ The failure can land in the controller's task store AFTER the executor has run — measured
  // 2026-10-13 on the controller's own v0.4.0, where every call including `list` answered FAILED
  // because the store's gob copy of the artifact was missing a type registration. A `create` on
  // that build could have started a container and still reported FAILED, so a caller that retried
  // on "it did not happen" would be acting on a claim the controller never made.
  const body = JSON.stringify({
    jsonrpc: '2.0',
    id: 1,
    result: { id: 't-1', contextId: 'ctx-1', status: { state: 'TASK_STATE_FAILED' } },
  })
  const { error } = a2aControlResponseFor(body, { sentOperation: 'create' })
  assert.match(error, /UNKNOWN outcome/)
  assert.match(error, /AFTER the executor has run/)
  assert.doesNotMatch(error, /it means the operation did not happen/)
})

// --- the controller-facing half: the list result ---------------------------------------------

test('an empty worker list is normalised from the null gob leaves behind', () => {
  // ⚠️ gob does not transmit zero values, so the controller's store collapses an empty slice into
  // `null` — the EMPTY case is the one that arrives as `null`, and it is the most common case at
  // the start of a run. Iterating it without a guard throws.
  assert.deepEqual(a2aWorkerList({ operation: 'list', list: { workers: null } }), { workers: [] })
  assert.deepEqual(a2aWorkerList({ operation: 'list', list: { workers: [] } }), { workers: [] })
  assert.deepEqual(a2aWorkerList({ operation: 'list', list: {} }), { workers: [] })
  assert.deepEqual(a2aWorkerList({ operation: 'list' }), { workers: [] })
})

test('a populated worker list passes through unchanged', () => {
  const workers = [{ worker_id: 'w-1', task: 'T', a2a_url: 'http://localhost:9091/a2a', backend: 'docker', state: 'running' }]
  assert.deepEqual(a2aWorkerList({ operation: 'list', list: { workers } }), { workers })
})

test('a list result that is not a list refuses rather than being read as empty', () => {
  // A `create` response read as a list would otherwise look like "no workers" — an empty answer
  // invented from a reply that says something else entirely.
  assert.match(a2aWorkerList({ operation: 'create', create: {} }).error, /expected a `list` response/)
  assert.match(a2aWorkerList({ operation: 'list', list: { workers: 'w-1' } }).error, /must be an array or null/)
  assert.match(a2aWorkerList('raw body').error, /`ControlResponse` object/)
  assert.match(a2aWorkerList(undefined).error, /`ControlResponse` object/)
})
