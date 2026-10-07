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

// Whether this answer may release the park: an arm delivered it, OR it is a BOARD answer
// on a HEADLESS park.
//
// ⚠️ The first half is the operator's ruling of 2026-09-26 and is unchanged: a `permission`
// gate releases only on an arm answer. The reason is asymmetry — a permission approval
// EXECUTES a command, so it is the one class a script must not be able to take, while a
// scripted answer to a `message` card costs little and the operator keeps the board flow
// there. The evidence is `resolved_by`, which `attention-answer.py` sources from its OWN
// `CLAUDE_CODE_SESSION_ID` rather than from a caller argument. The board never sends it.
//
// ⚠️ The second half AMENDS that ruling, and it does so by ANSWERING this guard's own
// stated premise rather than by deleting it. Until 2026-10-07 this comment read: "the
// board renders no answer controls on a `permission` card at all and its JS never sends
// `decision`, so a board click could not release a permission gate before this guard
// either. This hardens the path against a DIRECT API POST." ⚠️ **That premise is no longer
// true.** The 2026-10-01 narrowing made the board render Allow / Deny on a HEADLESS
// worker's park and send `decision` with it, so refusing a board answer no longer hardens
// anything — it removes the only answering surface that park has. A headless worker has no
// pane to press (its park lives only in this server's memory), so the board is the one
// place its gate can be answered at all; #296's rule then applies verbatim — a gate the
// product's primary flow cannot satisfy is not a fix, it is the feature deleted.
//
// ⚠️ Scoped to `mode === 'headless'` — POSITIVELY, with no fallback. A TAB worker's card
// renders no answering control at all (the 2026-10-01 narrowing), so a board answer for one
// is a defect rather than an answer and must not release; and a cluster worker's park lives
// in another machine's server, which this one cannot settle anyway. So an agent whose
// `mode` is absent, unrecognised, or anything other than `headless` REFUSES exactly as it
// did before. This is a relaxation for one named case, never a default.
//
// ⚠️ What this still does NOT do, so it is not read as broader than it is: it is NOT a fix
// for the falsified-probe threat — a real Playwright click stores `automation: false` and is
// indistinguishable from the operator's, which the operator accepted as residual, and this
// amendment accepts it for the headless case too. A scripted click on a headless park's
// Allow now releases a gate that executes a command. That is the cost, stated rather than
// implied, and it is the same cost the `message` class already carries.
//
// ⚠️ A missing `resolved_by` on a NON-headless item is refused, never waved through, and the
// refusal is logged with its reason rather than swallowed. An arm run by a child stripped of
// `CLAUDE_CODE_SESSION_ID` therefore cannot release such a park through the store — that is
// the cost of the rule, and it is a real case rather than a bug. It is bounded by
// `answer_permission`, the MCP tool, which releases a park directly and does not travel
// through the store at all, so no worker is stranded.
export function armDelivery({ item, agent }) {
  const resolvedBy = item?.resolved_by
  if (typeof resolvedBy === 'string' && resolvedBy !== '') {
    return { ok: true, resolvedBy }
  }
  if (agent?.mode === 'headless') {
    return { ok: true, boardAnswer: true }
  }
  return {
    ok: false,
    reason:
      'the item carries no resolved_by, so no arm delivered this answer, and a permission gate releases only on an arm answer — the sole exception is a board answer on a headless park, and this item\'s producing agent is not a headless worker',
  }
}

