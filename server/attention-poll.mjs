// attention-poll.mjs — delivering a store answer to a parked canUseTool promise.
//
// The problem this solves: an operator answers a `permission` item on the attention stack,
// the store records the verdict, and nothing tells the worker's parked prompt about it.
// `answer_permission` is an MCP tool call, so it only exists for a Claude session — which
// is exactly the relay this path exists to remove. This server is stdio and has no inbound
// listener, so nothing can push to it either. It reads.
//
// What this module owns is the *rule*: given a store item, which parked promise does it
// settle? That question is pure — no HTTP, no clock, no store — so it is testable on its
// own. The polling and the settling are the caller's.

import { inputKey } from './policy.mjs'

export const DECISIONS = ['allow', 'deny']

// The `decided_by` value a store-settled park carries.
//
// Distinct from `policy`, `escalated` and `manager` on purpose. A store release is its own
// actor: reusing `manager` would attribute it to a session that never answered, which is
// the false positive `decided_by` exists to prevent — and a park that writes no record at
// all is indistinguishable from a policy allow, which is the attribution gap this closes.
export const STORE_DECIDER = 'store'

// The `permissions.jsonl` record for a park the store released.
//
// Shaped to match what `answer_permission` writes (`server/supervisor.mjs`), so the two are
// comparable in one log: the same keys, with `decided_by` the field that differs. Built
// here rather than at the call site so the value this task exists to introduce is unit-
// testable — `supervisor.mjs` cannot be imported in-process.
export function storeDecisionRecord({ requestId, agentId, decision, park, now = new Date() }) {
  return {
    ts: now.toISOString(),
    agent: agentId,
    tool: park.toolName,
    key: inputKey(park.input),
    matched_rule: null,
    decision,
    decided_by: STORE_DECIDER,
    request_id: requestId,
    latency_ms: now.getTime() - Date.parse(park.requestedAt),
  }
}

// The verdict a store item carries, or a reason it carries none.
//
// A missing decision is a refusal rather than a default, for the same reason the arm
// refuses one: neither an implicit allow nor an implicit deny is safe, and an item that
// reads `answered` with no verdict is not an answer to a gate.
export function decisionOf(item) {
  const decision = item?.decision
  if (decision === undefined || decision === null || decision === '') {
    return { ok: false, reason: 'the item carries no decision, so there is nothing to settle' }
  }
  if (!DECISIONS.includes(decision)) {
    return { ok: false, reason: `the item's decision ${JSON.stringify(decision)} is not one of ${DECISIONS.join('/')}` }
  }
  return { ok: true, decision }
}

// Which parked promise does this item settle?
//
// The join is `item.producer_id` -> the agent this server spawned with that session id ->
// that agent's parked records. It is re-derived rather than carried, because the schema has
// no field for the route (silence 6): an item names its producer, never its parked prompt.
//
// Ambiguity is refused, never guessed. An agent with two parked prompts gives no way to
// tell which one the verdict answers, and settling the wrong one would release a gate the
// operator did not release — so it is reported and left for `answer_permission`.
export function selectParked({ item, agents, pending }) {
  if (item?.answer_mechanism !== 'permission') {
    return { ok: false, reason: `the item is ${item?.answer_mechanism}-class, not permission-class` }
  }
  const producerId = item?.producer_id
  if (typeof producerId !== 'string' || producerId === '') {
    return { ok: false, reason: 'the item names no producer, so it cannot be joined to a park' }
  }
  const agent = [...agents.values()].find((a) => a.sessionId === producerId)
  if (!agent) {
    return {
      ok: false,
      reason: `no agent of this server holds session ${producerId} — a worker spawned by another process has its park in that process`,
    }
  }
  const parked = [...pending.values()].filter((p) => p.agentId === agent.id)
  if (parked.length === 0) {
    return { ok: false, reason: `agent ${agent.id} has no parked prompt to settle` }
  }
  if (parked.length > 1) {
    return {
      ok: false,
      reason: `agent ${agent.id} has ${parked.length} parked prompts (${parked.map((p) => p.requestId).join(', ')}), so which one this verdict answers cannot be told — answer it with answer_permission instead`,
    }
  }
  return { ok: true, requestId: parked[0].requestId, agentId: agent.id }
}

// Start the poll loop, and return a stop function.
//
// A store error is logged and swallowed, never thrown: this loop runs inside a server that
// is serving live sessions, and a store outage must degrade to the behaviour that already
// exists (the park auto-denies at PERMISSION_TIMEOUT_MS) rather than take the server down.
export function startAttentionPoll({
  storeUrl,
  agents,
  pending,
  log = () => {},
  // Called once a park has been released by a store verdict, so the caller can record who
  // decided. Fired AFTER the settle, never before: the decision is the critical path, and a
  // recorder that throws must not be able to withhold it. The caller gets the park record
  // because `settle` removes it from `pending` — this is the last moment its own fields
  // (tool, input, requestedAt) are reachable, and a log record without them is not minable.
  onSettled = () => {},
  intervalMs = 2000,
  fetchImpl = globalThis.fetch,
  setTimeoutImpl = globalThis.setTimeout,
  clearTimeoutImpl = globalThis.clearTimeout,
}) {
  // Items already seen and still awaiting an answer. Bounded by the parks themselves: an
  // entry leaves when the park settles, and every park settles within the timeout.
  const watching = new Set()
  let stopped = false
  let timer = null

  async function getJson(path) {
    const res = await fetchImpl(`${storeUrl}${path}`)
    if (!res.ok) throw new Error(`store returned ${res.status} for ${path}`)
    return res.json()
  }

  async function tick() {
    try {
      const open = await getJson('/api/1.0/attention')
      for (const item of Array.isArray(open) ? open : []) {
        if (item?.answer_mechanism === 'permission') watching.add(item.item_id)
      }
      for (const itemId of [...watching]) {
        // Read by id, not from the list: the list is the render path and carries open
        // items only, while this read is the post-transition one and works on any state.
        const item = await getJson(`/api/1.0/attention/${itemId}`)
        if (item?.state !== 'answered') continue
        watching.delete(itemId)
        const verdict = decisionOf(item)
        if (!verdict.ok) {
          log(`attention item ${itemId} is answered but unsettleable: ${verdict.reason}`)
          continue
        }
        const target = selectParked({ item, agents, pending })
        if (!target.ok) {
          log(`attention item ${itemId} carries ${verdict.decision} but was not delivered: ${target.reason}`)
          continue
        }
        // No re-check that the park is still present: selectParked read `pending` on this
        // same tick with no await in between, so nothing can have settled it. A guard here
        // would be unreachable, and an unreachable guard reads as coverage.
        log(`permission ${target.requestId} ${verdict.decision.toUpperCase()}ED by the attention store (item ${itemId})`)
        const park = pending.get(target.requestId)
        park.settle({
          behavior: verdict.decision,
          message: `Attention store answer (item ${itemId}).`,
        })
        // `settle` deletes the park from `pending` but does not mutate the record, so the
        // fields the caller needs are still readable off the captured reference.
        onSettled({ requestId: target.requestId, agentId: target.agentId, decision: verdict.decision, itemId, park })
      }
    } catch (error) {
      log(`attention poll failed (${error.message}) — parks still auto-deny at the timeout`)
    }
    if (!stopped) timer = setTimeoutImpl(tick, intervalMs)
  }

  timer = setTimeoutImpl(tick, intervalMs)

  return function stop() {
    stopped = true
    clearTimeoutImpl(timer)
  }
}
