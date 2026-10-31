// Live **worker sessions** — the fleet-wide worker target's unit, on the server side.
//
// The target bounds the laptop's load, and that load is every worker the supervisor opened:
// interactive tabs, headless in-process workers, and cluster workers running as pods.
//
// **The definition takes one identity store and one liveness source.** A worker session is
// one that is
//
//   (a) present in the **spawn ledger**, which the **session-ledger endpoint** answers — what makes it a
//       session this supervisor opened, rather than a manager or one of the operator's own;
//       and
//   (b) **live**, which the **session-heartbeat endpoint** answers.
//
// ⚠️ **The liveness source is the endpoint, and the join survived the move.** Until 2026-10-09
// this read two channels — the registry (`~/.claude/sessions/<pid>.json`, pid-keyed and
// local-only, its entry deleted on exit) and the local heartbeat store — as a UNION, because
// neither answered alone: the registry saw every session holding a socket and was structurally
// blind to a worker with no pid of its own (a headless worker is an in-process SDK `query()`
// inside the server; a cluster worker is a process on another machine), while the store covered
// exactly those. The endpoint holds BOTH populations in one store — it serves `source: cluster`
// and `source: mcp-timer` rows beside local ones — so two channels collapse into one read and
// the composition rule goes with them. **Operator ruling 2026-10-09: keep the ledger join, move
// only the liveness source.**
//
// ⚠️ **The count legitimately goes UP, and that is the point of the move.** The registry was
// blind to headless workers, so a fleet that was silently under-counting now counts what it was
// rendering dead. Measured 2026-10-09 on this host: **11** under the registry join, **22** under
// the endpoint. `docs/fleet-surface.md` § Spawn a worker item 5 already carries the warning —
// re-read `spawn.maxConcurrent` before the first sweep after upgrading, because the first tick
// can read **at or over** the target with no error on either side.
//
//   * The ledger's own `status` is **not** a liveness source — measured 2026-10-01, 824 of
//     its 1075 entries read `running` against 26 live registry sessions. It is the durable
//     half, never the live one.
//
// ⚠️ **A ledger record is not by itself proof of "worker".** `spawn_agent(role="manager")`
// writes a record exactly like a worker's, so presence in the ledger alone counts a manager
// against this cap unless the record's own `role` excludes it — which is what the exclusion
// in `workerSessions` now does. ⚠️ **ONLY an explicit `manager` excludes**: a record with no
// `role` (every record written before v0.110.3) reads as a worker, because defaulting the
// other way would flip every pre-field worker into a manager and shrink the population this
// cap exists to bound. Managers started by hand — `/supervisor:open` in a plain wezterm tab —
// write no record at all. `scripts/worker-sessions.py` carries the same definition for shell
// callers.
import { mkdir, readFile, writeFile } from 'node:fs/promises'
import { join } from 'node:path'

import { config } from './config.mjs'
import { liveFromHeartbeat, readHeartbeat } from './session-heartbeat.mjs'
import { ledgerBySessionId, readSessionLedger } from './session-ledger.mjs'

// The last successful count, written on every successful read and used when the live read fails.
//
// ⚠️ **A FILE, not an in-memory value, because the other counter is a short-lived process.**
// `scripts/worker-sessions.py --count` exits after printing, so an in-memory cache could never
// outlive it; a shared file is the only shape both readers can use. The path is the supervisor's
// own state dir — `$XDG_STATE_HOME/claude-supervisor`, or the XDG default under the user's home —
// and `worker-sessions.py` resolves the same one.
//
// ⚠️ **The window is 60 s and the fallback is labelled.** A count from minutes ago is not evidence
// about the fleet now; past the window the refusal stands. And it is never `0`: the cache holds a
// real earlier count, and an absent cache is a refusal.
export const COUNT_CACHE_TTL_MS = 60_000
export const COUNT_CACHE_FILE = 'last-worker-count.json'

export function countCachePath(stateDir = config.stateDir) {
  return join(stateDir, COUNT_CACHE_FILE)
}

// `{ count, ageMs }` when the cache holds a fresh-enough count, else `null`.
//
// Every malformed shape is `null`: a cache written by another tool, a truncated write, a future
// timestamp (a clock step must not make a count look fresh forever). The cache is an optimisation
// for a failure path, so anything it cannot read is simply not a fallback.
export async function readCountCache({ stateDir = config.stateDir, now = Date.now } = {}) {
  try {
    const parsed = JSON.parse(await readFile(countCachePath(stateDir), 'utf8'))
    if (!parsed || typeof parsed !== 'object') return null
    if (!Number.isInteger(parsed.count) || parsed.count < 0) return null
    const at = Date.parse(parsed.at)
    if (!Number.isFinite(at)) return null
    const ageMs = now() - at
    if (ageMs < 0 || ageMs > COUNT_CACHE_TTL_MS) return null
    return { count: parsed.count, ageMs }
  } catch {
    return null
  }
}

// Best-effort: a cache that cannot be written must never fail the count it was recording.
async function writeCountCache(count, { stateDir = config.stateDir, now = Date.now } = {}) {
  try {
    await mkdir(stateDir, { recursive: true })
    await writeFile(
      countCachePath(stateDir),
      JSON.stringify({ count, at: new Date(now()).toISOString() }),
    )
  } catch {
    // Swallowed on purpose — see above.
  }
}

