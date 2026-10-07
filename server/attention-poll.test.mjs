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
import { armDelivery, decisionOf, producingAgent, selectParked, startAttentionPoll, storeDecisionRecord, STORE_DECIDER } from './attention-poll.mjs'

// An optional third element sets the agent's `mode`, which `armDelivery` reads. It is
// omitted by default so the many specs that predate the field keep the mode-less shape a
// rehydrated row actually has.
function agentsOf(...entries) {
  return new Map(
    entries.map(([id, sessionId, mode]) => [id, mode === undefined ? { id, sessionId } : { id, sessionId, mode }]),
  )
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
  const got = armDelivery({
    item: { resolved_by: '11111111-2222-3333-4444-555555555555' },
    agent: { mode: 'interactive' },
  })
  assert.equal(got.ok, true)
  assert.equal(got.resolvedBy, '11111111-2222-3333-4444-555555555555')
})

test('armDelivery accepts a board answer on a headless park', () => {
  // ⚠️ The 2026-10-07 amendment, and the whole point of this change. The board renders
  // Allow / Deny on a headless worker's park and its JS never sends `resolved_by`, so
  // without this branch the one control that park has records an answer and releases
  // nothing — while the worker auto-denies at the timeout.
  const got = armDelivery({ item: { decision: 'allow' }, agent: { mode: 'headless' } })
  assert.equal(got.ok, true)
  assert.equal(got.boardAnswer, true)
})

test('armDelivery refuses a board answer on a tab worker park', () => {
  // The 2026-10-01 narrowing: a tab worker answers in its own session's pane, so its card
  // renders no answering control at all — a board answer for one is a defect rather than
  // an answer, and must not release.
  const got = armDelivery({ item: { decision: 'allow' }, agent: { mode: 'interactive' } })
  assert.equal(got.ok, false)
  assert.match(got.reason, /no resolved_by/)
})

test('armDelivery refuses a cluster row, which carries no mode at all', () => {
  // ⚠️ `cluster` is a spawn TARGET, not a spawn mode: `SPAWN_MODES` is
  // ['interactive','headless'] (server/spawn-mode.mjs) and a cluster spawn refuses the
  // `interactive` argument outright. So the real cluster agent record carries
  // `status: 'cluster'` and **no `mode`**, and this guard refuses it by ABSENCE rather
  // than by recognising the word `cluster`. Pinned with the shape that actually occurs —
  // a spec passing `{ mode: 'cluster' }` would document a state `resolveSpawnMode` cannot
  // return, and would appear to cover a fold of `cluster` into `headless` that it never
  // exercised.
  const got = armDelivery({ item: { decision: 'allow' }, agent: { status: 'cluster' } })
  assert.equal(got.ok, false)
})

test('armDelivery refuses an answer with no resolved_by', () => {
  // ⚠️ The guard the operator's ruling of 2026-09-26 turns on. A verdict alone is not
  // enough for a permission gate: the board never sends `resolved_by`, so an answer
  // carrying a verdict but no arm provenance is not an operator decision — UNLESS the
  // park is headless, where the board is the only answering surface there is.
  for (const item of [{}, { resolved_by: '' }, { resolved_by: null }, { resolved_by: 7 }, null]) {
    const got = armDelivery({ item })
    assert.equal(got.ok, false, `expected refusal for ${JSON.stringify(item)}`)
    assert.match(got.reason, /no resolved_by/)
  }
})

