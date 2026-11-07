// park-refusal.mjs — what `answer_permission` says when the request is not this server's park.
//
// `pending` is ONE server process's Map. A park another supervisor server holds is not in it,
// and the old answer — `no pending permission <id> (already answered, or expired)` — named
// neither where the request lives nor how to reach it, so the manager that hit it had nothing
// to act on. The remedy is real and shipped: the worker's card is on the attention store, and
// `attention-answer.py` releases it, delivered by the spawning server's own poll.
//
// ⚠️ **The session ledger is the only join available, which is why this is a read and not a
// field lookup.** A store card carries `producer_id` but NO request id (see `Attention
// Permission Answer Path` § The join — carrying the route in `payload` was rejected, not merely
// dispreferred), so a request id cannot address a card directly. A worker's ledger row carries
// BOTH halves: its own `session_id`, which IS the card's `producer_id`, and
// `spawner_session_id`, the manager that opened it — and the park itself rides that row as
// `parked_permission.request_id` (`park-record.mjs`). The row is what turns a request id into
// an owner, and naming that owner is the whole point: a manager told only "no such permission"
// cannot tell a stale id from a sibling's live gate.
//
// A row that cannot be found still gets the route. An unreadable ledger and a request already
// settled here are not distinguishable at this point, and the route is the right advice for
// both — never a bare "no such permission".

// The one spelling of the remedy, so the message and the docs cannot drift on it.
export const ANSWER_ROUTE = 'attention-answer.py answer <item_id> --decision allow|deny'

// The ledger row whose park this request id names, or `null`.
//
// Pure, and deliberately tolerant of a non-array: `readSessionLedger` answers `null` for a
// store it could not read, and folding that into `[]` here would turn an outage into "no such
// row" — the same false-negative shape `open-cards.mjs` guards at its own boundary.
export function findParkRow(rows, requestId) {
  if (!Array.isArray(rows) || !requestId) return null
  return rows.find((row) => row && row.parked_permission && row.parked_permission.request_id === requestId) ?? null
}

// The refusal text. `row` is the park's ledger row, or `null` when none was found.
export function foreignParkMessage(requestId, row) {
  if (!row) {
    return `no pending permission ${requestId} in this server — it is not a park this server holds (it was already answered here, or it belongs to another manager's server). Read the worker's \`open_cards\` from that manager's \`list_agents\` and answer the card with \`${ANSWER_ROUTE}\`.`
  }
  const spawner = row.spawner_session_id || null
  const origin = spawner ? `, spawned by session ${spawner}` : ', with no recorded spawner'
  return `no pending permission ${requestId} in this server — it is not a park this server holds. It belongs to worker ${row.session_id}${origin}; that manager's server holds the gate and settles it. Read the \`item_id\` from that manager's \`list_agents\` \`open_cards\` and answer the card with \`${ANSWER_ROUTE}\` — a cross-session \`answer_permission\` cannot release another server's park.`
}

// The whole refusal: read the ledger, find the row, render the message.
//
// `readLedger` is injected so the lookup is testable without a store, the same seam
// `open-cards.mjs` keeps for its fetch.
export async function foreignParkRefusal({ requestId, readLedger }) {
  const rows = await readLedger()
  return foreignParkMessage(requestId, findParkRow(rows, requestId))
}
