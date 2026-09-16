// Unit tests for effective-permission-mode resolution.
//
// The load-bearing one is 'a trusted tier outranks a project-tier default': it is the
// regression test for a fail-open that shipped, where the guard read the merged settings
// value and reported the policy reachable while the worker ran under `auto`. These are
// pure-input tests — that a live worker really runs under the mode reported here is an
// integration fact they cannot see, and scripts/policy-drill.py is where that is checked.

import { test } from 'node:test'
import assert from 'node:assert/strict'
import { filterEscalatingDefaultMode } from '@anthropic-ai/claude-agent-sdk'
import { escalatingModeFromTrustedTier, resolveEffectiveMode } from './mode.mjs'

// A ResolvedSettings shape, built by hand: `sources` low→high precedence, `effective` the
// merge, `provenance` which tier supplied each top-level key.
const resolved = (sources, effective = {}, provenance = {}) => ({ effective, provenance, sources })
const tier = (source, defaultMode) => ({ source, settings: { permissions: { defaultMode } } })

test('a trusted tier outranks a project-tier default — the fail-open this fixes', () => {
  // The exact configuration measured 2026-09-16: user tier `auto`, project tier
  // `default`. A live worker ran under `auto`; the merged value says `default`.
  const settings = resolved(
    [tier('user', 'auto'), tier('project', 'default')],
    { permissions: { defaultMode: 'default' } },
    { permissions: { source: 'project' } },
  )

  // Both halves of the trap, asserted together so a future edit to either is visible:
  // the merged value is what the old guard trusted, and it is wrong.
  assert.equal(filterEscalatingDefaultMode(settings).permissions.defaultMode, 'default')
  assert.equal(resolveEffectiveMode({ resolved: settings, optionMode: 'default' }), 'auto')
})

test('an escalating mode from the project tier is ignored, as the trust filter intends', () => {
  const settings = resolved(
    [tier('project', 'auto')],
    { permissions: { defaultMode: 'auto' } },
    { permissions: { source: 'project' } },
  )

  assert.equal(escalatingModeFromTrustedTier(settings), null)
  assert.equal(resolveEffectiveMode({ resolved: settings, optionMode: 'default' }), 'default')
})

test('every trusted tier is read, not just the user tier', () => {
  for (const source of ['user', 'local', 'managed', 'flag']) {
    assert.equal(escalatingModeFromTrustedTier(resolved([tier(source, 'auto')])), 'auto', source)
  }
})

test('bypassPermissions is caught from a trusted tier', () => {
  const settings = resolved([tier('user', 'bypassPermissions')])
  assert.equal(resolveEffectiveMode({ resolved: settings, optionMode: 'default' }), 'bypassPermissions')
})

test('acceptEdits is not treated as unreachable — the policy still sees non-edit tools', () => {
  const settings = resolved([tier('user', 'acceptEdits')])
  assert.equal(escalatingModeFromTrustedTier(settings), null)
  assert.equal(resolveEffectiveMode({ resolved: settings, optionMode: 'default' }), 'default')
})

test('a non-escalating settings value leaves the query option governing', () => {
  const settings = resolved(
    [tier('user', 'default')],
    { permissions: { defaultMode: 'default' } },
    { permissions: { source: 'user' } },
  )

  assert.equal(resolveEffectiveMode({ resolved: settings, optionMode: 'acceptEdits' }), 'acceptEdits')
})

test('the query option is reported when settings say nothing at all', () => {
  assert.equal(resolveEffectiveMode({ resolved: resolved([]), optionMode: 'auto' }), 'auto')
})

test('a failed resolveSettings keeps the option rather than inventing a mode', () => {
  // The caller has already logged the uncertainty; refusing every spawn on an @alpha
  // hiccup would be a worse failure than the one this guards.
  assert.equal(resolveEffectiveMode({ resolved: null, optionMode: 'default' }), 'default')
})

test('tiers with no permissions block do not throw', () => {
  const settings = resolved([{ source: 'user', settings: {} }, { source: 'project', settings: {} }])
  assert.equal(escalatingModeFromTrustedTier(settings), null)
  assert.equal(resolveEffectiveMode({ resolved: settings, optionMode: 'default' }), 'default')
})
