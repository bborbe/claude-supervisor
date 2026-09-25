// park-record.mjs — the durable half of a parked permission.
//
// A parked permission lives in `pending`, an in-memory Map: the tool, its full input and
// its reason exist only there, and they die with the process. The spawner exiting is
// exactly the case that matters, so the park must also land on disk. It rides the
// per-worker ledger record, which is already keyed on `session_id` and already outlives
// the process — no new file scheme, no new directory.
//
// Only what cannot be re-derived is carried. The worker's session id is the record's own
// key, so restating it here would only invite the two copies to disagree. The store item
// is deliberately not a carrier: it is swept when its producer exits, so it cannot be
// the durable half.

export const PARK_FIELD = 'parked_permission'

// The park as persisted — a projection of the in-memory `pending` record built at park
// time in `supervisor.mjs` `makeCanUseTool`, never a re-derivation.
export function buildParkRecord({
  requestId,
  toolName,
  input,
  decisionReason = null,
  blockedPath = null,
  requestedAt = null,
}) {
  if (!requestId) throw new Error('buildParkRecord needs a requestId — it is the handle an answer names')
  if (!toolName) throw new Error('buildParkRecord needs a toolName — a park that cannot say what it is asking about is not recoverable')
  return {
    request_id: requestId,
    tool: toolName,
    // The FULL input, not the 200-char prose the hook log truncates to. Carrying the
    // reason this record exists rather than a summary of it is the point.
    input: input ?? null,
    reason: decisionReason,
    blocked_path: blockedPath,
    requested_at: requestedAt ?? new Date().toISOString(),
  }
}

// The patch that clears it.
//
// `null` rather than an omitted key, because `updateRecord` MERGES — an omitted key is
// not a deletion, so a finished worker would keep advertising a park nobody holds.
export const clearParkPatch = () => ({ [PARK_FIELD]: null })
