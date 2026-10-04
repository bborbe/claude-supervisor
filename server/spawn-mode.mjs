// Which way a worker opens — a wezterm tab or a headless SDK session — and which source
// decided it.
//
// Split out of supervisor.mjs for the usual reason (importing supervisor.mjs starts the
// MCP server), but it exists at all because of a failure worth writing down. On
// 2026-09-18 the fleet ran headless all morning while the server's own default was
// already `interactive`: the default lived in code, but the DECISION lived in the
// manager command files, which each passed `interactive=false` explicitly. Changing the
// fleet's mode therefore meant editing N instruction files and sending a
// course-correction message to every manager already running — measured that morning at
// 3 workers killed, 7 more discovered under two other managers, and two messages. One
// config file makes it one edit.
//
// Nothing here reads the filesystem, the environment, or the clock.

export const SPAWN_MODES = ['interactive', 'headless']

// The per-call argument is still a boolean, because that is the shape the MCP tool has
// always exposed and the debug escape hatch has to work in BOTH directions — forcing a
// tab when the fleet is headless is as necessary as the reverse.
const fromArgument = (interactive) =>
  interactive === undefined || interactive === null ? null : interactive ? 'interactive' : 'headless'

// Every string source, in precedence order, whether or not it will end up deciding.
//
// Collected before anything is chosen because validation is deliberately NOT limited to
// the winner. A typo in config.json must not stay invisible merely because this
// particular spawn passed an explicit argument: that is precisely how a file gets
// accepted, reported as applied, and silently ignored — the failure this repo has now
// shipped twice (policy.json in v0.3.0, permissionMode before mode.mjs). So an invalid
// value anywhere refuses every spawn until it is fixed, which is loud, immediate, and
// names the file.
function stringSources({ env, file }) {
  const sources = []
  if (env !== undefined && env !== null && env !== '') sources.push({ source: 'env', value: env })
  const fileMode = file?.spawn?.mode
  if (fileMode !== undefined && fileMode !== null && fileMode !== '') {
    sources.push({ source: 'config', value: fileMode })
  }
  return sources
}

// Keys the config file is allowed to carry. An unknown one is a warning rather than a
// refusal: a key this version does not know is how a config written for a newer version
// looks, and refusing every spawn over one would make the file impossible to roll
// forward. An unknown VALUE is different — it is a typo in a key that IS load-bearing.
// ⚠️ `cluster` is here because cluster-spawn.mjs READS it. Omitting it does not merely miss a
// warning — it emits a false one, every boot, telling an operator that the key the docs just
// told them to set is ignored. A key that is read while being reported as unread is worse
// than an unknown key, because it argues the operator out of a working configuration.
const KNOWN_TOP_LEVEL = ['spawn', 'cluster']
const KNOWN_SPAWN_KEYS = ['mode', 'maxConcurrent', 'maxConcurrentHard']
const KNOWN_CLUSTER_KEYS = ['url', 'token']

export function unknownKeyWarnings(file, path) {
  if (!file || typeof file !== 'object' || Array.isArray(file)) return []
  const warnings = []
  for (const key of Object.keys(file)) {
    if (!KNOWN_TOP_LEVEL.includes(key)) {
      warnings.push(`${path} has an unknown top-level key "${key}" — ignored. Known: ${KNOWN_TOP_LEVEL.join(', ')}`)
    }
  }
  const spawn = file.spawn
  if (spawn && typeof spawn === 'object' && !Array.isArray(spawn)) {
    for (const key of Object.keys(spawn)) {
      if (!KNOWN_SPAWN_KEYS.includes(key)) {
        warnings.push(`${path} has an unknown key "spawn.${key}" — ignored. Known: spawn.${KNOWN_SPAWN_KEYS.join(', spawn.')}`)
      }
    }
  }
  // Descended into for the same reason `spawn` is: a typo like "urll" is a typo in a key that
  // IS load-bearing, and the top-level loop above cannot see it.
  const cluster = file.cluster
  if (cluster && typeof cluster === 'object' && !Array.isArray(cluster)) {
    for (const key of Object.keys(cluster)) {
      if (!KNOWN_CLUSTER_KEYS.includes(key)) {
        warnings.push(`${path} has an unknown key "cluster.${key}" — ignored. Known: cluster.${KNOWN_CLUSTER_KEYS.join(', cluster.')}`)
      }
    }
  }
  return warnings
}

