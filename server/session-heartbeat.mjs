// The session-heartbeat endpoint — attention-controller's answer to "which sessions are live?"
//
// ONE reader, and it is the server's first: `grep -rn 'session-heartbeat' server/` returned
// nothing before this file. Until now the mjs twin answered liveness from the session registry
// ∪ the local heartbeat store (`liveness.mjs`), while `scripts/worker-sessions.py` had already
// moved to this endpoint — so the two halves of one definition disagreed about where liveness
// comes from, which is the drift `liveness.mjs`'s own header exists to prevent.
//
// ⚠️ **Why the endpoint rather than the two local channels.** The registry
// (`~/.claude/sessions/<pid>.json`) is pid-keyed and local-only: it sees every session holding a
// socket and is structurally blind to a worker with no pid of its own — a headless worker is an
// in-process SDK `query()` inside this server, and a cluster worker is a process on another
// machine. The local store covers exactly those. The endpoint holds BOTH populations in one
// store — it serves `source: cluster` and `source: mcp-timer` rows beside local ones — so two
// channels and their union rule collapse into one read. Operator ruling 2026-10-09: keep the
// ledger join, move only the liveness source.
//
// ⚠️ **`null` is "could not read it" and is never folded into `[]`.** An empty row list is a
// positive claim that nothing is live anywhere; a transport failure, a non-200, or a body that
// is not an array is the probe failing to run. The caller refuses on `null` and opens on `[]`,
// so collapsing the two turns a store outage into permission to spawn onto live work — the one
// direction a concurrent-worker cap must never fail in.
import { config } from './config.mjs'
import {
  attemptWithRetry,
  classifyReadError,
  describeReadFailure,
  logReadFailure,
} from './read-diagnostics.mjs'

export const HEARTBEAT_PATH = '/api/1.0/session-heartbeat'

// Seconds any one read may take. The store is local, but it is not fast under load: it rebuilds
// and serialises a 318 KB list per request, measured 2026-10-19 at ~0.95 s per concurrent read on a
// loaded host. This is a backstop against a wedged server, never a routine path — 5 s was inside
// the observed tail and refused healthy spawns fleet-wide, so it is 15 s.
//
// ⚠️ **Kept EQUAL to `session-ledger.mjs`'s `SESSION_LEDGER_TIMEOUT_MS` and to
// `session-liveness.py`'s `_HTTP_TIMEOUT` (and `spawn-ledger.py`'s).** The two halves of one count
// giving up at different points makes the failure depend on which store was slower, and the
// refusal then names a channel for a reason that is only about timing.
export const HEARTBEAT_TIMEOUT_MS = 15000

// Every row the store serves, or `null` when the read could not run.
//
// `endpoint` is resolved from `config.attentionStoreUrl` — the same variable and default
// `scripts/session-liveness.py` carries (`ATTENTION_STORE_URL`, else `http://localhost:18080`)
// — so a store moved once moves everywhere and this file gains no second convention to drift.
//
// ⚠️ **`off` is a `null` endpoint, and here that means the cap cannot be counted.** The config
// value is `null` when `SUPERVISOR_ATTENTION_STORE=off`, which is a deliberate opt-out of the
// attention stack. It is NOT treated as "no sessions are live": the store is the fleet's only
// liveness source now, so a deployment that turned it off has no way to answer this question,
// and the honest answer is `null` — which refuses a spawn rather than permitting one. The
// remedy for an operator who wants spawns is to run the store, not to have this reader guess.
//
// ⚠️ **Every failed read is LOGGED with its cause and elapsed milliseconds, and reported to
// `onFailure` when the caller passes one.** Until 2026-10-19 all three failures answered a bare
// `null`, so a refusal could not say whether the store timed out, refused the socket, or served
// something unparseable — measured: `worker-sessions.py --count` returned UNKNOWN at 21:49 and
// succeeded 20/20 at 22:07 with no cause captured for the gap. The log line is what makes the
// next occurrence diagnosable; `onFailure` is what puts the cause into `countError`.
export async function readHeartbeat({
  endpoint,
  fetchImpl = globalThis.fetch,
  timeoutMs = HEARTBEAT_TIMEOUT_MS,
  onFailure,
} = {}) {
  const base = endpoint === undefined ? config.attentionStoreUrl : endpoint
  const startedAt = Date.now()
  const fail = (cause) => {
    const message = describeReadFailure({
      what: 'session-heartbeat',
      cause,
      elapsedMs: Date.now() - startedAt,
    })
    logReadFailure(message)
    onFailure?.(message)
    return null
  }
  if (!base) return fail('no endpoint configured (the attention store is switched off)')
  const url = `${String(base).replace(/\/+$/, '')}${HEARTBEAT_PATH}`
  // The whole read is the attempt, so a retry re-dials rather than re-reading a consumed response.
  const attempt = async () => {
    let res
    try {
      res = await fetchImpl(url, {
        headers: { Accept: 'application/json' },
        // A wedged store must not hang the spawn path — this read sits in front of every spawn.
        signal: AbortSignal.timeout(timeoutMs),
      })
    } catch (error) {
      return { ok: false, cause: classifyReadError(error) }
    }
    // ⚠️ Only 200 is a read. A 404 is the per-id route's "never posted" and is not an answer to
    // the LIST question, and any other status is the store declining to answer — both are
    // "could not read it" here, which is the shape the caller refuses on.
    if (!res || res.status !== 200) {
      return { ok: false, cause: `HTTP ${res ? res.status : 'no response'}` }
    }
    let body
    try {
      body = await res.json()
    } catch (error) {
      return { ok: false, cause: `unparseable body: ${classifyReadError(error)}` }
    }
    if (!Array.isArray(body)) {
      return {
        ok: false,
        cause: `unparseable body: expected a JSON array, got ${body === null ? 'null' : typeof body}`,
      }
    }
    return { ok: true, body }
  }
  const outcome = await attemptWithRetry(attempt, {
    // Each failed attempt that will be retried is logged, so a recovered read is distinguishable
    // from one that never recovered — the two attempts are two lines, not one.
    onAttemptFailure: (failed, index, attempts) => {
      logReadFailure(
        describeReadFailure({
          what: 'session-heartbeat',
          cause: `${failed.cause} (attempt ${index} of ${attempts}, retrying)`,
          elapsedMs: Date.now() - startedAt,
        }),
      )
    },
  })
  if (!outcome.ok) return fail(outcome.cause)
  return outcome.body
}

// The live set as a `Map<sessionId, source>`, from rows `readHeartbeat` returned.
//
// ⚠️ **A row is live only on an explicit `live: true`.** A renamed or absent key must never be
// read as liveness, and a malformed row is skipped rather than allowed to throw — this runs on
// the spawn path, where an uncaught throw is a failed spawn rather than a wrong count.
//
// `source` is descriptive only (`mcp-timer` for a session posting its own heartbeat, `cluster`
// for a mirrored one); nothing decides on it. It replaces the registry's own `status`, which is
// no longer on this path.
export function liveFromHeartbeat(rows) {
  const live = new Map()
  for (const row of rows) {
    if (!row || typeof row !== 'object') continue
    const sessionId = row.session_id
    if (typeof sessionId !== 'string' || !sessionId) continue
    if (row.live !== true) continue
    live.set(sessionId, row.source ?? null)
  }
  return live
}
