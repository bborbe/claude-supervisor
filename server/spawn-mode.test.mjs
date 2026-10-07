// Tests for the spawn-mode resolution.
//
// The four-source precedence is the whole point of the module, so every source is
// exercised as the winner AND as the loser — a precedence table that only ever tests the
// winning case cannot tell "consulted and beaten" from "never consulted at all", which is
// the exact bug this module exists to fix.

import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { execSync } from 'node:child_process'
import {
  DEFAULT_MAX_CONCURRENT,
  DEFAULT_MAX_CONCURRENT_HARD,
  MAX_CONCURRENT_ENV,
  MAX_CONCURRENT_HARD_ENV,
  concurrentLimitRefusal,
  resolveEnvOverrides,
  resolveMaxConcurrent,
  resolveSpawnMode,
  resolveSpawnTarget,
  shellEnvExports,
  shellQuote,
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

test('the cluster key is KNOWN — warning about it is a false alarm about a key that IS read', () => {
  // cluster-spawn.mjs reads exactly this key, so listing it as unknown told an operator, on
  // every boot, that the key the docs had just told them to set was ignored — and the
  // likeliest response is to remove it and conclude the docs are wrong. A key that is read
  // while being reported as unread is worse than an unknown one.
  assert.deepEqual(unknownKeyWarnings({ cluster: { url: 'https://x.example', token: 't' } }, '/c.json'), [])

  // Descended into for the same reason `spawn` is: a typo in a key that IS load-bearing is
  // exactly the case the top-level loop cannot see.
  const nested = unknownKeyWarnings({ cluster: { urll: 'https://x.example' } }, '/c.json')
  assert.equal(nested.length, 1)
  assert.match(nested[0], /cluster\.urll/)

  // A MALFORMED cluster is the case the descent cannot reach — listing `cluster` as known stops
  // the top-level loop warning about it — so without its own branch it was silent at boot from
  // both loops and surfaced only as a spawn-time refusal.
  const malformed = unknownKeyWarnings({ cluster: 'https://x.example' }, '/c.json')
  assert.equal(malformed.length, 1)
  assert.match(malformed[0], /"cluster" as a string/)
  assert.match(malformed[0], /IGNORED/)
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

test('resolveMaxConcurrent: unset everywhere is the default 20/50, not unlimited', () => {
  // `unset → null` was the old contract; after the reversal it is the defect.
  assert.equal(DEFAULT_MAX_CONCURRENT, 20)
  assert.equal(DEFAULT_MAX_CONCURRENT_HARD, 50)
  const both = {
    limit: DEFAULT_MAX_CONCURRENT,
    hardLimit: DEFAULT_MAX_CONCURRENT_HARD,
    source: 'default',
    hardSource: 'default',
  }
  assert.deepEqual(resolveMaxConcurrent({}), both)
  assert.deepEqual(resolveMaxConcurrent({ env: null, file: {} }), both)
  assert.deepEqual(resolveMaxConcurrent({ env: '', file: { spawn: {} } }), both)
})

test('resolveMaxConcurrent: the file decides when the env is silent', () => {
  const expected = { limit: 6, hardLimit: 50, source: 'config', hardSource: 'default' }
  assert.deepEqual(resolveMaxConcurrent({ file: { spawn: { maxConcurrent: 6 } } }), expected)
  // A numeric string is accepted: config.json is JSON, but the env source is always a string.
  assert.deepEqual(resolveMaxConcurrent({ file: { spawn: { maxConcurrent: '6' } } }), expected)
})

test('resolveMaxConcurrent: the hard cap resolves on its own key, env over file', () => {
  // The two thresholds are independent keys: a hard cap set alone leaves the soft cap at its
  // default, and vice versa. Asserted because a resolver that read one key for both would
  // make `maxConcurrentHard` silently inert — the failure this module exists to stop.
  assert.deepEqual(
    resolveMaxConcurrent({ file: { spawn: { maxConcurrentHard: 80 } } }),
    { limit: 20, hardLimit: 80, source: 'default', hardSource: 'config' },
  )
  assert.deepEqual(
    resolveMaxConcurrent({ envHard: '90', file: { spawn: { maxConcurrent: 10, maxConcurrentHard: 80 } } }),
    { limit: 10, hardLimit: 90, source: 'config', hardSource: 'env' },
  )
})

test('resolveMaxConcurrent: the env wins over the file, and the loser is still validated', () => {
  assert.deepEqual(resolveMaxConcurrent({ env: '3', file: { spawn: { maxConcurrent: 6 } } }), { limit: 3, hardLimit: 50, source: 'env', hardSource: 'default' })
  // The losing source is a typo and the spawn is refused anyway. This is the module's whole
  // discipline: a bad value in the file must not stay invisible because this spawn happened
  // to be decided by the environment.
  const { error } = resolveMaxConcurrent({ env: '3', file: { spawn: { maxConcurrent: 'lots' } } })
  assert.match(error, /spawn\.maxConcurrent/)
  assert.match(error, /"lots"/)
})

test('resolveMaxConcurrent: 0 is unlimited, and it switches BOTH thresholds off', () => {
  // 0 is the value an operator reaches for to turn a limit OFF. Refusing it would make the
  // off switch a syntax error; reading it as "zero workers" would stop the fleet dead.
  //
  // ⚠️ After the 2026-10-01 reversal this is the ONLY route back to unbounded spawning — an
  // absent key no longer gets you there. That makes it load-bearing rather than a
  // convenience: a regression that dropped it would leave the operator no off switch at all.
  //
  // ⚠️ It disables the PAIR, and the short-circuit runs BEFORE the hard key is read: a key
  // documented as "no limit" must not leave the hard key quietly capping at 50, and a broken
  // hard value must not refuse a spawn the operator has deliberately uncapped.
  assert.deepEqual(resolveMaxConcurrent({ env: '0' }), { limit: null, hardLimit: null, source: 'env', hardSource: 'env' })
  assert.deepEqual(resolveMaxConcurrent({ file: { spawn: { maxConcurrent: 0 } } }), { limit: null, hardLimit: null, source: 'config', hardSource: 'config' })
  assert.deepEqual(
    resolveMaxConcurrent({ file: { spawn: { maxConcurrent: 0, maxConcurrentHard: 'nonsense' } } }),
    { limit: null, hardLimit: null, source: 'config', hardSource: 'config' },
  )
})

test('resolveMaxConcurrent: a hard cap below the soft one refuses rather than reordering', () => {
  // A ceiling beneath its own floor is not a limit: every spawn would be refused at the soft
  // count and the operator-named band would be empty. Which of the two numbers the operator
  // meant is not ours to guess, so the pair is refused and both are named.
  const { error } = resolveMaxConcurrent({ file: { spawn: { maxConcurrent: 30, maxConcurrentHard: 20 } } })
  assert.match(error, /below the soft one/)
  assert.match(error, /spawn\.maxConcurrentHard/)
  assert.match(error, /30/)
  assert.match(error, /20/)
  // Equal is NOT below: it is degenerate but coherent — no band, the soft count enforced for all.
  assert.deepEqual(
    resolveMaxConcurrent({ file: { spawn: { maxConcurrent: 30, maxConcurrentHard: 30 } } }),
    { limit: 30, hardLimit: 30, source: 'config', hardSource: 'config' },
  )
})

test('resolveMaxConcurrent: an unusable value refuses rather than coercing', () => {
  // `true` is the case worth naming: Number(true) is 1, so a blind coercion would read a
  // stray boolean in config.json as "one worker at a time" and never say so.
  for (const bad of ['-1', '2.5', 'many', true, {}, []]) {
    const { error } = resolveMaxConcurrent({ file: { spawn: { maxConcurrent: bad } } })
    assert.ok(error, `expected ${JSON.stringify(bad)} to be refused`)
    assert.match(error, /spawn\.maxConcurrent/)
    const hard = resolveMaxConcurrent({ file: { spawn: { maxConcurrentHard: bad } } })
    assert.ok(hard.error, `expected hard ${JSON.stringify(bad)} to be refused`)
    assert.match(hard.error, /spawn\.maxConcurrentHard/)
  }
  assert.match(resolveMaxConcurrent({ env: 'lots' }).error, new RegExp(MAX_CONCURRENT_ENV))
  assert.match(resolveMaxConcurrent({ envHard: 'lots' }).error, new RegExp(MAX_CONCURRENT_HARD_ENV))
})

test('both cap keys are known spawn keys — neither warns as unknown', () => {
  // Without this the key is accepted, reported as applied, and silently ignored — the
  // failure this repo shipped twice (policy.json v0.3.0, permissionMode before mode.mjs).
  assert.deepEqual(
    unknownKeyWarnings({ spawn: { mode: 'interactive', maxConcurrent: 6, maxConcurrentHard: 60 } }, 'x.json'),
    [],
  )
})

test('resolveMaxConcurrent: 0 on the HARD key is refused, unlike on the soft one', () => {
  // On the soft key `0` is the documented off switch. On the hard key it would silently delete
  // the ceiling while the soft cap stayed in force — a fleet that looks capped and is not,
  // which is the "a limit that silently governs nothing" failure this module refuses. The
  // fallback is never 0, so a null here can only have come from an explicit 0.
  const { error } = resolveMaxConcurrent({ file: { spawn: { maxConcurrentHard: 0 } } })
  assert.match(error, /spawn\.maxConcurrentHard/)
  assert.match(error, /is 0/)
  assert.match(error, /disables/)
  assert.match(resolveMaxConcurrent({ envHard: '0' }).error, new RegExp(MAX_CONCURRENT_HARD_ENV))
  // ⚠️ But the soft key's off switch still wins, and it is checked FIRST — so `0` on both keys
  // is the documented "no limit" state, not an error. A hard value that would be refused on
  // its own must not refuse a spawn the operator has deliberately uncapped.
  assert.deepEqual(
    resolveMaxConcurrent({ file: { spawn: { maxConcurrent: 0, maxConcurrentHard: 0 } } }),
    { limit: null, hardLimit: null, source: 'config', hardSource: 'config' },
  )
})

// --- the enforcement decision ---------------------------------------------------------
//
// ⚠️ Split out of `supervisor.mjs` so it CAN be tested: that module starts an MCP server on
// import, which is why this file exists at all. The four branches below are the enforcement
// half of the two-threshold change, and two of them are new.

const PAIR = { limit: 30, hardLimit: 50, source: 'config', hardSource: 'config' }

test('concurrentLimitRefusal: below the soft cap everything opens', () => {
  assert.equal(concurrentLimitRefusal({ limits: PAIR, liveCount: 29 }), null)
  assert.equal(concurrentLimitRefusal({ limits: PAIR, liveCount: 0, operatorNamed: false }), null)
})

test('concurrentLimitRefusal: the band refuses an ordinary spawn and admits an operator-named one', () => {
  // The whole point of the change. Both halves are asserted together, because either alone is
  // satisfied by a build that got the other wrong — raising the single cap for everyone passes
  // the exemption half, and refusing everyone passes the ordinary half.
  assert.match(concurrentLimitRefusal({ limits: PAIR, liveCount: 30 }), /OPERATOR-NAMED/)
  assert.equal(concurrentLimitRefusal({ limits: PAIR, liveCount: 30, operatorNamed: true }), null)
  assert.equal(concurrentLimitRefusal({ limits: PAIR, liveCount: 49, operatorNamed: true }), null)
})

test('concurrentLimitRefusal: the hard cap refuses everyone, operator-named included', () => {
  // The hard branch is asked FIRST, and that ordering is what makes this true: answering the
  // soft cap first would tell an operator-named caller it was exempt at a count where nothing
  // opens.
  const full = concurrentLimitRefusal({ limits: PAIR, liveCount: 50, operatorNamed: true })
  assert.match(full, /fleet is FULL/)
  assert.match(full, /not even an operator-named task/)
  assert.match(concurrentLimitRefusal({ limits: PAIR, liveCount: 51, operatorNamed: true }), /51 live workers/)
  assert.match(concurrentLimitRefusal({ limits: PAIR, liveCount: 50, operatorNamed: false }), /fleet is FULL/)
})

test('concurrentLimitRefusal: an equal pair leaves no band, so the exemption never applies', () => {
  // Degenerate but coherent, and the hard-first ordering is what keeps it coherent — at the
  // shared count the hard branch fires, so nothing slips through the exemption.
  const equal = { limit: 30, hardLimit: 30, source: 'config', hardSource: 'config' }
  assert.equal(concurrentLimitRefusal({ limits: equal, liveCount: 29, operatorNamed: true }), null)
  assert.match(concurrentLimitRefusal({ limits: equal, liveCount: 30, operatorNamed: true }), /fleet is FULL/)
})

test('concurrentLimitRefusal: an uncountable store refuses rather than reading as zero', () => {
  // `null` is "a store could not be read", NOT "no worker is live". A limit that cannot count
  // must not open — and the reading that would let it open is the one that looks healthy.
  const refusal = concurrentLimitRefusal({ limits: PAIR, liveCount: null })
  assert.match(refusal, /could not be/)
  assert.match(refusal, /unknown/)
})

test('concurrentLimitRefusal: the off switch and a resolver error pass straight through', () => {
  const off = { limit: null, hardLimit: null, source: 'config', hardSource: 'config' }
  assert.equal(concurrentLimitRefusal({ limits: off, liveCount: 999 }), null)
  assert.equal(concurrentLimitRefusal({ limits: { error: 'boom' }, liveCount: 0 }), 'boom')
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

// --- the per-call environment ---------------------------------------------------------
//
// `spawn_agent`'s `env`. TWO spawn paths consume this — a headless worker rides the SDK's
// `env` option, a tab worker is exported into its `bash -lc` command line — so the tests
// below pin the resolution and BOTH renderings. A call site cannot be covered here at all:
// importing supervisor.mjs connects a stdio server at load.

test('an absent env resolves to nothing rather than refusing', () => {
  assert.deepEqual(resolveEnvOverrides({}), { env: {} })
  assert.deepEqual(resolveEnvOverrides(), { env: {} })
  assert.deepEqual(resolveEnvOverrides({ env: null }), { env: {} })
})

test('a per-call env keeps its names and values', () => {
  const { env } = resolveEnvOverrides({ env: { ATTENTION_STORE_URL: 'http://localhost:18081' } })
  assert.deepEqual(env, { ATTENTION_STORE_URL: 'http://localhost:18081' })
})

test('a number or boolean value is stringified, so both paths carry the same type', () => {
  // The SDK's `env` wants strings and the tab path interpolates into a shell word. A number
  // left as a number would be one value on one path and a different type on the other.
  const { env } = resolveEnvOverrides({ env: { PORT: 8080, DRY_RUN: true } })
  assert.deepEqual(env, { PORT: '8080', DRY_RUN: 'true' })
})

test('an env that is not an object refuses rather than iterating something else', () => {
  // An array is the tempting mistake — `["A=1"]` reads like a pair list and is not one, and
  // `Object.entries` over it would yield index names the caller never chose.
  for (const bad of [[], ['A=1'], 'A=1', 7, true]) {
    const { error } = resolveEnvOverrides({ env: bad })
    assert.ok(error, `expected ${JSON.stringify(bad)} to be refused`)
    assert.match(error, /must be an object/)
  }
})

test('an illegal variable name refuses, because the tab path exports it literally', () => {
  // REFUSED, not dropped. A dropped key is a value the worker does not have, and a worker
  // missing one value looks exactly like one that was never given it — which is the reading
  // this parameter exists to make possible in the first place.
  for (const bad of ['foo-bar', '2FA', 'A B', '', 'A.B']) {
    const { error } = resolveEnvOverrides({ env: { [bad]: 'x' } })
    assert.ok(error, `expected ${JSON.stringify(bad)} to be refused`)
    assert.match(error, /not a legal shell variable name/)
  }
})

test('a value that is not string-ish refuses rather than being stringified into nonsense', () => {
  for (const bad of [null, undefined, {}, [], () => {}]) {
    const { error } = resolveEnvOverrides({ env: { OK: bad } })
    assert.ok(error, `expected ${String(bad)} to be refused`)
    assert.match(error, /must be a string, number or boolean/)
  }
})

test('a per-call value wins over the inherited environment', () => {
  // The whole point of the parameter: THIS one worker is redirected, without touching a
  // default every other session depends on.
  const env = workerEnvFor({
    mode: 'headless',
    source: 'argument',
    env: { ATTENTION_STORE_URL: 'http://localhost:18080', PATH: '/usr/bin' },
    overrides: { ATTENTION_STORE_URL: 'http://localhost:18081' },
  })
  assert.equal(env.ATTENTION_STORE_URL, 'http://localhost:18081')
  // ...and every key NOT given is inherited unchanged, PATH among them. Dropping the spread
  // would take PATH, HOME and ANTHROPIC_BASE_URL — the last silently stops router routing.
  assert.equal(env.PATH, '/usr/bin')
})

test('the mode handover outranks a per-call value, so a caller cannot lie to the worker', () => {
  // A caller able to set SUPERVISOR_WORKER_MODE could make a headless worker believe it has a
  // tab — the exact mis-model the handover was written to end.
  const env = workerEnvFor({
    mode: 'headless',
    source: 'argument',
    env: {},
    overrides: { [WORKER_MODE_ENV]: 'interactive', [WORKER_MODE_SOURCE_ENV]: 'config' },
  })
  assert.equal(env[WORKER_MODE_ENV], 'headless')
  assert.equal(env[WORKER_MODE_SOURCE_ENV], 'argument')
})

test('overrides still apply when there is no mode, so the tab path is not a special case', () => {
  // The tab path never sets a mode handover, so an implementation that only threaded
  // overrides through the `mode` branch would silently drop them for every tab worker.
  assert.deepEqual(workerEnvFor({ env: { PATH: '/usr/bin' }, overrides: { A: '1' } }), {
    PATH: '/usr/bin',
    A: '1',
  })
  assert.deepEqual(workerEnvFor({ overrides: { A: '1' } }), { A: '1' })
})

test('no overrides leaves the environment exactly as it was', () => {
  assert.deepEqual(workerEnvFor({ env: { PATH: '/usr/bin' } }), { PATH: '/usr/bin' })
  assert.deepEqual(workerEnvFor({ env: { PATH: '/usr/bin' }, overrides: {} }), { PATH: '/usr/bin' })
})

test('the tab path renders nothing when there is nothing to export', () => {
  // The command line is built by interpolation, so this must be the empty string rather than
  // `export undefined=undefined; ` or a literal `{}`.
  assert.equal(shellEnvExports({}), '')
  assert.equal(shellEnvExports(undefined), '')
  assert.equal(shellEnvExports(null), '')
})

test('the tab path exports each value single-quoted, so a space or a $ survives the shell', () => {
  // The value is interpolated into a command line a login shell parses. Unquoted, a URL with
  // a query string, or any value carrying a space, would be split or expanded before the
  // worker ever saw it — and the worker would report the wrong value with no error anywhere.
  assert.equal(
    shellEnvExports({ ATTENTION_STORE_URL: 'http://localhost:18081' }),
    "export ATTENTION_STORE_URL='http://localhost:18081'; ",
  )
  assert.equal(shellEnvExports({ A: 'x y', B: '$HOME' }), "export A='x y'; export B='$HOME'; ")
})

test("a single quote inside a value is escaped, so the quoting cannot be broken out of", () => {
  // The classic: an unescaped `'` ends the quote and everything after it is shell. `'\''` is
  // the POSIX escape, and it is why this is a shared helper rather than string interpolation
  // at the call site.
  assert.equal(shellQuote("it's"), "'it'\\''s'")
  assert.equal(shellEnvExports({ A: "it's" }), "export A='it'\\''s'; ")
})

test('an export is prefixed with `export`, never a bare assignment', () => {
  // ⚠️ A `VAR=value` prefix applies to ONE command, so `A=1 cd /x && exec y` would set A for
  // the `cd` and lose it before the `exec` that actually starts the worker — a silent no-op
  // that looks exactly like a working spawn. This is the test that keeps the verb.
  assert.match(shellEnvExports({ A: '1' }), /^export A=/)
})

test('a key named __proto__ survives, rather than being silently dropped', () => {
  // ⚠️ Measured 2026-10-07: `resolved['__proto__'] = 'x'` on a plain `{}` is a SILENT NO-OP.
  // `__proto__` is an accessor on Object.prototype whose setter ignores any value that is not
  // an object or null — and `JSON.parse` DOES create it as an own enumerable key, so a caller
  // reaching this path through MCP args really does produce it. Building by assignment
  // therefore dropped the key with no error anywhere: the exact failure this function's
  // refuse-never-drop rule exists to prevent, sitting inside the function itself. Found by
  // the PR review; the fix is to collect pairs and let Object.fromEntries define an OWN
  // property.
  const { env } = resolveEnvOverrides({ env: JSON.parse('{"__proto__":"http://localhost:18081"}') })
  assert.deepEqual(Object.keys(env), ['__proto__'])
  assert.equal(Object.getOwnPropertyDescriptor(env, '__proto__').value, 'http://localhost:18081')
  // ...and this is a plain object, not prototype pollution: the prototype is untouched.
  assert.equal(Object.getPrototypeOf(env), Object.prototype)
})

test('a caller cannot declare a worker\'s spawn mode', () => {
  // ⚠️ `workerEnvFor` makes the SERVER's mode win on the HEADLESS path, but the tab path has
  // no handover to win with — `shellEnvExports` would export whatever the caller passed — so
  // the identical override was inert on one path and live on the other. Live is not cosmetic:
  // `scripts/permission-answer-poll.py:287` returns early on
  // `SUPERVISOR_WORKER_MODE == "headless"`, so a caller telling a TAB worker it is headless
  // silences that worker's attention-store relay. Refused on both paths so the rule reads the
  // same wherever it is found. Found by the PR review (round 2).
  for (const name of [WORKER_MODE_ENV, WORKER_MODE_SOURCE_ENV]) {
    const { error } = resolveEnvOverrides({ env: { [name]: 'headless' } })
    assert.ok(error, `expected ${name} to be refused`)
    assert.match(error, /cannot set/)
  }
  // The refusal is about the NAME, not the value — `headless` on any other key is ordinary.
  assert.deepEqual(resolveEnvOverrides({ env: { SUPERVISOR_WORKER_MODE_X: 'headless' } }).env, {
    SUPERVISOR_WORKER_MODE_X: 'headless',
  })
})

test('the tab path quoting survives a real shell for every metacharacter', () => {
  // The claim the tab path rests on is that `shellQuote` is airtight for EVERY shell
  // metacharacter — a single-quoted context is literal for all of them — but only the quote
  // itself was pinned, so a refactor to a different quoting style would break newline,
  // backtick, `$(…)` and a trailing backslash silently. Named by the PR review (round 2).
  //
  // Driven through a REAL shell rather than asserted as a string shape, because the string
  // shape is the thing under test: this is the exact round trip `spawnInteractiveAgent`'s
  // command line makes — `export …; <command>` inside `bash -lc`.
  for (const value of ["it's", 'a\nb', '`id`', '$(id)', 'a\\', 'a;b', 'a b', '$HOME', '']) {
    const script = `${shellEnvExports({ PROBE: value })}printf '%s' "$PROBE"`
    const out = execSync(`bash -lc ${shellQuote(script)}`, { encoding: 'utf8' })
    assert.equal(out, value, `round trip failed for ${JSON.stringify(value)}`)
  }
})

test('both spawn paths are wired — the claim no pure-helper test can reach', () => {
  // ⚠️ THE CLAIM THIS CHANGE RESTS ON, and the one every test above cannot reach: a
  // headless-only implementation passes this entire file, and the CHANGELOG names that
  // exact implementation as the thing the change exists to refuse. `supervisor.mjs`
  // connects a stdio server at load and is unimportable, so this asserts over its SOURCE
  // TEXT — the same shape `scripts/check-spawn-mode.py` uses for the mode decision, and for
  // the same stated reason: a text scan cannot prove the wiring RUNS, but it does prove a
  // call site cannot silently omit it. Found by the PR review.
  const source = readFileSync(new URL('./supervisor.mjs', import.meta.url), 'utf8')
  assert.match(
    source,
    /spawnInteractiveAgent\(\{[^}]*env: envOverrides/,
    'the tab path must be handed envOverrides',
  )
  assert.match(
    source,
    /workerEnvFor\(\{[^}]*overrides: envOverrides/,
    'the headless path must be handed overrides',
  )
  assert.match(
    source,
    /if \(env && Object\.keys\(env\)\.length > 0\)/,
    'the cluster path must refuse env rather than accepting and dropping it',
  )
})
