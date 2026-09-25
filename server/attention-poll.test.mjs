// Tests for the attention-store delivery loop.
//
// Two things are load-bearing and both are tested against their negative:
//
//   * the join. An item names its producer, never its parked prompt (the schema has no
//     route field), so the park is re-derived — and a re-derived join can silently pick
//     the wrong park. Every way it can be wrong gets a case.
//   * the refusal to guess. An agent with two parked prompts must be reported, never
//     settled, because settling the wrong one releases a gate nobody released.

import { test } from 'node:test'
import assert from 'node:assert/strict'
import { decisionOf, selectParked, startAttentionPoll } from './attention-poll.mjs'

function agentsOf(...entries) {
  return new Map(entries.map(([id, sessionId]) => [id, { id, sessionId }]))
}

function pendingOf(...entries) {
  return new Map(
    entries.map(([requestId, agentId]) => [
      requestId,
      { requestId, agentId, settled: [], settle(result) { this.settled.push(result) } },
    ]),
  )
}

test('decisionOf accepts both verdicts', () => {
  assert.deepEqual(decisionOf({ decision: 'allow' }), { ok: true, decision: 'allow' })
  assert.deepEqual(decisionOf({ decision: 'deny' }), { ok: true, decision: 'deny' })
})

test('decisionOf refuses a missing verdict rather than defaulting', () => {
  for (const item of [{}, { decision: '' }, { decision: null }, { decision: undefined }]) {
    const got = decisionOf(item)
    assert.equal(got.ok, false)
    assert.match(got.reason, /no decision/)
  }
})

test('decisionOf refuses a verdict outside the enum', () => {
  const got = decisionOf({ decision: 'maybe' })
  assert.equal(got.ok, false)
  assert.match(got.reason, /not one of allow\/deny/)
})

test('selectParked joins producer to the one parked prompt', () => {
  const got = selectParked({
    item: { answer_mechanism: 'permission', producer_id: 'sess-1' },
    agents: agentsOf(['a1', 'sess-1']),
    pending: pendingOf(['perm_7', 'a1']),
  })
  assert.deepEqual(got, { ok: true, requestId: 'perm_7', agentId: 'a1' })
})

test('selectParked refuses a non-permission item', () => {
  const got = selectParked({
    item: { answer_mechanism: 'message', producer_id: 'sess-1' },
    agents: agentsOf(['a1', 'sess-1']),
    pending: pendingOf(['perm_7', 'a1']),
  })
  assert.equal(got.ok, false)
  assert.match(got.reason, /not permission-class/)
})

test('selectParked refuses a producer this server did not spawn', () => {
  // The park of a worker spawned by another process lives in that process. Delivering
  // here would be guessing at a park this server cannot see.
  const got = selectParked({
    item: { answer_mechanism: 'permission', producer_id: 'someone-elses-session' },
    agents: agentsOf(['a1', 'sess-1']),
    pending: pendingOf(['perm_7', 'a1']),
  })
  assert.equal(got.ok, false)
  assert.match(got.reason, /no agent of this server holds session/)
})

test('selectParked refuses when the agent has nothing parked', () => {
  const got = selectParked({
    item: { answer_mechanism: 'permission', producer_id: 'sess-1' },
    agents: agentsOf(['a1', 'sess-1']),
    pending: pendingOf(),
  })
  assert.equal(got.ok, false)
  assert.match(got.reason, /no parked prompt/)
})

test('selectParked refuses ambiguity rather than settling the wrong park', () => {
  const got = selectParked({
    item: { answer_mechanism: 'permission', producer_id: 'sess-1' },
    agents: agentsOf(['a1', 'sess-1']),
    pending: pendingOf(['perm_7', 'a1'], ['perm_8', 'a1']),
  })
  assert.equal(got.ok, false)
  assert.match(got.reason, /2 parked prompts/)
  assert.match(got.reason, /answer_permission/)
})

test('selectParked ignores another agent\'s parks', () => {
  const got = selectParked({
    item: { answer_mechanism: 'permission', producer_id: 'sess-1' },
    agents: agentsOf(['a1', 'sess-1'], ['a2', 'sess-2']),
    pending: pendingOf(['perm_7', 'a2']),
  })
  assert.equal(got.ok, false)
  assert.match(got.reason, /no parked prompt/)
})

// --- the loop -------------------------------------------------------------------------

