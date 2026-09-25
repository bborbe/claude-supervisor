// Tests for the decision a resume carries.
//
// The refusals carry the weight here, not the acceptance: a decision that is accepted
// wrongly is a gate released by something other than the operator, and that is the one
// failure this module exists to prevent. So the bare-`allow` case and the
// decision-for-a-different-park case are tested as first-class behaviour.

import { test } from 'node:test'
import assert from 'node:assert/strict'
import { validateDecision, renderResumePrompt, BEHAVIORS } from './resume-decision.mjs'

const park = (over = {}) => ({
  request_id: 'perm_3',
  tool: 'Bash',
  input: { command: 'echo hi' },
  blocked_path: '/tmp/x',
  requested_at: '2026-09-25T11:53:01.500Z',
  ...over,
})
const dec = (over = {}) => ({ item_id: 'perm_3', behavior: 'allow', ...over })

test('a decision naming the park on record validates', () => {
  const r = validateDecision({ decision: dec(), parkRecord: park(), sessionId: 's1' })
  assert.equal(r.ok, true)
  assert.equal(r.decision.item_id, 'perm_3')
  assert.equal(r.decision.behavior, 'allow')
})

test('a bare allow with no item_id is refused', () => {
  // The laundering shape, and also the honest-mistake shape. The refusal names what is
  // missing rather than assuming which it is.
  const r = validateDecision({ decision: { behavior: 'allow' }, parkRecord: park(), sessionId: 's1' })
  assert.equal(r.ok, false)
  assert.match(r.error, /item_id/)
})

test('an empty or non-string item_id is refused', () => {
  for (const bad of ['', '   ', 42, null, undefined]) {
    assert.equal(validateDecision({ decision: dec({ item_id: bad }), parkRecord: park() }).ok, false)
  }
})

test('an unknown behavior is refused', () => {
  for (const bad of ['yes', 'ALLOW', '', null, 1]) {
    const r = validateDecision({ decision: dec({ behavior: bad }), parkRecord: park() })
    assert.equal(r.ok, false)
    assert.match(r.error, /behavior/)
  }
  assert.deepEqual(BEHAVIORS, ['allow', 'deny'])
})

test('a decision with no park on record is refused', () => {
  // Resuming a worker that never parked has nothing to settle, and letting it through
  // would attach a decision to a turn that never asked for one.
  const r = validateDecision({ decision: dec(), parkRecord: null, sessionId: 's9' })
  assert.equal(r.ok, false)
  assert.match(r.error, /no parked permission/i)
  assert.match(r.error, /s9/)
})

test('a decision naming a DIFFERENT park is refused', () => {
  // The replay case: a decision recorded for one gate must not be applied to another.
  const r = validateDecision({ decision: dec({ item_id: 'perm_9' }), parkRecord: park(), sessionId: 's1' })
  assert.equal(r.ok, false)
  assert.match(r.error, /perm_9/)
  assert.match(r.error, /perm_3/)
})

test('a non-object decision is refused', () => {
  for (const bad of [null, undefined, 'allow', 42, []]) {
    assert.equal(validateDecision({ decision: bad, parkRecord: park() }).ok, false)
  }
})

test('message defaults to null, never undefined', () => {
  const r = validateDecision({ decision: dec(), parkRecord: park() })
  assert.equal(r.decision.message, null)
  const r2 = validateDecision({ decision: dec({ message: 'ok this once' }), parkRecord: park() })
  assert.equal(r2.decision.message, 'ok this once')
})

test('the resumed prompt states an approval and names the tool', () => {
  const p = renderResumePrompt({ decision: dec(), parkRecord: park() })
  assert.match(p, /APPROVED/)
  assert.match(p, /Bash/)
  assert.match(p, /\/tmp\/x/)
  assert.match(p, /perm_3/)
})

test('the resumed prompt states a denial', () => {
  const p = renderResumePrompt({ decision: dec({ behavior: 'deny' }), parkRecord: park() })
  assert.match(p, /DENIED/)
  assert.equal(p.includes('APPROVED'), false)
})

test('the operator note rides along when present, and is absent when not', () => {
  const withNote = renderResumePrompt({ decision: dec({ message: 'only this file' }), parkRecord: park() })
  assert.match(withNote, /only this file/)
  const without = renderResumePrompt({ decision: dec(), parkRecord: park() })
  assert.equal(without.includes('Their note:'), false)
})

test('the prompt does not re-run the tool by instruction', () => {
  // It reports a decision; it does not authorise a re-run, which is what keeps this
  // from being a way to smuggle an action past the gate that refused it.
  const p = renderResumePrompt({ decision: dec({ behavior: 'deny' }), parkRecord: park() })
  assert.match(p, /Do not re-run/i)
})
