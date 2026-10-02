// Tests for the spawn-mode resolution.
//
// The four-source precedence is the whole point of the module, so every source is
// exercised as the winner AND as the loser — a precedence table that only ever tests the
// winning case cannot tell "consulted and beaten" from "never consulted at all", which is
// the exact bug this module exists to fix.

import { test } from 'node:test'
import assert from 'node:assert/strict'
import {
  DEFAULT_MAX_CONCURRENT,
  MAX_CONCURRENT_ENV,
  resolveMaxConcurrent,
  resolveSpawnMode,
  resolveSpawnTarget,
  unknownKeyWarnings,
  workerEnvFor,
  SPAWN_MODES,
  SPAWN_TARGETS,
  WORKER_MODE_ENV,
  WORKER_MODE_SOURCE_ENV,
} from './spawn-mode.mjs'

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

test('the worker is told its own mode, so a wrong-way spawn is self-diagnosing', () => {
  // The failure this prevents: a per-call `interactive: false` opens a worker headless
  // while every signal that worker can read still says `interactive`, because config.json
  // describes the FLEET and not this worker. Measured 2026-09-20 — two such workers
  // reported they were in an interactive tab and waited for a keystroke that could never
  // be typed. The mode is not re-derived here; it is the one resolveSpawnMode returned.
  const env = workerEnvFor({ mode: 'headless', source: 'argument', env: {} })
  assert.equal(env[WORKER_MODE_ENV], 'headless')
})

test('the worker is told which source decided, not only the mode', () => {
  // "Why is this worker headless" has to be answerable from inside the worker, not only
  // from the manager's agent_status. The four sources are the diagnosis.
  const env = workerEnvFor({ mode: 'headless', source: 'config', env: {} })
  assert.equal(env[WORKER_MODE_SOURCE_ENV], 'config')
})

test('the inherited environment survives, because the SDK replaces rather than merges', () => {
  // The SDK's query `env` REPLACES the subprocess environment instead of merging with it.
  // Dropping the spread would take PATH, HOME and ANTHROPIC_BASE_URL — and the last of
  // those stops the worker routing through the router while looking like nothing at all.
  const env = workerEnvFor({
    mode: 'headless',
    source: 'argument',
    env: { PATH: '/usr/bin', ANTHROPIC_BASE_URL: 'http://127.0.0.1:8788' },
  })
  assert.equal(env.PATH, '/usr/bin')
  assert.equal(env.ANTHROPIC_BASE_URL, 'http://127.0.0.1:8788')
  assert.equal(env[WORKER_MODE_ENV], 'headless')
})

test('a missing mode leaves the environment untouched rather than exporting "undefined"', () => {
  // The caller checks resolveSpawnMode's error first, so this guards a future caller that
  // forgets to — an exported "undefined" would read as a real mode to anything downstream.
  assert.deepEqual(workerEnvFor({ env: { PATH: '/usr/bin' } }), { PATH: '/usr/bin' })
  assert.deepEqual(workerEnvFor(), {})
  assert.equal(workerEnvFor({ env: {} })[WORKER_MODE_ENV], undefined)
})

// --- the fleet-wide concurrent limit --------------------------------------------------
//
// ⚠️ THE DEFAULT IS 20, AND THAT REVERSES THE 2026-09-27 RULING. The key shipped unset
// meaning unlimited; since 2026-10-01 an absent key resolves to DEFAULT_MAX_CONCURRENT, and
// `0` is the only value that still means unlimited. The reversal is asserted rather than
// described, because a resolver that quietly reverted to `unset → null` would restore
// unbounded spawning with every other test in this file still green.

test('resolveMaxConcurrent: unset everywhere is the default 20, not unlimited', () => {
  // `unset → null` was the old contract; after the reversal it is the defect.
  assert.equal(DEFAULT_MAX_CONCURRENT, 20)
  assert.deepEqual(resolveMaxConcurrent({}), { limit: DEFAULT_MAX_CONCURRENT, source: 'default' })
  assert.deepEqual(resolveMaxConcurrent({ env: null, file: {} }), { limit: DEFAULT_MAX_CONCURRENT, source: 'default' })
  assert.deepEqual(resolveMaxConcurrent({ env: '', file: { spawn: {} } }), { limit: DEFAULT_MAX_CONCURRENT, source: 'default' })
})

test('resolveMaxConcurrent: the file decides when the env is silent', () => {
  assert.deepEqual(resolveMaxConcurrent({ file: { spawn: { maxConcurrent: 6 } } }), { limit: 6, source: 'config' })
  // A numeric string is accepted: config.json is JSON, but the env source is always a string.
  assert.deepEqual(resolveMaxConcurrent({ file: { spawn: { maxConcurrent: '6' } } }), { limit: 6, source: 'config' })
})

