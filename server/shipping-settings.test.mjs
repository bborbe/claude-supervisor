import assert from 'node:assert/strict'
import { test } from 'node:test'

import {
  SHIPPING_ALLOW,
  SHIPPING_MODE_ENV,
  SHIPPING_PERMISSION_MODE,
  launcherHonoursModeEnv,
  shippingSettings,
  shippingSupportError,
  shippingTabEnv,
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
test('shipping on a tab worker with a mode-preserving launcher is accepted', () => {
  assert.equal(
    shippingSupportError({ shipping: true, interactive: true, launcherHonoursModeEnv: true }),
    null,
  )
})

// ⚠️ THE case this check exists for. A launcher that assigns PERMISSION_MODE
// unconditionally overwrites the spawn's value, so the worker parks on the exact gate
// `shipping` removes — while every reply says it was granted. Refused, not warned.
test('shipping on a tab worker whose launcher overwrites the mode is refused', () => {
  const error = shippingSupportError({ shipping: true, interactive: true, launcherHonoursModeEnv: false })
  assert.match(error, /preserves an exported PERMISSION_MODE/)
})

test('shipping on a tab worker is refused when the launcher could not be read', () => {
  // `undefined` is what `launcherPreservesModeEnv` reports for an unreadable launcher.
  assert.match(
    shippingSupportError({ shipping: true, interactive: true }),
    /preserves an exported PERMISSION_MODE/,
  )
})

test('shipping on a cluster worker is refused', () => {
  assert.match(shippingSupportError({ shipping: true, cluster: true }), /local worker only/)
})

test('shipping on a local headless worker is accepted', () => {
  assert.equal(shippingSupportError({ shipping: true, interactive: false }), null)
})

// A headless worker never consults the launcher, so its support must not depend on one.
test('a headless spawn is accepted whatever the launcher does', () => {
  assert.equal(shippingSupportError({ shipping: true, interactive: false, launcherHonoursModeEnv: false }), null)
})

test('no shipping is never refused, whatever the target', () => {
  for (const args of [{}, { interactive: true }, { cluster: true }, { shipping: false, interactive: true }]) {
    assert.equal(shippingSupportError(args), null)
  }
})

// --- the tab path's carrier ---------------------------------------------------

test('a shipping tab spawn exports the mode under the launcher\'s own name', () => {
  assert.deepEqual(shippingTabEnv(true), { [SHIPPING_MODE_ENV]: SHIPPING_PERMISSION_MODE })
  assert.equal(SHIPPING_MODE_ENV, 'PERMISSION_MODE')
})

// The negative control, and the whole point of the marker: a non-shipping tab keeps
// whatever the launcher defaults to.
test('a non-shipping tab spawn exports nothing', () => {
  assert.equal(shippingTabEnv(false), null)
  assert.equal(shippingTabEnv(undefined), null)
  assert.equal(shippingTabEnv(), null)
})

test('only a literal true opts a tab into the mode', () => {
  for (const v of ['true', 'yes', 1, {}, []]) {
    assert.equal(shippingTabEnv(v), null, `${JSON.stringify(v)} must not opt in`)
  }
})

test('the tab env is frozen', () => {
  const env = shippingTabEnv(true)
  assert.throws(() => { env[SHIPPING_MODE_ENV] = 'bypassPermissions' })
})

// --- launcher detection -------------------------------------------------------

test('a launcher that DEFAULTS the mode and forwards it is detected', () => {
  const script = [
    'export PERMISSION_MODE="${PERMISSION_MODE:-auto}" # acceptEdits, auto',
    'claude \\',
    '--permission-mode "${PERMISSION_MODE}" \\',
    '"$@"',
  ].join('\n')
  assert.equal(launcherHonoursModeEnv(script), true)
})

// ⚠️ A commented-out default beside a live unconditional export satisfied the previous bare
// text search, so the worker ran on `auto` while the spawn reported shipping granted.
test('a launcher whose defaulting line is COMMENTED OUT is not honoured', () => {
  const script = [
    '# export PERMISSION_MODE="${PERMISSION_MODE:-auto}"',
    'export PERMISSION_MODE="auto"',
    'claude --permission-mode "${PERMISSION_MODE}"',
  ].join('\n')
  assert.equal(launcherHonoursModeEnv(script), false)
})

// Defaulting the variable without handing it to claude leaves the mode unused.
test('a launcher that defaults the mode but never forwards it is not honoured', () => {
  assert.equal(launcherHonoursModeEnv('export PERMISSION_MODE="${PERMISSION_MODE:-auto}"'), false)
})

// ⚠️ The LAST assignment decides. A defaulting export followed by a later unconditional one
// passes a set-membership test while the second overwrites the spawn's value — the worker
// parks on the very gate shipping removes and every reply still says it was granted.
test('a later unconditional export overrides an earlier defaulting one', () => {
  const script = [
    'export PERMISSION_MODE="${PERMISSION_MODE:-auto}"',
    'export PERMISSION_MODE="auto"',
    'claude --permission-mode "${PERMISSION_MODE}"',
  ].join('\n')
  assert.equal(launcherHonoursModeEnv(script), false)
})

// An assignment after the use cannot influence the value claude was given.
test('an assignment after the --permission-mode use does not count', () => {
  const script = [
    'claude --permission-mode "${PERMISSION_MODE}"',
    'export PERMISSION_MODE="${PERMISSION_MODE:-auto}"',
  ].join('\n')
  assert.equal(launcherHonoursModeEnv(script), false)
})

// The pre-change form, which is what every launcher carried: assigning unconditionally
// wins over the spawn's value, so this must read as NOT honouring.
test('a launcher that ASSIGNS the mode unconditionally is not', () => {
  assert.equal(launcherHonoursModeEnv('export PERMISSION_MODE="auto" # acceptEdits, auto'), false)
})

test('an unreadable or non-text launcher is not honoured', () => {
  for (const v of [null, undefined, '', 42, {}]) {
    assert.equal(launcherHonoursModeEnv(v), false, `${JSON.stringify(v)} must not read as honoured`)
  }
})