// The agent of THIS server that produced this item, or undefined.
//
// Extracted because two callers resolve the same join and must not disagree about who
// produced an item: `selectParked`, and the delivery guard — which reads the agent's `mode`
// and therefore needs the agent itself. `selectParked` runs only after the guard, so the
// guard cannot borrow its result.
export function producingAgent({ item, agents }) {
  const producerId = item?.producer_id
  if (typeof producerId !== 'string' || producerId === '') return undefined
  return [...agents.values()].find((a) => a.sessionId === producerId)
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
  // `owner` says whether THIS server holds the producing session. Only an owner can have
  // attempted a delivery, so only an owner may record one — see the tick below.
  const producerId = item?.producer_id
  if (typeof producerId !== 'string' || producerId === '') {
    return {
      ok: false,
      owner: false,
      reason: 'the item names no producer, so it cannot be joined to a park',
    }
  }
  const agent = producingAgent({ item, agents })
  if (!agent) {
    return {
      ok: false,
      owner: false,
      reason: `no agent of this server holds session ${producerId} — a worker spawned by another process has its park in that process`,
    }
  }
  const parked = [...pending.values()].filter((p) => p.agentId === agent.id)
  if (parked.length === 0) {
    return { ok: false, owner: true, reason: `agent ${agent.id} has no parked prompt to settle` }
  }
  if (parked.length > 1) {
    return {
      ok: false,
      owner: true,
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
  // How often a server holding NO park still reads, so the delivery trail keeps its
  // `failed` record. See the two-speed note on `tick` below for why this is not zero.
  idleIntervalMs = 30000,
  // How often a server holding no park re-evaluates `pending` — a Map lookup, never a
  // request. This is what bounds the window between a park appearing and the read that
  // can settle it, so it is deliberately far below `intervalMs`.
  idleCheckMs = 250,
  fetchImpl = globalThis.fetch,
  setTimeoutImpl = globalThis.setTimeout,
  clearTimeoutImpl = globalThis.clearTimeout,
  nowImpl = Date.now,
}) {
  // Items already seen and still awaiting an answer. Bounded by the parks themselves: an
  // entry leaves when the park settles, and every park settles within the timeout.
  const watching = new Set()
  let stopped = false
  let timer = null
  // ⚠️ NEGATIVE_INFINITY, not `nowImpl()` and not `0`. The first tick must always read —
  // a loop that skipped its own first read would never populate `watching` and could never
  // settle anything — and `0` does not guarantee that: a caller-supplied clock that starts
  // at 0 (a test's) makes `now - 0` less than `idleIntervalMs`, so the first read would be
  // skipped and every spec would pass for the wrong reason.
  let lastReadAtMs = Number.NEGATIVE_INFINITY

  async function getJson(path) {
    const res = await fetchImpl(`${storeUrl}${path}`)
    if (!res.ok) throw new Error(`store returned ${res.status} for ${path}`)
    return res.json()
  }

  async function postJson(path, body) {
    const res = await fetchImpl(`${storeUrl}${path}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    })
    if (!res.ok) throw new Error(`store returned ${res.status} for ${path}`)
    return res.json()
  }

  // Record what this arm observed about the delivery, on the arm that ATTEMPTED it.
  //
  // ⚠️ This is the permission class's carrier. The message class is delivered by the
  // command wrapping `attention-answer.py`, and `commands/attention-next.md` records
  // nothing on its `DELIVERY: supervisor poll` branch precisely because this runs
  // instead — so if this write is missing, a delivered permission verdict reads as
  // `never_attempted`, the absence the trail exists to make readable.
  //
  // ⚠️ Called AFTER the settle, never before: an attempt is a fact about a delivery
  // that happened, and writing it first would record an attempt that may not occur.
  // A failure here is logged rather than thrown — the gate has already been released,
  // and a missing trail entry must not be reported as a failed delivery.
  async function recordAttempt(itemId, outcome) {
    try {
      await postJson(`/api/1.0/attention/${itemId}/attempt`, {
        carrier: 'supervisor:attention-poll',
        outcome,
      })
    } catch (error) {
      log(`attention item ${itemId} delivery attempt record failed (${outcome}): ${error.message}`)
    }
  }

  // ⚠️ This loop reads at TWO speeds, and both halves are load-bearing.
  //
  // Measured 2026-10-07 on the live board (host `burn`, pid 96646): 46 `supervisor.mjs`
  // instances held ~94 connections and issued ~46 req/s against `/api/1.0/attention`
  // while the store had **zero** permission-class items open, and the board spent
  // 53.8 % of its CPU under `net/http.(*conn).serve`. Two marginal-cost A/B runs against
  // the board's own `process_cpu_seconds_total` — +48.4 req/s → 18.86 %→22.16 % (+3.30 pp)
  // and +59.8 req/s → 21.20 %→23.86 % (+2.66 pp) — price the pollers at **0.044–0.068 pp
  // of CPU per req/s**, i.e. ~2–3 pp of the board's ~19 %.
  //
  // ⚠️ That bound is the honest one, and it is why this change does NOT claim the board's
  // 5 % target: cutting every poller read lands it at ~16 %, still 3× the target. The
  // remainder is per-connection HTTP serving, the netpoll/scheduler and the board's own
  // baseline — see the sibling task that owns the threshold argument.
  //
  // The read is what costs the board, so it is skipped while nothing is parked — but
  // only while nothing is parked AND the trail read is not yet due. Two things must not
  // break, and neither is obvious from the tick body:
  //
  //  1. An answer that lands between two reads is LOST, not delayed. `GET
  //     /api/1.0/attention` returns OPEN items only (see `answered-watch.py`'s header,
  //     which measured this), so an item that opens and is answered between two reads is
  //     never watched, never settled, and its park auto-denies at PERMISSION_TIMEOUT_MS.
  //     A park can appear at any moment, so widening the interval is not available: the
  //     loop re-checks `pending` every `idleCheckMs` — a Map lookup, no HTTP — and reads
  //     IMMEDIATELY once anything is parked. That bounds the window at 250 ms, narrower
  //     than the 2 s it was before this change.
  //
  //  2. The delivery trail must keep its `failed` record. `recordAttempt(itemId,
  //     'failed')` fires for an item this server saw open, was answered, and whose park
  //     is gone — the one record that says an answer reached nobody. It needs the item in
  //     `watching`, and only a read populates that, so a server holding no park still
  //     reads every `idleIntervalMs`. That is 1 read / 15 s per server rather than 1 / s,
  //     not zero: the cut is ~93 %, and dropping to zero would silently delete the record
  //     this file's own test suite pins.
  async function tick() {
    const parked = pending.size > 0
    const now = nowImpl()
    if (!parked && now - lastReadAtMs < idleIntervalMs) {
      // Nothing to settle and the trail read is not due. No HTTP on this pass.
      if (!stopped) timer = setTimeoutImpl(tick, idleCheckMs)
      return
    }
    // Set BEFORE the read, so a store that throws is retried on the idle cadence rather
    // than hammered every `idleCheckMs` — the existing posture is that a store outage
    // degrades to the park's own timeout, not to a hot retry loop.
    lastReadAtMs = now
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
        // A verdict alone does not release a permission gate, and the guard deciding
        // whether THIS one may is now two-sided — an arm answer, or a board answer on a
        // headless park — so it reads the producing agent's `mode` and resolves that agent
        // itself. ⚠️ It stays BEFORE `selectParked`, where it has always been: the `failed`
        // attempt record on that function's owner branch must fire only for an answer the
        // guard would have let through, or it writes a carrier failure for an answer that
        // nothing ever attempted to carry. Kept after `decisionOf` so a verdict-less item
        // is still reported by its own, more specific reason.
        const delivery = armDelivery({ item, agent: producingAgent({ item, agents }) })
        if (!delivery.ok) {
          log(`attention item ${itemId} carries ${verdict.decision} but was not arm-delivered: ${delivery.reason}`)
          continue
        }
        const target = selectParked({ item, agents, pending })
        if (!target.ok) {
          log(`attention item ${itemId} carries ${verdict.decision} but was not delivered: ${target.reason}`)
          // Only the server holding the producing session attempted anything: for it the
          // park it was aimed at is gone, so this is `failed`, not `never_attempted`.
          // ⚠️ Every other server records NOTHING — each watches every open `permission`
          // item, so without this gate the non-owners would outvote the owner's real
          // outcome in a trail that keeps one record per item. Same rule as
          // message-delivery.mjs, where it was measured live on 2026-09-30.
          if (target.owner) await recordAttempt(itemId, 'failed')
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
        await recordAttempt(itemId, 'delivered')
      }
    } catch (error) {
      log(`attention poll failed (${error.message}) — parks still auto-deny at the timeout`)
    }
    // A parked prompt is being waited on, so the next read is due at the fast cadence;
    // with nothing parked the only reason to read again is the trail, on the idle one.
    if (!stopped) timer = setTimeoutImpl(tick, parked ? intervalMs : idleCheckMs)
  }

  timer = setTimeoutImpl(tick, intervalMs)

  return function stop() {
    stopped = true
    clearTimeoutImpl(timer)
  }
}
