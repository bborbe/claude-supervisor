// Measure the headless stamp's refresh interval while the cluster reader hangs.
//
// Reproduces the server's structure rather than approximating it: ONE `clusterTimer` (as
// `server/supervisor.mjs` creates at boot) and several headless stamp timers (as
// `heartbeat.start()` creates, one per spawned worker) sharing a single event loop.
//
// Several stamp timers at different offsets, because that is what the fleet actually looks
// like: the cluster timer is server-wide and fixed, while each worker's stamp timer starts
// when that worker spawns. The phase between the two is therefore arbitrary per worker, and
// the number the 60 s TTL has to survive is the WORST phase, not the one a single-offset run
// happens to land on.
//
// Two instruments, because the defect has two faces and only one of them is the stamp:
//
//   * **stamp gap** — the stamp file's own write times. The stamp is refreshed exactly at each
//     write, so the inter-write gap IS the peak age `readLive` would compute, and measuring it
//     from inside avoids a sampler that the same block would freeze.
//   * **loop gap** — a 100 ms ticker's worst inter-tick interval. This is the direct measure of
//     whether the read blocks the event loop, and it is what separates the two `--run` shapes.
//
// `--run blocking` (default) is the pre-fix shape: `spawnSync` against a child that outlives the
// 20 s budget, so the stall is genuine. `--run async` is the post-fix shape: the same 20 s wait,
// off the loop. Both are stubs of the `run` seam, so the comparison is like-for-like.
//
// Usage: node scripts/heartbeat-stamp-latency.mjs [--seconds 200] [--run blocking|async]

import { spawnSync } from 'node:child_process'
import { mkdtempSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'

import { pollCluster } from '../server/cluster-heartbeat.mjs'
import { HEARTBEAT_INTERVAL_MS, HEARTBEAT_TTL_MS, listLive, stampRecord } from '../server/heartbeat.mjs'

const argv = process.argv.slice(2)
const argOf = (name, fallback) => {
  const i = argv.indexOf(name)
  return i === -1 ? fallback : argv[i + 1]
}
const SECONDS = Number(argOf('--seconds', 200))
const RUN = argOf('--run', 'blocking')
const OFFSETS_MS = [0, 6_000, 12_000, 18_000, 24_000]

const dir = mkdtempSync(join(tmpdir(), 'heartbeat-latency-'))

// Pre-fix: the real timeout path — the child outlives the 20 s budget, `spawnSync` kills it,
// and the call returns only after a genuine synchronous stall.
const blockingRun = () => {
  const proc = spawnSync('sleep', ['60'], { encoding: 'utf8', timeout: 20_000 })
  return { status: null, signal: proc.signal, stdout: '', error: proc.error }
}

// Post-fix: the identical 20 s wait and the identical timed-out result shape, off the loop.
const asyncRun = () =>
  new Promise((resolve) =>
    setTimeout(() => resolve({ status: null, signal: 'SIGTERM', stdout: '', error: new Error('ETIMEDOUT') }), 20_000),
  )

const run = RUN === 'async' ? asyncRun : blockingRun

const sessions = OFFSETS_MS.map((offset) => ({ offset, id: `probe-${offset}`, writes: [] }))
const polls = []
const timers = []

// Loop liveness: the worst gap between ticks is how long the loop was unavailable.
const ticks = [Date.now()]
const loopTicker = setInterval(() => ticks.push(Date.now()), 100)
loopTicker.unref?.()

// The readers' own verdict — the symptom, not a proxy for it. A session that `listLive` omits is
// one the fleet cap counts as dead, which is the failure the fix exists to prevent.
const liveTicker = setInterval(() => {
  const live = listLive({ dir })
  if (!Array.isArray(live)) return
  const seen = new Set(live.map((entry) => entry.sessionId))
  for (const session of sessions) {
    if (session.writes.length === 0) continue
    session.liveReads = (session.liveReads ?? 0) + 1
    if (!seen.has(session.id)) session.liveMisses = (session.liveMisses ?? 0) + 1
  }
}, 250)
liveTicker.unref?.()

// Server-wide, created first — as `supervisor.mjs` does at module load.
timers.push(
  setInterval(async () => {
    const started = Date.now()
    const result = await pollCluster({ dir, run })
    polls.push({ started, ended: Date.now(), ok: result.ok })
  }, HEARTBEAT_INTERVAL_MS),
)

// One per "worker", each starting at its own offset.
for (const session of sessions) {
  const start = () => {
    session.writes.push(Date.now())
    stampRecord(dir, { sessionId: session.id, pid: process.pid, mode: 'headless' })
    timers.push(
      setInterval(() => {
        session.writes.push(Date.now())
        stampRecord(dir, { sessionId: session.id, pid: process.pid, mode: 'headless' })
      }, HEARTBEAT_INTERVAL_MS),
    )
  }
  if (session.offset === 0) start()
  else setTimeout(start, session.offset)
}

const gaps = (times) => times.slice(1).map((t, i) => t - times[i])

setTimeout(() => {
  for (const t of timers) clearInterval(t)
  clearInterval(loopTicker)
  clearInterval(liveTicker)

  const loopGaps = gaps(ticks)
  const worstLoop = loopGaps.length ? Math.max(...loopGaps) : 0
  const liveReads = sessions.reduce((n, s) => n + (s.liveReads ?? 0), 0)
  const liveMisses = sessions.reduce((n, s) => n + (s.liveMisses ?? 0), 0)

  console.log(`run shape:           ${RUN}`)
  console.log(`interval / TTL:      ${HEARTBEAT_INTERVAL_MS} / ${HEARTBEAT_TTL_MS} ms`)
  console.log(`cluster polls:       ${polls.length} (all ok=${polls.every((p) => p.ok)})`)
  console.log(`worst loop gap:      ${worstLoop} ms   <- how long the event loop was unavailable`)
  console.log(`listLive misses:     ${liveMisses} of ${liveReads} reads   <- a miss is a worker the cap counts dead`)
  console.log('')
  console.log('offset  writes  gaps(ms)                       max-gap')
  let worst = 0
  for (const s of sessions) {
    const g = gaps(s.writes)
    const max = g.length ? Math.max(...g) : 0
    worst = Math.max(worst, max)
    console.log(
      `${String(s.offset).padStart(6)}  ${String(s.writes.length).padStart(6)}  ` +
        `${g.join(', ').padEnd(30)}  ${max}`,
    )
  }
  console.log('')
  console.log(`WORST stamp gap: ${worst} ms   (TTL ${HEARTBEAT_TTL_MS} ms, margin ${HEARTBEAT_TTL_MS - worst} ms)`)
  console.log(worst >= HEARTBEAT_TTL_MS ? 'VERDICT: TTL BREACHED' : 'VERDICT: within TTL')

  rmSync(dir, { recursive: true, force: true })
  process.exit(0)
}, SECONDS * 1000)
