// Tests for the cluster → local heartbeat mirror.
//
// The load-bearing property is the one that is invisible when it works: a FAILED poll must not
// refresh the reachability marker. Refreshing it would tell `live-workers.py` that the cluster
// was readable when it was not, and every cluster stamp would then read as a dead worker rather
// than as "cannot tell" — the answer that permits a resume onto a worker that may be alive
// behind a network fault.
//
// `pollCluster` is async (see `defaultRun` in the module — a synchronous read blocked the loop
// the headless stamp timers share), so every test awaits it. The `run` seam stays injectable
// and may return either a plain object or a promise.

import { mkdtempSync, readFileSync, existsSync, statSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import test from 'node:test'
import assert from 'node:assert/strict'

import { defaultRun, pollCluster, REACHABILITY_FILE } from './cluster-heartbeat.mjs'

const ok = (stamps) => () => ({ status: 0, stdout: JSON.stringify(stamps), stderr: '' })
const fail = (status = 1) => () => ({ status, stdout: '', stderr: 'connection refused' })

function tmp() {
  return mkdtempSync(join(tmpdir(), 'cluster-heartbeat-'))
}

test('a successful poll stamps each session with source=cluster', async () => {
  const dir = tmp()
  const result = await pollCluster({ dir, run: ok([{ session_id: 'a1b2c3d4-1111', age_seconds: 3, pid: null }]) })

  assert.equal(result.ok, true)
  assert.deepEqual(result.stamped, ['a1b2c3d4-1111'])
  const record = JSON.parse(readFileSync(join(dir, 'a1b2c3d4-1111.json'), 'utf8'))
  assert.equal(record.source, 'cluster', 'the readers key their cluster staleness rule on this field')
  assert.equal(record.mode, 'cluster')
})

test('a successful poll refreshes the reachability marker', async () => {
  const dir = tmp()
  await pollCluster({ dir, run: ok([{ session_id: 'a1b2c3d4-1111', age_seconds: 3 }]) })
  assert.equal(existsSync(join(dir, REACHABILITY_FILE)), true)
})

test('a failed poll does NOT refresh the marker, so the stamps it wrote go stale', async () => {
  const dir = tmp()
  await pollCluster({ dir, run: ok([{ session_id: 'a1b2c3d4-1111', age_seconds: 3 }]) })
  const before = statSync(join(dir, REACHABILITY_FILE)).mtimeMs

  const result = await pollCluster({ dir, run: fail() })
  assert.equal(result.ok, false)
  assert.equal(result.reason, 'cluster store unreadable')
  assert.equal(
    statSync(join(dir, REACHABILITY_FILE)).mtimeMs,
    before,
    'refreshing the marker on a failed poll would launder an outage into a death',
  )
})

test('a failed poll never creates a marker where none existed', async () => {
  const dir = tmp()
  await pollCluster({ dir, run: fail() })
  assert.equal(existsSync(join(dir, REACHABILITY_FILE)), false)
})

test('unparseable reader output is a failure, not an empty success', async () => {
  const dir = tmp()
  const result = await pollCluster({ dir, run: () => ({ status: 0, stdout: 'not json', stderr: '' }) })
  assert.equal(result.ok, false)
  assert.equal(result.reason, 'unparseable reader output')
  assert.equal(existsSync(join(dir, REACHABILITY_FILE)), false)
})

test('a stamp with no session id is skipped rather than written under an empty key', async () => {
  const dir = tmp()
  const result = await pollCluster({ dir, run: ok([{ age_seconds: 1 }, { session_id: 'b2c3d4e5-2222', age_seconds: 1 }]) })
  assert.deepEqual(result.stamped, ['b2c3d4e5-2222'])
  assert.equal(existsSync(join(dir, '.json')), false)
})

// The hanging reader is the case that made this read async: `spawnSync` stopped the whole event
// loop for the full 20 s budget, and the headless stamp timers share that loop, so a worker's
// heartbeat was delayed by exactly as long as the cluster took to time out.
//
// A `run` that answers asynchronously is the seam's own shape for it, and it is what makes this
// test discriminating rather than decorative: a synchronous `pollCluster` reads the returned
// promise as the process result, finds no `status` on it, and reports "cluster store
// unreadable" — so this fails on the pre-fix behaviour and cannot pass on a no-op.
test('a reader that answers asynchronously is awaited, not misread as a failed poll', async () => {
  const dir = tmp()
  let ticks = 0
  const ticker = setInterval(() => {
    ticks += 1
  }, 20)

  const hanging = (ms) => () =>
    new Promise((resolve) =>
      setTimeout(
        () => resolve({ status: 0, stdout: JSON.stringify([{ session_id: 'c3d4e5f6-3333', age_seconds: 2 }]), stderr: '' }),
        ms,
      ),
    )

  let result
  try {
    result = await pollCluster({ dir, run: hanging(200) })
  } finally {
    clearInterval(ticker)
  }

  assert.equal(result.ok, true, 'a promise returned by run must be awaited, not mistaken for a failed poll')
  assert.deepEqual(result.stamped, ['c3d4e5f6-3333'])
  assert.equal(existsSync(join(dir, REACHABILITY_FILE)), true)
  // The discriminating assertion is `result.ok` above; this is supporting evidence that the loop
  // stayed free, so the floor is deliberately loose. A tight count would turn a GC pause on a
  // loaded runner into a red build, while the pre-fix behaviour it guards against yields zero.
  assert.ok(ticks >= 3, `the loop kept running while the reader waited (${ticks} ticks in ~200 ms)`)
})

// `defaultRun` is the only code here that can actually spawn and hang, so it carries its own
// coverage rather than being reached only through an injected `run`. Both knobs are injectable
// for exactly that reason: the timeout and the output cap are otherwise 20 s and 1 MB away.
test('defaultRun resolves the spawn result, times out to the spawnSync shape, and caps output', async () => {
  const ok = await defaultRun(['echo', 'hello'])
  assert.equal(ok.status, 0)
  assert.equal(ok.stdout, 'hello\n')

  const missing = await defaultRun(['definitely-not-a-real-binary-xyz'])
  assert.equal(missing.status, null, 'a spawn error resolves rather than rejecting')
  assert.ok(missing.error)

  const timedOut = await defaultRun(['sleep', '5'], { timeoutMs: 50 })
  assert.equal(timedOut.status, null, 'a timed-out child fails the poll, as it did under spawnSync')
  assert.equal(timedOut.error.message, 'ETIMEDOUT')

  const overflowed = await defaultRun([process.execPath, '-e', "process.stdout.write('x'.repeat(5000))"], {
    maxOutputBytes: 1024,
  })
  assert.equal(overflowed.error.message, 'ENOBUFS', 'an oversized reader is bounded, never accumulated')
})

// The SIGTERM→SIGKILL escalation. It had no coverage for two reasons, and the second is why this
// test asserts on the child's disappearance rather than on the returned signal:
//
//  1. `killGraceMs` was the one knob in `defaultRun` that was not injectable, and the existing
//     timeout test lets SIGTERM kill the child, so the 2 s grace was never reached.
//  2. The escalation is invisible in the resolved value. The timeout path calls `kill()` and then
//     `finish(...)` immediately, so `settled` is already true when the child's `close` arrives and
//     the real signal is dropped by the guard. Measured on the pre-change code: a SIGTERM-ignoring
//     child resolves at ~55 ms with `signal: 'SIGTERM'`, is still alive at resolve and at +500 ms,
//     and is reaped only after the grace elapses. The caller can never observe `SIGKILL`.
//
// The observable is therefore the reaping itself — which is the contract the escalation exists for
// ("a reader that traps or ignores SIGTERM is never reaped ... the child and its pipes stay live
// handles for the rest of the server's life"). The timing bound is the discriminating half: with
// the grace ignored the child survives the window at the 2 s default, so asserting only "eventually
// gone" would pass on the pre-change code and prove nothing.
test('defaultRun reaps a SIGTERM-ignoring child inside the injected grace window', async (t) => {
  const pidFile = join(tmp(), 'child.pid')
  const alive = (pid) => {
    try {
      process.kill(pid, 0)
      return true
    } catch {
      return false
    }
  }

  // The child reports its pid to a FILE, not to stdout. `defaultRun` resolves at the timeout with
  // whatever stdout it has collected by then, so reading the pid from the result races the child's
  // own start-up — measured under the full suite, where a 50 ms timeout won that race and returned
  // an empty stdout while the same test passed when its file was run alone. The file is written
  // within a few milliseconds, far inside the 1 s budget below, so this read is deterministic.
  const script =
    "import('node:fs').then((fs) => fs.writeFileSync(" +
    `${JSON.stringify(pidFile)}, String(process.pid))); ` +
    "process.on('SIGTERM', () => {}); setTimeout(() => {}, 60000)"

  const result = await defaultRun([process.execPath, '-e', script], {
    timeoutMs: 1000,
    killGraceMs: 50,
  })
  assert.ok(existsSync(pidFile), 'the child wrote its pid well before the 1 s timeout')
  const pid = Number(readFileSync(pidFile, 'utf8').trim())
  assert.ok(Number.isInteger(pid) && pid > 0, `the child reported its pid (${pid})`)

  // Bound the damage on the failure path. If an assertion below throws, or the escalation regresses
  // and the grace is ignored, the child would otherwise outlive the test by its own 60 s timer and
  // keep the suite's event loop alive — a red test stalling the run instead of failing fast. Costs
  // nothing on the pass path: the pid is already gone, so the kill raises ESRCH and is swallowed.
  t.after(() => {
    try {
      process.kill(pid, 'SIGKILL')
    } catch {
      // already reaped — the expected case
    }
  })

  // The prompt-resolve half: the escalation must not be bought by making callers wait for it.
  assert.equal(result.signal, 'SIGTERM', 'a timed-out reader resolves as it did under spawnSync')
  assert.equal(result.error.message, 'ETIMEDOUT')

  // The escalation half, bounded well under the 2 s default grace: with the grace ignored the child
  // survives this window, with it honoured the child is gone ~100 ms in. Polled rather than slept,
  // so a slow runner reports the bound it actually missed instead of racing a fixed delay.
  const deadline = Date.now() + 1200
  while (alive(pid) && Date.now() < deadline) await new Promise((resolve) => setTimeout(resolve, 20))

  assert.equal(
    alive(pid),
    false,
    'a reader that traps SIGTERM must be reaped inside the grace, or it holds its pipes for the life of the server',
  )
})