// The mode this spawn actually runs under, plus the source that decided it.
//
// Precedence, highest first: per-call argument, SUPERVISOR_SPAWN_MODE, the config file,
// the built-in default. `source` is returned rather than inferred by the caller because
// "why is this worker headless" is otherwise unanswerable from the outside — it is
// carried into agent_status, list_agents and the ledger record for exactly that reason.
//
// Returns `{ error }` instead of a mode when any string source holds a value that is not
// a known mode. Refusing rather than falling back is the repo's convention: a spawn that
// silently ignored the operator's file would reproduce the morning this module exists to
// prevent.
export function resolveSpawnMode({ interactive, env, file, path = 'the supervisor config' } = {}) {
  for (const { source, value } of stringSources({ env, file })) {
    if (typeof value !== 'string' || !SPAWN_MODES.includes(value)) {
      const where = source === 'env' ? 'SUPERVISOR_SPAWN_MODE' : `"spawn.mode" in ${path}`
      return {
        error:
          `${where} is ${JSON.stringify(value)}, which is not a spawn mode — refusing to spawn rather than ` +
          `guessing, since a worker opened the wrong way is discovered only by noticing it. ` +
          `Valid values: ${SPAWN_MODES.join(', ')}.`,
      }
    }
  }

  const argument = fromArgument(interactive)
  if (argument) return { mode: argument, source: 'argument' }

  const [first] = stringSources({ env, file })
  if (first) return { mode: first.value, source: first.source }

  // Interactive, and not arbitrarily. A worker must come up with the same tooling a
  // normal session has — launcher env, plugin skills, MCP servers, settings.json
  // permissions. A worker missing its normal tooling is not a cheaper worker, it is one
  // that fails in unfamiliar ways and cannot be watched.
  return { mode: 'interactive', source: 'default' }
}

// WHERE a worker is created — this Mac, or the nuke cluster — and the argument that decided it.
//
// This is a second question from `resolveSpawnMode`, not a third answer to it, and keeping
// them apart is the whole point. "Which way does this worker open" (a tab the operator can
// watch, or a headless in-process query) is a property of the worker and is selectable from
// the fleet's own configuration. "Where is it created" is a property of the CALL, and it must
// never be reachable from a config file: the cluster is a second option rather than the
// default, so a `spawn.mode: cluster` in `~/.config/claude-supervisor/config.json` able to
// default the entire fleet into the cluster is exactly the shape this split forecloses.
//
// ⚠️ So `cluster` is deliberately NOT a `SPAWN_MODES` value. Adding it there would make it
// env- and config-selectable for free, which is the failure above arriving silently — the
// list has no notion of "per-call only", and every future reader of it would reasonably
// assume otherwise.
//
// An unknown value REFUSES rather than falling back, for the reason `resolveSpawnMode`
// refuses: a spawn that ignored the caller's target would create the worker somewhere the
// caller did not ask for, and where a worker was created is otherwise discovered only by
// noticing it. Falling back to `local` would look exactly like a working call.
export const SPAWN_TARGETS = ['local', 'cluster']

// The target this spawn runs under, plus the source that decided it — or `{error}` for a
// value that is not a known target.
//
// There is no config or environment source, and that absence is deliberate rather than
// unfinished: `source` exists so a future reader can tell "the caller asked for this" from
// "nobody asked and it defaulted", and today those are the only two answers there are.
export function resolveSpawnTarget({ target } = {}) {
  if (target === undefined || target === null || target === '') {
    return { target: 'local', source: 'default' }
  }
  if (typeof target !== 'string' || !SPAWN_TARGETS.includes(target)) {
    return {
      error:
        `"target" is ${JSON.stringify(target)}, which is not a spawn target — refusing to spawn rather than ` +
        `guessing, since a worker created somewhere the caller did not ask for is discovered only by noticing it. ` +
        `Valid values: ${SPAWN_TARGETS.join(', ')} (omit the argument for the local default).`,
    }
  }
  return { target, source: 'argument' }
}

// How many workers the fleet may hold open at once — one value, fleet-wide.
//
// This replaces the per-manager spawn cap (2 per sweep, 4 per rolling 30 min) that lived in
// `docs/fleet-surface.md` § Spawn a worker item 5 until 2026-09-27. The operator's ruling
// that day: *"These limits are artificial and should be removed … It's more a global
// concurrent limit we should aim than these local limits."* So there is one bound, it is
// global, and it counts LIVE WORKERS rather than spawns-per-window — a rate cap and a
// concurrency cap answer different questions, and only the second is what was asked for.
//
// ⚠️ THE DEFAULT IS 20, AND THIS REVERSES THE 2026-09-27 RULING. That ruling removed the
// limits and shipped the key unset, meaning unlimited. The operator's design of 2026-10-01
// reinstated one — *"keep a total worker limit so the laptop isn't overwhelmed. Target e.g.
// 20"* — so an absent key now resolves to DEFAULT_MAX_CONCURRENT rather than to unlimited,
// and `0` remains the off switch for an operator who wants the old behaviour back. The
// reversal is recorded here rather than left with both statements standing: a reader who
// finds only the 2026-09-27 note concludes the fleet is unbounded, which is no longer true.
//
// ⚠️ ONE VALUE SERVES TWO ROLES, DELIBERATELY. The same number is the hard cap enforced in
// `spawnAgent` and the target the manager loops read when deciding whether to propose work.
// Two keys — a cap and a target — were considered and rejected: they can disagree, and a
// fleet capped at 20 while its managers propose against 30 is a defect with no error on it.
export const MAX_CONCURRENT_ENV = 'SUPERVISOR_MAX_CONCURRENT'

