// Tests for the attention-store message-delivery loop.
//
// Three things are load-bearing, and each is tested against its negative:
//
//   * the refusal to settle on no content. An item that reads `answered` with nothing in
//     it must not release a park — that would tell a worker it had been answered when
//     nobody said anything.
//   * the join, and the freshness guard on it. The park is re-derived from the producer,
//     and one producer answers many cards over its life, so an answer from an earlier gate
//     must not settle a later park.
//   * the split from the permission arm. A `permission` item must never be settled here,
//     and `attention-poll.mjs`'s own watch set must stay permission-only.

import { test } from 'node:test'
import assert from 'node:assert/strict'
import {
  OPERATOR_PREFIX,
  deliveryMessage,
  messageTextOf,
  selectMessagePark,
  startMessageDelivery,
  storeMessageRecord,
} from './message-delivery.mjs'

function agentsOf(...entries) {
  return new Map(entries.map(([id, sessionId]) => [id, { id, sessionId }]))
}

function pendingOf(...entries) {
  return new Map(
    entries.map(([requestId, agentId, requestedAt = new Date().toISOString()]) => [
      requestId,
      {
        requestId,
        agentId,
        toolName: 'AskUserQuestion',
        input: { questions: [] },
        requestedAt,
        settled: [],
        settle(result) {
          this.settled.push(result)
        },
      },
    ]),
  )
}

// One tick of the loop, driven by hand: `setTimeoutImpl` captures the callback instead of
// scheduling it, so a test decides when time passes.
function harness({ open = [], items = {}, agents, pending, log }) {
  const ticks = []
  startMessageDelivery({
    storeUrl: 'http://store',
    agents,
    pending,
    log,
    setTimeoutImpl: (fn) => {
      ticks.push(fn)
      return ticks.length
    },
    clearTimeoutImpl: () => {},
    fetchImpl: async (url) => ({
      ok: true,
      json: async () => (url.endsWith('/attention') ? open : items[url.split('/').pop()]),
    }),
  })
  return ticks
}

test('messageTextOf reads a text answer', () => {
  assert.deepEqual(messageTextOf({ answer: { kind: 'text', value: 'ship it' } }), {
    ok: true,
    text: 'ship it',
  })
})

test('messageTextOf reads an option answer', () => {
  assert.deepEqual(messageTextOf({ answer: { kind: 'option', value: 'yes' } }), {
    ok: true,
    text: 'yes',
  })
})

test('messageTextOf joins every pick of a multiple answer', () => {
  // ⚠️ Reading only `value` here would silently deliver the first pick and drop the rest.
  const got = messageTextOf({ answer: { kind: 'option', values: ['a', 'b', 'c'] } })
  assert.deepEqual(got, { ok: true, text: 'a, b, c' })
})

test('messageTextOf refuses a skip, an absent answer and an empty one', () => {
  for (const item of [
    { answer: { kind: 'skip' } },
    { answer: { kind: 'text', value: '   ' } },
    { answer: { kind: 'option' } },
    {},
    { answer: null },
  ]) {
    const got = messageTextOf(item)
    assert.equal(got.ok, false, `expected refusal for ${JSON.stringify(item)}`)
    assert.match(got.reason, /no answer content/)
  }
})

test('messageTextOf carries the tab of each per-question answer', () => {
  const got = messageTextOf({
    answers: [
      { tab: 'Scope', kind: 'option', value: 'narrow' },
      { tab: 'Risk', kind: 'text', value: 'low' },
    ],
  })
  assert.deepEqual(got, { ok: true, text: 'Scope: narrow\nRisk: low' })
})

test('messageTextOf refuses a per-question answer with no content', () => {
  const got = messageTextOf({ answers: [{ tab: 'A', kind: 'skip' }] })
  assert.equal(got.ok, false)
  assert.match(got.reason, /per-question answer with no content/)
})

test('deliveryMessage carries the operator prefix llms.txt fixes', () => {
  assert.equal(deliveryMessage('yes'), `${OPERATOR_PREFIX} yes`)
  assert.match(deliveryMessage('yes'), /^Operator answer, via supervisor: /)
})

test('selectMessagePark refuses a permission-class item', () => {
  const got = selectMessagePark({
    item: { answer_mechanism: 'permission', producer_id: 'sess-1' },
    agents: agentsOf(['a1', 'sess-1']),
    pending: pendingOf(['req-1', 'a1']),
  })
  assert.equal(got.ok, false)
  assert.match(got.reason, /permission-class, not message-class/)
})

test('selectMessagePark refuses an item that names no producer', () => {
  const got = selectMessagePark({
    item: { answer_mechanism: 'message' },
    agents: agentsOf(['a1', 'sess-1']),
    pending: pendingOf(['req-1', 'a1']),
  })
  assert.equal(got.ok, false)
  assert.match(got.reason, /names no producer/)
})

test('selectMessagePark refuses a producer this server does not hold', () => {
  const got = selectMessagePark({
    item: { answer_mechanism: 'message', producer_id: 'someone-else' },
    agents: agentsOf(['a1', 'sess-1']),
    pending: pendingOf(['req-1', 'a1']),
  })
  assert.equal(got.ok, false)
  assert.match(got.reason, /no agent of this server holds session/)
})

