// Every branch of the end-of-life decision, because the caller cannot be tested.
//
// `supervisor.mjs` connects a stdio server at load, so the predicate that decides whether a
// headless worker is stopped would otherwise be unreachable from any test — and an untested
// predicate here means the behaviour a whole change exists to deliver is asserted nowhere.
//
// ⚠️ The keep-alive branches are the ones that matter most, and they are asserted individually
// rather than as "not end". A wrong stop orphans the card a person is about to answer, which is
// the defect this change exists to prevent; a wrong keep costs one idle process. The asymmetry is
// why each reason is pinned by name instead of collapsing to `false`.

import { test } from 'node:test'
import assert from 'node:assert/strict'
import { armZeroTurnGrace, shouldEndIdleWorker } from './idle-worker.mjs'

const idleSession = { state: 'idle', pending_turns: 0, question: null }

test('an idle worker with a closed turn and nothing parked is ended', () => {
  assert.deepEqual(shouldEndIdleWorker({ session: idleSession, cards: [] }), {
    end: true,
    reason: 'idle-with-nothing-pending',
  })
})

test('an unreadable wrapper keeps the worker', () => {
  for (const session of [null, undefined]) {
    assert.equal(shouldEndIdleWorker({ session, cards: [] }).end, false)
    assert.equal(shouldEndIdleWorker({ session, cards: [] }).reason, 'unreadable-wrapper')
  }
})

test('a wrapper record missing state or pending_turns is reported, not treated as idle', () => {
  // The failure this branch exists for: an older wrapper omits the fields, every check reads
  // falsy, and the end-of-life silently never fires — so the idle-worker leak survives the very
  // fix that exists to remove it, with nothing in the log to say so.
  for (const session of [
    { pending_turns: 0, question: null },
    { state: 'idle', question: null },
    {},
  ]) {
    const verdict = shouldEndIdleWorker({ session, cards: [] })
    assert.equal(verdict.end, false)
    assert.equal(verdict.reason, 'wrapper-record-incomplete')
  }
})

test('a worker that is not idle is kept', () => {
  for (const state of ['generating', 'starting', 'exited']) {
    const verdict = shouldEndIdleWorker({ session: { ...idleSession, state }, cards: [] })
    assert.equal(verdict.end, false)
    assert.equal(verdict.reason, 'not-idle')
  }
})

test('a worker with a turn still in flight is kept', () => {
  // A `result` can arrive MID-turn, so this is the branch that stops a mid-turn result from
  // ending a worker that is still generating.
  const verdict = shouldEndIdleWorker({ session: { ...idleSession, pending_turns: 1 }, cards: [] })
  assert.equal(verdict.end, false)
  assert.equal(verdict.reason, 'turn-in-flight')
})

test('a worker parked on a permission question is kept', () => {
  const verdict = shouldEndIdleWorker({
    session: { ...idleSession, question: { request_id: 'req-1' } },
    cards: [],
  })
  assert.equal(verdict.end, false)
  assert.equal(verdict.reason, 'parked-question')
})

test('a worker with an open attention card is kept', () => {
  // The criterion this branch serves: a card park is not a wrapper `question`, so without this
  // check the end-of-life would stop the worker and orphan the card a person is about to answer.
  const verdict = shouldEndIdleWorker({
    session: idleSession,
    cards: [{ item_id: 'abc', answer_mechanism: 'attention-answer' }],
  })
  assert.equal(verdict.end, false)
  assert.equal(verdict.reason, 'open-card')
})

test('an unreadable attention store keeps the worker', () => {
  // `null` from the store is "could not read", never "no cards" — see open-cards.mjs. Reading it
  // as empty would stop a worker that might be holding a live card.
  for (const cards of [null, undefined]) {
    const verdict = shouldEndIdleWorker({ session: idleSession, cards })
    assert.equal(verdict.end, false)
    assert.equal(verdict.reason, 'unreadable-cards')
  }
})

