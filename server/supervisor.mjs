#!/usr/bin/env node
// supervisor.mjs — MCP server that spawns Claude sessions and lets the calling
// manager session answer THEIR permission prompts.
//
// Prototype. Each agent is one `query()` session from @anthropic-ai/claude-agent-sdk.
// Its `canUseTool` callback parks the request here; the manager fetches it with
// await_permission / pending_permissions and resolves it with answer_permission.

import { Server } from '@modelcontextprotocol/sdk/server/index.js'
import { StdioServerTransport } from '@modelcontextprotocol/sdk/server/stdio.js'
import { ListToolsRequestSchema, CallToolRequestSchema } from '@modelcontextprotocol/sdk/types.js'
import { query, resolveSettings } from '@anthropic-ai/claude-agent-sdk'
import { appendFileSync, mkdirSync, readFileSync } from 'fs'
import { homedir } from 'os'
import { isAbsolute, join } from 'path'
import { realpathSync } from 'fs'
import { spawnSync } from 'child_process'
import { config } from './config.mjs'
import { runAgentLoop } from './agent-loop.mjs'
import { POLICY_UNREACHABLE_MODES, resolveEffectiveMode } from './mode.mjs'
import { decide as decideWith, inputKey, overlayRules } from './policy.mjs'
import { checkLiveness, findRegisteredByName, sessionIdsNamed, uniqueTabName } from './liveness.mjs'
import { resolveSpawnMode, unknownKeyWarnings, workerEnvFor } from './spawn-mode.mjs'
import { windowIdArgument } from './window-id.mjs'
import { resolveRole } from './role-map.mjs'
import { policySupportError, resumeSupportError, sendToPane } from './tab.mjs'
import { buildRecord, parentSessionId, UNOBSERVED_STATUS, unobservedPatch, updateRecord, writeRecord } from './ledger.mjs'
import { awaitingInput, currentToolCallFrom, lastAssistantTextFrom, sessionStatusFor, transcriptPathFor } from './tab-read.mjs'

const PERMISSION_TIMEOUT_MS = 15 * 60 * 1000

const agents = new Map() // id -> agent record
const pending = new Map() // requestId -> permission record
const waiters = new Set() // resolvers waiting for the next permission
let seq = 0

// Every environment read the server makes happens in config.mjs — see that file for
// why the surface lives in one place. stderr is the transport-safe log channel (stdout
// carries MCP frames); the file log is opt-in, so nothing depends on a machine-local
// path by default.
const LOG_FILE = config.logFile

const log = (...a) => {
  const line = `[supervisor] ${a.join(' ')}\n`
  process.stderr.write(line)
  if (!LOG_FILE) return
  try {
    appendFileSync(LOG_FILE, `${new Date().toISOString()} ${line}`)
  } catch {}
}

// ── policy layer ────────────────────────────────────────────────────────────
// Rules decide what a worker may do WITHOUT waking the manager. Anything the
// rules do not cover defers to canUseTool, which parks it for the manager.
// Every request is logged, so the policy can be grown from decisions actually
// made rather than guessed up front.

// ── configuration ───────────────────────────────────────────────────────────
// User-editable files live under XDG paths, never inside this checkout: a config
// you edit must not dirty a git tree, and `git pull` must not be able to clobber it.
//
//   ~/.config/claude-supervisor/policy.json            your rules, checked first
//   ~/.local/state/claude-supervisor/permissions.jsonl every request, for mining
//   <plugin>/server/policy.json                        shipped defaults (fallback)

const CONFIG_DIR = config.configDir
const STATE_DIR = config.stateDir

const USER_POLICY = config.userPolicy
const BUNDLED_POLICY = new URL('./policy.json', import.meta.url).pathname
const PERMISSION_LOG = config.permissionLog

const PERMISSION_MODES = ['default', 'acceptEdits', 'bypassPermissions', 'plan', 'dontAsk', 'auto']

// Validated once, at the boundary. An unvalidated mode would silently fall through
// to the SDK's default and make a typo indistinguishable from a decision.
const PERMISSION_MODE = (() => {
  const raw = config.permissionMode
  if (!raw) return 'default'
  if (!PERMISSION_MODES.includes(raw)) {
    log(`WARNING: SUPERVISOR_PERMISSION_MODE="${raw}" is not a known mode — falling back to "default". Known: ${PERMISSION_MODES.join(', ')}`)
    return 'default'
  }
  if (raw !== 'default') log(`WARNING: permissionMode "${raw}" — see README; 'auto' bypasses the hook and canUseTool entirely, leaving workers unsupervised.`)
  return raw
})()

// The config file is optional, so its ABSENCE is silence. Its presence with a problem is
// not: a file the operator wrote and believes is in effect, which the server could not
// read, is the exact shape of a setting that is accepted and then ignored. Reported once
// at boot rather than per spawn, since the read happens once.
if (config.configFileError) {
  log(`WARNING: ${config.configFileError} — falling back to the built-in spawn default`)
}
for (const warning of unknownKeyWarnings(config.configFileContents, config.configFile)) {
  log(`WARNING: ${warning}`)
}

// The settings tiers a headless worker loads. Named once because the query option and
// the effective-mode resolution below must agree — resolving a different set would
// report a mode the worker does not actually run under, which is worse than not checking.
const SETTING_SOURCES = ['user', 'project', 'local']

// The permission mode a worker will ACTUALLY run under.
//
// The decision itself lives in mode.mjs: it is the part worth unit-testing, and the file
// is where the reasoning is written down — including why the merged settings value is not
// a safe answer and the tiers are read individually instead.
//
// Measured 2026-09-15, and the reason this exists at all: `permissions.defaultMode`
// resolved to "auto" from ~/.claude/settings.json on this machine, and a live worker
// confirmed what that means — the command ran, reported success, and neither the hook nor
// canUseTool was called at all. The policy code was correct, unit-tested, and doing
// nothing.
//
// Naming that file is load-bearing, not colour: the same value read from the managed tier
// would be a machine policy the operator cannot edit, while this one is theirs to change.
async function effectivePermissionMode(cwd) {
  try {
    const resolved = await resolveSettings({ cwd, settingSources: SETTING_SOURCES })
    return resolveEffectiveMode({ resolved, optionMode: PERMISSION_MODE })
  } catch (error) {
    // @alpha API: a failure is reported rather than thrown, and then falls back to the
    // option we send — the best available answer, with the uncertainty already logged
    // rather than silently read as "default".
    //
    // The whole resolution sits inside the try, not just the await: `resolveEffectiveMode`
    // calls `filterEscalatingDefaultMode`, which is @alpha too, and a throw from it raised
    // outside this block would take the server down rather than degrade it — the opposite
    // of what a diagnostic should do.
    log(`WARNING: cannot resolve the effective permission mode: ${error.message} — whether policy rules apply is unknown`)
    return PERMISSION_MODE
  }
}