test('armDelivery refuses when the agent mode is absent or unrecognised — never a default', () => {
  // ⚠️ The positive-condition rule this repo states for every guard: an agent whose `mode`
  // is missing (a rehydrated row, or one spawned before the field existed) must REFUSE
  // rather than be read as headless. Reading absence as headless would put a board control
  // back on a class the 2026-10-01 narrowing deliberately took it off.
  for (const agent of [undefined, null, {}, { mode: undefined }, { mode: null }, { mode: 'nonsense' }]) {
    const got = armDelivery({ item: { decision: 'allow' }, agent })
    assert.equal(got.ok, false, `expected refusal for agent ${JSON.stringify(agent)}`)
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

function harness({ items = {}, open = [], log = () => {}, onSettled = () => {}, pendingEntries = [['perm_7', 'a1']], agentMode } = {}) {
  const agents = agentsOf(agentMode === undefined ? ['a1', 'sess-1'] : ['a1', 'sess-1', agentMode])
  const pending = pendingOf(...pendingEntries)
  const ticks = []
  const posts = []
  const gets = []
  // The DELAY each reschedule asked for, kept beside the callback. Without it the
  // `parked ? intervalMs : idleCheckMs` choice is unasserted — a spec driving ticks by
  // hand reads whatever it likes regardless of the delay, so replacing that expression
  // with a constant would leave every cadence spec green.
  const delays = []
  // A clock the spec owns, so the idle cadence is DRIVEN rather than waited on. It starts
  // at 0 on purpose: `lastReadAtMs` is NEGATIVE_INFINITY precisely so the first read does
  // not depend on the clock having advanced past `idleIntervalMs`.
  const clock = { ms: 0 }
  const stop = startAttentionPoll({
    storeUrl: 'http://store',
    agents,
    pending,
    log,
    onSettled,
    nowImpl: () => clock.ms,
    setTimeoutImpl: (fn, delay) => {
      ticks.push(fn)
      delays.push(delay)
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
      gets.push(path)
      const body = path === '/api/1.0/attention' ? open : items[path.split('/').pop()]
      if (body === undefined) return { ok: false, status: 404, json: async () => ({}) }
      return { ok: true, status: 200, json: async () => body }
    },
  })
  return { ticks, stop, pending, posts, gets, clock, delays }
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

test('a board answer on a HEADLESS park is settled — the release this change adds', async () => {
  // ⚠️ No `resolved_by`: this is a board click, and the board's JS cannot produce one.
  // Before the 2026-10-07 amendment the guard refused it, `selectParked` never ran, and
  // the worker auto-denied at PERMISSION_TIMEOUT_MS — the failure this task removes.
  const { ticks, pending } = harness({
    open: [{ item_id: 'i1', answer_mechanism: 'permission' }],
    items: { i1: { item_id: 'i1', state: 'answered', decision: 'allow', answer_mechanism: 'permission', producer_id: 'sess-1' } },
    agentMode: 'headless',
  })
  await ticks[0]()
  assert.deepEqual(pending.get('perm_7').settled, [
    { behavior: 'allow', message: 'Attention store answer (item i1).' },
  ])
})

test('a board answer on a TAB worker park is refused, and records no attempt', async () => {
  // Two claims, and the second is what the guard's PLACEMENT exists for. The 2026-10-01
  // narrowing means a tab worker's card renders no answering control at all, so a board
  // answer for one is a defect rather than an answer — it must not settle. And because the
  // guard runs BEFORE `selectParked`, it must not record a `failed` attempt either: this
  // arm never tried to carry it. `pendingEntries: []` is the configuration that makes the
  // point — with the guard placed after `selectParked`, this item reached that function's
  // owner branch and wrote a carrier failure for a delivery nothing attempted.
  const logged = []
  const { ticks, posts } = harness({
    open: [{ item_id: 'i1', answer_mechanism: 'permission' }],
    items: { i1: { item_id: 'i1', state: 'answered', decision: 'allow', answer_mechanism: 'permission', producer_id: 'sess-1' } },
    agentMode: 'interactive',
    pendingEntries: [],
    log: (m) => logged.push(m),
  })
  await ticks[0]()
  assert.match(logged.join('\n'), /not arm-delivered/)
  assert.equal(posts.length, 0)
})

test('producingAgent resolves the producer to this server own agent, or nothing', () => {
  const agents = agentsOf(['a1', 'sess-1'])
  assert.deepEqual(producingAgent({ item: { producer_id: 'sess-1' }, agents }), { id: 'a1', sessionId: 'sess-1' })
  for (const item of [{ producer_id: 'sess-other' }, { producer_id: '' }, {}]) {
    assert.equal(producingAgent({ item, agents }), undefined, `expected undefined for ${JSON.stringify(item)}`)
  }
})

test('an owned session with no park to settle records the attempt as failed', async () => {
  // This server holds the producing session, so it is the carrier; the park it was
  // aimed at is gone, so an attempt WAS made and failed — not `never_attempted`.
  const { ticks, posts } = harness({
    open: [{ item_id: 'i1', answer_mechanism: 'permission' }],
    items: { i1: { item_id: 'i1', state: 'answered', decision: 'allow', resolved_by: 'sess-abc', answer_mechanism: 'permission', producer_id: 'sess-1' } },
    pendingEntries: [],
  })
  await ticks[0]()
  assert.equal(posts.length, 1)
  assert.equal(posts[0].body.outcome, 'failed')
})

test('an answer for a session another server holds records nothing', async () => {
  // ⚠️ This spec previously asserted `failed` here, reasoning that "an attempt WAS
  // made". It was not: every supervisor server watches every open permission item,
  // and only the one holding the producing session attempts anything. A non-owner
  // writing `failed` outvotes the owner's real outcome in a one-record-per-item trail.
  const { ticks, posts } = harness({
    open: [{ item_id: 'i1', answer_mechanism: 'permission' }],
    items: { i1: { item_id: 'i1', state: 'answered', decision: 'allow', resolved_by: 'sess-abc', answer_mechanism: 'permission', producer_id: 'sess-2' } },
  })
  await ticks[0]()
  assert.equal(posts.length, 0)
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

// --- the idle cadence ------------------------------------------------------------------
//
// Measured 2026-10-07 on the live board (host `burn`, pid 96646): 46 `supervisor.mjs`
// instances issued ~46 req/s against `/api/1.0/attention` while the store held ZERO
// permission-class items open. The three cases below pin the cadence that replaces that,
// and each one is a way the change could have been written wrong.

test('a server holding no park reads once, then stops reading until the trail is due', async () => {
  const { ticks, gets, clock } = harness({ open: [], pendingEntries: [] })
  await ticks[0]()
  assert.deepEqual(gets, ['/api/1.0/attention'], 'the first read must happen')

  clock.ms += 250
  await ticks[1]()
  assert.equal(gets.length, 1, 'nothing parked, trail not due — no request at all')

  clock.ms += 250
  await ticks[2]()
  assert.equal(gets.length, 1, 'still nothing parked, so still no request')

  // ⚠️ Not zero: `recordAttempt(…, 'failed')` needs the item in `watching`, and only a
  // read populates that. A loop that stopped reading entirely would delete that record
  // silently — which is why the assertion below is 2 and not 1.
  clock.ms += 30000
  await ticks[3]()
  assert.equal(gets.length, 2, 'the idle cadence elapses, so the trail read happens')
})

test('a park that appears is read on the next idle check, not on the idle interval', async () => {
  // The hazard the cadence must not create: `GET /api/1.0/attention` returns OPEN items
  // only, so an item that opens and is answered between two reads is never watched and
  // never settled — its park then auto-denies. A park can appear at any moment, so the
  // loop re-checks `pending` every `idleCheckMs` and reads IMMEDIATELY, which is what
  // bounds that window at 250 ms rather than at the idle interval.
  const { ticks, gets, clock, pending } = harness({ open: [], pendingEntries: [] })
  await ticks[0]()
  assert.equal(gets.length, 1)

  clock.ms += 250
  pending.set('perm_9', { requestId: 'perm_9', agentId: 'a1', settled: [], settle() {} })
  await ticks[1]()
  assert.equal(gets.length, 2, 'a park must be read at once, not one idle interval later')
})

test('a parked prompt keeps the fast cadence', async () => {
  const { ticks, gets, clock, delays } = harness({
    open: [{ item_id: 'i1', answer_mechanism: 'permission' }],
    items: { i1: { item_id: 'i1', state: 'open', answer_mechanism: 'permission' } },
  })
  await ticks[0]()
  clock.ms += 2000
  await ticks[1]()
  clock.ms += 2000
  await ticks[2]()
  const listReads = gets.filter((p) => p === '/api/1.0/attention').length
  assert.equal(listReads, 3, 'a held park reads on every fast tick, as it always did')
  // ⚠️ The DELAY is the assertion, not just the read count. Without this the
  // `parked ? intervalMs : idleCheckMs` expression is unasserted: a constant would
  // leave the read counts above unchanged, because these specs drive ticks by hand.
  assert.deepEqual(
    delays.slice(0, 3),
    [2000, 2000, 2000],
    'a held park must reschedule at the fast interval, never at the idle one',
  )
})

test('a store that throws while idle is retried on the idle cadence, not every idle check', async () => {
  // The one state the placement of `lastReadAtMs` exists for, and the only spec that
  // reaches it: the existing store-error spec (`a store error is logged and swallowed`)
  // runs with a NON-empty `pending`, so it exercises the parked branch and would stay
  // green if `lastReadAtMs` were assigned after the await. Here nothing is parked and
  // the read throws, so a hot retry loop would show up as one read per idle check.
  const logged = []
  const ticks = []
  const gets = []
  const delays = []
  const clock = { ms: 0 }
  startAttentionPoll({
    storeUrl: 'http://store',
    agents: agentsOf(['a1', 'sess-1']),
    pending: pendingOf(),
    log: (m) => logged.push(m),
    nowImpl: () => clock.ms,
    setTimeoutImpl: (fn, delay) => {
      ticks.push(fn)
      delays.push(delay)
      return ticks.length
    },
    clearTimeoutImpl: () => {},
    fetchImpl: async (url) => {
      gets.push(url.replace('http://store', ''))
      throw new Error('boom')
    },
  })
  await ticks[0]()
  assert.equal(gets.length, 1, 'the first read happens and throws')
  assert.match(logged.join('\n'), /attention poll failed/)

  // Four idle checks elapse with nothing parked — a hot retry would read on every one.
  for (let i = 0; i < 4; i += 1) {
    clock.ms += 250
    await ticks[ticks.length - 1]()
  }
  assert.equal(gets.length, 1, 'a throwing store must not be re-read on every idle check')
  // `delays[0]` is the INITIAL schedule (`setTimeoutImpl(tick, intervalMs)` at start-up);
  // the first reschedule the tick itself chose is `delays[1]`.
  assert.equal(delays[1], 250, 'and the loop keeps re-checking cheaply while it backs off')

  // Once the idle cadence elapses the read is attempted again.
  clock.ms += 30000
  await ticks[ticks.length - 1]()
  assert.equal(gets.length, 2, 'the idle cadence still retries the read')
})
