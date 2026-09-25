// resume-decision.mjs — carrying an operator's decision into a resumed worker.
//
// The problem this solves: a headless worker parked on a permission and its spawner died.
// The worker is resumable (`spawn_agent(resume=<id>)`) but `resume` takes a session id
// only — nothing on that path can say what was decided, so a resume could only ever
// re-run the worker with its prompt unchanged, which is not an answer.
//
// What this module does NOT decide is whether the answer was legitimate. It checks that a
// decision is *about this park* — well-formed, and referring to the park the record
// holds. Whether the operator actually gave it is a question about the store item, and it
// is deliberately kept out of here: this module is pure, so the rule is testable without
// a store, a server or a clock.

export const BEHAVIORS = ['allow', 'deny']

// A decision is `{ item_id, behavior, message? }`.
//
// `item_id` is required and load-bearing: it is what ties the decision to the park it
// answers, so a resume cannot carry a decision that belongs to some other gate. A bare
// `allow` with no reference is refused — that is the shape a laundering attempt takes,
// and it is also the shape an honest mistake takes, which is why the refusal names what
// is missing rather than assuming malice.
export function validateDecision({ decision, parkRecord, sessionId = null } = {}) {
  if (!decision || typeof decision !== 'object') {
    return { ok: false, error: 'a decision must be an object carrying item_id and behavior' }
  }
  const { item_id: itemId, behavior, message } = decision
  if (typeof itemId !== 'string' || itemId.trim() === '') {
    return {
      ok: false,
      error: 'a decision must name the item it answers (item_id) — a resume cannot carry a decision that does not say which park it settles',
    }
  }
  if (!BEHAVIORS.includes(behavior)) {
    return { ok: false, error: `a decision's behavior must be one of ${BEHAVIORS.join('/')}, got ${JSON.stringify(behavior)}` }
  }
  if (!parkRecord) {
    return {
      ok: false,
      error: `no parked permission is on record for ${sessionId ?? 'this session'} — a decision can only answer a park, and resuming a worker that never parked has nothing to settle`,
    }
  }
  if (parkRecord.request_id !== itemId) {
    // The tie between the decision and the park. Comparing the ids is what stops a
    // decision recorded for one gate being replayed onto another.
    return {
      ok: false,
      error: `this decision answers ${itemId}, but the park on record for ${sessionId ?? 'this session'} is ${parkRecord.request_id} — refusing to apply a decision to a park it does not name`,
    }
  }
  return { ok: true, decision: { item_id: itemId, behavior, message: message ?? null } }
}

// What the resumed worker is told. The park is restated rather than referenced: the
// worker is a fresh `query()` continuing a conversation, and it cannot read the ledger
// record the supervisor wrote.
//
// A `deny` says the tool was refused and to continue without it; an `allow` says it was
// approved. Neither re-runs the tool — the decision is information for the next turn,
// and the worker decides what to do with it, exactly as it would have when the gate was
// first raised.
export function renderResumePrompt({ decision, parkRecord }) {
  const what = `${parkRecord.tool}${parkRecord.blocked_path ? ` (${parkRecord.blocked_path})` : ''}`
  const verdict =
    decision.behavior === 'allow'
      ? `was APPROVED by the operator`
      : `was DENIED by the operator`
  const why = decision.message ? `\nTheir note: ${decision.message}` : ''
  return [
    `A permission you raised was answered while your session was stopped.`,
    ``,
    `The tool: ${what}`,
    `The request id: ${parkRecord.request_id}`,
    `The decision: ${verdict}.${why}`,
    ``,
    `Continue from where you stopped. Do not re-run that exact tool call unless the decision was an approval and you still need its result.`,
  ].join('\n')
}
