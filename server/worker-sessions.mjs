// Live **worker sessions** — the fleet-wide worker target's unit, on the server side.
//
// The target bounds the laptop's load, and that load is every worker the supervisor opened:
// interactive tabs, headless in-process workers, and cluster workers running as pods.
//
// **The definition takes one identity store and one liveness source.** A worker session is
// one that is
//
//   (a) present in the **spawn ledger** (`config.ledgerDir`) — which is what makes it a
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
// ⚠️ A manager ever opened through `spawn_agent` would be counted, because it would hold a
// ledger record like any worker. Managers are normally started by hand, which is why all
// four measured here had zero ledger entries — but the limit is real and stated rather than
// hidden. `scripts/worker-sessions.py` carries the same definition for shell callers.
import { readdirSync, readFileSync } from 'node:fs'
import { join } from 'node:path'

import { config } from './config.mjs'
import { liveFromHeartbeat, readHeartbeat } from './session-heartbeat.mjs'

// `{sessionId: record}`, or `null` when the ledger directory cannot be read.
//
// An absent directory is `{}` — nobody has ever been spawned — which is a different answer
// from `null`, "could not read it". Collapsing them turns a permissions error into a
// confident empty fleet, and an empty fleet is the answer that opens past a limit.
export function readLedger(dir = config.ledgerDir) {
  let names
  try {
    names = readdirSync(dir)
  } catch (error) {
    if (error?.code === 'ENOENT') return {}
    return null
  }
  const known = {}
  for (const name of names) {
    if (!name.endsWith('.json')) continue
    try {
      const record = JSON.parse(readFileSync(join(dir, name), 'utf8'))
      if (record && typeof record.session_id === 'string') known[record.session_id] = record
    } catch {
      continue // a half-written record is skipped, not fatal
    }
  }
  return known
}

// Live worker sessions as `[{sessionId, status, label}]`, or `null` when a store failed.
//
// The ledger supplies the worker identity; the **session-heartbeat endpoint** supplies
// liveness. A session live at the endpoint but not in the ledger is a manager or one of the
// operator's own; a session in the ledger and not live at the endpoint has exited.
//
// ⚠️ **`null` is "could not be read", and it is returned rather than folded into an empty
// list.** The caller refuses on `null` and opens on `[]`, so collapsing the two turns a store
// outage into permission to spawn onto live work. That is the same rule the ledger read
// carries one level up: an absent ledger directory is `{}` ("nobody has ever been spawned"),
// which is a different answer from `null`.
//
// ⚠️ **Asynchronous, because the endpoint is.** It reads over HTTP rather than the filesystem,
// so both callers (`concurrentLimitError`'s two spawn paths) await it — and they must, because
// a count resolved after the spawn has already spent the budget it protects.
//
// `status` names the store row's `source` — `mcp-timer` for a session posting its own
// heartbeat, `cluster` for a mirrored cluster session. It is no longer the registry's own
// `status`, because the registry is no longer on this path. Descriptive only — nothing decides
// on it.
export async function workerSessions({ endpoint, ledgerDir, fetchImpl, now } = {}) {
  const ledger = readLedger(ledgerDir)
  if (ledger === null) return null

  // Read once, not once per ledger entry: the endpoint answers the whole fleet in one request,
  // where a per-id probe would be ~1000 round trips over this ledger.
  const rows = await readHeartbeat({ endpoint, fetchImpl })
  if (rows === null) return null
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
    workers.push({ sessionId, status: live.get(sessionId), label: record.label })
  }
  return workers
}
