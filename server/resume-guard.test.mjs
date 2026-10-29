// Tests for the resume guard.
//
// The point of this module is a negative: which statuses do NOT block a resume. A guard
// that only tests the blocking case cannot tell "checked and cleared" from "never
// checked at all" — so every non-terminal status is exercised, and the parked case is
// the one that regressed in production.

import { test } from 'node:test'
import assert from 'node:assert/strict'
import { findLiveHolder, isFinished, resumeRefusal, TERMINAL_STATUSES } from './resume-guard.mjs'

const worker = (sessionId, status, id = 'a1') => ({ id, sessionId, status })

test('a worker parked on a permission blocks a resume', () => {
  // The regression this module exists for: a parked worker is mid-turn — blocked inside
  // a tool call — but carries `blocked-on-permission`, not `running`. A guard matching
  // only `running` let it through, it read as closed, and it was resumed into two
  // writers on one conversation.
  const holder = findLiveHolder([worker('s1', 'blocked-on-permission')], 's1')
  assert.notEqual(holder, null)
  assert.equal(holder.status, 'blocked-on-permission')
})

test('a running worker blocks a resume', () => {
  assert.notEqual(findLiveHolder([worker('s1', 'running')], 's1'), null)
})

test('an interactive worker blocks a resume', () => {
  // Non-terminal too: it is a live tab session, and resuming it is two writers.
  assert.notEqual(findLiveHolder([worker('s1', 'interactive')], 's1'), null)
})

test('a finished worker does not block a resume', () => {
  assert.equal(findLiveHolder([worker('s1', 'done')], 's1'), null)
  assert.equal(findLiveHolder([worker('s1', 'error')], 's1'), null)
})

test('a live worker on a different session does not block', () => {
  // The join is on sessionId, not on liveness alone.
  assert.equal(findLiveHolder([worker('other', 'running')], 's1'), null)
})

test('the holder is found among finished siblings', () => {
  const agents = [worker('s1', 'done', 'a1'), worker('s2', 'done', 'a2'), worker('s1', 'blocked-on-permission', 'a3')]
  assert.equal(findLiveHolder(agents, 's1')?.id, 'a3')
})

test('nothing to hold returns null, not undefined', () => {
  assert.equal(findLiveHolder([], 's1'), null)
})

test('every status the server assigns is classified', () => {
  // The vocabulary supervisor.mjs/agent-loop.mjs assign, so a new one is a deliberate
  // edit here rather than a silent pass-through.
  for (const live of ['running', 'blocked-on-permission', 'interactive']) {
    assert.equal(isFinished(live), false, `${live} must count as a live holder`)
  }
  for (const finished of ['done', 'error']) {
    assert.equal(isFinished(finished), true, `${finished} must count as finished`)
  }
  assert.deepEqual([...TERMINAL_STATUSES].sort(), ['done', 'error'])
})

// ── resumeRefusal ────────────────────────────────────────────────────────────────────
//
// The refusal is what the resume path returns, and `live` is the only thing that
// separates a dead tab from a live headless worker. Both directions are asserted here:
// the release, and the safety property that must survive it.

const refusalFor = (records, live, sessionId = 's1') =>
  resumeRefusal({ agentRecords: records, sessionId, live })

test('a headless row holds even when liveness says the session is gone', () => {
  // ⚠️ THE SAFETY PROPERTY. A headless worker is an in-process `query()` and the registry
  // cannot always see it — that is why this guard exists at all — so `live: false` is not
  // evidence it is dead. A fix that released on `live === false` alone would put two
  // writers on one conversation, which is worse than the deadlock it removes.
  const records = [{ id: 'agent_9', sessionId: 's1', status: 'running', mode: 'headless' }]
  const refusal = refusalFor(records, false)
  assert.notEqual(refusal, null)
  assert.match(refusal, /worker agent_9 is mid-turn in this server/)
})

test('a dead tab releases the session', () => {
  // The defect this module was extended for: a tab row is `interactive` and nothing ever
  // settles it, so before this release EVERY tab worker blocked a resume of its own
  // session for the spawning server's whole life — live or long closed.
  const records = [{ id: 'agent_3', sessionId: 's1', status: 'interactive', mode: 'interactive' }]
  assert.equal(refusalFor(records, false), null)
})

test('a live tab still holds', () => {
  // The other half of the same branch: the registry sees a live tab, so the release must
  // not fire. `checkLiveness` refuses this case first anyway; this is the second line.
  const records = [{ id: 'agent_3', sessionId: 's1', status: 'interactive', mode: 'interactive' }]
  assert.notEqual(refusalFor(records, true), null)
})

test('an unreadable probe releases nothing, for either mode', () => {
  // `live: null` means neither probe could be read. "Could not tell" is not "confirmed
  // closed", and the failure is asymmetric: a refused resume costs one retry, an
  // unguarded one corrupts a conversation.
  for (const status of ['interactive', 'running', 'blocked-on-permission']) {
    const records = [{ id: 'agent_1', sessionId: 's1', status }]
    assert.notEqual(refusalFor(records, null), null, `${status} must hold on live: null`)
  }
})

test('a rehydrated row holds and names the other process', () => {
  // Its worker lives in the supervisor process that SPAWNED it, so naming "this server"
  // would send the operator to close the wrong thing. It is never released from here:
  // this process cannot see that worker at all.
  const records = [{ id: 'agent_7', sessionId: 's1', status: 'interactive', rehydrated: true }]
  const refusal = refusalFor(records, false)
  assert.notEqual(refusal, null)
  assert.match(refusal, /in the supervisor process that spawned it/)
})

test('a finished row holds nothing', () => {
  for (const status of ['done', 'error']) {
    assert.equal(refusalFor([{ id: 'a1', sessionId: 's1', status }], false), null)
  }
})

test('a row for another session holds nothing', () => {
  assert.equal(refusalFor([{ id: 'a1', sessionId: 'other', status: 'running' }], false), null)
})

test('an empty table holds nothing', () => {
  assert.equal(refusalFor([], false), null)
})

test('the refusal names the holder, so the operator knows what to close', () => {
  const records = [{ id: 'agent_42', sessionId: 's1', status: 'blocked-on-permission' }]
  assert.match(refusalFor(records, false), /worker agent_42 is mid-turn in this server/)
})

test('narrowing the holder lookup to rehydrated rows fails these tests', () => {
  // The removal mode this suite must catch, stated as an assertion rather than a comment:
  // a `findLiveHolder` filtered to `rehydrated` rows returns null for the LOCAL rows above,
  // so every `assert.notEqual(..., null)` in this block goes red. Deletion fails harder —
  // the module would not load. Both modes are covered by the same assertions.
  const local = [{ id: 'agent_9', sessionId: 's1', status: 'running', mode: 'headless' }]
  assert.notEqual(findLiveHolder(local, 's1'), null, 'a local headless row must be findable')
})
