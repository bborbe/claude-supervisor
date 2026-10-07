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
function harness({ open = [], items = {}, agents, pending, log, posts }) {
  const ticks = []
  // Attached to the array rather than returned beside it, so the many specs that read
  // `harness(…)[0]` keep working unchanged. `clock` is a spec-owned clock, so the idle
  // cadence is DRIVEN rather than waited on; it starts at 0, and `lastReadAtMs` is
  // NEGATIVE_INFINITY precisely so the first read does not depend on it having advanced.
  ticks.gets = []
  ticks.clock = { ms: 0 }
  // The DELAY each reschedule asked for, beside the callback — without it the
  // `parked ? intervalMs : idleCheckMs` choice is unasserted, because a spec driving
  // ticks by hand reads whenever it likes regardless of the delay passed.
  ticks.delays = []
  startMessageDelivery({
    storeUrl: 'http://store',
    agents,
    pending,
    log,
    nowImpl: () => ticks.clock.ms,
    setTimeoutImpl: (fn, delay) => {
      ticks.push(fn)
      ticks.delays.push(delay)
      return ticks.length
    },
    clearTimeoutImpl: () => {},
    // The delivery trail is written by POST. Captured into a caller-supplied array, so a
    // spec can assert what this arm recorded — and, for the negative case, that it
    // recorded nothing at all.
    fetchImpl: async (url, options = {}) => {
      if (options?.method === 'POST') {
        if (posts) {
          posts.push({
            path: url.replace('http://store', ''),
            body: JSON.parse(options.body),
          })
        }
        return { ok: true, json: async () => ({}) }
      }
      ticks.gets.push(url.replace('http://store', ''))
      return {
        ok: true,
        json: async () => (url.endsWith('/attention') ? open : items[url.split('/').pop()]),
      }
    },
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

test('an unreadable item does not wedge the tick for the items behind it', async () => {
  // ⚠️ Without the per-item guard the 404 escapes to the outer catch, every later item in
  // the tick is skipped, and the same id throws again on every subsequent tick — delivery
  // wedged permanently and silently. The item behind it settling is the proof.
  const pending = pendingOf(['req-1', 'a1'])
  const ticks = []
  startMessageDelivery({
    storeUrl: 'http://store',
    agents: agentsOf(['a1', 'sess-1']),
    pending,
    log: () => {},
    setTimeoutImpl: (fn) => {
      ticks.push(fn)
      return ticks.length
    },
    clearTimeoutImpl: () => {},
    fetchImpl: async (url) => {
      if (url.endsWith('/attention')) {
        return {
          ok: true,
          json: async () => [
            { item_id: 'gone', answer_mechanism: 'message' },
            { item_id: 'i1', answer_mechanism: 'message' },
          ],
        }
      }
      if (url.endsWith('/gone')) return { ok: false, status: 404, json: async () => ({}) }
      return {
        ok: true,
        json: async () => ({
          item_id: 'i1',
          state: 'answered',
          answer_mechanism: 'message',
          producer_id: 'sess-1',
          answer: { kind: 'text', value: 'done' },
        }),
      }
    },
  })
  await ticks[0]()
  assert.equal(pending.get('req-1').settled.length, 1)
  assert.equal(pending.get('req-1').settled[0].message, `${OPERATOR_PREFIX} done`)
})

test('a terminal-but-unanswered item leaves the watch set instead of being re-read forever', async () => {
  let reads = 0
  let lists = 0
  const ticks = []
  startMessageDelivery({
    storeUrl: 'http://store',
    agents: agentsOf(['a1', 'sess-1']),
    pending: pendingOf(),
    log: () => {},
    setTimeoutImpl: (fn) => {
      ticks.push(fn)
      return ticks.length
    },
    clearTimeoutImpl: () => {},
    fetchImpl: async (url) => {
      if (url.endsWith('/attention')) {
        // Served open once, then gone — the item went terminal while we watched it, so
        // the second tick's list no longer carries it and only the watch set could.
        lists += 1
        return {
          ok: true,
          json: async () => (lists === 1 ? [{ item_id: 'gone', answer_mechanism: 'message' }] : []),
        }
      }
      reads += 1
      return {
        ok: true,
        json: async () => ({ item_id: 'gone', state: 'closed', answer_mechanism: 'message' }),
      }
    },
  })
  await ticks[0]()
  await ticks[1]()
  assert.equal(reads, 1, 'a closed item must not be re-read on the next tick')
})

// The delivery trail. This arm is the `message` class's carrier for a card answered ON
// THE BOARD, so it — not `commands/attention-next.md`, which carries the conversational
// path — is what must record the attempt for these. Measured live 2026-09-29: 39
// board-answered message cards had been delivered, one of them into a running session,
// and every one read `never_attempted`.

test('a settled message card records the attempt as delivered', async () => {
  const pending = pendingOf(['req-1', 'a1'])
  const posts = []
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
    posts,
  })
  await ticks[0]()
  assert.deepEqual(posts, [
    {
      path: '/api/1.0/attention/i1/attempt',
      body: { carrier: 'supervisor:message-delivery', outcome: 'delivered' },
    },
  ])
})

test('an answer with no park to settle records the attempt as failed', async () => {
  // An attempt WAS made and could not be delivered — the park it was aimed at is gone —
  // so this is `failed`, not `never_attempted`.
  const posts = []
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
    pending: pendingOf(),
    log: () => {},
    posts,
  })
  await ticks[0]()
  assert.equal(posts.length, 1)
  assert.equal(posts[0].body.outcome, 'failed')
})