// One reader, two callers with opposite failure semantics. It reports rather than
// decides: "no such file" is normal for the optional user overlay and never normal for a
// path a caller named explicitly at spawn time, so the caller owns that judgement.
function loadRules(path) {
  let raw
  try {
    raw = readFileSync(path, 'utf8')
  } catch (error) {
    return { error: `cannot read policy ${path}: ${error.message}`, code: error.code }
  }
  let parsed
  try {
    parsed = JSON.parse(raw)
  } catch (error) {
    return { error: `policy ${path} is not valid JSON: ${error.message}` }
  }
  const rules = Array.isArray(parsed?.rules) ? parsed.rules : []
  for (const rule of rules) {
    if (!rule?.action || !['allow', 'deny', 'escalate'].includes(rule.action)) {
      log(`ERROR: policy ${path} has a rule with an unknown action ${JSON.stringify(rule?.action)} — it will never match`)
    }
  }
  return { rules }
}

// The optional user overlay: absent is the normal case, since most users have none.
// Unreadable or malformed is not normal — collapsing the two would let a corrupted
// policy look empty and quietly run under the bundled defaults instead.
function readRules(path) {
  const { rules, error, code } = loadRules(path)
  if (error) {
    if (code !== 'ENOENT') log(`ERROR: ${error} — its rules are NOT in effect`)
    return []
  }
  return rules
}

// Overlay, not replace: your rules are evaluated first, and the bundled set fills
// in whatever you have not decided. Growing the list means appending, never copying.
const policy = { rules: [...readRules(USER_POLICY), ...readRules(BUNDLED_POLICY)] }

// Evaluation is pure and lives in policy.mjs; this binds it to whichever rule set the
// worker runs under — the server's by default, its own when spawn_agent named one.
const decide = (toolName, input, cwd, rules = policy.rules) => decideWith(rules, toolName, input, cwd)

// A policy file named by the caller at spawn time. A relative path resolves against the
// WORKER's cwd, not the server's: the caller is naming a file in the tree the worker
// will run in, while the server's own cwd is an accident of how its MCP client happened
// to launch it.
//
// Fails closed on an unreadable file. Falling back to the server default would run the
// worker under rules the caller did not choose while reporting success — the exact shape
// of the bug this repo already shipped once, when policy.json existed, was documented,
// and was not read.
function resolveSpawnPolicy(policyPath, cwd) {
  if (typeof policyPath !== 'string' || !policyPath.trim()) {
    return { error: `policy must be a path to a JSON file, got ${JSON.stringify(policyPath)}` }
  }
  const path = isAbsolute(policyPath) ? policyPath : join(cwd, policyPath)
  const { rules, error } = loadRules(path)
  if (error) {
    return {
      error: `${error} — refusing to spawn, because a named policy that silently falls back to the server default is not the policy you asked for`,
    }
  }
  return { rules: overlayRules(rules, policy.rules), path }
}

// The state dir is static — create it once rather than on every write.
let logDirReady = false
let logFailureReported = false

function logPermission(record) {
  if (!PERMISSION_LOG) return
  try {
    if (!logDirReady) {
      mkdirSync(STATE_DIR, { recursive: true })
      logDirReady = true
    }
    appendFileSync(PERMISSION_LOG, `${JSON.stringify(record)}\n`)
  } catch (error) {
    // Report once: a failing disk must not turn into a log flood of its own, but a
    // silent catch would leave permission decisions unrecorded with no signal at all.
    if (!logFailureReported) {
      logFailureReported = true
      log(`ERROR: cannot write permission log ${PERMISSION_LOG}: ${error.message} — decisions are no longer being recorded`)
    }
  }
}

// PermissionRequest hook: answer what policy knows, defer everything else.
// Returning no decision falls through to canUseTool — that fall-through IS the
// escalation path, and it is also precisely what gets logged for policy mining.
function makePermissionHook(agent) {
  return async (hookInput) => {
    const toolName = hookInput?.tool_name ?? 'unknown'
    const { action, key, rule } = decide(toolName, hookInput?.tool_input, agent.cwd, agent.rules)
    const base = {
      ts: new Date().toISOString(),
      agent: agent.id,
      label: agent.label,
      tool: toolName,
      key,
      matched_rule: rule ? `${rule.tool}:${rule.match}` : null,
      // Which policy this worker ran under, so a decision mined out of the log can be
      // traced back to the file that produced it rather than to the rule text alone.
      policy: agent.policyPath ?? null,
    }

    if (action === 'allow' || action === 'deny') {
      logPermission({ ...base, decision: action, decided_by: 'policy' })
      log(`policy ${action} ${toolName} for ${agent.id} (${rule.tool}:${rule.match})`)
      return {
        hookSpecificOutput: {
          hookEventName: 'PermissionRequest',
          decision:
            action === 'allow'
              ? { behavior: 'allow' }
              : { behavior: 'deny', message: `supervisor policy denied ${toolName}` },
        },
      }
    }

    logPermission({ ...base, decision: null, decided_by: 'escalated' })
    return {}
  }
}

const publicPerm = (p) => ({
  request_id: p.requestId,
  agent_id: p.agentId,
  tool_name: p.toolName,
  prompt: p.title ?? `Claude wants to use ${p.toolName}`,
  display_name: p.displayName ?? null,
  description: p.description ?? null,
  reason: p.decisionReason ?? null,
  blocked_path: p.blockedPath ?? null,
  input: p.input,
  requested_at: p.requestedAt,
})

function makeCanUseTool(agent) {
  return (toolName, input, opts = {}) =>
    new Promise((resolve) => {
      const requestId = `perm_${++seq}`
      const settle = (result) => {
        clearTimeout(timer)
        pending.delete(requestId)
        if (agent.status === 'blocked-on-permission') agent.status = 'running'
        resolve(result)
      }
      const record = {
        requestId,
        agentId: agent.id,
        toolName,
        input,
        title: opts.title,
        displayName: opts.displayName,
        description: opts.description,
        decisionReason: opts.decisionReason,
        blockedPath: opts.blockedPath,
        requestedAt: new Date().toISOString(),
        settle,
      }
      // Fail safe: an unanswered prompt must not park a worker forever.
      const timer = setTimeout(() => {
        log(`permission ${requestId} timed out -> deny`)
        settle({ behavior: 'deny', message: 'Supervisor did not answer within 15 minutes.' })
      }, PERMISSION_TIMEOUT_MS)

      pending.set(requestId, record)
      agent.status = 'blocked-on-permission'
      agent.permissions.push(requestId)
      log(`permission requested by ${agent.id}: ${toolName} (${requestId})`)
      for (const waiter of [...waiters]) waiter(publicPerm(record))
    })
}