test('selectMessagePark refuses an agent with no parked prompt', () => {
  // The idle case — a closer-panel card whose session is not parked. This arm has nothing
  // to settle, and must say so rather than reach for a park that is not there.
  const got = selectMessagePark({
    item: { answer_mechanism: 'message', producer_id: 'sess-1' },
    agents: agentsOf(['a1', 'sess-1']),
    pending: pendingOf(),
  })
  assert.equal(got.ok, false)
  assert.match(got.reason, /no parked prompt to settle/)
})

test('selectMessagePark refuses two parked prompts rather than guessing', () => {
  const got = selectMessagePark({
    item: { answer_mechanism: 'message', producer_id: 'sess-1' },
    agents: agentsOf(['a1', 'sess-1']),
    pending: pendingOf(['req-1', 'a1'], ['req-2', 'a1']),
  })
  assert.equal(got.ok, false)
  assert.match(got.reason, /2 parked prompts/)
})

test('selectMessagePark refuses an answer older than the park it would settle', () => {
  // ⚠️ The guard that stops a stale card settling a fresh gate. Both are joined by
  // producer, and a producer answers many cards over its life.
  const got = selectMessagePark({
    item: {
      answer_mechanism: 'message',
      producer_id: 'sess-1',
      created_at: '2026-09-29T07:00:00.000Z',
    },
    agents: agentsOf(['a1', 'sess-1']),
    pending: pendingOf(['req-1', 'a1', '2026-09-29T07:26:59.000Z']),
  })
  assert.equal(got.ok, false)
  assert.match(got.reason, /refusing to settle a gate this answer does not belong to/)
})

test('selectMessagePark accepts an answer created with the park', () => {
  const at = '2026-09-29T07:26:59.000Z'
  const got = selectMessagePark({
    item: { answer_mechanism: 'message', producer_id: 'sess-1', created_at: at },
    agents: agentsOf(['a1', 'sess-1']),
    pending: pendingOf(['req-1', 'a1', at]),
  })
  assert.equal(got.ok, true)
  assert.equal(got.requestId, 'req-1')
  assert.equal(got.agentId, 'a1')
})

test('storeMessageRecord mirrors the permission record and carries the words', () => {
  const park = { toolName: 'AskUserQuestion', input: { command: 'x' }, requestedAt: new Date().toISOString() }
  const record = storeMessageRecord({ requestId: 'req-1', agentId: 'a1', park, text: 'yes' })
  assert.equal(record.decision, 'deny')
  assert.equal(record.decided_by, 'store')
  assert.equal(record.message, 'yes')
  assert.equal(record.request_id, 'req-1')
  assert.equal(record.agent, 'a1')
  assert.equal(record.matched_rule, null)
})

test('an answered message card settles the parked prompt as deny + the operator words', async () => {
  const pending = pendingOf(['req-1', 'a1'])
  const ticks = harness({
    open: [{ item_id: 'i1', answer_mechanism: 'message' }],
    items: {
      i1: {
        item_id: 'i1',
        state: 'answered',
        answer_mechanism: 'message',
        producer_id: 'sess-1',
        answer: { kind: 'text', value: 'done' },
      },
    },
    agents: agentsOf(['a1', 'sess-1']),
    pending,
    log: () => {},
  })
  await ticks[0]()
  const settled = pending.get('req-1').settled
  assert.equal(settled.length, 1)
  assert.equal(settled[0].behavior, 'deny')
  assert.equal(settled[0].message, `${OPERATOR_PREFIX} done`)
})

test('a permission-class item is never settled by this arm', async () => {
  const pending = pendingOf(['req-1', 'a1'])
  const logged = []
  const ticks = harness({
    open: [{ item_id: 'i1', answer_mechanism: 'permission' }],
    items: {
      i1: {
        item_id: 'i1',
        state: 'answered',
        decision: 'allow',
        answer_mechanism: 'permission',
        producer_id: 'sess-1',
      },
    },
    agents: agentsOf(['a1', 'sess-1']),
    pending,
    log: (m) => logged.push(m),
  })
  await ticks[0]()
  assert.equal(pending.get('req-1').settled.length, 0)
})

test('an answered item with no content is refused, never settled', async () => {
  const pending = pendingOf(['req-1', 'a1'])
  const logged = []
  const ticks = harness({
    open: [{ item_id: 'i1', answer_mechanism: 'message' }],
    items: {
      i1: {
        item_id: 'i1',
        state: 'answered',
        answer_mechanism: 'message',
        producer_id: 'sess-1',
        answer: { kind: 'skip' },
      },
    },
    agents: agentsOf(['a1', 'sess-1']),
    pending,
    log: (m) => logged.push(m),
  })
  await ticks[0]()
  assert.equal(pending.get('req-1').settled.length, 0)
  assert.match(logged.join('\n'), /undeliverable/)
})

test('a store error is logged and swallowed, never thrown', async () => {
  const logged = []
  const ticks = []
  startMessageDelivery({
    storeUrl: 'http://store',
    agents: agentsOf(['a1', 'sess-1']),
    pending: pendingOf(['req-1', 'a1']),
    log: (m) => logged.push(m),
    setTimeoutImpl: (fn) => {
      ticks.push(fn)
      return ticks.length
    },
    clearTimeoutImpl: () => {},
    fetchImpl: async () => {
      throw new Error('store unreachable')
    },
  })
  await ticks[0]()
  assert.match(logged.join('\n'), /message delivery failed/)
  assert.match(logged.join('\n'), /still auto-deny at the timeout/)
})
