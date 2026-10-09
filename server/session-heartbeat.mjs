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

export const HEARTBEAT_PATH = '/api/1.0/session-heartbeat'

// Seconds any one read may take. The store is local and answers in milliseconds; this is a
// backstop against a wedged server, never a routine path. Mirrors `session-liveness.py`'s
// `_HTTP_TIMEOUT` so the two readers give up at the same point.
export const HEARTBEAT_TIMEOUT_MS = 5000

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
export async function readHeartbeat({ endpoint, fetchImpl = globalThis.fetch, timeoutMs = HEARTBEAT_TIMEOUT_MS } = {}) {
  const base = endpoint === undefined ? config.attentionStoreUrl : endpoint
  if (!base) return null
  const url = `${String(base).replace(/\/+$/, '')}${HEARTBEAT_PATH}`
  let res
  try {
    res = await fetchImpl(url, {
      headers: { Accept: 'application/json' },
      // A wedged store must not hang the spawn path — this read sits in front of every spawn.
      signal: AbortSignal.timeout(timeoutMs),
    })
  } catch {
    return null
  }
  // ⚠️ Only 200 is a read. A 404 is the per-id route's "never posted" and is not an answer to
  // the LIST question, and any other status is the store declining to answer — both are
  // "could not read it" here, which is the shape the caller refuses on.
  if (!res || res.status !== 200) return null
  let body
  try {
    body = await res.json()
  } catch {
    return null
  }
  if (!Array.isArray(body)) return null
  return body
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