function waitForPermission(timeoutMs) {
  const first = pending.values().next().value
  if (first) return Promise.resolve(publicPerm(first))
  return new Promise((resolve) => {
    const handler = (perm) => {
      clearTimeout(timer)
      waiters.delete(handler)
      resolve(perm)
    }
    const timer = setTimeout(() => {
      waiters.delete(handler)
      resolve(null)
    }, timeoutMs)
    waiters.add(handler)
  })
}

const shellQuote = (s) => `'${String(s).replace(/'/g, `'\\''`)}'`

// Where Claude Code writes a session's transcript, so it can be tailed live.
// Claude Code escapes the RESOLVED cwd by replacing "/" with "-", which is why
// /tmp/sup-verify lands under -private-tmp-sup-verify on macOS.
function transcriptDirFor(cwd) {
  let resolved = cwd
  try {
    resolved = realpathSync(cwd)
  } catch {}
  return join(config.claudeHome, 'projects', resolved.replace(/\//g, '-'))
}

// The durable record. Written at spawn, so a long-running worker is recorded while it
// is still running, and updated with the outcome when it finishes.
//
// Best-effort by design — a ledger that cannot be written must not take the spawn down
// with it — but never silent, because a missing record is indistinguishable from a
// worker that was never spawned. A worker whose session id never resolved gets no
// record at all rather than one filed under a key that would never be looked up.
function writeLedger(agent, patch) {
  if (!agent.sessionId) {
    log(`WARNING: no ledger record for ${agent.id} — its session id never resolved, so there is no key to file it under`)
    return null
  }
  try {
    if (patch) return updateRecord(config.ledgerDir, agent.sessionId, patch)
    const record = buildRecord({
      sessionId: agent.sessionId,
      agentId: agent.id,
      label: agent.label,
      mode: agent.status === 'interactive' ? 'interactive' : 'headless',
      modeSource: agent.modeSource ?? null,
      cwd: agent.cwd,
      launcher: agent.launcher ?? null,
      paneId: agent.paneId ?? null,
      resumedFrom: agent.resumedFrom ?? null,
      policy: agent.policyPath ?? null,
      parentSession: agent.parentSession ?? null,
      spawnedAt: agent.createdAt,
    })
    writeRecord(config.ledgerDir, record)
    return record
  } catch (error) {
    log(`WARNING: could not write the ledger record for ${agent.id}: ${error.message}`)
    return null
  }
}

// The shutdown path: what this server owes the ledger when it goes away.
//
// Without it, a killed or restarted server leaves every record it wrote asserting
// `running` forever — the defect this exists to close. The hook is deliberately
// WRITE-ONLY and fast: it does not probe liveness. `checkLiveness` reads and parses the
// session registry per session, so probing every agent inside a signal handler would
// make shutdown slow and unbounded — the same unbounded wait that ruled out a
// drain-and-wait mode. Resolving an `unknown` record against liveness belongs at READ
// time, where a reader can afford it and where the answer is fresh rather than stale by
// construction.
//
// An agent whose session id never resolved has no ledger key, so it has no record to
// stamp either. writeLedger already says so; this must not invent one.
function stampUnobservedWorkers() {
  let stamped = 0
  for (const agent of agents.values()) {
    // `done` and `error` are real observed outcomes. Never overwrite one with an unknown.
    if (agent.status === 'done' || agent.status === 'error') continue
    const mode = agent.status === 'interactive' ? 'interactive' : 'headless'
    if (writeLedger(agent, unobservedPatch({ mode }))) stamped += 1
  }
  return stamped
}

// SIGTERM is what a restart or an operator's kill sends; SIGINT is Ctrl-C for a server
// run by hand. Both must run the same path — a handler on one alone leaves the other
// dying hard, which is exactly the state this task exists to remove.
//
// process.exit after the writes: installing a signal handler suppresses Node's default
// exit, so without it the server would keep running after being told to stop.
for (const signal of ['SIGTERM', 'SIGINT']) {
  process.on(signal, () => {
    const stamped = stampUnobservedWorkers()
    log(`received ${signal} — stamped ${stamped} unobserved worker(s) as ${UNOBSERVED_STATUS}, then exited`)
    process.exit(0)
  })
}

// Spawn a worker as a real interactive session in a wezterm tab.
//
// The trade is deliberate: this worker's approval prompts are answered IN ITS TAB,
// so it is not supervised the way the headless path is. That also means no filter
// is needed in pending_permissions / await_permission — an interactive worker never
// calls canUseTool, so no permission record is ever created for it.
// Resolve the LAUNCHER SCRIPT, not the bare `claude` binary. The cc-* scripts carry
// the router env (ANTHROPIC_BASE_URL), the MCP config and the model selection, and
// invoking `claude` directly routes around all three — the same reason /open reads
// claude_script from vault-cli config instead of calling the binary.
function resolveClaudeCmd(cwd) {
  if (config.claudeCmd) return config.claudeCmd
  try {
    const out = spawnSync('vault-cli', ['config', 'list', '--output', 'json'], { encoding: 'utf8' })
    if (out.status === 0) {
      const vaults = JSON.parse(out.stdout)
      const match =
        vaults.find((v) => v.path && cwd.startsWith(v.path)) ||
        vaults.find((v) => v.name === 'personal')
      if (match?.claude_script) return match.claude_script
    }
  } catch {}
  return 'claude'
}

// Read the servers the launcher passes via `--mcp-config <file>`. These arrive as a
// CLI flag, not as settings, so settingSources cannot reach them: an SDK worker with
// settings loaded still saw 7 fewer servers than its tab twin. Handing the same file
// to query()'s mcpServers is what makes the two modes functionally identical.
function resolveMcpServers(claudeCmd) {
  let path = config.mcpConfig
  if (!path) {
    try {
      // The scripts write `--mcp-config ~/.claude/...`, so ~ must be expanded by us.
      const match = readFileSync(claudeCmd, 'utf8').match(/--mcp-config\s+["']?(~?[^"'\s\\]+)/)
      if (!match) return {}
      path = match[1].replace(/^~/, homedir())
    } catch {
      return {}
    }
  }
  try {
    const servers = JSON.parse(readFileSync(path, 'utf8')).mcpServers ?? {}
    log(`headless workers inherit ${Object.keys(servers).length} servers from ${path}`)
    return servers
  } catch (error) {
    log(`WARNING: cannot read MCP config ${path}: ${error.message} — headless workers get fewer servers than tab workers`)
    return {}
  }
}

async function spawnInteractiveAgent({ id, prompt, cwd, label, windowId, chip }) {
  const claudeCmd = resolveClaudeCmd(cwd)
  // Prefix the tab TITLE so a supervised worker is identifiable at a glance in the
  // tab bar and the fleet roster — the two places a normal session is otherwise
  // indistinguishable. The agent's own `label` stays unprefixed, so list_agents
  // joins to ListAgents by stripping the marker.
  //
  // The TASK goes in the spawn argv, where nothing can lose it. The COLOUR does not:
  // seeding it as the prompt's first line never worked, because Claude Code parses one
  // submitted message as one command and `/color` takes the entire trimmed argument —
  // `/color pink\n\n<task>` validated as `Invalid color "pink\n\n<task>"`. It is sent
  // as its own message once the pane is up; see tab.mjs for why that needs an
  // activated tab and a readiness poll. A colour that fails to apply is logged and
  // non-fatal: the worker still has its task.
  // The tab name is the ONLY join back from this tab to its session id, and a name is
  // NOT unique — so it is made unique HERE, before the process exists. `find` returns the
  // first holder, so a label reused while an earlier worker still answers to it would
  // resolve this spawn to THAT worker and write its id into this one's ledger record.
  // Deriving a free name first, and snapshotting who held it, is what makes the join
  // unambiguous whatever produced the earlier holder — a guard on the name, not a fix for
  // one cause of a collision. Measured 2026-09-22: a spawn whose name collided returned
  // `sessionId: null` and wrote no ledger record, while the same call with a free name
  // resolved and wrote one.
  const tabName = uniqueTabName(`⚙ ${label}`)
  const preexisting = new Set(sessionIdsNamed(tabName) ?? [])
  const inner = `cd ${shellQuote(cwd)} && exec ${claudeCmd} -n ${shellQuote(tabName)} ${shellQuote(prompt)}`
  // `--window-id` is what puts the tab in a ROLE's window. Omitted, the tab inherits
  // WEZTERM_PANE from the calling session and lands in whatever window the caller is
  // in — which is how a human-only task came up in the Agents window (2026-09-20).
  // Passed only when the caller resolved one: an empty value would be an invalid
  // window id rather than "no preference", so the flag is dropped entirely.
  const spawnArgs = ['cli', 'spawn']
  if (windowId !== undefined && windowId !== null && String(windowId).trim() !== '') {
    spawnArgs.push('--window-id', String(windowId))
  }
  spawnArgs.push('--', 'bash', '-lc', inner)
  const res = spawnSync('wezterm', spawnArgs, { encoding: 'utf8' })

  if (res.error || res.status !== 0) {
    const why = res.error?.message || res.stderr?.trim() || `exit ${res.status}`
    return { error: `could not open a tab: ${why}` }
  }
  const paneId = (res.stdout || '').trim()
  log(`interactive worker ${id} opened in a tab (pane ${paneId || 'unknown'})`)

  // The colour is a ROLE signal, so it comes from the same resolution that chose the
  // window — one lookup, so the two can never disagree. `SUPERVISOR_WORKER_COLOR` is an
  // explicit operator override and wins; `off` still means "send nothing".
  //
  // A spawn whose role did not resolve gets NO colour rather than the old hardcoded pink:
  // a wrong role signal is worse than a missing one, and painting everything the agent
  // colour is precisely how a manager came up indistinguishable from a worker.
  const colorCommand = config.workerColor || (chip ? `/color ${chip}` : null)
  let color = null
  if (paneId && colorCommand && colorCommand !== 'off') {
    // Confirmed against the pane's own output, not the send's exit code — the exit code
    // is true whether or not the message submitted, which is how this reported
    // `applied: true` for a colour that never applied (3 of 6 spawns, 2026-09-20).
    color = await sendToPane(paneId, colorCommand, { confirm: { marker: 'Session color set to' } })
    if (color.error) log(`WARNING: worker ${id} colour not applied: ${color.error}`)
  }

  // A tab worker is a separate process, so unlike a headless one its session id is
  // never reported to us — and without it there is no key for the ledger. The registry
  // carries the tab name we set, so that is the join. Polled rather than assumed,
  // because the session registers about a second after the pane opens.
  //
  // `exclude` carries the holders snapshotted before the spawn, so a name taken in the
  // race between that snapshot and this poll still cannot be matched. The poll can then
  // only resolve to the session this call created — or to nothing, which is the honest
  // "never registered" the caller already has to handle.
  let sessionId = null
  if (paneId) {
    const deadline = Date.now() + 8000
    while (!sessionId && Date.now() < deadline) {
      sessionId = findRegisteredByName(tabName, { exclude: preexisting })
      if (!sessionId) await new Promise((resolve) => setTimeout(resolve, 250))
    }
  }
  return { paneId: paneId || null, color, sessionId, launcher: claudeCmd }
}

// Interactive is the DEFAULT, and `interactive` here is deliberately NOT defaulted in
// the signature. A worker must come up with the same tooling a normal session has — the
// launcher's env, plugin skills, MCP servers, settings.json permissions — and one
// missing that is not a cheaper worker, it is one that fails in unfamiliar ways and
// cannot be watched. But WHERE that default comes from is the point: a code default is
// invisible to the operator and was, in practice, overridden by every command file that
// passed the argument explicitly. So an omitted argument stays `undefined` all the way
// into resolveSpawnMode, which consults SUPERVISOR_SPAWN_MODE and then the operator's
// config.json before falling back. Passing `interactive` explicitly still wins — it is
// the debug escape hatch, and it has to work in both directions.
//
// `resume` opens a NEW session continuing a CLOSED one's conversation. The session
// must be closed: resuming a live one puts two writers on one conversation, which is
// what the guard below — see liveness.mjs — refuses rather than silently producing.
async function spawnAgent({ prompt, cwd, label, interactive, resume, policy: policyPath, windowId, role }) {
  const id = `agent_${++seq}`

  // Resolved first, because every guard below asks which way this worker opens and they
  // must all get the same answer. A bad value in the env or the file refuses here,
  // before a worker, a ledger record or a tab exists.
  const spawnMode = resolveSpawnMode({
    interactive,
    env: config.spawnMode,
    file: config.configFileContents,
    path: config.configFile,
  })
  if (spawnMode.error) return { error: spawnMode.error }
  const opensInteractive = spawnMode.mode === 'interactive'

  // Resolved on the same principle and at the same point: a bad role costs no worker, no
  // ledger record and no tab. `resolveRole` refuses an unknown role and only DEGRADES on
  // an unusable map, because the map publishes on a reconcile tick and a headless worker
  // has no window to route — a missing map must not block a spawn that never needed one.
  const roleResolution = resolveRole({ role, map: readRoleMap() })
  if (roleResolution.error) return { error: roleResolution.error }
  if (roleResolution.warning) log(`WARNING: worker ${id}: ${roleResolution.warning}`)

  // An explicit window id WINS — the caller may need a window the map does not describe.
  // Otherwise the role decides, in-process, and no window id crosses the tool boundary.
  const targetWindowId = windowId ?? roleResolution.windowId

  // Refused before the liveness probe: there is no point guarding an argument the tab
  // path would drop anyway, and the caller needs to hear about the limitation rather
  // than about the session's liveness.
  const unsupported = resumeSupportError({ resume, interactive: opensInteractive })
  if (unsupported) return { error: unsupported }

  // Same reasoning and the same class of bug: a policy the tab path cannot consult is
  // refused, not accepted and quietly ignored.
  const unsupportedPolicy = policySupportError({ policy: policyPath, interactive: opensInteractive })
  if (unsupportedPolicy) return { error: unsupportedPolicy }

  // Resolved before anything is spawned, so a bad path costs no worker, no ledger
  // record, and no half-started session running under rules nobody chose.
  const workerCwd = cwd || process.cwd()
  let workerRules = null
  let resolvedPolicyPath = null
  if (policyPath) {
    // Checked before the file, because the mode decides whether the file could ever
    // matter. A policy that cannot be REACHED is the same failure as one that is never
    // read — accepted, reported as applied, and inert — which is the failure this repo
    // already shipped once, so it is refused rather than warned about.
    const mode = await effectivePermissionMode(workerCwd)
    if (POLICY_UNREACHABLE_MODES.includes(mode)) {
      return {
        error:
          `policy would never be consulted: this worker's effective permission mode is "${mode}", which answers ` +
          `tool calls without consulting the PermissionRequest hook, so ${policyPath} would be accepted and then ` +
          `ignored. The mode comes from settings, not from the spawn. Omit policy to spawn the worker anyway under ` +
          `that mode's own rules, or set permissions.defaultMode to "default" in the settings tier that supplies it.`,
      }
    }
    const resolved = resolveSpawnPolicy(policyPath, workerCwd)
    if (resolved.error) return { error: resolved.error }
    workerRules = resolved.rules
    resolvedPolicyPath = resolved.path
  }

  if (resume) {
    const { live, probes, reason } = checkLiveness(resume)
    if (live === true) {
      return {
        error: `session ${resume} is still running (${reason}) — close it before resuming, or you will have two writers on one conversation`,
      }
    }
    // The half `checkLiveness` cannot see, and the only channel that can see it: a
    // headless worker this server spawned is an in-process SDK `query()`, so it has no
    // pid for the registry and no argv for any process probe. Its record here is the
    // sole evidence it is running, and `status` genuinely closes — `running` is set at
    // spawn and cleared to `done`/`error` when the query ends — unlike the ledger's
    // `running`, which never closes at all.
    //
    // Without this, a `false` from the registry would be read as "closed" and a live
    // worker resumed: two writers on one conversation, the corruption this guard
    // exists to prevent. Scoped to this server's own spawns by construction; a worker
    // spawned by a different server process is not visible here.
    const running = [...agents.values()].find(
      (a) => a.sessionId === resume && a.status === 'running',
    )
    if (running) {
      return {
        error: `session ${resume} is still running (worker ${running.id} is mid-turn in this server) — close it before resuming, or you will have two writers on one conversation`,
      }
    }
    // Neither probe could be read. Fail CLOSED: "could not tell" and "confirmed
    // closed" are different answers, and only one of them is safe to act on. A
    // refused resume costs one retry; an unguarded one corrupts a conversation.
    if (live === null) {
      log(`refusing to resume ${resume}: ${reason}`)
      return {
        error: `cannot confirm session ${resume} is closed (${reason}) — refusing, since resuming a live session puts two writers on one conversation. Point SUPERVISOR_SESSIONS_DIR at the session registry if it lives somewhere else.`,
      }
    }
    log(`resume of ${resume} allowed: ${reason} [probes: ${probes.join(', ') || 'none'}]`)
  }

  const agent = {
    id,
    label: label || id,
    prompt,
    cwd: workerCwd,
    status: 'running',
    // The rule set this worker's permission hook evaluates: the server's when no policy
    // was named, its own overlay when one was.
    rules: workerRules ?? policy.rules,
    policyPath: resolvedPolicyPath,
    sessionId: null,
    // Which conversation this one continues, when it is an adoption rather than a
    // fresh start. `sessionId` alone cannot say: on resume the SDK reports the SAME
    // id, so the two fields together are what tell the operator which conversation
    // they are now in.
    resumedFrom: resume ?? null,
    // The spawn edge, resolved once: the manager session that called spawn_agent, found
    // by walking up from our own pid to the nearest ancestor the live registry knows.
    // NOT our direct parent — `.mcp.json` starts this server through a `bun run`
    // wrapper, so the direct parent is that wrapper and a bare `process.ppid` lookup
    // named nothing, which is why this field was null in every record ever written.
    // Stamped here so it outlives the registry entry it came from.
    parentSession: parentSessionId({ dir: config.sessionsDir }),
    // Which of argument / env / config / default decided the mode. Carried on the agent
    // so agent_status, list_agents and the ledger all answer "why is this worker
    // headless" from one value rather than three independent guesses.
    modeSource: spawnMode.source,
    // What the role resolved to, and the window actually targeted. Carried on the agent so
    // the reply, agent_status and the ledger can all answer "where did this spawn go, and
    // why" from one value — and so a role-routed spawn is OBSERVABLE rather than inferred
    // from wherever the tab happened to land.
    role: roleResolution.role,
    windowId: targetWindowId ?? null,
    launcher: null,
    permissions: [],
    transcript: [],
    error: null,
    createdAt: new Date().toISOString(),
  }
  agents.set(id, agent)

  if (opensInteractive) {
    agent.status = 'interactive'
    const res = await spawnInteractiveAgent({ id, prompt, cwd: agent.cwd, label: agent.label, windowId: targetWindowId, chip: roleResolution.chip })
    if (res.error) {
      agents.delete(id)
      return { error: res.error }
    }
    agent.paneId = res.paneId
    agent.sessionId = res.sessionId ?? null
    agent.launcher = res.launcher ?? null
    agent.transcriptDir = transcriptDirFor(agent.cwd)
    writeLedger(agent)
    return {
      agent_id: id,
      label: agent.label,
      cwd: agent.cwd,
      status: agent.status,
      interactive: true,
      mode_source: agent.modeSource,
      role: agent.role,
      window_id: agent.windowId,
      pane_id: agent.paneId,
      // Resolved from the registry by the tab name we set — a tab worker is a separate
      // process, so this is the only way to learn it. Null when it did not register in
      // time, which is also why the ledger has no record for such a worker.
      session_id: agent.sessionId,
      transcript_dir: agent.transcriptDir,
      // Whether the colour actually landed, not that we asked for it: `sendToPane` was
      // asked to confirm against the pane's own "Session color set to" output and
      // returns an error when that never appears. Null when colouring is off. The
      // previous version of this comment claimed the same guarantee while the code
      // checked only that the send had not errored — which is how a spawn reported
      // `applied: true` for a colour that never applied (3 of 6 spawns, 2026-09-20).
      color: res.color ? (res.color.error ? { error: res.color.error } : { applied: true }) : null,
    }
  }

  // Resolved once and kept, so the ledger records which launcher the worker inherited
  // rather than re-deriving it later from a config that may have changed.
  agent.launcher = resolveClaudeCmd(agent.cwd)

  const q = query({
    prompt,
    options: {
      cwd: agent.cwd,
      // Hand the worker the mode this server already resolved, so it never has to infer
      // its own from a config file that describes the fleet rather than this worker. The
      // SDK's `env` REPLACES the subprocess environment instead of merging with it, which
      // is why workerEnvFor spreads the inherited env — without that spread the worker
      // loses PATH, HOME and ANTHROPIC_BASE_URL, and the last of those silently stops it
      // routing through the router. That base env comes from config.mjs rather than being
      // read here, because this module owns no environment reads at all.
      env: workerEnvFor({ mode: spawnMode.mode, source: spawnMode.source, env: config.baseEnv }),
      // Load the same settings an interactive session gets. Without this the SDK
      // starts from nothing — no plugin skills, no settings.json permissions, no
      // user MCP servers — and a worker missing its normal tooling is not a cheaper
      // worker, it is one that fails in unfamiliar ways.
      settingSources: SETTING_SOURCES,
      // Same reason as settingSources: parity with a tab worker. Spawning is rare, so
      // the per-spawn launcher lookup is not a hot path.
      mcpServers: resolveMcpServers(agent.launcher),
      // Continuing an existing conversation. Because THIS call creates the session,
      // canUseTool applies to it — which is the whole point: a session you did not
      // create cannot be supervised, but one you resume you do create.
      ...(resume ? { resume } : {}),
      // Resolved and validated once at module load — never read env in the hot path.
      permissionMode: PERMISSION_MODE,
      canUseTool: makeCanUseTool(agent),
      hooks: { PermissionRequest: [{ hooks: [makePermissionHook(agent)] }] },
    },
  })
  agent.query = q

  // Fire-and-forget on purpose: a spawn must return its agent id without waiting for
  // the worker's first turn. The loop itself lives in agent-loop.mjs so a test can
  // drive it with a synthetic stream and await its completion — this module cannot be
  // imported by a test at all, since it connects a stdio server at load.
  void runAgentLoop({ q, agent, writeLedger, log })

  return {
    agent_id: id,
    label: agent.label,
    cwd: agent.cwd,
    status: agent.status,
    interactive: false,
    mode_source: agent.modeSource,
    // Reported here for the SAME reason as on the tab path, and it is not redundant: the
    // resolution runs whichever way the worker opens, so a caller that declares a role is
    // entitled to see what it resolved to. `window_id` is carried even though a headless
    // worker has no tab to put it in — `interactive: false` sits in this same object, so
    // "resolved" is not misread as "routed". Omitting both is what made a headless
    // spawn's routing unverifiable from its own response.
    role: agent.role,
    window_id: agent.windowId,
    // Reported so a caller can see which policy actually took effect, rather than
    // inferring it from the absence of an error.
    policy: agent.policyPath,
  }
}

