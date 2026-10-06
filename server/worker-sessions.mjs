// Live **worker sessions** — the fleet-wide worker target's unit, on the server side.
//
// The target bounds the laptop's load, and that load is every worker the supervisor opened:
// interactive tabs, headless in-process workers, and cluster workers running as pods.
//
// **The definition takes one identity store and two liveness channels.** A worker session is
// one that is
//
//   (a) present in the **spawn ledger** (`config.ledgerDir`) — which is what makes it a
//       session this supervisor opened, rather than a manager or one of the operator's own;
//       and
//   (b) **live**, which the registry OR the heartbeat store answers.
//
// Neither liveness channel answers alone, and the reason is structural rather than
// historical:
//
//   * The **registry** (`~/.claude/sessions/<pid>.json`) is pid-keyed and local-only. It sees
//     every session holding a socket — every interactive tab — and its entry is deleted on
//     exit, which is what makes absence meaningful. It cannot see a worker with no pid of its
//     own: a headless worker is an in-process SDK `query()` inside the server, and a cluster
//     worker is a process on another machine.
//   * The **heartbeat store** is what covers those. `supervisor.mjs` stamps a headless worker
//     from its agent loop, and `cluster-heartbeat.mjs`'s `pollCluster()` mirrors cluster
//     sessions into the same store on the server's own timer.
//
// ⚠️ **This is a correction, and the shape of the defect is worth keeping.** Until
// 2026-10-05 the count was *registry ∩ ledger* alone — itself a fix, because the instrument
// before it read the heartbeat store only, which is stamped for headless workers, so it
// answered **0 while 11 interactive tabs were live** (measured 2026-10-01). That fix traded
// one blindness for another: it restored the tabs and lost every worker with no registry
// entry, which is both headless and cluster. Two populations cannot be recovered by fixing
// one channel, so the union is the only reading that counts what the cap exists to bound.
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
import { liveSessionIds } from './liveness.mjs'

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
// The ledger supplies the worker identity; the registry and the heartbeat store supply
// liveness, as a UNION. A session in either liveness channel but not the ledger is a manager
// or one of the operator's own; a session in the ledger and in neither channel has exited.
//
// `null` from either channel is "could not be read", and it is returned rather than folded
// into an empty set: the caller refuses on `null` and opens on `[]`, so collapsing the two
// turns a permissions error into permission to spawn onto live work. The heartbeat store
// carries the same rule one level down — a cluster stamp whose own store could not be read
// is reported `unknown` by `live-workers.py` and is therefore not counted here, while the
// read itself still succeeds.
//
// `status` names whichever channel answered: the registry's own `status` for a session
// holding a socket, the stamp's `mode` for a headless or cluster worker. It is descriptive
// only — nothing decides on it.
export function workerSessions({ registryDir, ledgerDir, heartbeatDir, now } = {}) {
  const ledger = readLedger(ledgerDir)
  if (ledger === null) return null

  // The union's ONE home is `liveness.mjs`, and this counter passes presence explicitly — the
  // helper REQUIRES `isAlive`, so the roster reader and the cap counter cannot drift apart on
  // what "live" means, and the note on why presence is the right reading *here* lives there.
  // Both channels are read once, not once per ledger entry: `checkLiveness` answers for a
  // single id and re-reads the registry on every call, which over a ~1000-record ledger is a
  // directory listing per record.
  const live = liveSessionIds({ registryDir, heartbeatDir, now, isAlive: () => true })
  if (live === null) return null

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
    // ledger record and no registry entry and was excluded *by construction*. Reading the
    // heartbeat store is exactly what removes that accident, so the rule has to be written
    // down for the first time or the union silently un-excludes the population item 5 names.
    if (record.resumed_from) continue
    workers.push({ sessionId, status: live.get(sessionId), label: record.label })
  }
  return workers
}
