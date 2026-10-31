// Unit tests for the endpoint readers' failure diagnostics.
//
// The property that matters is that the three failures the operator must tell apart are told
// apart: a timeout, a refused socket, and a store answering something unparseable. Until
// 2026-10-19 every one of them reached the spawn refusal as the same sentence — "the live-worker
// count could not be taken" — which is what made the fleet's lost spawns undiagnosable.
//
// These cases are pure: no transport is dialled, and the shapes are the ones Node actually
// produces (`AbortSignal.timeout()` rejects with a `DOMException` named `TimeoutError`, and
// `fetch` wraps a socket failure with the code on `cause`).

import { test } from 'node:test'
import assert from 'node:assert/strict'

import {
  attemptWithRetry,
  classifyReadError,
  describeReadFailure,
  FAILURE_LOG_PREFIX,
  logReadFailure,
  READ_RETRY_DELAY_MS,
} from './read-diagnostics.mjs'

test('a timeout is named a timeout, whichever name the runtime uses', () => {
  for (const name of ['TimeoutError', 'AbortError']) {
    assert.equal(classifyReadError(Object.assign(new Error('aborted'), { name })), 'timeout', name)
  }
})

test('a socket failure is named by its code, including through fetch’s cause wrapper', () => {
  // Node's fetch reports `TypeError: fetch failed`; the useful code lives on `cause`. Reporting
  // the TypeError alone is exactly the collapse this module exists to remove.
  assert.equal(
    classifyReadError(Object.assign(new Error('fetch failed'), { cause: { code: 'ECONNREFUSED' } })),
    'ECONNREFUSED (fetch failed)',
  )
  assert.equal(
    classifyReadError(Object.assign(new Error('connect refused'), { code: 'ECONNREFUSED' })),
    'ECONNREFUSED (connect refused)',
  )
})

test('an unexpected error keeps its name and message rather than being folded into a code', () => {
  assert.equal(classifyReadError(new SyntaxError('Unexpected token < in JSON')), 'SyntaxError: Unexpected token < in JSON')
  assert.equal(classifyReadError(null), 'unknown error')
})

test('the line carries what failed, why, and how long it waited', () => {
  assert.equal(
    describeReadFailure({ what: 'session-heartbeat', cause: 'timeout', elapsedMs: 5003 }),
    'session-heartbeat read failed after 5003 ms: timeout',
  )
})

// ---- the one retry -------------------------------------------------------------------------
//
// ⚠️ The failure this covers is INTERMITTENT — the same `--count` returned UNKNOWN at 21:49 and
// succeeded 20/20 at 22:07. These cases pin that a transient failure is retried, that a permanent
// one still answers failure, and that the retry never turns a failure into a success.

test('a read that succeeds first time is not retried', async () => {
  let calls = 0
  const sleeps = []
  const outcome = await attemptWithRetry(
    async () => {
      calls += 1
      return { ok: true, body: [1] }
    },
    { sleep: async (ms) => sleeps.push(ms) },
  )
  assert.deepEqual(outcome, { ok: true, body: [1] })
  assert.equal(calls, 1)
  assert.deepEqual(sleeps, [])
})

test('a read that fails once is retried after the ~250 ms backoff and succeeds', async () => {
  let calls = 0
  const sleeps = []
  const outcome = await attemptWithRetry(
    async () => {
      calls += 1
      return calls === 1 ? { ok: false, cause: 'timeout' } : { ok: true, body: [1] }
    },
    { sleep: async (ms) => sleeps.push(ms) },
  )
  assert.deepEqual(outcome, { ok: true, body: [1] })
  assert.equal(calls, 2)
  assert.deepEqual(sleeps, [READ_RETRY_DELAY_MS])
})

test('two failed attempts answer the last cause, never a success', async () => {
  let calls = 0
  const outcome = await attemptWithRetry(
    async () => {
      calls += 1
      return { ok: false, cause: `failure ${calls}` }
    },
    { sleep: async () => {} },
  )
  assert.deepEqual(outcome, { ok: false, cause: 'failure 2' })
  assert.equal(calls, 2)
})

test('only a failure that will be retried is reported to the observer', async () => {
  // The last failure is the one the caller reports; announcing it as "retrying" would be a lie.
  const seen = []
  await attemptWithRetry(async () => ({ ok: false, cause: 'x' }), {
    sleep: async () => {},
    onAttemptFailure: (outcome, index, attempts) => seen.push([index, attempts, outcome.cause]),
  })
  assert.deepEqual(seen, [[1, 2, 'x']])
})

test('the default sink writes the prefixed line to stderr', () => {
  // The prefix is what makes the line greppable in the server's stderr, so it is pinned rather
  // than left to a caller's formatting.
  const original = process.stderr.write
  const written = []
  process.stderr.write = (chunk) => {
    written.push(String(chunk))
    return true
  }
  try {
    logReadFailure('session-ledger read failed after 12 ms: HTTP 503')
  } finally {
    process.stderr.write = original
  }
  assert.deepEqual(written, [`${FAILURE_LOG_PREFIX} session-ledger read failed after 12 ms: HTTP 503\n`])
})