test('a zero-turn last result keeps the worker even when the wrapper reads idle with nothing pending', () => {
  const session = { state: 'idle', pending_turns: 0 }
  assert.deepEqual(
    shouldEndIdleWorker({ session, cards: [], lastResult: { num_turns: 0 } }),
    { end: false, reason: 'zero-turn-result' },
  )
  assert.equal(shouldEndIdleWorker({ session, cards: [], lastResult: { num_turns: 3 } }).end, true)
})

test('an error result with num_turns 0 is not kept — a CLI that failed before its first turn still ends', () => {
  const session = { state: 'idle', pending_turns: 0 }
  assert.equal(shouldEndIdleWorker({ session, cards: [], lastResult: { num_turns: 0, is_error: true } }).end, true)
})

test('a zero-turn result never masks a more specific keep reason', () => {
  const zero = { num_turns: 0 }
  assert.equal(shouldEndIdleWorker({ session: { state: 'running', pending_turns: 1 }, cards: [], lastResult: zero }).reason, 'not-idle')
  assert.equal(shouldEndIdleWorker({ session: { state: 'idle', pending_turns: 1 }, cards: [], lastResult: zero }).reason, 'turn-in-flight')
  assert.equal(shouldEndIdleWorker({ session: { state: 'idle', pending_turns: 0, question: { id: 'q' } }, cards: [], lastResult: zero }).reason, 'parked-question')
  assert.equal(shouldEndIdleWorker({ session: { state: 'idle', pending_turns: 0 }, cards: [{ id: 'c' }], lastResult: zero }).reason, 'open-card')
  assert.equal(shouldEndIdleWorker({ session: { state: 'idle' }, cards: [], lastResult: zero }).reason, 'wrapper-record-incomplete')
})

test('once the zero-turn grace has expired, a still-idle session is ended', () => {
  const session = { state: 'idle', pending_turns: 0 }
  assert.deepEqual(
    shouldEndIdleWorker({ session, cards: [], lastResult: { num_turns: 0 }, zeroTurnGraceExpired: true }),
    { end: true, reason: 'idle-with-nothing-pending' },
  )
  // ...but grace expiry never overrides a live keep reason.
  assert.equal(shouldEndIdleWorker({ session: { state: 'running', pending_turns: 1 }, cards: [], lastResult: { num_turns: 0 }, zeroTurnGraceExpired: true }).end, false)
})

// Drives the timer with a captured callback instead of a real clock.
function fakeTimer() {
  const t = { fn: null, ms: null }
  t.setTimer = (fn, ms) => { t.fn = fn; t.ms = ms; return {} }
  return t
}

test('the zero-turn grace timer re-runs the full idle decision with the grace expired', async () => {
  const agent = { id: 'a', status: 'running', result: { num_turns: 0 } }
  const calls = []
  const t = fakeTimer()
  armZeroTurnGrace({ agent, recheck: (...args) => calls.push(args), graceMs: 5, setTimer: t.setTimer })
  assert.equal(t.ms, 5)
  assert.equal(calls.length, 0)
  await t.fn()
  assert.equal(calls.length, 1)
  assert.equal(calls[0][0], agent)
  assert.deepEqual(calls[0][1], { zeroTurnGraceExpired: true })
})

test('the zero-turn grace timer still fires after the worker settled — it is that path\'s only reaper', async () => {
  const t = fakeTimer()
  const calls = []
  const settled = { id: 'a', status: 'error', result: { num_turns: 0, zero_turn: true } }
  armZeroTurnGrace({ agent: settled, recheck: (...args) => calls.push(args), setTimer: t.setTimer })
  await t.fn()
  assert.equal(calls.length, 1)
  assert.deepEqual(calls[0][1], { zeroTurnGraceExpired: true })
})

test('the zero-turn grace timer is a no-op once a newer result replaced the one it was armed for', async () => {
  const t1 = fakeTimer()
  const calls = []
  const replaced = { id: 'a', status: 'running', result: { num_turns: 0 } }
  armZeroTurnGrace({ agent: replaced, recheck: () => calls.push('r'), setTimer: t1.setTimer })
  replaced.result = { num_turns: 3 }
  await t1.fn()
  assert.deepEqual(calls, [])
})