test('resolveMaxConcurrent: the env wins over the file, and the loser is still validated', () => {
  assert.deepEqual(resolveMaxConcurrent({ env: '3', file: { spawn: { maxConcurrent: 6 } } }), { limit: 3, source: 'env' })
  // The losing source is a typo and the spawn is refused anyway. This is the module's whole
  // discipline: a bad value in the file must not stay invisible because this spawn happened
  // to be decided by the environment.
  const { error } = resolveMaxConcurrent({ env: '3', file: { spawn: { maxConcurrent: 'lots' } } })
  assert.match(error, /spawn\.maxConcurrent/)
  assert.match(error, /"lots"/)
})

test('resolveMaxConcurrent: 0 is unlimited, not a zero-worker limit', () => {
  // 0 is the value an operator reaches for to turn a limit OFF. Refusing it would make the
  // off switch a syntax error; reading it as "zero workers" would stop the fleet dead.
  //
  // ⚠️ After the 2026-10-01 reversal this is the ONLY route back to unbounded spawning — an
  // absent key no longer gets you there. That makes it load-bearing rather than a
  // convenience: a regression that dropped it would leave the operator no off switch at all.
  assert.deepEqual(resolveMaxConcurrent({ env: '0' }), { limit: null, source: 'env' })
  assert.deepEqual(resolveMaxConcurrent({ file: { spawn: { maxConcurrent: 0 } } }), { limit: null, source: 'config' })
})

test('resolveMaxConcurrent: an unusable value refuses rather than coercing', () => {
  // `true` is the case worth naming: Number(true) is 1, so a blind coercion would read a
  // stray boolean in config.json as "one worker at a time" and never say so.
  for (const bad of ['-1', '2.5', 'many', true, {}, []]) {
    const { error } = resolveMaxConcurrent({ file: { spawn: { maxConcurrent: bad } } })
    assert.ok(error, `expected ${JSON.stringify(bad)} to be refused`)
    assert.match(error, /spawn\.maxConcurrent/)
  }
  assert.match(resolveMaxConcurrent({ env: 'lots' }).error, new RegExp(MAX_CONCURRENT_ENV))
})

test('maxConcurrent is a known spawn key — it does not warn as unknown', () => {
  // Without this the key is accepted, reported as applied, and silently ignored — the
  // failure this repo shipped twice (policy.json v0.3.0, permissionMode before mode.mjs).
  assert.deepEqual(unknownKeyWarnings({ spawn: { mode: 'interactive', maxConcurrent: 6 } }, 'x.json'), [])
})

test('a spawn with no target is local', () => {
  assert.deepEqual(resolveSpawnTarget({}), { target: 'local', source: 'default' })
  assert.deepEqual(resolveSpawnTarget(), { target: 'local', source: 'default' })
  // An empty string is "the caller passed nothing", not a target named "" — the same
  // absent-vs-present split `stringSources` makes for the mode's env and config sources.
  assert.deepEqual(resolveSpawnTarget({ target: '' }), { target: 'local', source: 'default' })
})

test('an explicit target is taken from the argument', () => {
  assert.deepEqual(resolveSpawnTarget({ target: 'cluster' }), { target: 'cluster', source: 'argument' })
  assert.deepEqual(resolveSpawnTarget({ target: 'local' }), { target: 'local', source: 'argument' })
})

test('cluster is NOT a SPAWN_MODES value', () => {
  // The load-bearing decision, asserted rather than left to a comment. Putting `cluster` in
  // SPAWN_MODES would make it selectable from SUPERVISOR_SPAWN_MODE and `spawn.mode` in the
  // operator's config file, so one edit could default the whole fleet into the cluster — and
  // the cluster is a second option, never the default. `resolveSpawnMode` refuses any value
  // not in that list, so a config of `cluster` is refused loudly today; this test is what
  // keeps that true after someone "helpfully" adds the third mode.
  assert.ok(!SPAWN_MODES.includes('cluster'))
  assert.ok(SPAWN_TARGETS.includes('cluster'))
  assert.match(resolveSpawnMode({ env: 'cluster' }).error, /not a spawn mode/)
  assert.match(resolveSpawnMode({ file: { spawn: { mode: 'cluster' } } }).error, /not a spawn mode/)
})

test('an unknown target refuses rather than falling back to local', () => {
  // Falling back is the failure, not the safe answer: a worker created locally when the
  // caller asked for the cluster looks exactly like a working call, and is otherwise
  // discovered only by noticing where the worker actually ran.
  for (const bad of ['Cluster', 'remote', 'nuke', ' cluster', true, 0, {}]) {
    const { error } = resolveSpawnTarget({ target: bad })
    assert.ok(error, `expected ${JSON.stringify(bad)} to be refused`)
    assert.match(error, /not a spawn target/)
  }
})
