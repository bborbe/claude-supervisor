import assert from 'node:assert/strict'
import { test } from 'node:test'

import {
  SHIPPING_ALLOW,
  SHIPPING_PERMISSION_MODE,
  shippingSettings,
  shippingSupportError,
} from './shipping-settings.mjs'

test('a shipping spawn carries acceptEdits', () => {
  assert.equal(shippingSettings(true).permissions.defaultMode, 'acceptEdits')
})

test('a shipping spawn carries the three git verbs and nothing else', () => {
  assert.deepEqual(shippingSettings(true).permissions.allow, [
    'Bash(git add:*)',
    'Bash(git commit:*)',
    'Bash(git push:*)',
  ])
})

// The negative control. Without it, a function that always returned the settings
// would pass every test above — and SC5's whole point is that a NON-shipping
// worker is spawned without them.
test('a non-shipping spawn carries nothing', () => {
  assert.equal(shippingSettings(false), null)
  assert.equal(shippingSettings(undefined), null)
  assert.equal(shippingSettings(), null)
})

// Only `true` is shipping. A truthy string is a caller that meant to opt in and
// mistyped, and reading it as consent would hand commit rights to a worker whose
// caller never clearly asked — the fail-open direction.
test('only a literal true opts in', () => {
  for (const v of ['true', 'yes', 1, {}, []]) {
    assert.equal(shippingSettings(v), null, `${JSON.stringify(v)} must not opt in`)
  }
})

// ⚠️ The mode must stay OUT of the unreachable set. If someone later adds
// `acceptEdits` to POLICY_UNREACHABLE_MODES, this class silently becomes
// unsupervised and every other assertion here still passes.
test('acceptEdits is not a mode that makes the policy unreachable', async () => {
  const { POLICY_UNREACHABLE_MODES } = await import('./mode.mjs')
  assert.ok(
    !POLICY_UNREACHABLE_MODES.includes(SHIPPING_PERMISSION_MODE),
    `${SHIPPING_PERMISSION_MODE} must stay policy-reachable`,
  )
})

test('the returned settings are frozen', () => {
  const s = shippingSettings(true)
  assert.throws(() => { s.permissions.defaultMode = 'bypassPermissions' })
  assert.throws(() => { s.permissions.allow.push('Bash(rm:*)') })
  assert.ok(Object.isFrozen(SHIPPING_ALLOW))
})

// Refused, never silently ignored — the policySupportError rule applied to shipping.
test('shipping on a tab worker is refused', () => {
  assert.match(shippingSupportError({ shipping: true, interactive: true }), /headless worker only/)
})

test('shipping on a cluster worker is refused', () => {
  assert.match(shippingSupportError({ shipping: true, cluster: true }), /local headless worker only/)
})

test('shipping on a local headless worker is accepted', () => {
  assert.equal(shippingSupportError({ shipping: true, interactive: false }), null)
})

test('no shipping is never refused, whatever the target', () => {
  for (const args of [{}, { interactive: true }, { cluster: true }, { shipping: false, interactive: true }]) {
    assert.equal(shippingSupportError(args), null)
  }
})
