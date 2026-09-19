// Every environment read the server makes — and the user's config file — in one place.
//
// Scattered `process.env` access makes a service's real configuration surface
// impossible to enumerate and defeats fail-fast validation — a typo in a rarely-hit
// path fails at runtime rather than at boot. The Node service guide states this as a
// MUST (RULE node/config/env-read-at-boundary), and the review of the liveness guard
// caught `liveness.mjs` reading two vars at module level: a library module, not a
// bootstrap, so squarely in scope.
//
// Fixing only those two would have missed the point. The rule's rationale is that the
// surface be *enumerable*, and two of thirteen is not — the other eleven sat in the
// entrypoint, which the rule exempts but which was hiding the same surface. So this
// owns all of them, entrypoint included, and `config.test.mjs` fails the build if any
// other module reads `process.env` again.
//
// Values are resolved once at load and frozen; nothing downstream reads the
// environment a second time, so tests inject values instead of depending on ambient
// process state. The same holds for the config file read below: resolved once, which is
// why editing it takes effect on the next server start rather than the next spawn.

import { readFileSync } from 'node:fs'
import { homedir } from 'node:os'
import { join } from 'node:path'

const ENV = process.env

const CONFIG_HOME = ENV.XDG_CONFIG_HOME || join(homedir(), '.config')
const STATE_HOME = ENV.XDG_STATE_HOME || join(homedir(), '.local', 'state')
const CLAUDE_HOME = ENV.CLAUDE_CONFIG_DIR || join(homedir(), '.claude')

const CONFIG_DIR = join(CONFIG_HOME, 'claude-supervisor')
const STATE_DIR = join(STATE_HOME, 'claude-supervisor')

// `off` is a value, not an absence: an unset var means "use the default path", while
// `off` means "no file log at all". Reading them as one thing turns off into default.
const PERMISSION_LOG =
  ENV.SUPERVISOR_PERMISSION_LOG === 'off'
    ? null
    : ENV.SUPERVISOR_PERMISSION_LOG || join(STATE_DIR, 'permissions.jsonl')

// The SDK prices a turn from Anthropic's list, so the figure is only the truth when the
// traffic actually goes there. Under claude-code-router it goes to whatever backend the
// router points at, and the number becomes fiction — measured 2026-09-13 at $0.40–$0.79
// per worker against vLLM. The env var is the whole signal: unset means the SDK reaches
// Anthropic itself, set means something else is on the other end.
const ANTHROPIC_BASE_URL = ENV.ANTHROPIC_BASE_URL || null
const COST_FIGURES_MEANINGFUL =
  !ANTHROPIC_BASE_URL || ANTHROPIC_BASE_URL.includes('api.anthropic.com')

// The user's config file, read here for the same reason every env var is: so the whole
// configuration surface is enumerable in one place and resolved once at boot rather than
// re-read in a hot path. Absent is the normal case — most users have none and take the
// built-in default — so ENOENT is silence, while a file that EXISTS and cannot be parsed
// is reported, because that is a file the operator wrote and believes is in effect.
//
// Kept raw and unvalidated: which keys mean what, and which values are legal, belongs to
// spawn-mode.mjs, where it can be unit-tested without touching a filesystem.
const CONFIG_FILE = ENV.SUPERVISOR_CONFIG || join(CONFIG_DIR, 'config.json')

const readConfigFile = (path) => {
  let raw
  try {
    raw = readFileSync(path, 'utf8')
  } catch (error) {
    return error.code === 'ENOENT' ? { file: null, error: null } : { file: null, error: `cannot read ${path}: ${error.message}` }
  }
  try {
    return { file: JSON.parse(raw), error: null }
  } catch (error) {
    return { file: null, error: `${path} is not valid JSON: ${error.message}` }
  }
}

const CONFIG_FILE_READ = readConfigFile(CONFIG_FILE)

export const config = Object.freeze({
  // stderr is the transport-safe log channel — stdout carries MCP frames — so a file
  // log is opt-in and nothing depends on a machine-local path by default.
  logFile: ENV.SUPERVISOR_LOG || null,

  // User-editable files live under XDG paths, never inside this checkout: a config you
  // edit must not dirty a git tree, and `git pull` must not be able to clobber it.
  configDir: CONFIG_DIR,
  stateDir: STATE_DIR,
  claudeHome: CLAUDE_HOME,

  userPolicy: ENV.SUPERVISOR_POLICY || join(CONFIG_DIR, 'policy.json'),
  permissionLog: PERMISSION_LOG,

  // Where the spawn default comes from. Three fields rather than one resolved answer:
  // the PATH is what a refusal message has to name so the operator knows which file to
  // edit, the parsed FILE and the env var are the two sources whose precedence is
  // decided per spawn in spawn-mode.mjs, and the read ERROR is surfaced by the server's
  // own logger rather than swallowed here.
  configFile: CONFIG_FILE,
  configFileContents: CONFIG_FILE_READ.file,
  configFileError: CONFIG_FILE_READ.error,

  // Raw and deliberately unvalidated, exactly like permissionMode below: the legal
  // values and the message naming a bad one belong to the module that owns the meaning.
  spawnMode: ENV.SUPERVISOR_SPAWN_MODE || null,

  // Whether a reported cost figure describes the traffic that actually ran. See the
  // note above the resolution — this is a property of the deployment, not of a turn.
  anthropicBaseUrl: ANTHROPIC_BASE_URL,
  costFiguresMeaningful: COST_FIGURES_MEANINGFUL,

  // Claude Code's live session registry, keyed by pid. Read-only, and the only probe
  // that sees a session whose id appears in no process's command line.
  sessionsDir: ENV.SUPERVISOR_SESSIONS_DIR || join(CLAUDE_HOME, 'sessions'),

  // Where Claude Code writes session transcripts, as `<projectsDir>/<escaped-cwd>/<id>.jsonl`.
  // Only the ROOT is configuration: the per-session directory is escaped from a cwd, and
  // deriving it back from a cwd is the unreliability tab-read.mjs exists to avoid. A
  // reader that needs a transcript resolves it by session id against this root instead.
  projectsDir: ENV.SUPERVISOR_PROJECTS_DIR || join(CLAUDE_HOME, 'projects'),

  // The durable spawn ledger: one uuid-keyed record per worker this server spawns,
  // outliving the session it describes. Deliberately NOT reusing
  // SUPERVISOR_SESSIONS_DIR — that name already means the live registry above, a
  // different store with a different lifetime, and one variable meaning two things is
  // how a reader ends up pointing the ledger at Claude Code's directory.
  ledgerDir: ENV.SUPERVISOR_LEDGER_DIR || join(STATE_DIR, 'sessions'),

  // Raw and deliberately unvalidated here: the allowed-mode list and the warning that
  // names a bad value both belong to the server's own logger, not to this module.
  permissionMode: ENV.SUPERVISOR_PERMISSION_MODE || null,

  claudeCmd: ENV.SUPERVISOR_CLAUDE_CMD || null,
  mcpConfig: ENV.SUPERVISOR_MCP_CONFIG || null,
  workerColor: ENV.SUPERVISOR_WORKER_COLOR ?? '/color pink',
})