const lastAssistantText = (agent) => {
  for (let i = agent.transcript.length - 1; i >= 0; i--) {
    const m = agent.transcript[i]
    if (m.type === 'assistant' && Array.isArray(m.message?.content)) {
      const text = m.message.content.filter((c) => c.type === 'text').map((c) => c.text).join('\n')
      if (text) return text.slice(0, 2000)
    }
  }
  return null
}

const agentView = (a) => {
  // The in-memory transcript is pushed by the headless SDK loop alone, so it is empty for
  // exactly the workers `agent_status` used to report null for — the tab ones. The disk
  // read is what answers for them, and it is skipped when memory already holds the text.
  const transcript = transcriptPathFor(a.sessionId)
  const fromDisk = a.transcript.length === 0 ? lastAssistantTextFrom(transcript) : null
  // Read from disk for BOTH kinds of worker, unlike the message above. The in-memory array
  // does carry the call, but no timestamp with it, so a duration cannot come from there —
  // and duration is the half of this field that turns "git fetch" into "git fetch, 14
  // minutes", which is what makes a held worker visible without a pane.
  const currentToolCall = currentToolCallFrom(transcript)
  // `null` means the registry does not list this session — a different fact from any
  // status value, and from "not waiting". See tab-read.mjs.
  const sessionStatus = sessionStatusFor(a.sessionId)
  return {
    agent_id: a.id,
    label: a.label,
    status: a.status,
    cwd: a.cwd,
    session_id: a.sessionId,
    resumed_from: a.resumedFrom ?? null,
    // Null means the worker runs under the server policy — the same absence-means-default
    // the ledger record uses, so the two cannot disagree about what "no policy" reads as.
    policy: a.policyPath ?? null,
    // Which source decided interactive-vs-headless: argument, env, config or default. A
    // worker that opened the wrong way is otherwise diagnosed by guessing which of four
    // places was consulted.
    mode_source: a.modeSource ?? null,
    // Same id = the conversation continued; a different one = the SDK forked it. Only
    // answerable once init has reported a session id, so it is null until then rather
    // than a guess.
    continued: a.resumedFrom && a.sessionId ? a.sessionId === a.resumedFrom : null,
    created_at: a.createdAt,
    pending_permissions: a.permissions.filter((id) => pending.has(id)),
    // Memory first — free, and already correct for headless workers — then the worker's
    // own transcript on disk, which is the only source a tab worker has.
    last_message: lastAssistantText(a) ?? fromDisk,
    // The call the worker is inside right now and how long it has been held, or null when
    // nothing is in flight. This is the field that separates a worker executing a long tool
    // call from one parked on a gate: `session_status` says both are not-idle, and this says
    // which call, for how long. Read from the transcript, not from memory — see above.
    current_tool_call: currentToolCall,
    // Read from the session registry, NOT the transcript: a pending tool call reads the
    // same whether the worker is executing a tool or parked on a permission prompt, so the
    // transcript cannot separate them. `awaiting_input` is null rather than false when the
    // registry does not list the session — unlisted is not the same as not waiting.
    session_status: sessionStatus,
    awaiting_input: awaitingInput(sessionStatus),
    result: a.result ?? null,
    error: a.error,
  }
}

