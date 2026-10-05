// Fold the cluster heartbeat store into the local one — the bridge that makes a cluster worker
// visible to every local liveness reader.
//
// A cluster worker runs as a pod on another machine: it holds no registry entry (that directory
// is pid-keyed and local-only) and no process on this host, so every local probe finds the same
// nothing for a worker mid-turn and for one that finished an hour ago. It publishes into a
// ConfigMap instead; `scripts/cluster-heartbeat.py` reads that store, and this writes what it
// finds into the local heartbeat store, where the readers already look.
//
// ⚠️ **The reachability marker is the whole reason this file exists, and it is not bookkeeping.**
// When the cluster cannot be read, this mirror stops refreshing the stamps it previously wrote,
// so they go stale — and a stale stamp is indistinguishable from a dead worker. `live-workers.py`
// tells the two apart by this marker: a stale *cluster* stamp whose marker is ALSO stale is
// `UNKNOWN`, never `STALE`, because `STALE` is the answer that permits a resume onto a worker
// that may be alive behind a network fault. Refreshing the marker on every successful poll, and
// leaving it strictly alone on every failure, is what makes that distinction real. A marker
// refreshed on a failed poll would launder an outage into a death, which is the exact inversion
// this store's readers exist to prevent.
//
// ⚠️ **Nothing here decides liveness.** The reader does, from the stamp's age against the TTL —
// this only moves facts across the machine boundary. It deliberately does not sweep stale
// cluster stamps: deleting one would destroy the evidence that a cluster worker existed, and
// `heartbeat.mjs`'s `sweepStale` already collects anything past the TTL.

import { spawnSync } from 'node:child_process'
import { mkdirSync, renameSync, writeFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

import { config } from './config.mjs'
import { REACHABILITY_FILE, stampRecord } from './heartbeat.mjs'

// The marker's name has one home — `heartbeat.mjs`, which owns the store and now skips the
// marker when it lists stamps. Re-exported here so this module's consumers are unchanged.
export { REACHABILITY_FILE }

const HERE = dirname(fileURLToPath(import.meta.url))
export const READER = join(HERE, '..', 'scripts', 'cluster-heartbeat.py')

function defaultRun(argv) {
  return spawnSync(argv[0], argv.slice(1), { encoding: 'utf8', timeout: 20_000 })
}

// Returns `{ok, reason?, stamped}`. `ok: false` means the cluster was not read — the caller may
// log it, but must not treat it as "no cluster workers are live": that is the reader's job, and
// it reaches the right answer only because the marker was left unrefreshed.
export function pollCluster({
  dir = config.heartbeatDir,
  reader = READER,
  run = defaultRun,
  now = Date.now(),
  fs = { mkdirSync, writeFileSync, renameSync },
} = {}) {
  const proc = run(['python3', reader, '--list', '--json'])
  if (!proc || proc.status !== 0) {
    return { ok: false, reason: 'cluster store unreadable', stamped: [] }
  }

  let stamps
  try {
    stamps = JSON.parse(proc.stdout || '[]')
  } catch {
    return { ok: false, reason: 'unparseable reader output', stamped: [] }
  }
  if (!Array.isArray(stamps)) return { ok: false, reason: 'unexpected reader output', stamped: [] }

  const stamped = []
  for (const stamp of stamps) {
    if (!stamp || typeof stamp.session_id !== 'string' || !stamp.session_id) continue
    stampRecord(
      dir,
      { sessionId: stamp.session_id, pid: stamp.pid ?? null, mode: 'cluster', source: 'cluster' },
      { fs },
    )
    stamped.push(stamp.session_id)
  }

  // Only after the cluster answered. See the header: a marker refreshed on a failed poll would
  // turn an outage into a death verdict.
  fs.mkdirSync(dir, { recursive: true })
  const marker = join(dir, REACHABILITY_FILE)
  const tmp = `${marker}.tmp`
  fs.writeFileSync(tmp, `${JSON.stringify({ at: new Date(now).toISOString(), stamped: stamped.length }, null, 2)}\n`)
  fs.renameSync(tmp, marker)

  return { ok: true, stamped }
}
