// Tests for where a follow-up to a worker goes.
//
// The thing worth pinning is the SHAPE of the refusal, because a wrong branch here does
// not error — it silently types into nothing, or posts a prompt at a conversation that is
// not the one the caller named. The cluster case is the new one: it is the first worker
// kind `send_agent_message` can reach without a pane, and the trap is that a cluster
// record carries `paneId: null` by construction, so a check written as "has a pane?"
// refuses every cluster worker while reading as a general guard.

import { test } from 'node:test'
import assert from 'node:assert/strict'
import { resolveMessageChannel } from './message-channel.mjs'

const agent = (over = {}) => ({ id: 'agent_7', status: 'interactive', paneId: '1714', sessionId: '3f2a91c4', ...over })

test('a tab worker resolves to its pane', () => {
  assert.deepEqual(resolveMessageChannel(agent()), { channel: 'pane', paneId: '1714' })
})

test('a cluster worker resolves to its session, and its null pane is not a refusal', () => {
  // The whole point of the module: `paneId: null` is what a cluster record always carries,
  // so a guard phrased as "no pane ⇒ refuse" would reject every cluster worker.
  assert.deepEqual(
    resolveMessageChannel(agent({ status: 'cluster', paneId: null, sessionId: '6bc052b0-c236-4ed2-a826-c9c58eae36c1' })),
    { channel: 'cluster', sessionId: '6bc052b0-c236-4ed2-a826-c9c58eae36c1' },
  )
})

test('a cluster worker with no session id is refused rather than addressed blindly', () => {
  // A cluster follow-up is addressed BY session id. With none there is no conversation to
  // address, and the service resolves a header-less POST to its shared `identity`
  // conversation — a wrong conversation that looks like a successful send.
  const r = resolveMessageChannel(agent({ status: 'cluster', paneId: null, sessionId: null }))
  assert.ok(r.error, 'expected a refusal')
  assert.match(r.error, /session id/)
})

// The registry join is injected, so these cases pin the BRANCH rather than a directory read.
const names = (list) => () => list

test('a headless worker resolves to its registry name — the peer channel is the caller\'s to send', () => {
  // ⚠️ This is the case that used to be refused with "reach it mid-task with SendMessage",
  // which the caller could not do: `SendMessage` takes a NAME and the caller holds an ID.
  // Measured 2026-11-08 — the worker was live and registered as `claude-1f`.
  const r = resolveMessageChannel(agent({ status: 'running', paneId: null, sessionId: '6d16758d' }), {
    peerNamesFor: names(['claude-1f']),
  })
  assert.deepEqual(r, { channel: 'peer', name: 'claude-1f' })
})

test('a headless worker with no registry entry is refused, and the refusal says the session exited', () => {
  // SC5: the fix widens the SUCCESS path only. A session id nothing registers under is a
  // worker no `SendMessage` can address, and the refusal must say why rather than name a
  // channel that would fail at the caller.
  const r = resolveMessageChannel(agent({ status: 'running', paneId: null, sessionId: 'gone' }), {
    peerNamesFor: names([]),
  })
  assert.ok(r.error, 'expected a refusal')
  assert.match(r.error, /session exited/)
  assert.doesNotMatch(r.error, /reach it mid-task with SendMessage/)
})

test('a headless worker registered under two names is refused rather than guessed at', () => {
  // Picking either would deliver into the wrong conversation, which reads as success.
  const r = resolveMessageChannel(agent({ status: 'running', paneId: null, sessionId: 'x' }), {
    peerNamesFor: names(['claude-1f', 'claude-2a']),
  })
  assert.match(r.error, /claude-1f/)
  assert.match(r.error, /claude-2a/)
  assert.match(r.error, /wrong conversation/)
})

test('an unreadable registry is retryable, not a claim that the worker is gone', () => {
  // null is "no information"; folding it into "no entry" would report a live worker as
  // exited and send the caller looking for a replacement.
  const r = resolveMessageChannel(agent({ status: 'running', paneId: null, sessionId: 'x' }), {
    peerNamesFor: names(null),
  })
  assert.match(r.error, /could not be read/)
  assert.doesNotMatch(r.error, /session exited/)
})

test('every headless refusal names a remedy the caller can actually perform', () => {
  // SC2: the old text offered `SendMessage` with no address, which fails for the id the
  // caller holds. Every headless refusal must instead carry a name the caller can pass, or
  // name the parked-prompt channel — never a bare channel name with nothing to address.
  const cases = [
    names([]),
    names(null),
    names(['claude-1f', 'claude-2a']),
  ]
  for (const peerNamesFor of cases) {
    const { error } = resolveMessageChannel(agent({ status: 'running', paneId: null, sessionId: 'x' }), { peerNamesFor })
    assert.ok(error, 'expected a refusal')
    assert.match(error, /answer_permission|retry|list_agents/, `no performable remedy in: ${error}`)
    assert.doesNotMatch(error, /reach it mid-task with SendMessage/, `unaddressable channel in: ${error}`)
  }
})

test('an interactive row with no pane is refused — the tab is gone, not the worker kind', () => {
  const r = resolveMessageChannel(agent({ paneId: null }))
  assert.ok(r.error, 'expected a refusal')
  assert.match(r.error, /pane/)
})

test('the refusal names the agent, so a manager with several workers knows which one', () => {
  const r = resolveMessageChannel(agent({ id: 'agent_12', status: 'running', paneId: null }))
  assert.match(r.error, /agent_12/)
})

test('a missing agent is refused rather than throwing', () => {
  assert.ok(resolveMessageChannel(undefined).error)
  assert.ok(resolveMessageChannel(null).error)
})

test('an A2A worker resolves to its own endpoint, token and conversation', () => {
  const a = agent({ status: 'docker', paneId: null, a2aUrl: 'http://localhost:9093/a2a', workerToken: 'not-a-real-token', contextId: 'ctx-1' })
  assert.deepEqual(resolveMessageChannel(a), { channel: 'a2a', endpoint: 'http://localhost:9093/a2a', token: 'not-a-real-token', contextId: 'ctx-1' })
})

test('the channel carries the protocol version the worker advertised, so a follow-up speaks it', () => {
  // ⚠️ A follow-up is a fresh request to the same worker, and the worker's version gate does not
  // remember the first turn — so the version must travel on the channel, not be re-read or dropped.
  const a = agent({
    status: 'docker',
    paneId: null,
    a2aUrl: 'http://localhost:9093/a2a',
    workerToken: 'not-a-real-token',
    contextId: 'ctx-1',
    protocolVersion: '1.0',
  })
  assert.equal(resolveMessageChannel(a).protocolVersion, '1.0')
})

test('a re-adopted A2A row holds no token and is refused, never sent unauthenticated', () => {
  const { error } = resolveMessageChannel(agent({ status: 'k8s', paneId: null, a2aUrl: 'http://w/a2a', workerToken: undefined, contextId: 'ctx-1' }))
  assert.match(error, /does not hold/)
})

test('an A2A worker whose first turn was never sent has no conversation to continue', () => {
  const { error } = resolveMessageChannel(agent({ status: 'docker', paneId: null, a2aUrl: 'http://w/a2a', workerToken: 't', contextId: undefined }))
  assert.match(error, /no conversation recorded/)
})
