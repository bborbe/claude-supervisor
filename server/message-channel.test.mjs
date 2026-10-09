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

test('a headless worker is refused, and the refusal names what DOES reach it', () => {
  // Not a generic "cannot message this": the caller needs the channel that works, or the
  // refusal sends them looking for a pane that will never exist.
  const r = resolveMessageChannel(agent({ status: 'running', paneId: null }))
  assert.ok(r.error, 'expected a refusal')
  assert.match(r.error, /headless/)
  assert.match(r.error, /answer_permission/)
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
