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

import { spawn } from 'node:child_process'
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

// ⚠️ Async, and that is the point of this function rather than a style choice. `spawnSync`
// blocked the whole event loop for the full 20 s budget, and the headless stamp timers share
// that loop — so a hanging cluster reader delayed a worker's heartbeat by the same 20 s.
// Measured 2026-10-05: the worst stamp gap was 50 007 ms against a 60 000 ms TTL, ~10 s of
// margin, and anything else blocking the loop spent it. `spawn` + a promise waits the same
// 20 s without stopping the world; the result shape is identical, so `run`'s seam and every
// caller's contract are unchanged.
const RUN_TIMEOUT_MS = 20_000

// `spawnSync` capped a child's output at Node's default `maxBuffer` of 1 MB and failed the call
// past it; `spawn` has no such bound, so the cap is restored here rather than left implicit. It
// is not decoration: without it an oversized reader would grow this process's heap, and it would
// do so on the very event loop the async change exists to keep free.
const RUN_MAX_OUTPUT_BYTES = 1_048_576

// How long a child gets to honour SIGTERM before SIGKILL. Without the escalation a reader that
// traps or ignores SIGTERM is never reaped: `finish`'s `settled` guard drops the later `close`,
// so the child and its pipes stay live handles for the rest of the server's life.
const RUN_KILL_GRACE_MS = 2_000

// Exported, and both knobs injectable, so the spawn path itself is testable. Every other test
// injects `run`, which left the only code that can actually spawn and hang with no coverage —
// and a real 20 s timeout is not something a unit test can wait for.
export function defaultRun(argv, { timeoutMs = RUN_TIMEOUT_MS, maxOutputBytes = RUN_MAX_OUTPUT_BYTES } = {}) {
  return new Promise((resolve) => {
    const child = spawn(argv[0], argv.slice(1), { stdio: ['ignore', 'pipe', 'pipe'] })
    let stdout = ''
    let stderr = ''
    let bytes = 0
    let settled = false
    let timer

    const finish = (result) => {
      if (settled) return
      settled = true
      clearTimeout(timer)
      resolve(result)
    }

    const kill = () => {
      child.kill('SIGTERM')
      const escalation = setTimeout(() => child.kill('SIGKILL'), RUN_KILL_GRACE_MS)
      escalation.unref?.()
    }

    // Past the cap the child is killed and the poll fails, which is what `spawnSync` did on
    // hitting the same bound: a truncated read must never be parsed as a session list.
    const collect = (chunk, toStderr) => {
      if (settled) return
      bytes += chunk.length
      if (bytes > maxOutputBytes) {
        kill()
        finish({ status: null, signal: 'SIGTERM', stdout, stderr, error: new Error('ENOBUFS') })
        return
      }
      if (toStderr) stderr += chunk
      else stdout += chunk
    }

    timer = setTimeout(() => {
      kill()
      finish({ status: null, signal: 'SIGTERM', stdout, stderr, error: new Error('ETIMEDOUT') })
    }, timeoutMs)
    timer.unref?.()

    child.stdout?.on('data', (chunk) => collect(chunk, false))
    child.stderr?.on('data', (chunk) => collect(chunk, true))
    child.on('error', (error) => finish({ status: null, signal: null, stdout, stderr, error }))
    child.on('close', (status, signal) => finish({ status, signal, stdout, stderr }))
  })
}

// Resolves to `{ok, reason?, stamped}`. `ok: false` means the cluster was not read — the caller
// may log it, but must not treat it as "no cluster workers are live": that is the reader's job,
// and it reaches the right answer only because the marker was left unrefreshed.
//
// Async so that a hanging reader cannot block the loop its callers share — see `defaultRun`.
// An injected `run` may be sync or async; `await` covers both.
export async function pollCluster({
  dir = config.heartbeatDir,
  reader = READER,
  run = defaultRun,
  now = Date.now(),
  fs = { mkdirSync, writeFileSync, renameSync },
} = {}) {
  const proc = await run(['python3', reader, '--list', '--json'])
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