const TOOLS = [
  {
    name: 'spawn_agent',
    description: 'Start a new Claude session (worker). It runs with normal permissions, so any tool it needs approval for is parked for you to answer.',
    inputSchema: {
      type: 'object',
      properties: {
        prompt: { type: 'string', description: 'The task for the new session.' },
        cwd: { type: 'string', description: 'Working directory (default: supervisor cwd).' },
        label: { type: 'string', description: 'Short label so you can tell agents apart.' },
        role: {
          type: 'string',
          enum: ['manager', 'agent', 'human'],
          description:
            'The role this session plays, which resolves BOTH its colour and its window from the role map the WezTerm config publishes (~/.cache/wezterm-role-map.json) — manager → orange/Managers, agent → pink/Agents, human → cyan/Direct. Omit for agent, which is the correct default for every task that has not declared a role. PREFER THIS OVER window_id: a role is a word, so it cannot be mistyped or dropped in transit, and the id is looked up in-process at the moment of spawn instead of being carried across the tool boundary — which is where `window_id: 0` was lost intermittently (measured 2026-09-20: 4 of 6 spawns routed, and the two failures were a run\'s first spawn). Passing a role alone is the reliable path.',
        },
        window_id: {
          type: 'string',
          description:
            'An EXPLICIT WezTerm window to open the tab in, overriding whatever `role` resolved. Usually unnecessary — prefer `role`, which resolves the window from the role map in-process. Pass this only when you need a window the role map does not describe. Omit to let `role` decide, or — with no role either — to inherit the calling session\'s window, which is the old behaviour. Tab path only: a headless worker has no tab, so the value is ignored there.',
        },
        interactive: {
          type: 'boolean',
          description:
            'Per-call override of the fleet default. OMIT IT unless you specifically need to force one mode for this one worker: with no argument the server uses SUPERVISOR_SPAWN_MODE, then spawn.mode in ~/.config/claude-supervisor/config.json, then its built-in default of interactive — so the operator changes the whole fleet in one edit instead of in every command file. true opens the worker as a real session in a wezterm tab, with the same tooling a normal session has (launcher env, plugin skills, MCP servers, settings.json permissions), watchable and drivable by hand; its approval prompts are answered IN THAT TAB, so it never appears in pending_permissions. false opens a headless worker whose prompts park for the manager instead, answered with answer_permission. Tooling is the same in both: a headless worker loads the same settingSources and the launcher\'s own MCP servers (measured 2026-09-18 — a headless worker resolved vault MCP tools and returned a real vault hit). The difference is who answers its prompts and whether you can watch it.',
        },
        resume: {
          type: 'string',
          description:
            'Session id to continue. Requires interactive:false — a tab worker cannot honour it (the tab path launches the cc-* launcher, which is never handed the flag) and the call is refused rather than quietly opening a fresh conversation. The session MUST be closed: resuming a live one puts two writers on one conversation, so a session found still running is refused, and so is one whose liveness cannot be determined. The resumed worker is created here, so unlike the original session it IS supervised and its prompts park for the manager.',
        },
        policy: {
          type: 'string',
          description:
            'Path to a JSON file of approval rules for THIS worker, evaluated ahead of the user and bundled rules — first match wins, so a rule here beats both, while the bundled set still covers whatever it does not name. Full replacement is reachable by ending the file with a {"tool":"*","match":"*","action":"escalate"} catch-all. An absolute path is used as-is; a relative one resolves against the worker\'s cwd. The file must exist and parse — a named policy that cannot be read refuses the spawn rather than silently falling back to the server default. Headless only: a tab worker answers its own prompts in its tab, so combining this with interactive:true is refused. Omit to use the server policy.',
        },
      },
      required: ['prompt'],
    },
  },
  {
    name: 'send_agent_message',
    description:
      'Send a follow-up message to a running INTERACTIVE (tab) worker — the send_to_agent this server has never had. It activates the worker tab, waits for the input prompt, and types the message, so it STEALS FOCUS and only works on a tab worker: a headless worker has no pane to send into. Returns an error rather than a false success when the pane is gone, the tab cannot be activated, or the prompt never appears.',
    inputSchema: {
      type: 'object',
      properties: {
        agent_id: { type: 'string', description: 'The tab worker to message.' },
        message: { type: 'string', description: 'The message to type into its input box and submit.' },
      },
      required: ['agent_id', 'message'],
    },
  },
  {
    name: 'list_agents',
    description: 'All spawned sessions with status and how many permissions are pending. Each row also carries `current_tool_call` (the call in flight and how long it has been held) and `last_message`, so one sweep says what every worker is doing without a pane.',
    inputSchema: { type: 'object', properties: {} },
  },
  {
    name: 'agent_status',
    description: 'One session: status, last assistant message, the current tool call, result, pending permission ids. `current_tool_call` is `{name, input_summary, started_at, held_seconds}` — the call the worker is inside right now and how long it has been held — or null when nothing is in flight; it is read from the worker\'s transcript and is what tells a long tool call apart from a worker parked on a gate. For a tab worker the last message is read from its transcript on disk. `session_status` and `awaiting_input` come from the session registry; `awaiting_input` is null (not false) when the session is not listed, and means "blocked on input" rather than specifically "a permission gate is open".',
    inputSchema: { type: 'object', properties: { agent_id: { type: 'string' } }, required: ['agent_id'] },
  },
  {
    name: 'pending_permissions',
    description: 'Permission prompts currently waiting for your answer, across all agents.',
    inputSchema: { type: 'object', properties: {} },
  },
  {
    name: 'await_permission',
    description: 'Block until any agent asks for permission, or until the timeout. Use this instead of polling.',
    inputSchema: {
      type: 'object',
      properties: { timeout_ms: { type: 'number', description: 'Default 60000, max 600000.' } },
    },
  },
  {
    name: 'answer_permission',
    description: 'Answer a parked permission prompt: allow or deny. This is what unblocks the worker.',
    inputSchema: {
      type: 'object',
      properties: {
        request_id: { type: 'string' },
        behavior: { type: 'string', enum: ['allow', 'deny'] },
        message: { type: 'string', description: 'Reason shown to the agent when denying.' },
      },
      required: ['request_id', 'behavior'],
    },
  },
]

