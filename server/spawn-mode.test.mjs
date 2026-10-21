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
  IMPLEMENTED_SPAWN_TARGETS,
  spawnTargetImplementationRefusal,
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

test('concurrentLimitRefusal: a fresh stale count decides, and the message says so', () => {
  // ⚠️ A count from the last minute is a real measurement; refusing on it is what lost spawns on
  // 2026-10-19. The decision uses it, and the message names its age and the live failure.
  const refusal = concurrentLimitRefusal({
    limits: PAIR,
    liveCount: null,
    staleCount: 30,
    staleAgeMs: 5000,
    countError: 'timeout',
  })
  assert.match(refusal, /30 live/)
  assert.match(refusal, /last successful count, 5 s old/)
  assert.match(refusal, /the live read failed: timeout/)
})

test('concurrentLimitRefusal: a fresh stale count below the cap opens', () => {
  assert.equal(
    concurrentLimitRefusal({ limits: PAIR, liveCount: null, staleCount: 29, staleAgeMs: 1000 }),
    null,
  )
})

test('concurrentLimitRefusal: no stale count keeps the refusal', () => {
  const refusal = concurrentLimitRefusal({
    limits: PAIR,
    liveCount: null,
    staleCount: null,
    staleAgeMs: null,
  })
  assert.match(refusal, /could not be/)
  assert.doesNotMatch(refusal, /last successful count/)
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
  assert.deepEqual(resolveSpawnTarget({ target: 'local' }), { target: 'local', source: 'argument' })
  // The A2A worker-controller targets. Listing them here is what makes the CALL well-formed;
  // whether the spawn can be CARRIED OUT is `IMPLEMENTED_SPAWN_TARGETS`' question, asserted
  // separately below. Both answers are needed — without the first, a caller is told "not a
  // spawn target" for a target that is one, which names the wrong problem.
  assert.deepEqual(resolveSpawnTarget({ target: 'docker' }), { target: 'docker', source: 'argument' })
  assert.deepEqual(resolveSpawnTarget({ target: 'k8s' }), { target: 'k8s', source: 'argument' })
  // A cluster-named target is a well-formed call too. The CLUSTER itself is validated by the
  // resolver that knows the configuration; this function only decides the shape.
  assert.deepEqual(resolveSpawnTarget({ target: 'k8s:nukedev' }), { target: 'k8s:nukedev', source: 'argument' })
  assert.deepEqual(resolveSpawnTarget({ target: 'k8s:octopus-dev' }), { target: 'k8s:octopus-dev', source: 'argument' })
})

test('the retired `cluster` target is refused by name, and the refusal lists the survivors', () => {
  // ⚠️ The refusal's TEXT is asserted here rather than at a live call site, and that is a
  // consequence of retiring the target rather than a shortcut: `cluster` is gone from the
  // `spawn_agent` tool schema's enum too, so a live call is rejected CLIENT-SIDE by Claude Code
  // and never reaches this server — there is no server-side refusal string left to read from a
  // live probe. This assertion is what carries the wording.
  //
  // ⚠️ A bare `assert.ok(error)` does NOT satisfy this. It passes while the message says nothing
  // about `cluster` or about which targets survive, which is exactly the state a botched
  // retirement produces: the target refuses, but the caller is told nothing actionable. Both
  // clauses below are load-bearing — the first pins that the rejected VALUE is named, the second
  // that the caller is pointed at the targets that still work.
  const { error } = resolveSpawnTarget({ target: 'cluster' })
  assert.ok(error, 'cluster must be refused once retired')
  assert.match(error, /"cluster"/, 'the refusal must name the rejected value')
  assert.match(error, /Valid values: local, docker, k8s/, 'the refusal must list the surviving targets')
  // ⚠️ The retired name may appear once as the rejected value and once inside the accepted
  // `k8s:<cluster>` grammar — but never as a value of its own. An unqualified
  // `!/cluster/.test(error)` would now fail on the grammar, so the assertion is on the quoted
  // form, which is how a value is named in these messages.
  assert.equal(
    (error.match(/"cluster"/g) ?? []).length,
    1,
    'cluster must be named only as the rejected value, never offered',
  )
})