// The HARD ceiling's env counterpart — named as the sibling of the line above rather than
// as a second concept, because it is one pair of keys: one soft, one hard.
export const MAX_CONCURRENT_HARD_ENV = 'SUPERVISOR_MAX_CONCURRENT_HARD'

// The fleet-wide worker target, and the value an absent key resolves to.
//
// Exported so the manager loops, the docs and the tests all name one number rather than
// restating it — this repo's standing rule for a constant with several call sites, where a
// restated copy is a second counter a grep cannot tell from the real one.
export const DEFAULT_MAX_CONCURRENT = 20

// The HARD ceiling, and the value an absent `spawn.maxConcurrentHard` resolves to.
//
// ⚠️ TWO THRESHOLDS SINCE 2026-10-04, ON THE OPERATOR'S RULING — *"lets start with 30 =
// soft cap and 50 = hard cap"*. `maxConcurrent` is the SOFT cap: the count at which the
// manager loops stop proposing and an ORDINARY spawn is refused. This is the HARD cap: the
// count at which EVERY spawn is refused, including one the operator has named by hand. The
// band between them is the point of the change — it is where an operator-named priority
// task may still open when routine work may not, so the fleet never refuses the operator's
// own named work for a reason the operator did not choose.
//
// ⚠️ Both thresholds come from config, and both carry a default, deliberately: the
// operator's numbers are reachable with no config edit at all, and this is what ships.
// `0` on `spawn.maxConcurrent` remains the OFF SWITCH and disables BOTH — a key whose
// documented meaning is "no limit" must not leave a second key quietly capping at 50.
export const DEFAULT_MAX_CONCURRENT_HARD = 50

// One threshold, resolved from the environment and the config file, in that precedence.
//
// Extracted so the soft and hard halves cannot drift in how they validate — the same reason
// the header gives for checking every source rather than only the winner: a rule restated
// per key is a second rule a reader cannot tell from the first.
function resolveThreshold({ env, fileValue, envName, key, path, fallback }) {
  const sources = []
  if (env !== undefined && env !== null && env !== '') sources.push({ source: 'env', value: env })
  if (fileValue !== undefined && fileValue !== null && fileValue !== '') {
    sources.push({ source: 'config', value: fileValue })
  }

  for (const { source, value } of sources) {
    const where = source === 'env' ? envName : `"spawn.${key}" in ${path}`
    // Only a number or a numeric string is a candidate. `Number(true)` is 1 and
    // `Number(null)` is 0, so coercing blindly would read a stray boolean in config.json as
    // a limit of one worker — the kind of silent acceptance this module exists to refuse.
    const parsed = typeof value === 'number' || typeof value === 'string' ? Number(value) : NaN
    if (!Number.isInteger(parsed) || parsed < 0) {
      return {
        error:
          `${where} is ${JSON.stringify(value)}, which is not a concurrent-worker limit — refusing to spawn ` +
          `rather than guessing, since a limit that silently governs nothing is discovered only by the load it ` +
          `was meant to bound. Valid values: a non-negative integer (0 for unlimited), or omit ` +
          `the key for the default of ${fallback}.`,
      }
    }
  }

  const [first] = sources
  if (!first) return { limit: fallback, source: 'default' }
  const parsed = Number(first.value)
  return { limit: parsed === 0 ? null : parsed, source: first.source }
}

