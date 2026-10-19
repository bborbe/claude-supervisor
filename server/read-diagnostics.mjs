// Failure diagnostics for the endpoint readers on the spawn path.
//
// ⚠️ **Every reader used to answer a bare `null`, and that was the whole answer.** A timeout, a
// refused socket and an unparseable body all reached the operator as one sentence — "the
// live-worker count could not be taken" — so a refusal named no cause and the next occurrence was
// undiagnosable without a manual probe. Measured 2026-10-19: `worker-sessions.py --count` returned
// `UNKNOWN — cannot read the session-heartbeat endpoint` at 21:49 and succeeded 20/20 at 22:07, and
// the failure could not be attributed.
//
// The readers now call this module on every failed read: the cause and the elapsed milliseconds go
// to stderr (always), and are handed to an optional `onFailure` observer (the count path collects
// them for `countError`, which the spawn refusal prints).
//
// ⚠️ **A failed read is still `null`, never `[]` or `0`.** This module only makes the failure
// legible; it never changes the three-state contract. A limit that cannot count must not open.

// The prefix every supervisor log line carries, so a failure is greppable in the server's stderr.
export const FAILURE_LOG_PREFIX = '[supervisor]'

// The cause, in the operator's terms — `timeout` / `ECONNREFUSED` / an unexpected error name.
//
// ⚠️ **`AbortSignal.timeout()` is a `DOMException`, not an `Error`**, so `instanceof Error` is the
// wrong test and `name` is the only reliable discriminator: Node rejects with `TimeoutError`, and
// an older runtime spells the same abort `AbortError`. Both are a timeout here.
export function classifyReadError(error) {
  if (!error) return 'unknown error'
  const name = error.name || 'Error'
  if (name === 'TimeoutError' || name === 'AbortError') return 'timeout'
  // Node's `fetch` wraps a socket failure in a generic `TypeError: fetch failed` and puts the
  // useful code on `cause`; a direct socket error carries it at the top level. Read both, because
  // reporting `TypeError` alone is the collapse this module exists to remove.
  const code = error.code || error.cause?.code
  if (code) return error.message ? `${code} (${error.message})` : String(code)
  return error.message ? `${name}: ${error.message}` : name
}

// One line naming what failed, why, and how long it waited before giving up.
export function describeReadFailure({ what, cause, elapsedMs }) {
  return `${what} read failed after ${elapsedMs} ms: ${cause}`
}

// The default sink: stderr, where the server's other diagnostics already go.
//
// ⚠️ **It runs even when a caller passes `onFailure`.** The observer is how the count path gets
// the cause into a refusal; it is not a replacement for the log line, and a caller that collected
// the message without logging would put the cause only in a response the operator may never see.
export function logReadFailure(message) {
  process.stderr.write(`${FAILURE_LOG_PREFIX} ${message}\n`)
}