test('the published tool schema lists exactly the spawn targets, so the two cannot drift', () => {
  // ⚠️ THIS IS THE DEFECT THE RETIREMENT NEARLY SHIPPED WITH. The `spawn_agent` tool schema
  // carries its own HARDCODED `enum` — `supervisor.mjs` imports only `A2A_SPAWN_TARGETS`, never
  // `SPAWN_TARGETS` — so removing `cluster` from the list alone would have left the published
  // surface still OFFERING a target the server refuses, while every other test in this file
  // stayed green. A caller reading the schema is the population that matters and nothing else
  // in the suite reads it.
  //
  // Pinned by reading supervisor.mjs's text rather than importing it: it starts an MCP server
  // at import, so it cannot be imported behaviourally. Same remedy, same reason, as
  // `cluster-spawn.test.mjs` and `attention-poll.test.mjs`.
  const src = readFileSync(new URL('./supervisor.mjs', import.meta.url), 'utf8')
  // Two steps, and the split is what keeps this from failing for the wrong reason.
  //
  // 1. Slice out the `target:` PROPERTY OBJECT, brace-balanced, so the `enum` is read from the
  //    right place. The file carries four enums (`role`, the target, and two `behavior` pairs),
  //    so an unanchored `enum: [` silently reads the wrong one and passes while asserting nothing
  //    about the surface a caller sees — which is how this test first shipped broken.
  const open = src.indexOf('target: {')
  assert.ok(open > 0, 'the target property must exist in the tool schema')
  let depth = 0
  let end = -1
  for (let i = src.indexOf('{', open); i < src.length; i += 1) {
    if (src[i] === '{') depth += 1
    else if (src[i] === '}') {
      depth -= 1
      if (depth === 0) { end = i; break }
    }
  }
  assert.ok(end > open, 'the target property object must be brace-balanced')
  const targetBlock = src.slice(open, end)
  // 2. Read the PATTERN from inside it WITHOUT coupling to the surrounding property order. It was
  //    an `enum` until 2026-10-20; the cluster names are the operator's configuration, so a closed
  //    enum would publish a list the server does not own. The pattern still bounds the shape a
  //    caller may send, which is the part a schema CAN state — and it must accept every kind in
  //    `SPAWN_TARGETS` plus the `k8s:<cluster>` grammar, while still refusing the retired name.
  const patternMatch = targetBlock.match(/pattern: '([^']+)'/)
  assert.ok(patternMatch, 'the target pattern must exist in the tool schema')
  const published = new RegExp(patternMatch[1])
  for (const t of SPAWN_TARGETS) {
    assert.ok(published.test(t), `the published pattern must accept the spawn target "${t}"`)
  }
  assert.ok(published.test('k8s:nukedev'), 'the pattern must accept a cluster-named target')
  assert.ok(published.test('k8s:octopus-dev'), 'the pattern must accept a hyphenated cluster name')
  assert.ok(!published.test('k8s:'), 'the pattern must refuse an empty cluster name')
  assert.ok(!published.test('k8s:NukeDev'), 'the pattern must refuse a name the server would refuse')
  assert.ok(!published.test('cluster'), 'the retired target must not be offered')
  // And the retired target must not survive in the description either, which is the other half
  // of the surface a caller reads. `local` appears there legitimately (it is the default), so
  // this asserts on the retired name alone.
  const descStart = src.indexOf("'WHERE the worker is created.")
  assert.ok(descStart > 0, 'the target description must exist')
  const desc = src.slice(descStart, src.indexOf("',\n", descStart))
  assert.match(desc, /`cluster` was RETIRED/, 'the description must record the retirement')
  assert.ok(!/target: "cluster"` (starts|is|requires)/.test(desc), 'the description must not present cluster as usable')
})

test('cluster is NOT a SPAWN_MODES value', () => {
  // The load-bearing decision, asserted rather than left to a comment. Putting `cluster` in
  // SPAWN_MODES would make it selectable from SUPERVISOR_SPAWN_MODE and `spawn.mode` in the
  // operator's config file, so one edit could default the whole fleet into the cluster — and
  // the cluster is a second option, never the default. `resolveSpawnMode` refuses any value
  // not in that list, so a config of `cluster` is refused loudly today; this test is what
  // keeps that true after someone "helpfully" adds the third mode.
  assert.ok(!SPAWN_MODES.includes('cluster'))
  // ⚠️ `cluster` is ALSO no longer a spawn target as of 2026-10-15 — retired, because the
  // deferral that held it (the goal's `# Non-goals`: *"retirement is a follow-up once SC3 is
  // proven"*) expired when SC3 was met on 2026-10-14. Asserted here so a later reader cannot
  // re-add it to the target list on the strength of the SPAWN_MODES argument alone.
  assert.ok(!SPAWN_TARGETS.includes('cluster'))
  assert.match(resolveSpawnMode({ env: 'cluster' }).error, /not a spawn mode/)
  assert.match(resolveSpawnMode({ file: { spawn: { mode: 'cluster' } } }).error, /not a spawn mode/)
})

