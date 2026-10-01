// Live **worker sessions** — the fleet-wide worker target's unit, on the server side.
//
// The target bounds the laptop's load, and that load is the interactive worker tabs. The
// instrument this replaced read the headless heartbeat store, which is stamped only for
// in-process workers (`mode: 'headless'`, written from the agent loop) — so it answered **0
// while 11 interactive workers were live** (measured 2026-10-01). A cap that reads 0 on a
// busy fleet never binds, and a target that reads 0 always proposes; the two must count the
// same population or they disagree with no error on either.
//
// **The definition takes two stores.** A worker session is one that is
//
//   (a) present in the **live registry** (`~/.claude/sessions/<pid>.json`) — the liveness
//       authority, whose entry is deleted on exit; and
//   (b) present in the **spawn ledger** (`config.ledgerDir`) — which is what makes it a
//       session this supervisor opened, rather than a manager or one of the operator's own.
//
// Neither answers alone:
//
//   * The ledger's own `status` is **not** a liveness source — measured 2026-10-01, 824 of
//     its 1075 entries read `running` against 26 live registry sessions. It is the durable
//     half; the registry is the live half.
//   * The registry has **no name field**, so "is this a worker" is not derivable from it.
//
// ⚠️ A manager ever opened through `spawn_agent` would be counted, because it would hold a
// ledger record like any worker. Managers are normally started by hand, which is why all
// four measured here had zero ledger entries — but the limit is real and stated rather than
// hidden. `scripts/worker-sessions.py` carries the same definition for shell callers.
import { readdirSync, readFileSync } from 'node:fs'
import { join } from 'node:path'

import { config } from './config.mjs'
import { readRegistry } from './liveness.mjs'

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
// The registry supplies liveness, the ledger supplies the worker identity. A session in the
// registry but not the ledger is a manager or one of the operator's own; a session in the
// ledger but not the registry has exited.
export function workerSessions({ registryDir, ledgerDir } = {}) {
  const registry = readRegistry(registryDir)
  if (registry === null) return null
  const ledger = readLedger(ledgerDir)
  if (ledger === null) return null

  const workers = []
  for (const entry of registry) {
    const record = ledger[entry.sessionId]
    if (record) {
      workers.push({ sessionId: entry.sessionId, status: entry.status, label: record.label })
    }
  }
  return workers
}