// Read the version from the plugin manifest rather than hardcoding it. A literal
// here drifts silently on every release, and scripts/check-versions.py — which
// guards the four version strings — cannot see a fifth one buried in code.
function pluginVersion() {
  const manifest = new URL('../.claude-plugin/plugin.json', import.meta.url).pathname
  try {
    const version = JSON.parse(readFileSync(manifest, 'utf8')).version
    if (!version) throw new Error('no "version" field')
    return version
  } catch (error) {
    // Loud, not silent: a wrong '0.0.0' would look like an absent version and hide
    // a manifest that check-versions.py believes is fine.
    process.stderr.write(`[supervisor] WARNING: cannot read version from ${manifest}: ${error.message}\n`)
    return '0.0.0'
  }
}

// The role map, re-read on every spawn rather than cached at boot — see config.roleMap.
//
// A missing map is SILENCE, while a map that EXISTS and cannot be parsed is REPORTED: it
// is a file the WezTerm config believes it published. That is the same split the config
// file read makes, for the same reason — absence is normal (WezTerm may not be running,
// and a headless worker has no tab), corruption is not.
function readRoleMap() {
  let raw
  try {
    raw = readFileSync(config.roleMap, 'utf8')
  } catch (error) {
    if (error.code !== 'ENOENT') log(`WARNING: cannot read the role map at ${config.roleMap}: ${error.message}`)
    return null
  }
  try {
    return JSON.parse(raw)
  } catch (error) {
    log(`WARNING: the role map at ${config.roleMap} is not valid JSON: ${error.message}`)
    return null
  }
}