test('docker and k8s are targets but NOT SPAWN_MODES values', () => {
  // The same rule as `cluster`, extended 2026-10-13 with the A2A worker-controller targets. A
  // target reachable from SUPERVISOR_SPAWN_MODE or `spawn.mode` would let one config edit
  // default the whole fleet somewhere, which is exactly what "per-call only" forbids — so
  // this asserts the absence on both sources, not just membership in the target list.
  for (const t of ['docker', 'k8s']) {
    assert.ok(SPAWN_TARGETS.includes(t), `${t} should be a spawn target`)
    assert.ok(!SPAWN_MODES.includes(t), `${t} must not be a spawn mode`)
    assert.match(resolveSpawnMode({ env: t }).error, /not a spawn mode/)
    assert.match(resolveSpawnMode({ file: { spawn: { mode: t } } }).error, /not a spawn mode/)
  }
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

test('a listed target with no client refuses rather than falling through to local', () => {
  // ⚠️ The load-bearing guard, and the reason `IMPLEMENTED_SPAWN_TARGETS` exists at all.
  // `supervisor.mjs` dispatches each target with a client and then falls through to the LOCAL
  // path for everything else — so a target present in `SPAWN_TARGETS` with no dispatch branch of
  // its own would open an ordinary local worker and report success. A `docker` call answered by a local
  // tab is precisely the "looks exactly like a working call" failure this module refuses an
  // UNKNOWN target to avoid; this asserts the same refusal for a KNOWN one.
  for (const t of SPAWN_TARGETS) {
    const refusal = spawnTargetImplementationRefusal({ target: t })
    if (IMPLEMENTED_SPAWN_TARGETS.includes(t)) {
      assert.equal(refusal, null, `${t} is implemented and must be allowed`)
    } else {
      assert.match(refusal, /has no client yet/, `${t} is unimplemented and must refuse`)
    }
  }
  // Both directions, so neither list can drift alone: every implemented target is also a known
  // target, and the difference between the two lists is exactly the unimplemented set.
  //
  // ⚠️ THE DIFFERENCE IS EMPTY AS OF 2026-10-13, and that is the point rather than a reason to
  // delete the guard. `docker` and `k8s` joined `IMPLEMENTED_SPAWN_TARGETS` in the same change
  // as the dispatch branch that creates their worker, so no known target is unimplemented now —
  // but the guard is what makes the NEXT target's author do the same, and removing it would
  // restore the silent fall-through the moment someone widened `SPAWN_TARGETS` alone.
  for (const t of IMPLEMENTED_SPAWN_TARGETS) {
    assert.ok(SPAWN_TARGETS.includes(t), `${t} is implemented but is not a known spawn target`)
  }
  assert.deepEqual(
    SPAWN_TARGETS.filter((t) => !IMPLEMENTED_SPAWN_TARGETS.includes(t)),
    [],
  )
  // The refusal still fires for a value the list does not carry — the second line of defence
  // behind `resolveSpawnTarget`, which is what would normally have refused this. Asserted so the
  // guard is exercised rather than merely present, and because the message must keep naming the
  // fall-through it prevents: a version that only said "not implemented" would leave the next
  // reader free to conclude the safe answer was to open a local worker.
  const refusal = spawnTargetImplementationRefusal({ target: 'some-future-target' })
  assert.match(refusal, /not implemented/)
  assert.match(refusal, /falling\s+through to the local path/)
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
  // The tab path carries the caller's env either directly or spread into the shipping
  // merge — both keep the claim (the call site cannot silently omit it), and the second
  // is the shipping path, so the two assertions below cover each half.
  assert.match(
    source,
    /spawnInteractiveAgent\(\{[^}]*env: (?:envOverrides|tabShippingEnv \? \{ \.\.\.envOverrides)/,
    'the tab path must be handed envOverrides',
  )
  // ⚠️ The tab path's shipping carrier, asserted for the same reason as the env itself:
  // a shipping tab spawn that omitted this would pass every pure-helper test in
  // `shipping-settings.test.mjs` and still park on the operator's mode switch.
  assert.match(
    source,
    /spawnInteractiveAgent\(\{[^}]*\.\.\.tabShippingEnv/,
    'the tab path must be handed the shipping env',
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
