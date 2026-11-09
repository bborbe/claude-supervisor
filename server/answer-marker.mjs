// answer-marker.mjs — record WHO answered a worker's question, where the worker's own
// hook can find it.
//
// The defect this closes: the local attention event log records the session that ASKED
// (`session_id` on the open line) and the event that cleared the gate (`closed_by` on the
// close line, an event name such as `PreToolUse`) — never the party that answered. So a
// supervised worker's `AskUserQuestion` settled by its manager reads exactly like one the
// operator answered, and "worker questions that no manager answered" cannot be counted.
//
// The fix is a marker, not a new store. `~/.claude/hooks/attention-log.py` already reads
// `<session>.posted.json` from this same directory to suppress a duplicated closer echo;
// this is the same cross-process shape, so the hook gains one more reader rather than a
// second source of truth.
//
// ⚠️ **The writer is here; the reader is the operator-local hook.** Deploying one without
// the other is a silent no-op in both directions — a marker nobody reads, or a reader whose
// file never exists. They ship as a pair, and the hook's own docstring says so.
//
// ⚠️ **`ts` is epoch SECONDS, not milliseconds.** The reader parses it with `float()` and
// compares against `time.time()`. Milliseconds would read as a date ~50 000 years ahead,
// which the reader rejects as future-dated — the marker would be silently ignored on every
// close, the one failure this file cannot detect from its own side.

import { mkdirSync, writeFileSync, renameSync } from 'node:fs'
import { join } from 'node:path'

// The values the hook accepts. Anything else is ignored there, so an unknown kind here
// would be a write that can never be read — refused at the writer instead.
export const ANSWERER_KINDS = ['manager', 'operator', 'other']

// ⚠️ This module reads no environment variable. The state directory is resolved in
// `config.mjs` (`attentionStateDir`) and passed in, because this repo keeps its whole
// configuration surface enumerable in one place and `config.test.mjs` fails the build on a
// module that reads `process.env` directly.

// One marker per session, single-slot: the hook reads it when the gate closes, which is the
// next event after the answer. A per-request filename would accumulate one file per park and
// nothing would ever remove them.
// A Claude session id — the only thing the hook keys a marker on. Validated before the value
// reaches a filesystem path: an absolute value would make `join` discard the state dir and
// `..` segments would walk out of it.
const SESSION_ID = /^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$/

export function answerMarkerPath({ stateDir, sessionId }) {
  if (typeof sessionId !== 'string' || !SESSION_ID.test(sessionId)) return null
  return join(stateDir, `${sessionId}.answer.json`)
}

// The record, or null when the kind is not one the hook reads.
//
// `answerer_session` is empty for an operator answer and that is meaningful, not missing:
// the board is an ARM, not a session, so there is no id to name. The hook keeps `""` with
// `"operator"` precisely so "the operator answered, unnamed" stays distinguishable from
// "nothing is known" (both fields absent).
export function answerMarker({ answererSession = '', answererKind, now = new Date() }) {
  if (!ANSWERER_KINDS.includes(answererKind)) return null
  return {
    answerer_session: typeof answererSession === 'string' ? answererSession : '',
    answerer_kind: answererKind,
    ts: now.getTime() / 1000,
  }
}

// Write the marker. Best-effort by design, and returns whether it landed.
//
// Never throws: this is a measurement side-channel, and failing to record WHO answered must
// never stop the answer from being delivered. The caller is mid-transition — the park is
// about to settle — so a thrown error here would trade a missing attribution for a stranded
// worker, which is the wrong way round.
export function writeAnswerMarker({
  stateDir,
  sessionId,
  answererSession = '',
  answererKind,
  now = new Date(),
}) {
  const record = answerMarker({ answererSession, answererKind, now })
  if (!record) return false
  const path = answerMarkerPath({ stateDir, sessionId })
  if (!path) return false
  // pid-suffixed temp + rename, the same reason `<sid>.posted.json` does it: a fixed `.tmp`
  // lets two concurrent writers interleave, and a crash between write and rename orphans it.
  const tmp = `${path}.${process.pid}.tmp`
  try {
    mkdirSync(stateDir, { recursive: true })
    writeFileSync(tmp, JSON.stringify(record), 'utf8')
    renameSync(tmp, path)
    return true
  } catch {
    return false
  }
}