test('an answer for a session another server holds records nothing', async () => {
  // Every supervisor server watches every open `message` card, so one answer is read by
  // the whole fleet. Only the server holding the producing session attempted anything;
  // if this one wrote `failed`, the fleet would outvote the owner's real outcome.
  // Measured live 2026-09-30: 19 `failed` records and 0 `delivered`.
  const posts = []
  const ticks = harness({
    open: [{ item_id: 'i1', answer_mechanism: 'message' }],
    items: {
      i1: {
        item_id: 'i1',
        state: 'answered',
        answer_mechanism: 'message',
        producer_id: 'sess-elsewhere',
        answer: { kind: 'text', value: 'done' },
      },
    },
    agents: agentsOf(['a1', 'sess-1']),
    pending: pendingOf(['req-1', 'a1']),
    log: () => {},
    posts,
  })
  await ticks[0]()
  assert.equal(posts.length, 0)
})

test('an answer naming no producer records nothing', async () => {
  const posts = []
  const ticks = harness({
    open: [{ item_id: 'i1', answer_mechanism: 'message' }],
    items: {
      i1: {
        item_id: 'i1',
        state: 'answered',
        answer_mechanism: 'message',
        answer: { kind: 'text', value: 'done' },
      },
    },
    agents: agentsOf(['a1', 'sess-1']),
    pending: pendingOf(['req-1', 'a1']),
    log: () => {},
    posts,
  })
  await ticks[0]()
  assert.equal(posts.length, 0)
})

test('an answer with no content records nothing, because no delivery was attempted', async () => {
  // Recording `failed` here would claim a carrier tried and could not deliver, which is
  // a different and false statement: nothing was attempted, so nothing is recorded.
  const posts = []
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
    pending: pendingOf(['req-1', 'a1']),
    log: () => {},
    posts,
  })
  await ticks[0]()
  assert.equal(posts.length, 0)
})

// --- the idle cadence ------------------------------------------------------------------

test('a server holding no park reads once, then stops reading until the trail is due', async () => {
  const ticks = harness({ open: [], agents: agentsOf(['a1', 'sess-1']), pending: pendingOf() })
  await ticks[0]()
  assert.deepEqual(ticks.gets, ['/api/1.0/attention'], 'the first read must happen')

  ticks.clock.ms += 250
  await ticks[1]()
  assert.equal(ticks.gets.length, 1, 'nothing parked, trail not due — no request at all')

  ticks.clock.ms += 30000
  await ticks[2]()
  assert.equal(ticks.gets.length, 2, 'the idle cadence elapses, so the trail read happens')
  // The delay is asserted, not just the read count: `delays[0]` is the initial schedule,
  // so `delays[1]` is what the first (idle) tick chose for itself.
  assert.equal(ticks.delays[1], 250, 'an idle tick must re-check cheaply, not sleep the idle interval')
})

test('a park that appears is read on the next idle check, not on the idle interval', async () => {
  // Same hazard as the permission arm: the list returns OPEN cards only, so a card that
  // opens and is answered between two reads is never watched and never delivered.
  const pending = pendingOf()
  const ticks = harness({ open: [], agents: agentsOf(['a1', 'sess-1']), pending })
  await ticks[0]()
  assert.equal(ticks.gets.length, 1)

  ticks.clock.ms += 250
  pending.set('req-9', { requestId: 'req-9', agentId: 'a1', settled: [], settle() {} })
  await ticks[1]()
  assert.equal(ticks.gets.length, 2, 'a park must be read at once, not one idle interval later')
  assert.equal(ticks.delays[2], 2000, 'and a held park must drop back to the fast cadence')
})

