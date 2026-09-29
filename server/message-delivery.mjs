// message-delivery.mjs — delivering a store answer to a parked prompt on a `message` card.
//
// The problem this solves: a hook-raised `message` card — the class `attention-watcher.py`
// pushes for a `question`, which is what a closer panel and an `AskUserQuestion` both
// become — is answered on the board, the store records it, and nothing settles the prompt
// the asking worker is parked on. The worker then sits until `PERMISSION_TIMEOUT_MS` and
// exits with its question unanswered, which is the failure `llms.txt` § Install already
// documents by hand for an `AskUserQuestion`: *an `allow` lets the tool run in a tty-less
// session, where it waits ~11 minutes and the worker exits with the question unanswered.*
//
// ⚠️ WHY THIS IS NOT `attention-poll.mjs`, and the split is deliberate rather than tidy.
// That module answers one question: *which parked promise does a store VERDICT settle?*
// Its watch set is permission-only (`:174`) and that is correct as written — a `message`
// card carries no `decision`, so `decisionOf` would refuse it and `selectParked` would
// refuse it again for having no park to join. Widening that set would not make a message
// card work; it would move the same refusal one step later. The two arms differ in the
// rule they apply, not only in the class they accept, so they are two modules.
//
// The rule here is the one `llms.txt` § Install prescribes for an `AskUserQuestion`:
// **answer with `deny` + a `message`**, never `allow`. This arm is that prescription,
// applied from the board instead of by hand from a manager session.
//
// ⚠️ `armDelivery` IS deliberately not applied here, and the omission is the operator's
// 2026-09-26 ruling rather than an oversight — `attention-poll.mjs`'s own comment records
// it: a permission approval EXECUTES a command, so it is the one class a script must not be
// able to take, while a scripted answer to a `message` card costs little and the operator
// keeps the board flow there. So a board click, which sends no `resolved_by`, is a legal
// answer to this arm's cards and an illegal one to the permission arm's.

import { inputKey } from './policy.mjs'
import { STORE_DECIDER } from './attention-poll.mjs'

// The prefix `llms.txt` fixes for an operator answer, and the reason it is not free-form:
// the worker reads the voice off it, and `Manager answer, via supervisor:` used for an
// operator answer would forge a provenance claim the worker is entitled to reject.
export const OPERATOR_PREFIX = 'Operator answer, via supervisor:'

// How far an item may predate the park it would settle, in milliseconds.
//
// Not a fudge factor. The two timestamps are written by different processes — the item's
// `created_at` by the store, the park's `requestedAt` by this server — so an exact ordering
// cannot be asserted, and a small negative allowance is what keeps a same-instant pair from
// being refused. It is small on purpose: the guard exists to stop an answer from an EARLIER
// gate settling a LATER park, and a generous window would reopen exactly that.
export const FRESHNESS_SKEW_MS = 5000

// The operator's words from a store answer, or a reason there are none.
//
// A refusal rather than a default, for the same reason `decisionOf` refuses one: an item
// that reads `answered` with no content is not an answer to anything, and settling a park
// on it would tell the worker it had been answered when nobody said anything.
export function messageTextOf(item) {
  const answers = item?.answers
  if (Array.isArray(answers) && answers.length > 0) {
    const lines = []
    for (const entry of answers) {
      const text = oneAnswer(entry)
      if (text === null) {
        return {
          ok: false,
          reason: `the item carries a per-question answer with no content (${JSON.stringify(entry?.kind ?? null)}), so there is nothing to deliver`,
        }
      }
      // The tab is carried because a multi-question ask is answered per tab, and the
      // worker needs to know which answer belongs to which question. A tab-less entry
      // falls back to the bare text rather than inventing a label.
      const tab = typeof entry?.tab === 'string' && entry.tab.trim() ? entry.tab.trim() : ''
      lines.push(tab ? `${tab}: ${text}` : text)
    }
    return { ok: true, text: lines.join('\n') }
  }
  const text = oneAnswer(item?.answer)
  if (text === null) {
    return {
      ok: false,
      reason: 'the item carries no answer content, so there is nothing to deliver',
    }
  }
  return { ok: true, text }
}

// One answer unit's text, or null when it carries none.
//
// `skip` returns null deliberately: it is the operator declining to answer, which is not a
// thing to type at a worker. `option` prefers `values` because a `multiple` question's
// answer is a set of labels, and reading only `value` would drop every pick but the first.
function oneAnswer(answer) {
  if (!answer || typeof answer !== 'object') return null
  if (answer.kind === 'text') {
    return typeof answer.value === 'string' && answer.value.trim() ? answer.value.trim() : null
  }
  if (answer.kind === 'option') {
    const values = Array.isArray(answer.values)
      ? answer.values.filter((v) => typeof v === 'string' && v.trim())
      : []
    if (values.length > 0) return values.join(', ')
    return typeof answer.value === 'string' && answer.value.trim() ? answer.value.trim() : null
  }
  return null
}

