// Tests for the spawn-mode resolution.
//
// The four-source precedence is the whole point of the module, so every source is
// exercised as the winner AND as the loser — a precedence table that only ever tests the
// winning case cannot tell "consulted and beaten" from "never consulted at all", which is
// the exact bug this module exists to fix.

import { test } from 'node:test'
import assert from 'node:assert/strict'
import { resolveSpawnMode, unknownKeyWarnings, SPAWN_MODES } from './spawn-mode.mjs'

const file = (mode) => ({ spawn: { mode } })

test('with nothing configured, a worker opens interactively', () => {
  assert.deepEqual(resolveSpawnMode({}), { mode: 'interactive', source: 'default' })
  assert.deepEqual(resolveSpawnMode(), { mode: 'interactive', source: 'default' })
})

test('the config file decides when nothing outranks it', () => {
  assert.deepEqual(resolveSpawnMode({ file: file('headless') }), { mode: 'headless', source: 'config' })
  assert.deepEqual(resolveSpawnMode({ file: file('interactive') }), { mode: 'interactive', source: 'config' })
})

test('the env var outranks the config file', () => {
  assert.deepEqual(resolveSpawnMode({ env: 'interactive', file: file('headless') }), {
    mode: 'interactive',
    source: 'env',
  })
  assert.deepEqual(resolveSpawnMode({ env: 'headless', file: file('interactive') }), {
    mode: 'headless',
    source: 'env',
  })
})

test('the per-call argument outranks everything, in both directions', () => {
  // Both directions matter: forcing a tab while the fleet runs headless is as necessary
  // as the reverse, and an escape hatch that only opens one way is not one.
  assert.deepEqual(resolveSpawnMode({ interactive: true, env: 'headless', file: file('headless') }), {
    mode: 'interactive',
    source: 'argument',
  })
  assert.deepEqual(resolveSpawnMode({ interactive: false, env: 'interactive', file: file('interactive') }), {
    mode: 'headless',
    source: 'argument',
  })
})

test('an omitted argument is not a false one', () => {
  // The regression that motivated the module: the MCP handler coerced an absent
  // `interactive` to a boolean, so the server received an explicit mode on every call and
  // the file below it could never be reached.
  for (const absent of [undefined, null]) {
    assert.deepEqual(resolveSpawnMode({ interactive: absent, file: file('headless') }), {
      mode: 'headless',
      source: 'config',
    })
  }
})

test('an unknown mode in the config file refuses the spawn and names the file', () => {
  const { error, mode } = resolveSpawnMode({ file: file('interactiv'), path: '/home/x/.config/claude-supervisor/config.json' })
  assert.equal(mode, undefined, 'a refusal must not also report a mode')
  assert.match(error, /interactiv/, 'the bad value is quoted back')
  assert.match(error, /\/home\/x\/\.config\/claude-supervisor\/config\.json/, 'the operator must learn which file to edit')
  for (const valid of SPAWN_MODES) assert.match(error, new RegExp(valid), 'the valid values are listed')
})

test('an unknown mode in the env var refuses the spawn and names the variable', () => {
  const { error } = resolveSpawnMode({ env: 'tab' })
  assert.match(error, /SUPERVISOR_SPAWN_MODE/)
  assert.match(error, /tab/)
})

test('a bad value refuses even when something outranks it', () => {
  // Deliberate: validating only the winner would leave a typo in config.json invisible on
  // every spawn that passed an explicit argument, and visible only later, on the one that
  // did not. A file that is accepted and silently ignored is the failure mode this repo
  // has already shipped twice.
  assert.ok(resolveSpawnMode({ interactive: true, file: file('nope') }).error)
  assert.ok(resolveSpawnMode({ interactive: false, env: 'nope' }).error)
  assert.ok(resolveSpawnMode({ env: 'interactive', file: file('nope') }).error)
})

test('a non-string mode is refused rather than coerced', () => {
  for (const value of [true, 1, {}, []]) {
    assert.ok(resolveSpawnMode({ file: { spawn: { mode: value } } }).error, `${JSON.stringify(value)} must refuse`)
  }
})

test('an absent or malformed file is the default, not an error', () => {
  // Most users have no config file at all, and a file holding only unrelated keys is not
  // a broken one. Neither may refuse a spawn.
  for (const value of [null, undefined, {}, { spawn: {} }, { other: 1 }]) {
    assert.deepEqual(resolveSpawnMode({ file: value }), { mode: 'interactive', source: 'default' })
  }
})

test('an empty env var is an absent one', () => {
  // Unset and set-to-empty must not disagree: shells produce the latter routinely.
  assert.deepEqual(resolveSpawnMode({ env: '', file: file('headless') }), { mode: 'headless', source: 'config' })
})

test('unknown keys warn by name and known keys stay quiet', () => {
  assert.deepEqual(unknownKeyWarnings({ spawn: { mode: 'interactive' } }, '/c.json'), [])
  assert.deepEqual(unknownKeyWarnings(null, '/c.json'), [])

  const top = unknownKeyWarnings({ spawn: { mode: 'headless' }, policy: {} }, '/c.json')
  assert.equal(top.length, 1)
  assert.match(top[0], /unknown top-level key "policy"/)
  assert.match(top[0], /\/c\.json/)

  const nested = unknownKeyWarnings({ spawn: { mode: 'headless', colour: 'pink' } }, '/c.json')
  assert.equal(nested.length, 1)
  assert.match(nested[0], /spawn\.colour/)
})

test('an unknown key warns but never refuses', () => {
  // A config written for a newer version must still be usable by this one; refusing every
  // spawn over a key we do not recognise would make the file impossible to roll forward.
  assert.deepEqual(resolveSpawnMode({ file: { spawn: { mode: 'headless' }, future: true } }), {
    mode: 'headless',
    source: 'config',
  })
})