const server = new Server({ name: 'supervisor', version: pluginVersion() }, { capabilities: { tools: {} } })

server.setRequestHandler(ListToolsRequestSchema, async () => ({ tools: TOOLS }))

server.setRequestHandler(CallToolRequestSchema, async (request) => {
  const args = request.params.arguments ?? {}
  const reply = (payload) => ({ content: [{ type: 'text', text: JSON.stringify(payload, null, 2) }] })

  switch (request.params.name) {
    case 'spawn_agent':
      if (typeof args.prompt !== 'string' || !args.prompt.trim()) {
        return reply({ error: 'prompt is required' })
      }
      return reply(
        await spawnAgent({
          prompt: args.prompt,
          cwd: args.cwd,
          label: args.label,
          // Passed through as-is, undefined included. Coercing an omitted argument to a
          // boolean here is what made the config unreachable: the server would receive
          // an explicit mode on every call and never consult anything else.
          interactive: typeof args.interactive === 'boolean' ? args.interactive : undefined,
          resume: args.resume,
          policy: args.policy,
          // Passed through raw and validated in role-map.mjs, which owns the legal values
          // and the refusal message — the same split as the spawn mode below.
          role: args.role,
          // An EXPLICIT override, not the primary path. `role` above resolves the window
          // in-process from the role map; this is passed through untouched, `undefined`
          // included, so the tab path can still tell "no preference" from an explicit id.
          //
          // Kept because a caller may need a window the map does not describe. It is no
          // longer how a role is routed: `window_id: 0` reached the server only 4 times
          // in 6 (measured 2026-09-20), and not crossing this boundary is the whole point
          // of resolving a role instead. See window-id.mjs and role-map.mjs.
          windowId: windowIdArgument(args.window_id),
        }),
      )

    case 'send_agent_message': {
      const agent = agents.get(args.agent_id)
      if (!agent) return reply({ error: `unknown agent ${args.agent_id}` })
      if (typeof args.message !== 'string' || !args.message.trim()) {
        return reply({ error: 'message is required' })
      }
      // Refused rather than attempted: a headless worker has no pane, and typing into
      // one that does not exist is how a channel reports success while delivering
      // nothing. Its prompts are answered with answer_permission instead.
      if (agent.status !== 'interactive' || !agent.paneId) {
        return reply({
          error: `agent ${args.agent_id} is not a tab worker (status "${agent.status}", pane ${agent.paneId ?? 'none'}) — send_agent_message reaches a pane, so it only works on interactive workers`,
        })
      }
      const res = await sendToPane(agent.paneId, args.message)
      if (res.error) return reply({ error: res.error })
      agent.messages = [...(agent.messages ?? []), { at: new Date().toISOString(), text: args.message }]
      log(`sent a follow-up message to ${agent.id} (pane ${agent.paneId}, tab ${res.tabId})`)
      return reply({ agent_id: agent.id, sent: true, tab_id: res.tabId, pane_id: res.paneId })
    }

    case 'list_agents':
      return reply([...agents.values()].map(agentView))

    case 'agent_status': {
      const agent = agents.get(args.agent_id)
      return agent ? reply(agentView(agent)) : reply({ error: `unknown agent ${args.agent_id}` })
    }

    case 'pending_permissions':
      return reply([...pending.values()].map(publicPerm))

    case 'await_permission': {
      const timeout = Math.min(Math.max(Number(args.timeout_ms) || 60000, 1000), 600000)
      const perm = await waitForPermission(timeout)
      return reply(perm ?? { timed_out: true, note: 'no permission request within the window' })
    }

    case 'answer_permission': {
      const record = pending.get(args.request_id)
      if (!record) {
        return reply({ error: `no pending permission ${args.request_id} (already answered, or expired)` })
      }
      // Record the manager's verdict too: a key the manager allows over and over
      // is the raw material for the next policy rule.
      const logDecision = (behavior) =>
        logPermission({
          ts: new Date().toISOString(),
          agent: record.agentId,
          tool: record.toolName,
          key: inputKey(record.input),
          matched_rule: null,
          decision: behavior,
          decided_by: 'manager',
          request_id: args.request_id,
          latency_ms: Date.now() - Date.parse(record.requestedAt),
        })

      if (args.behavior === 'allow') {
        logDecision('allow')
        record.settle({ behavior: 'allow' })
        log(`permission ${args.request_id} ALLOWED by manager`)
        return reply({ answered: args.request_id, behavior: 'allow', agent_id: record.agentId })
      }
      logDecision('deny')
      record.settle({ behavior: 'deny', message: args.message || 'Denied by the supervising session.' })
      log(`permission ${args.request_id} DENIED by manager`)
      return reply({ answered: args.request_id, behavior: 'deny', agent_id: record.agentId })
    }

    default:
      return reply({ error: `unknown tool ${request.params.name}` })
  }
})

await server.connect(new StdioServerTransport())
log('supervisor ready')

// Said once at startup, so an operator learns the policy layer is unreachable without
// having to spawn a worker and wonder why nothing happened. The per-spawn path refuses
// loudly for the case that matters; this is the standing condition behind it.
//
// After connect(), not before: this awaits an @alpha SDK call, and a diagnostic must
// never be able to delay the server it diagnoses — every session pays that startup.
const startupMode = await effectivePermissionMode(process.cwd())
if (POLICY_UNREACHABLE_MODES.includes(startupMode)) {
  log(
    `WARNING: effective permission mode is "${startupMode}" — the PermissionRequest hook is never consulted, so ` +
      `policy.json (${USER_POLICY} and the bundled defaults) has no effect on any worker. See README § Status.`,
  )
}