// `{sessionId: record}`, or `null` when the ledger cannot be read.
//
// ⚠️ **The source is the ENDPOINT, and the three-state contract survived the move.** An empty
// store is `{}` — nobody has ever been spawned — which is a different answer from `null`,
// "could not read it". Collapsing them turns a store outage into a confident empty fleet, and
// an empty fleet is the answer that opens past a limit.
//
// ⚠️ **Async, because the endpoint is.** Both callers already await `workerSessions`, and a
// count resolved after the spawn has already spent the budget it protects is the failure the
// awaited read exists to prevent.
export async function readLedger({ endpoint, fetchImpl, onFailure } = {}) {
  const rows = await readSessionLedger({ endpoint, fetchImpl, onFailure })
  if (rows === null) return null
  return Object.fromEntries(ledgerBySessionId(rows))
}

// Live worker sessions as `{ workers, error }` — `workers: null` when a store failed, with
// `error` naming what failed and how long it waited.
//
// The ledger supplies the worker identity; the **session-heartbeat endpoint** supplies
// liveness. A session live at the endpoint but not in the ledger is a manager or one of the
// operator's own; a session in the ledger and not live at the endpoint has exited.
//
// ⚠️ **`workers: null` is "could not be read", and it is returned rather than folded into an
// empty list.** The caller refuses on it and opens on `[]`, so collapsing the two turns a store
// outage into permission to spawn onto live work. That is the same rule the ledger read
// carries one level up: an absent ledger directory is `{}` ("nobody has ever been spawned"),
// which is a different answer from `null`.
//
// ⚠️ **`error` travels WITH a `null` count, and that is the change of 2026-10-19.** A bare `null`
// left the refusal able to say only "the count could not be taken", so a slow store, a refused
// socket and an unparseable body were one indistinguishable failure — measured on 2026-10-19,
// when the fleet lost at least 5 spawns to exactly that message with no cause recorded. The
// readers log every failure and report it here; the refusal prints it.
//
// ⚠️ **Asynchronous, because the endpoint is.** It reads over HTTP rather than the filesystem,
// so both callers (`concurrentLimitError`'s two spawn paths) await it — and they must, because
// a count resolved after the spawn has already spent the budget it protects.
//
// `status` names the store row's `source` — `mcp-timer` for a session posting its own
// heartbeat, `cluster` for a mirrored cluster session. It is no longer the registry's own
// `status`, because the registry is no longer on this path. Descriptive only — nothing decides
// on it.
export async function workerSessions({
  endpoint,
  fetchImpl,
  now = Date.now,
  onFailure,
  stateDir,
} = {}) {
  // ⚠️ `ledgerDir` left the signature with the file read, and a caller still passing it is
  // harmless rather than broken — an extra key on a destructured parameter is ignored. It is
  // named because a silent acceptance would let a reader believe a path is still in force.
  //
  // The failures are collected so the caller can print the cause; each reader still logs it, so a
  // caller that passes no `onFailure` loses the message but not the record.
  const failures = []
  const observe = (message) => {
    failures.push(message)
    onFailure?.(message)
  }
  const cause = (fallback) => failures.join('; ') || fallback

  // ⚠️ A failed read is `workers: null` AND, when the cache is fresh enough, a `stale` count the
  // refusal may decide on instead. `stale` is never `0` unless a real earlier read counted 0.
  const failed = async (fallback) => ({
    workers: null,
    error: cause(fallback),
    stale: await readCountCache({ stateDir, now }),
  })

  const ledger = await readLedger({ endpoint, fetchImpl, onFailure: observe })
  if (ledger === null) return failed('the session-ledger endpoint could not be read')

  // Read once, not once per ledger entry: the endpoint answers the whole fleet in one request,
  // where a per-id probe would be ~1000 round trips over this ledger.
  const rows = await readHeartbeat({ endpoint, fetchImpl, onFailure: observe })
  if (rows === null) {
    return failed('the session-heartbeat endpoint could not be read')
  }
  const live = liveFromHeartbeat(rows)

  const workers = []
  for (const [sessionId, record] of Object.entries(ledger)) {
    if (!live.has(sessionId)) continue
    // Auto-resumes are excluded — they answer to the auto-resume gate's own 30-min crash-loop
    // cap, and counting them here would leave a sweep that revived two dead workers unable to
    // start any new one (docs/fleet-surface.md § Spawn a worker item 5). The marker is the
    // ledger's `resumed_from`, the same field `scripts/check-spawn-ledger.py` reads.
    //
    // ⚠️ **This filter is not new behaviour being invented; it is a contract that used to hold
    // by accident.** The exclusion was never implemented in code, and every auto-resume is
    // HEADLESS — the gate hands over path A, which `resumeSupportError` refuses alongside an
    // interactive resolution — so under the old registry-only count a resumed worker held a
    // ledger record and no registry entry and was excluded *by construction*. Reading a channel
    // that can see headless workers is exactly what removes that accident, so the rule has to be
    // written down or the move silently un-excludes the population item 5 names.
    if (record.resumed_from) continue
    // A manager is not a worker. `spawn_agent(role="manager")` writes a ledger record exactly
    // like a worker's, so presence in the ledger alone would count a manager against the cap
    // and push the fleet toward refusing ordinary spawns. ⚠️ **ONLY an explicit `manager`
    // excludes** — never `role !== 'agent'`: a record with no `role` is one written before the
    // field existed, and those are workers and managers alike, so treating absence as "manager"
    // would drop every pre-field worker out of the count this cap exists to enforce. The same
    // rule, and the same reason, is in `scripts/gate-owner-filter.py`'s `is_manager`.
    if (record.role === 'manager') continue
    workers.push({ sessionId, status: live.get(sessionId), label: record.label })
  }
  // Record the count for the next failed read. Best-effort: a cache that cannot be written must
  // not fail a count that succeeded.
  await writeCountCache(workers.length, { stateDir, now })
  return { workers, error: null, stale: null }
}