// The limit this spawn runs under, plus the source that decided it — or `null` for
// unlimited, which only an explicit `0` now produces.
//
// ⚠️ ONE CALL RETURNS BOTH THRESHOLDS, BECAUSE THEY CONSTRAIN EACH OTHER. Resolved
// separately, a config could produce `soft 60 / hard 50` with each half reporting itself as
// valid — a soft cap sitting above its own ceiling, which is not a limit at all. The pair is
// therefore resolved and validated as a pair here, and no caller can hold one without the
// other.
//
// Validation follows `resolveSpawnMode` exactly, and for the same reason: every source is
// checked, not only the winner. A typo in config.json must not stay invisible merely
// because this particular spawn was capped by the environment instead — that is how a file
// gets accepted, reported as applied, and silently ignored, the failure this repo has
// shipped twice (policy.json in v0.3.0, permissionMode before mode.mjs).
//
// An absent key and `0` are NO LONGER the same answer. An absent key resolves to
// DEFAULT_MAX_CONCURRENT; `0` means unlimited. `0` is accepted rather than refused because
// it is the value an operator reaches for when turning a limit off, refusing it would make
// the off switch a syntax error, and after the 2026-10-01 reversal it is the only way back
// to the previous behaviour.
export function resolveMaxConcurrent({ env, envHard, file, path = 'the supervisor config' } = {}) {
  const soft = resolveThreshold({
    env,
    fileValue: file?.spawn?.maxConcurrent,
    envName: MAX_CONCURRENT_ENV,
    key: 'maxConcurrent',
    path,
    fallback: DEFAULT_MAX_CONCURRENT,
  })
  if (soft.error) return soft

  // ⚠️ THE OFF SWITCH DISABLES THE PAIR. `0` on the soft key has meant "no limit" since the
  // key shipped, and it is what an operator reaches for to turn the cap off; leaving the
  // hard key to cap at its own default 50 would make that switch a lie. Short-circuited
  // BEFORE the hard key is read, so an unparseable hard value cannot refuse a spawn the
  // operator has deliberately uncapped.
  if (soft.limit === null) {
    return { limit: null, hardLimit: null, source: soft.source, hardSource: soft.source }
  }

  const hard = resolveThreshold({
    env: envHard,
    fileValue: file?.spawn?.maxConcurrentHard,
    envName: MAX_CONCURRENT_HARD_ENV,
    key: 'maxConcurrentHard',
    path,
    fallback: DEFAULT_MAX_CONCURRENT_HARD,
  })
  if (hard.error) return hard

  // ⚠️ `0` IS NOT AN OFF SWITCH ON THE HARD KEY, unlike on the soft one, and it is refused
  // rather than accepted. On the soft key `0` is the documented off switch; here it would
  // silently delete the ceiling while leaving the soft cap in force — a fleet that looks
  // capped and is not, which is the "a limit that silently governs nothing" failure this
  // module exists to refuse. It would also skip the pair check below. The fallback is never
  // 0, so a null here can only have come from an explicit `0`.
  if (hard.limit === null) {
    // Name the source that actually carried the 0 — the env var when it came from there, the
    // file otherwise. The same rule `resolveThreshold` follows above, and for the same reason:
    // a refusal that names the wrong place sends the operator to edit a file that is not the
    // one governing the spawn.
    const where = hard.source === 'env' ? MAX_CONCURRENT_HARD_ENV : `"spawn.maxConcurrentHard" in ${path}`
    return {
      error:
        `${where} is 0, which is not a hard concurrent-worker limit — refusing to spawn rather than ` +
        `removing the ceiling while the soft cap stays in force, because a fleet that looks capped and is ` +
        `not is discovered only by the load it was meant to bound. Set a non-negative integer above the ` +
        `soft cap, or use the off switch: "spawn.maxConcurrent": 0 disables BOTH thresholds.`,
    }
  }

  // A ceiling beneath its own floor is not a limit: every spawn would be refused at the
  // soft count and the operator-named band would be empty. Refused rather than silently
  // reordered, because which of the two numbers the operator meant is not ours to guess.
  if (hard.limit < soft.limit) {
    return {
      error:
        `the hard concurrent-worker limit is below the soft one: "spawn.maxConcurrent" resolves to ` +
        `${soft.limit} (source: ${soft.source}) but "spawn.maxConcurrentHard" resolves to ${hard.limit} ` +
        `(source: ${hard.source}) — refusing to spawn rather than running under a ceiling beneath its own ` +
        `floor, which would empty the operator-named band and refuse every spawn at the soft count. Raise ` +
        `spawn.maxConcurrentHard in ${path} to at least ${soft.limit}, or lower spawn.maxConcurrent.`,
    }
  }

  return { limit: soft.limit, hardLimit: hard.limit, source: soft.source, hardSource: hard.source }
}

