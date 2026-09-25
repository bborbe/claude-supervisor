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

export const REDACTED = '[REDACTED]'

// Secret-shaped *keys*, mirroring the hook's `_SECRET` in
// `~/.claude/hooks/attention-log.py` — the sibling path that already persists tool
// detail. Same list, so the two cannot drift on what counts as a secret.
const SECRET_KEY = /^(authorization|proxy-authorization|cookie|set-cookie|x-api-key|api[-_]?key|access[-_]?token|refresh[-_]?token|bearer|password|passwd|secret|private[-_]?key|client[-_]?secret)$/i

// A bare bearer token with no `name:` prefix — `Bearer eyJ…` sitting in a command line.
const BEARER = /\bbearer\s+[A-Za-z0-9._~+/=-]{8,}/gi

// Scrub secrets from a tool input while keeping its SHAPE.
//
// Shape matters here in a way it does not for a log line: this record is what a resume
// replays, so flattening the object to a redacted string would trade a leak for a
// resume that no longer works. The value is redacted, never the field — the same choice
// the hook makes and for the same reason: an absent field and a redacted one are not
// the same fact.
//
// A redacted secret cannot be replayed, and that is the correct outcome: a token that
// had to be scrubbed before it could be written is a token a resume must not re-send.
export function redactInput(input) {
  if (typeof input === 'string') return input.replace(BEARER, `Bearer ${REDACTED}`)
  if (Array.isArray(input)) return input.map(redactInput)
  if (input && typeof input === 'object') {
    const out = {}
    for (const [k, v] of Object.entries(input)) {
      out[k] = SECRET_KEY.test(k) ? REDACTED : redactInput(v)
    }
    return out
  }
  return input
}

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
    // The FULL input, not the 200-char prose the hook log truncates to — carrying the
    // reason this record exists rather than a summary of it is the point. Redacted,
    // because this is a new place a tool input lands on disk and the hook that already
    // persists tool detail scrubs it first; "full" and "unredacted" are different
    // properties, and only the first is needed to resume.
    input: redactInput(input ?? null),
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
