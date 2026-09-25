// Tests for settling the gate a resumed worker re-raises.
//
// The refusals carry the weight: a carried decision that fires on the wrong gate is the
// replay failure this whole path exists to prevent, and an `allow` that slips past the
// live guards is a laundering path with a new spelling. Both are tested as first-class
// behaviour, not as edge cases.

import { test } from 'node:test'
import assert from 'node:assert/strict'
import { buildCarriedDecision, settleFromCarried, mayApplyAllow } from './decision-settle.mjs'

const decision = (over = {}) => ({ item_id: 'perm_4', behavior: 'deny', ...over })
const park = (over = {}) => ({ request_id: 'perm_4', tool: 'Bash', blocked_path: '/tmp/x', ...over })
const carried = (over = {}) => buildCarriedDecision({ decision: decision(), parkRecord: park(), ...over })

test('a carried decision remembers the park it answers', () => {
  const c = carried()
  assert.equal(c.item_id, 'perm_4')
  assert.equal(c.behavior, 'deny')
  assert.equal(c.tool, 'Bash')
  assert.equal(c.blocked_path, '/tmp/x')
  assert.equal(c.message, null)
})

test('with no decision or no park there is nothing to carry', () => {
  assert.equal(buildCarriedDecision({ decision: null, parkRecord: park() }), null)
  assert.equal(buildCarriedDecision({ decision: decision(), parkRecord: null }), null)
})

test('the re-raise of the same tool is answered', () => {
  const s = settleFromCarried(carried(), { toolName: 'Bash', blockedPath: '/tmp/x' })
  assert.notEqual(s, null)
  assert.equal(s.behavior, 'deny')
  assert.equal(s.item_id, 'perm_4')
})

test('a DIFFERENT tool is not answered', () => {
  // The replay guard. Applying a decision to a gate it never answered is the failure the
  // resume validation refuses one level up; this is the same rule at the gate.
  assert.equal(settleFromCarried(carried(), { toolName: 'Write', blockedPath: '/tmp/x' }), null)
})

test('a different blocked path is not answered when the park named one', () => {
  assert.equal(settleFromCarried(carried(), { toolName: 'Bash', blockedPath: '/etc/hosts' }), null)
})

test('a park that named no path matches on tool alone', () => {
  const c = buildCarriedDecision({ decision: decision(), parkRecord: park({ blocked_path: null }) })
  assert.notEqual(settleFromCarried(c, { toolName: 'Bash', blockedPath: '/anything' }), null)
})

test('no carried decision, or no tool being raised, answers nothing', () => {
  assert.equal(settleFromCarried(null, { toolName: 'Bash' }), null)
  assert.equal(settleFromCarried(carried(), { toolName: null }), null)
  assert.equal(settleFromCarried(carried(), {}), null)
})

test('a carried deny is always safe to apply', () => {
  // Refusing can only withhold; a worker that stops is what the operator asked for.
  assert.equal(mayApplyAllow(carried(), { serverWouldAllow: false }), true)
  assert.equal(mayApplyAllow(carried(), {}), true)
})

test('a carried allow is honoured only when the server would have allowed it', () => {
  // Otherwise this becomes a way around the live guards — laundering with a new spelling.
  const allow = buildCarriedDecision({ decision: decision({ behavior: 'allow' }), parkRecord: park() })
  assert.equal(mayApplyAllow(allow, { serverWouldAllow: true }), true)
  assert.equal(mayApplyAllow(allow, { serverWouldAllow: false }), false)
  assert.equal(mayApplyAllow(allow, {}), false)
})

test('nothing carried is never applicable', () => {
  assert.equal(mayApplyAllow(null, { serverWouldAllow: true }), false)
})
