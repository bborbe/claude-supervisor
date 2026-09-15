// Every environment read the server makes, in one place.
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
// process state.

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

  // Whether a reported cost figure describes the traffic that actually ran. See the
  // note above the resolution — this is a property of the deployment, not of a turn.
  anthropicBaseUrl: ANTHROPIC_BASE_URL,
  costFiguresMeaningful: COST_FIGURES_MEANINGFUL,

  // Claude Code's live session registry, keyed by pid. Read-only, and the only probe
  // that sees a session whose id appears in no process's command line.
  sessionsDir: ENV.SUPERVISOR_SESSIONS_DIR || join(CLAUDE_HOME, 'sessions'),

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
