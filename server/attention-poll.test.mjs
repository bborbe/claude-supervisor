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
import { readFileSync } from 'node:fs'
import { armDelivery, decisionOf, selectParked, startAttentionPoll, storeDecisionRecord, STORE_DECIDER } from './attention-poll.mjs'

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

test('armDelivery accepts an answer an arm delivered', () => {
  const got = armDelivery({ resolved_by: '11111111-2222-3333-4444-555555555555' })
  assert.equal(got.ok, true)
  assert.equal(got.resolvedBy, '11111111-2222-3333-4444-555555555555')
})

test('armDelivery refuses an answer with no resolved_by', () => {
  // ⚠️ The guard the operator's ruling turns on. A verdict alone is not enough
  // for a permission gate: the board never sends `resolved_by`, so an answer
  // carrying a verdict but no arm provenance is not an operator decision.
  for (const item of [{}, { resolved_by: '' }, { resolved_by: null }, { resolved_by: 7 }, null]) {
    const got = armDelivery(item)
    assert.equal(got.ok, false, `expected refusal for ${JSON.stringify(item)}`)
    assert.match(got.reason, /no resolved_by/)
  }
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

function harness({ items = {}, open = [], log = () => {}, onSettled = () => {}, pendingEntries = [['perm_7', 'a1']] } = {}) {
  const agents = agentsOf(['a1', 'sess-1'])
  const pending = pendingOf(...pendingEntries)
  const ticks = []
  const posts = []
  const stop = startAttentionPoll({
    storeUrl: 'http://store',
    agents,
    pending,
    log,
    onSettled,
    setTimeoutImpl: (fn) => {
      ticks.push(fn)
      return ticks.length
    },
    clearTimeoutImpl: () => {},
    fetchImpl: async (url, options = {}) => {
      const path = url.replace('http://store', '')
      // The delivery trail is written by POST. Captured rather than answered from
      // `items`, so a spec can assert what this arm recorded and — for the negative
      // case — that it recorded nothing at all.
      if (options.method === 'POST') {
        posts.push({ path, body: JSON.parse(options.body) })
        return { ok: true, status: 200, json: async () => ({}) }
      }
      const body = path === '/api/1.0/attention' ? open : items[path.split('/').pop()]
      if (body === undefined) return { ok: false, status: 404, json: async () => ({}) }
      return { ok: true, status: 200, json: async () => body }
    },
  })
  return { ticks, stop, pending, posts }
}

test('an answered allow settles the parked promise', async () => {
  const { ticks, pending } = harness({
    open: [{ item_id: 'i1', answer_mechanism: 'permission' }],
    // `resolved_by` is in the fixture because a permission gate releases only on
    // an ARM answer. Without it the same item is refused — see the guard's spec.
    items: { i1: { item_id: 'i1', state: 'answered', decision: 'allow', resolved_by: 'sess-abc', answer_mechanism: 'permission', producer_id: 'sess-1' } },
  })
  await ticks[0]()
  assert.equal(pending.get('perm_7').settled.length, 1)
  assert.equal(pending.get('perm_7').settled[0].behavior, 'allow')
})

test('an answered deny settles as a deny, not an allow', async () => {
  const { ticks, pending } = harness({
    open: [{ item_id: 'i1', answer_mechanism: 'permission' }],
    items: { i1: { item_id: 'i1', state: 'answered', decision: 'deny', resolved_by: 'sess-abc', answer_mechanism: 'permission', producer_id: 'sess-1' } },
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

// The delivery trail. This arm is the permission class's carrier, so it — not the
// command that answers message items — is what must record the attempt. Without
// these writes a delivered verdict would read as `never_attempted`.

test('a settled permission answer records the attempt as delivered', async () => {
  const { ticks, posts } = harness({
    open: [{ item_id: 'i1', answer_mechanism: 'permission' }],
    items: { i1: { item_id: 'i1', state: 'answered', decision: 'allow', resolved_by: 'sess-abc', answer_mechanism: 'permission', producer_id: 'sess-1' } },
  })
  await ticks[0]()
  assert.deepEqual(posts, [
    {
      path: '/api/1.0/attention/i1/attempt',
      body: { carrier: 'supervisor:attention-poll', outcome: 'delivered' },
    },
  ])
})

test('an attempt with no park to settle records the attempt as failed', async () => {
  // The park it was aimed at belongs to another agent, so the delivery could not
  // happen — an attempt WAS made and failed, which is not `never_attempted`.
  const { ticks, posts } = harness({
    open: [{ item_id: 'i1', answer_mechanism: 'permission' }],
    items: { i1: { item_id: 'i1', state: 'answered', decision: 'allow', resolved_by: 'sess-abc', answer_mechanism: 'permission', producer_id: 'sess-2' } },
  })
  await ticks[0]()
  assert.equal(posts.length, 1)
  assert.equal(posts[0].body.outcome, 'failed')
})

test('a non-arm answer records nothing, because no delivery was attempted', async () => {
  // No `resolved_by` means no arm delivered this answer, so the gate is not released
  // and nothing was attempted. Recording `failed` here would claim a carrier tried
  // and could not deliver, which is a different and false statement.
  const { ticks, posts } = harness({
    open: [{ item_id: 'i1', answer_mechanism: 'permission' }],
    items: { i1: { item_id: 'i1', state: 'answered', decision: 'allow', answer_mechanism: 'permission', producer_id: 'sess-1' } },
  })
  await ticks[0]()
  assert.equal(posts.length, 0)
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

test('a verdict with no arm provenance does not settle — and says why', async () => {
  // ⚠️ This is the operator's ruling: a permission gate releases only on an ARM
  // answer. The item below is well-formed and carries a valid verdict, so
  // `decisionOf` passes it — the refusal is `armDelivery`'s, and the park stays
  // parked rather than being released by something that is not an operator
  // decision. It is LOGGED, never silently refused.
  const logged = []
  const { ticks, pending } = harness({
    open: [{ item_id: 'i1', answer_mechanism: 'permission' }],
    items: {
      i1: {
        item_id: 'i1',
        state: 'answered',
        decision: 'allow',
        answered_by: 'attention-board',
        answer_mechanism: 'permission',
        producer_id: 'sess-1',
      },
    },
    log: (m) => logged.push(m),
  })
  await ticks[0]()
  assert.equal(pending.get('perm_7').settled.length, 0)
  assert.match(logged.join('\n'), /not arm-delivered/)
  assert.match(logged.join('\n'), /no resolved_by/)
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
    items: { i1: { item_id: 'i1', state: 'answered', decision: 'allow', resolved_by: 'sess-abc', answer_mechanism: 'permission', producer_id: 'sess-1' } },
    log: (m) => logged.push(m),
  })
  pending.delete('perm_7')
  await ticks[0]()
  assert.match(logged.join('\n'), /no parked prompt/)
})

// --- the store arm's own record ---------------------------------------------------------
//
// The distinction this exists to pin: a park the STORE released must be recorded as the
// store's, and a park the store did NOT release must never acquire that value. Both
// directions are tested, because a one-directional check passes on a recorder that fires
// unconditionally — and that recorder would mislabel every manager-answered park, which is
// exactly the false attribution `decided_by` is there to prevent.

const EXISTING_DECIDERS = ['policy', 'escalated', 'manager']

test('storeDecisionRecord names the store arm, not one of the existing deciders', () => {
  const record = storeDecisionRecord({
    requestId: 'perm_7',
    agentId: 'a1',
    decision: 'allow',
    park: { toolName: 'Bash', input: { command: 'ls' }, requestedAt: '2026-09-26T10:00:00.000Z' },
    now: new Date('2026-09-26T10:00:02.000Z'),
  })
  assert.equal(record.decided_by, STORE_DECIDER)
  assert.ok(
    !EXISTING_DECIDERS.includes(record.decided_by),
    'the store arm must not reuse a value that already means another actor',
  )
})

test('storeDecisionRecord carries the park fields a mined rule needs', () => {
  const record = storeDecisionRecord({
    requestId: 'perm_7',
    agentId: 'a1',
    decision: 'deny',
    park: { toolName: 'Bash', input: { command: 'rm -rf /tmp/x' }, requestedAt: '2026-09-26T10:00:00.000Z' },
    now: new Date('2026-09-26T10:00:02.000Z'),
  })
  assert.equal(record.tool, 'Bash')
  assert.equal(record.key, 'rm -rf /tmp/x')
  assert.equal(record.decision, 'deny')
  assert.equal(record.matched_rule, null)
  assert.equal(record.request_id, 'perm_7')
  assert.equal(record.agent, 'a1')
  assert.equal(record.latency_ms, 2000)
  assert.equal(record.ts, '2026-09-26T10:00:02.000Z')
})

test('the loop reports a store-released park, with the park itself', async () => {
  const seen = []
  const { ticks } = harness({
    open: [{ item_id: 'i1', answer_mechanism: 'permission' }],
    items: { i1: { item_id: 'i1', state: 'answered', decision: 'allow', resolved_by: 'sess-abc', answer_mechanism: 'permission', producer_id: 'sess-1' } },
    onSettled: (e) => seen.push(e),
  })
  await ticks[0]()
  assert.equal(seen.length, 1)
  assert.equal(seen[0].requestId, 'perm_7')
  assert.equal(seen[0].agentId, 'a1')
  assert.equal(seen[0].decision, 'allow')
  assert.equal(seen[0].itemId, 'i1')
  // The park is passed because `settle` has already removed it from `pending` — without it
  // the caller cannot build a minable record, and an unminable record is not worth writing.
  assert.equal(seen[0].park.requestId, 'perm_7')
})

test('onSettled is not called when the store released nothing', async () => {
  for (const [name, item] of [
    ['an open item', { state: 'open', decision: 'allow' }],
    ['an answered item with no verdict', { state: 'answered' }],
    ['a verdict outside the enum', { state: 'answered', decision: 'maybe' }],
  ]) {
    const seen = []
    const { ticks } = harness({
      open: [{ item_id: 'i1', answer_mechanism: 'permission' }],
      items: { i1: { item_id: 'i1', answer_mechanism: 'permission', producer_id: 'sess-1', ...item } },
      onSettled: (e) => seen.push(e),
    })
    await ticks[0]()
    assert.equal(seen.length, 0, `${name} must not be recorded as a store decision`)
  }
})

test('onSettled is not called when the park is ambiguous', async () => {
  // Two parks on one agent: the verdict cannot be attributed to either, so nothing settles
  // — and nothing may be recorded as the store's decision either.
  const seen = []
  const { ticks, pending } = harness({
    open: [{ item_id: 'i1', answer_mechanism: 'permission' }],
    items: { i1: { item_id: 'i1', state: 'answered', decision: 'allow', resolved_by: 'sess-abc', answer_mechanism: 'permission', producer_id: 'sess-1' } },
    onSettled: (e) => seen.push(e),
    pendingEntries: [['perm_7', 'a1'], ['perm_8', 'a1']],
  })
  await ticks[0]()
  assert.equal(seen.length, 0)
  assert.equal(pending.get('perm_7').settled.length, 0)
  assert.equal(pending.get('perm_8').settled.length, 0)
})

test('onSettled fires after the settle, so a failing recorder cannot withhold the decision', async () => {
  const { ticks, pending } = harness({
    open: [{ item_id: 'i1', answer_mechanism: 'permission' }],
    items: { i1: { item_id: 'i1', state: 'answered', decision: 'allow', resolved_by: 'sess-abc', answer_mechanism: 'permission', producer_id: 'sess-1' } },
    onSettled: () => {
      throw new Error('recorder exploded')
    },
  })
  await ticks[0]()
  // The park is released regardless: the decision is the critical path, the record is not.
  assert.equal(pending.get('perm_7').settled.length, 1)
  assert.equal(pending.get('perm_7').settled[0].behavior, 'allow')
})

test('the manager-settled path keeps its own decider, distinct from the store arm', () => {
  // `supervisor.mjs` cannot be imported — importing it starts the MCP server, which is why
  // `policy.mjs` exists as a separate module at all — so the manager-settled path is pinned
  // by reading its source. That is weaker than the behavioural tests above and is recorded
  // as such: it catches the one edit that matters, a manager site repointed at the store
  // value (which would mislabel every manager answer), not a behavioural regression inside
  // it. A behavioural test needs the harness that does not exist.
  const src = readFileSync(new URL('./supervisor.mjs', import.meta.url), 'utf8')
  assert.match(src, /decided_by: 'manager'/, 'the answer_permission site must keep its own decider')
  assert.doesNotMatch(
    src,
    /decided_by: STORE_DECIDER/,
    'the store value must be written at the poll site only, never by a second site',
  )
  assert.notEqual(STORE_DECIDER, 'manager')
})