function harness({ items = {}, open = [], log = () => {} } = {}) {
  const agents = agentsOf(['a1', 'sess-1'])
  const pending = pendingOf(['perm_7', 'a1'])
  const ticks = []
  const stop = startAttentionPoll({
    storeUrl: 'http://store',
    agents,
    pending,
    log,
    setTimeoutImpl: (fn) => {
      ticks.push(fn)
      return ticks.length
    },
    clearTimeoutImpl: () => {},
    fetchImpl: async (url) => {
      const path = url.replace('http://store', '')
      const body = path === '/api/1.0/attention' ? open : items[path.split('/').pop()]
      if (body === undefined) return { ok: false, status: 404, json: async () => ({}) }
      return { ok: true, status: 200, json: async () => body }
    },
  })
  return { ticks, stop, pending }
}

test('an answered allow settles the parked promise', async () => {
  const { ticks, pending } = harness({
    open: [{ item_id: 'i1', answer_mechanism: 'permission' }],
    items: { i1: { item_id: 'i1', state: 'answered', decision: 'allow', answer_mechanism: 'permission', producer_id: 'sess-1' } },
  })
  await ticks[0]()
  assert.equal(pending.get('perm_7').settled.length, 1)
  assert.equal(pending.get('perm_7').settled[0].behavior, 'allow')
})

test('an answered deny settles as a deny, not an allow', async () => {
  const { ticks, pending } = harness({
    open: [{ item_id: 'i1', answer_mechanism: 'permission' }],
    items: { i1: { item_id: 'i1', state: 'answered', decision: 'deny', answer_mechanism: 'permission', producer_id: 'sess-1' } },
  })
  await ticks[0]()
  assert.equal(pending.get('perm_7').settled[0].behavior, 'deny')
})

test('an open item is not settled', async () => {
  const { ticks, pending } = harness({
    open: [{ item_id: 'i1', answer_mechanism: 'permission' }],
    items: { i1: { item_id: 'i1', state: 'open', answer_mechanism: 'permission', producer_id: 'sess-1' } },
  })
  await ticks[0]()
  assert.equal(pending.get('perm_7').settled.length, 0)
})

test('an answered item with no verdict is reported, not defaulted', async () => {
  const logged = []
  const { ticks, pending } = harness({
    open: [{ item_id: 'i1', answer_mechanism: 'permission' }],
    items: { i1: { item_id: 'i1', state: 'answered', answer_mechanism: 'permission', producer_id: 'sess-1' } },
    log: (m) => logged.push(m),
  })
  await ticks[0]()
  assert.equal(pending.get('perm_7').settled.length, 0)
  assert.match(logged.join('\n'), /no decision/)
})

test('a message-class item is never watched', async () => {
  const { ticks, pending } = harness({
    open: [{ item_id: 'i1', answer_mechanism: 'message' }],
    items: { i1: { item_id: 'i1', state: 'answered', decision: 'allow', answer_mechanism: 'message', producer_id: 'sess-1' } },
  })
  await ticks[0]()
  assert.equal(pending.get('perm_7').settled.length, 0)
})

test('a store error is logged and swallowed, never thrown', async () => {
  const logged = []
  const ticks = []
  const stop = startAttentionPoll({
    storeUrl: 'http://store',
    agents: agentsOf(['a1', 'sess-1']),
    pending: pendingOf(['perm_7', 'a1']),
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
  assert.match(logged.join('\n'), /attention poll failed/)
  // And the loop re-armed rather than dying: a store outage must not stop delivery for
  // the rest of the server's life.
  assert.equal(ticks.length, 2)
  stop()
})

test('a verdict arriving after its park settled is reported, not crashed on', async () => {
  // The park can settle (answered, denied, or timed out) between the store recording the
  // verdict and the next tick. There is then no park left to settle, and that must read as
  // a report rather than as an exception inside the loop.
  const logged = []
  const { ticks, pending } = harness({
    open: [{ item_id: 'i1', answer_mechanism: 'permission' }],
    items: { i1: { item_id: 'i1', state: 'answered', decision: 'allow', answer_mechanism: 'permission', producer_id: 'sess-1' } },
    log: (m) => logged.push(m),
  })
  pending.delete('perm_7')
  await ticks[0]()
  assert.match(logged.join('\n'), /no parked prompt/)
})
