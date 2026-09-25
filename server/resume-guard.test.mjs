// Tests for the resume guard.
//
// The point of this module is a negative: which statuses do NOT block a resume. A guard
// that only tests the blocking case cannot tell "checked and cleared" from "never
// checked at all" — so every non-terminal status is exercised, and the parked case is
// the one that regressed in production.

import { test } from 'node:test'
import assert from 'node:assert/strict'
import { findLiveHolder, isFinished, TERMINAL_STATUSES } from './resume-guard.mjs'

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