// Whether a spawn may proceed — a refusal string, or `null` to allow it.
//
// ⚠️ PURE, AND SPLIT OUT OF `supervisor.mjs` FOR THAT REASON. The enforcement lived inline in
// the server module, which starts an MCP server on import and so cannot be unit-tested — the
// same reason this file exists at all. The decision is four branches and two of them are the
// two-threshold change, so it is exactly the logic that must not ship uncovered.
//
// ⚠️ `operatorNamed` is the CALLER's assertion, resolved from the task's own operator-set
// flag and passed in — never inferred here. This module cannot read the vault, and a default
// of `true` would make the soft cap unenforceable, so an absent argument means an ordinary
// spawn. The marker and its provenance rule have one home in `commands/open.md` § Step 1.5.
//
// ⚠️ THE HARD CAP IS ASKED FIRST because it refuses everyone, so it is the answer whatever
// named the task — and answering the soft cap first would tell an operator-named caller it
// was exempt at a count where nothing opens.
export function concurrentLimitRefusal({ limits, liveCount, operatorNamed = false, configFile }) {
  if (limits.error) return limits.error
  // `null` is unlimited — the soft key's off switch, which disables the pair.
  if (limits.limit === null) return null

  // `null` is "a store could not be read", which is NOT "no worker is live". Refusing on it is
  // the same asymmetry the mode rule carries: a limit that cannot count must not open, because
  // opening past an uncountable limit is how the limit silently stops existing — and a caller
  // acting on the other reading spawns onto live work.
  if (liveCount === null) {
    return (
      `the concurrent-worker limit is set to ${limits.limit} but the live-worker count could not be ` +
      `taken, so it is unknown — refusing rather than opening past a limit that cannot be counted. Both the ` +
      `session registry and the spawn ledger must be readable; point SUPERVISOR_SESSIONS_DIR and ` +
      `SUPERVISOR_LEDGER_DIR at them if they live elsewhere.`
    )
  }

  if (limits.hardLimit !== null && liveCount >= limits.hardLimit) {
    return (
      `the fleet is FULL: ${liveCount} live workers against a hard cap of ${limits.hardLimit} ` +
      `(source: ${limits.hardSource}), so nothing opens — not even an operator-named task. The soft ` +
      `cap is ${limits.limit} (source: ${limits.source}). Raise spawn.maxConcurrentHard in ` +
      `${configFile} to open past ${limits.hardLimit}, or set spawn.maxConcurrent to 0 for unlimited.`
    )
  }

  if (liveCount >= limits.limit && !operatorNamed) {
    return (
      `the fleet-wide concurrent-worker limit is reached: ${liveCount} live, ${limits.limit} ` +
      `allowed (source: ${limits.source}). Open nothing further and report the remainder as ` +
      `held-on-limit; it is picked up next sweep. An OPERATOR-NAMED task may still open here — this one was ` +
      `not named, so it is refused. Raise spawn.maxConcurrent in ${configFile}, or ` +
      `set it to 0 for unlimited.`
    )
  }
  return null
}

export const WORKER_MODE_ENV = 'SUPERVISOR_WORKER_MODE'
export const WORKER_MODE_SOURCE_ENV = 'SUPERVISOR_WORKER_MODE_SOURCE'

// The environment a headless worker is spawned with, carrying the mode this server has
// already resolved.
//
// Why this exists: a headless worker is an in-process SDK `query()` with no pid and no
// argv (see the resume guard in supervisor.mjs), so it cannot learn its own mode from a
// process listing — and the only file it can read, config.json, describes the FLEET, not
// this worker. A per-call `interactive: false` therefore opens a worker headless while
// leaving every signal it can see still saying `interactive`. Measured 2026-09-20: two
// such workers reported they were in an interactive tab and waited for a keystroke that
// could never be typed. The mis-model is deterministic, not incidental.
//
// The fix is a handover rather than a probe: `resolveSpawnMode` already returns `source`
// alongside `mode`, so the spawner hands the worker the answer it computed instead of
// leaving the worker to infer it. `source` rides along so a worker that opened the wrong
// way can say which of the four decided it.
//
// ⚠️ The SDK's query `env` option REPLACES the subprocess environment rather than merging
// with it, so `env` is spread unconditionally here. Dropping the spread would take PATH,
// HOME and ANTHROPIC_BASE_URL with it — and the last of those stops the worker routing
// through the router while looking like nothing at all.
//
// A missing mode leaves the environment untouched rather than exporting the string
// "undefined". The caller checks `resolveSpawnMode`'s error first, so this guards a
// future caller that forgets to, not a reachable state today.
export function workerEnvFor({ mode, source, env } = {}) {
  if (!mode) return { ...(env ?? {}) }
  return {
    ...(env ?? {}),
    [WORKER_MODE_ENV]: mode,
    [WORKER_MODE_SOURCE_ENV]: source,
  }
}
