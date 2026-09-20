// Unit tests for the window-id argument.
//
// The case that matters most is the one nobody exercises: an ABSENT window id must stay
// absent. A naive widening — accepting anything that is not `undefined` — turns a dropped
// flag into a flag that silently targets window 0 whenever the caller passed nothing.
// That is the same class of bug as the one being fixed, pointing the other way, and it
// would be invisible for exactly the same reason: the spawn still succeeds.
//
// The regression itself is asserted directly. Before this, the number 0 failed a
// `typeof === 'string'` gate and the flag was dropped; "00" routing correctly was what
// proved the target window is reachable, so 0 must be a target here, not an absence.

import { test } from 'node:test'
import assert from 'node:assert/strict'

import { windowIdArgument } from './window-id.mjs'

test('absent stays absent', () => {
  assert.equal(windowIdArgument(undefined), undefined)
})

test('null stays absent, rather than becoming the string "null"', () => {
  assert.equal(windowIdArgument(null), undefined)
})

test('the number 0 is a target, not an absence', () => {
  assert.equal(windowIdArgument(0), '0')
  assert.notEqual(windowIdArgument(0), undefined)
})

test('a zero-valued id is indistinguishable from a non-zero one', () => {
  assert.equal(windowIdArgument(0), windowIdArgument('0'))
})

test('ids coerce to the string wezterm expects', () => {
  assert.equal(windowIdArgument(1), '1')
  assert.equal(windowIdArgument(99), '99')
  assert.equal(windowIdArgument('5'), '5')
})

test('empty and whitespace pass through for the downstream guard to drop', () => {
  // Emptiness is decided in ONE place — spawnInteractiveAgent's
  // `String(windowId).trim() !== ''` — so this module must not also resolve it.
  assert.equal(windowIdArgument(''), '')
  assert.equal(windowIdArgument('   '), '   ')
})