// The message a settled park carries. One home, so the prefix cannot drift from the text.
export function deliveryMessage(text) {
  return `${OPERATOR_PREFIX} ${text}`
}

// Which parked prompt does this answered `message` item settle?
//
// The join is the same one the permission arm makes — `item.producer_id` -> the agent this
// server spawned with that session id -> that agent's parked records — because the schema
// still has no route field. Ambiguity is refused here for the same reason it is refused
// there: settling the wrong park answers a gate the operator did not answer.
export function selectMessagePark({ item, agents, pending }) {
  if (item?.answer_mechanism !== 'message') {
    return {
      ok: false,
      reason: `the item is ${item?.answer_mechanism}-class, not message-class`,
    }
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
    return {
      ok: false,
      reason: `agent ${agent.id} has no parked prompt to settle — a card for an idle session is not this arm's to deliver`,
    }
  }
  if (parked.length > 1) {
    return {
      ok: false,
      reason: `agent ${agent.id} has ${parked.length} parked prompts (${parked.map((p) => p.requestId).join(', ')}), so which one this answer settles cannot be told`,
    }
  }
  const park = parked[0]
  // The freshness guard. Without it an item answered at an EARLIER gate would settle this
  // one: the park is joined by producer, and one producer answers many cards over its life.
  const createdAt = Date.parse(item?.created_at ?? '')
  const requestedAt = Date.parse(park.requestedAt ?? '')
  if (Number.isFinite(createdAt) && Number.isFinite(requestedAt)) {
    if (createdAt < requestedAt - FRESHNESS_SKEW_MS) {
      return {
        ok: false,
        reason: `the item was answered at ${item.created_at}, before the park it would settle was requested at ${park.requestedAt} — refusing to settle a gate this answer does not belong to`,
      }
    }
  }
  return { ok: true, requestId: park.requestId, agentId: agent.id, park }
}

// The `permissions.jsonl` record for a park this arm released.
//
// Shaped to match `storeDecisionRecord` so the two are comparable in one log, with the
// operator's own words carried alongside: the permission arm's record is minable because
// the verdict is in it, and a message release whose text was dropped would be a record of
// an answer nobody can read back. `decision` is `deny` because that is the verdict this arm
// sends — the answer travels in `message`, which is the shape an `AskUserQuestion` takes.
export function storeMessageRecord({ requestId, agentId, park, text, now = new Date() }) {
  return {
    ts: now.toISOString(),
    agent: agentId,
    tool: park.toolName,
    key: inputKey(park.input),
    matched_rule: null,
    decision: 'deny',
    decided_by: STORE_DECIDER,
    message: text,
    request_id: requestId,
    latency_ms: now.getTime() - Date.parse(park.requestedAt),
  }
}

// Start the message arm, and return a stop function.
//
// Same posture as the permission loop: a store error is logged and swallowed, never
// thrown, because this runs inside a server serving live sessions and a store outage must
// degrade to the behaviour that already exists rather than take the server down.
export function startMessageDelivery({
  storeUrl,
  agents,
  pending,
  log = () => {},
  onSettled = () => {},
  intervalMs = 2000,
  fetchImpl = globalThis.fetch,
  setTimeoutImpl = globalThis.setTimeout,
  clearTimeoutImpl = globalThis.clearTimeout,
}) {
  // Items seen open and still awaiting an answer. This is the freshness guard's first half
  // as well as a bound: only a card this process watched go open can be settled, so the
  // whole answered history is never rescanned.
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
        if (item?.answer_mechanism === 'message') watching.add(item.item_id)
      }
      for (const itemId of [...watching]) {
        // Read by id, not from the list: the list is the render path and carries open
        // items only, while this read is the post-transition one and works on any state.
        const item = await getJson(`/api/1.0/attention/${itemId}`)
        if (item?.state !== 'answered') continue
        watching.delete(itemId)
        const content = messageTextOf(item)
        if (!content.ok) {
          log(`attention item ${itemId} is answered but undeliverable: ${content.reason}`)
          continue
        }
        const target = selectMessagePark({ item, agents, pending })
        if (!target.ok) {
          log(`attention item ${itemId} carries an answer but was not delivered: ${target.reason}`)
          continue
        }
        log(`message ${target.requestId} answered by the attention store (item ${itemId})`)
        target.park.settle({ behavior: 'deny', message: deliveryMessage(content.text) })
        onSettled({
          requestId: target.requestId,
          agentId: target.agentId,
          itemId,
          park: target.park,
          text: content.text,
        })
      }
    } catch (error) {
      log(`message delivery failed (${error.message}) — parks still auto-deny at the timeout`)
    }
    if (!stopped) timer = setTimeoutImpl(tick, intervalMs)
  }

  timer = setTimeoutImpl(tick, intervalMs)

  return function stop() {
    stopped = true
    clearTimeoutImpl(timer)
  }
}
