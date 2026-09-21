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
const KNOWN_TOP_LEVEL = ['spawn']
const KNOWN_SPAWN_KEYS = ['mode']

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
