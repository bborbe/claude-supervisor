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
  A2A_STREAM_METHOD,
  a2aContextIdFor,
  a2aFrames,
  a2aOutcome,
  a2aStreamRequestFor,
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
