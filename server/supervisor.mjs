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
import { appendFileSync, existsSync, mkdirSync, readFileSync, writeFileSync } from 'fs'
import { homedir } from 'os'
import { isAbsolute, join } from 'path'
import { realpathSync } from 'fs'
import { spawnSync } from 'child_process'
import { config } from './config.mjs'
import { runAgentLoop } from './agent-loop.mjs'
import { HEARTBEAT_INTERVAL_MS, a2aStamp, clearStamp, heartbeatDir, selfStampRefusal, stampRecord, startSelfStamp } from './heartbeat.mjs'
import { workerSessions } from './worker-sessions.mjs'
import { pollCluster } from './cluster-heartbeat.mjs'
import { startAttentionPoll, storeDecisionRecord } from './attention-poll.mjs'
import { startMessageDelivery, storeMessageRecord } from './message-delivery.mjs'
import { POLICY_UNREACHABLE_MODES, resolveEffectiveMode } from './mode.mjs'
import { decide as decideWith, inputKey, overlayRules } from './policy.mjs'
import { checkLiveness, findRegisteredByName, sessionIdsNamed, uniqueTabName } from './liveness.mjs'
import { DEFAULT_MAX_CONCURRENT, DEFAULT_MAX_CONCURRENT_HARD, concurrentLimitRefusal, managerLimitRefusal, resolveEnvOverrides, resolveMaxConcurrent, resolveMaxManagers, resolveSpawnMode, resolveSpawnTarget, shellEnvExports, shellQuote, spawnTargetImplementationRefusal, unknownKeyWarnings, workerEnvFor } from './spawn-mode.mjs'
import { randomUUID } from 'node:crypto'
import { A2A_SPAWN_TARGETS, a2aSpawnArgumentRefusal, a2aTurnPatch, a2aWorkerRecord, collectWorkerTurn, createWorkerOnController, fetchWorkerCard, ledgerModeForStatus, listWorkersOnController, openWorkerTurn, pendingPushRegistration, pollA2A, pushDiagnosticPatch, readPushCallbackFromWorker, reapExistingWorkerForTask, registerPushCallbackOnWorker, resolveA2AControllerTarget, stopWorkerOnController } from './a2a-spawn.mjs'
import { createA2APushRegistry } from './a2a-push-registry.mjs'
import { RATE_STATE_FILE, reserveSlot, resolveMaxStarts } from './start-rate.mjs'
import { parseLauncherModel, resolveManagerLauncher, resolveTaskLauncher, resolveWorkerTarget } from './spawn-cwd.mjs'
import { readTaskLaunchers } from './task-launcher.mjs'
import { CLUSTER_ANSWER_MAX, classifyClusterAnswer, newSessionId, resolveClusterTarget, startClusterSession } from './cluster-spawn.mjs'
import { resolveMessageChannel } from './message-channel.mjs'
import { bindSessionToTask } from './task-binding.mjs'
import { windowIdArgument } from './window-id.mjs'
import { resolveRole } from './role-map.mjs'
import { managerSessions } from './manager-sessions.mjs'
import { mtimeReloadedConfig } from './config-reload.mjs'
import { policySupportError, resumeSupportError, sendToPane } from './tab.mjs'
import { buildRecord, ledgerWriteRoute, mergeRecord, parentSessionId, stoppedPatch, UNOBSERVED_STATUS, unobservedPatch } from './ledger.mjs'
import { awaitingInput, currentToolCallFrom, lastAssistantTextFrom, sessionStatusFor, transcriptPathFor } from './tab-read.mjs'
import { findLiveHolder } from './resume-guard.mjs'
import { fetchAttentionItems, fetchOpenCards, openCardsOf } from './open-cards.mjs'
import { foreignParkRefusal } from './park-refusal.mjs'
import { buildParkRecord, clearParkPatch, PARK_FIELD } from './park-record.mjs'
import { renderResumePrompt, validateDecision } from './resume-decision.mjs'
import { buildCarriedDecision, mayApplyAllow, settleFromCarried } from './decision-settle.mjs'
import { SHIPPING_PERMISSION_MODE, shippingSettings, shippingSupportError } from './shipping-settings.mjs'
import { liveSessionIds, rehydratableAgents, unionRoster } from './registry-rehydrate.mjs'
import { patchSessionLedgerRow, postSessionLedger, readSessionLedger, readSessionLedgerRow, recordToWire } from './session-ledger.mjs'

const PERMISSION_TIMEOUT_MS = 15 * 60 * 1000

const agents = new Map() // id -> agent record

// The worker→spawner status channel: one loopback receiver per server, started at boot, and the
// per-worker tokens it accepts.
//
// ⚠️ ONE PER SERVER, AND THAT IS THE DESIGN RATHER THAN AN IMPLEMENTATION DETAIL. The callback
// URL travels per `create`, so the pushes for a worker land in the records of the session that
// SPAWNED it — which is the session whose `agent_status` / `list_agents` must show them. A shared
// receiver would need a worker→session routing table that does not exist today.
//
// ⚠️ THE BIND IS LAZY AND ITS FAILURE IS NON-FATAL. A receiver that cannot take a port must not
// make every A2A spawn fail: the channel is additive, and a spawn without it produces exactly the
// pre-change worker. `ensureListening()` is called at boot so the port is ready before the first
// spawn, and again by `beginCreate` — which is what keeps a boot-time failure from being the only
// attempt, without ever retrying per spawn.
const a2aPushRegistry = createA2APushRegistry()

// A headless worker's liveness, published where a process that did NOT spawn it can read it.
//
// The `agents` Map above answers this question correctly and only for this server's own
// workers, which is precisely the gap: the manager that must decide whether to resume a
// worker is the one caller that has no access to this process. So the answer is written to
// disk while the worker runs, and read back by whoever needs it. See heartbeat.mjs.
//
// ⚠️ The refresh timer is the load-bearing half. A stamp written once at start and removed at
// the end answers only the graceful case — `kill -9` leaves it behind forever, and a worker
// that reads live for good is the defect this replaces. Re-stamping every INTERVAL means the
// stamp goes stale on its own, so the ungraceful case needs no separate handling.
const heartbeat = {
  timers: new Map(),
  start(sessionId) {
    this.stop(sessionId)
    this.stamp(sessionId)
    const timer = setInterval(() => this.stamp(sessionId), HEARTBEAT_INTERVAL_MS)
    // Unref'd so a pending refresh never holds the process open after its work is done.
    timer.unref?.()
    this.timers.set(sessionId, timer)
  },
  stop(sessionId) {
    const timer = this.timers.get(sessionId)
    if (timer) {
      clearInterval(timer)
      this.timers.delete(sessionId)
    }
    clearStamp(sessionId, { dir: heartbeatDir })
  },
  stamp(sessionId) {
    try {
      stampRecord(heartbeatDir, { sessionId, pid: process.pid, mode: 'headless' })
    } catch (error) {
      // A worker whose stamp cannot be written still runs. Losing the heartbeat degrades a
      // manager's verdict to "could not tell" — which is the honest answer — whereas failing
      // the spawn would turn an unwritable state directory into a broken fleet.
      log(`WARNING: cannot stamp heartbeat for ${sessionId}: ${error.message}`)
    }
  },
  // The session this server runs INSIDE.
  //
  // ⚠️ The lifecycle itself lives in `heartbeat.mjs`, which owns the stamp and can therefore
  // be tested — `supervisor.mjs` has no export surface (a test importing it would start an MCP
  // server), so a self-stamp owned here could never be covered at all. See `startSelfStamp`
  // for the design notes: the per-worker stamps cannot cover this session, the anchor is
  // deliberately omitted, and the activity starts at `idle`.
  startSelf(sessionId) {
    this.stopSelf()
    // ⚠️ Standing down is LOGGED rather than silent, because "this session has no self-stamp" is
    // otherwise indistinguishable from a stamp that failed to write — and only one of those is
    // intended. `config.workerMode` is the spawn's own handover, so a server running inside a
    // headless worker knows the stamp belongs to the server that spawned that worker; see
    // `selfStampRefusal` for the measured defect this removes.
    const refusal = selfStampRefusal({ workerMode: config.workerMode })
    if (refusal) {
      log(`no self-stamp for ${sessionId ?? 'this server'}: ${refusal}`)
      return false
    }
    this.self = startSelfStamp(sessionId, {
      workerMode: config.workerMode,
      // Same reasoning as `stamp` above: the session still runs, and a manager's verdict
      // degrades to "could not tell" rather than the server failing to start.
      onError: (error) => log(`WARNING: cannot stamp own heartbeat for ${sessionId}: ${error.message}`),
    })
    return this.self !== null
  },
  stopSelf() {
    if (this.self) {
      this.self.stop()
      this.self = undefined
    }
  },
}
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

// ⚠️ The config-dir refusal is a hard exit, not a warning, and it sits here because this is
// the first point at which the logger exists: above it `log` is in its temporal dead zone, so
// only `console` could write, and RULE node/logging/structured-not-console bars `console`
// from service code. Nothing between the imports and this line starts a session, opens a
// transport or writes state, so the refusal still lands before any work.
//
// A server whose parent carried a different `CLAUDE_CONFIG_DIR` resolves its session
// registry and transcript root from the wrong place, then runs normally while reporting a
// `transcript_dir` no fleet reader can see and registering nowhere the fleet looks — a silent
// failure, so refusing to start is the only honest outcome. See the resolution in config.mjs
// for the measurement.
if (config.claudeHomeRefusal) {
  log(config.claudeHomeRefusal)
  process.exit(1)
}

// ── roster rehydration ──────────────────────────────────────────────────────
// A reconnected MCP client (`/mcp`) gets a NEW server process, so the `agents` Map above
// comes up empty and `list_agents` answers `[]` while `agent_status` answers
// `unknown agent <id>` — even though the worker sessions are still running. The ledger
// already carries the identity of everything this supervisor spawned; this is the read side
// that was missing. See registry-rehydrate.mjs for why identity and liveness are separate
// channels, and why the ledger's own `status` must never be trusted as liveness.
//
// Best-effort by design: a store that cannot be read leaves the roster as it was before this
// fix — empty — rather than taking the server down. The warning is the observable.
{
  const rehydrated = await rehydratableAgents({ log })
  if (rehydrated === null) {
    log('WARNING: a store could not be read — the roster starts empty and will not show workers spawned before this process')
  } else {
    for (const agent of rehydrated) agents.set(agent.id, agent)
    // ⚠️ **An EMPTY store is the loud case, not the quiet one.** `[]` and `null` are
    // different answers and only one of them is benign: `null` is a store that could not be
    // read, while `[]` is a store that answered and holds nothing. On a fresh install that
    // is correct; on a deploy that has not yet run the one-time import it means every
    // worker spawned before this process is MISSING from the roster — which is worse than
    // the file ledger this replaced, and used to be indistinguishable from a healthy empty
    // fleet because the line below reads the same either way.
    if (rehydrated.length === 0) {
      log('WARNING: the ledger endpoint answered with NO rows — a fresh install looks like this, but so does a deploy whose one-time import has not run yet, in which case every worker spawned before this process is invisible to the roster')
    }
    log(`rehydrated ${rehydrated.length} agent(s) from the ledger`)
  }
}

// A rehydrated row carries a BOOT-TIME liveness reading and nothing here can refresh it — the
// ledger is the durable half, never the live one. So a worker that dies after this process
// starts would otherwise sit in the roster forever under a status that reads as fine, which
// is the mirror of the hazard the module above names. Re-checking at READ time is where the
// answer can be fresh, and it is cheap: the liveness channels are a readdir and a handful of
// files, not the ~1400-record ledger.
//
// ⚠️ `null` from the channels means "could not tell", NOT "nothing is live" — the rows are
// kept, because dropping a live worker on an unreadable store is the failure this whole
// change exists to remove, one direction over.
function pruneRehydratedAgents() {
  const adopted = [...agents.values()].filter((a) => a.rehydrated)
  if (adopted.length === 0) return
  const live = liveSessionIds()
  if (live === null) return
  for (const agent of adopted) {
    if (!live.has(agent.sessionId)) agents.delete(agent.id)
  }
}

// The roster AS OF NOW: this process's own workers, union'd with every live worker the ledger
// endpoint knows that this process did not spawn.
//
// ⚠️ **This is the fix, and the adoption block above is not a substitute for it.** Adoption
// runs ONCE, at import, and `pruneRehydratedAgents` can only remove — so a session that was not
// live at that instant stays invisible to this process for its whole life, however live it
// becomes. Measured 2026-10-13: 4 of 35 live-and-ledgered sessions were absent from a live
// roster, this server's own session id among them, because its heartbeat is written by the very
// server that performs the adoption and so cannot exist at import. A read here happens at
// REQUEST time and cannot freeze, which is the property the adoption block cannot have.
//
// ⚠️ **This process's own row WINS on a `session_id` collision**, and the direction is
// deliberate rather than incidental: an in-process agent carries the live transcript, the park
// queue and the permission rules, while an endpoint row carries identity alone. Letting the
// fresh row win would replace a working worker's record with a hollow one.
//
// ⚠️ **A store that cannot be read returns the in-process roster rather than an empty one.**
// The endpoint is a second source, not a replacement for what this process already knows;
// dropping its own workers on a store outage would be the same failure this change exists to
// remove, one direction over.
async function currentRoster() {
  // Still pruned: an adopted row carries a BOOT-TIME liveness reading, and a worker that died
  // since would otherwise sit in the roster under a status that reads as fine.
  pruneRehydratedAgents()
  const fresh = await rehydratableAgents({ log })
  if (fresh === null) {
    // ⚠️ **Logged, because the degraded answer is otherwise indistinguishable from the
    // correct one.** A store outage silently reverts this call to in-process-only — the
    // exact failure this whole change exists to remove — and the manager reading
    // `list_agents` has no way to tell. The boot path above already logs its `null`; this
    // one did not, which threw away the `null`-vs-`[]` distinction `readSessionLedger`
    // deliberately preserves.
    log('WARNING: the ledger endpoint could not be read — this roster is this process\'s own workers ONLY, so a worker spawned elsewhere is missing from it until the store answers again')
    return new Map(agents)
  }
  // ⚠️ **The union lives in `registry-rehydrate.mjs` so it can be TESTED.** `supervisor.mjs`
  // cannot be imported — it starts an MCP server at module load — which left the heart of
  // this change pinned only by regexes over the source text, and a regex cannot catch a
  // behavioural regression. One did get through: the foreign row's id was derived from a set
  // seeded by THIS process's Map, so a spawn landing between a manager's `list_agents` and its
  // `send_agent_message` flipped that row's id and handed the message to the wrong worker.
  return unionRoster(agents, fresh)
}

// ── attention-store delivery ────────────────────────────────────────────────
// An operator answers a `permission` item on the attention stack; the store records the
// verdict; nothing pushes it here, because this process is stdio with no inbound
// listener. So it reads — and that read is what makes the answer arrive without a Claude
// session relaying it, which is the whole point: `answer_permission` exists only for a
// session, and a session cannot release another session's parked gate.
//
// Started once at load and deliberately never stopped: the loop's lifetime is the
// process's, which is also `pending`'s lifetime, so there is nothing to outlive. Absent
// when the store is switched off (`SUPERVISOR_ATTENTION_STORE=off`) — see config.mjs.
if (config.attentionStoreUrl) {
  startAttentionPoll({
    storeUrl: config.attentionStoreUrl,
    agents,
    pending,
    log,
    // The store is a decider, and this is the only site that records it as one. Without
    // this the store-settled park writes nothing at all, so its release is
    // indistinguishable in permissions.jsonl from a policy allow — the attribution this
    // delivery path exists to make auditable.
    onSettled: ({ requestId, agentId, decision, park }) =>
      logPermission(storeDecisionRecord({ requestId, agentId, decision, park })),
  })
  // The second arm, and it is a second arm rather than a wider watch set on purpose — see
  // message-delivery.mjs's header. A `message` card carries no verdict, so the loop above
  // cannot settle it however its watch set is written; what it needs is a different rule
  // (`deny` + the operator's words), which is what this starts.
  //
  // Without it a hook-raised `message` card is answered on the board and reaches nobody:
  // the worker stays parked until `PERMISSION_TIMEOUT_MS` and exits with its question
  // unanswered. The board's recorded job is *"to be able to do quickly answers to all cloud
  // sessions"*, and this is the arm that makes an answer to one of them arrive.
  startMessageDelivery({
    storeUrl: config.attentionStoreUrl,
    agents,
    pending,
    log,
    onSettled: ({ requestId, agentId, park, text }) =>
      logPermission(storeMessageRecord({ requestId, agentId, park, text })),
  })
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
        // The park is over, so the durable record must stop advertising it — on every
        // settle, not only on the answer: a timeout, a deny and an allow all end the
        // park, and a record left behind would offer a resume a decision nobody made.
        writeLedger(agent, clearParkPatch())
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

      // A decision carried by a resume answers the gate it names, rather than letting the
      // worker park it a second time. The motivating run (`spawner-exit-resume`) did not
      // demonstrate this — it was resumed with a bare `Continue.` and carried no decision,
      // so its re-raise is just what a worker does when resumed with no information. What
      // stopped the re-ask in the run that DID carry a decision is `renderResumePrompt`'s
      // closing line: the worker read it and declined to re-run the call. This guard is the
      // second line for a worker that ignores that note — correct, narrow and unit-tested,
      // but no observed live run has made it fire. There is no original promise left to
      // settle; it died with the spawner, and this is the gate that matters now.
      //
      // Consumed once — a decision answers one park, not the worker's whole future — and
      // a carried `allow` is NOT auto-applied here (`serverWouldAllow: false`), because
      // honouring one without the live policy check would make this a laundering path
      // with a new spelling. A carried `deny` only ever withholds, so it always applies.
      const carried = settleFromCarried(agent.carriedDecision, { toolName, blockedPath: opts.blockedPath ?? null })
      if (carried && mayApplyAllow(agent.carriedDecision, { serverWouldAllow: false })) {
        agent.carriedDecision = null
        log(`permission ${requestId} answered from the decision ${agent.id} carried: ${carried.behavior} for ${carried.item_id}`)
        settle({ behavior: carried.behavior, message: carried.message ?? undefined })
        return
      }

      pending.set(requestId, record)
      agent.status = 'blocked-on-permission'
      agent.permissions.push(requestId)
      // The durable half. `pending` dies with this process, and the spawner exiting is
      // the case this exists for, so the park lands on the per-worker ledger record —
      // already keyed on session_id, already outliving the process. Best-effort in both
      // directions: `writeLedger` logs and returns when the session id has not resolved
      // yet (so an early park is not durable *yet*, rather than fatal), and the build is
      // guarded because a park that cannot describe itself must not take the gate down
      // with it.
      try {
        writeLedger(agent, {
          [PARK_FIELD]: buildParkRecord({
            requestId,
            toolName,
            input,
            decisionReason: opts.decisionReason,
            blockedPath: opts.blockedPath,
            requestedAt: record.requestedAt,
          }),
        })
      } catch (error) {
        log(`WARNING: could not build the park record for ${requestId}: ${error.message}`)
      }
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

// `shellQuote` moved to spawn-mode.mjs — one definition, and one a test can import.

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
  // ⚠️ **THE SINGLE-WRITER GUARANTEE, and the DECISION lives in `ledgerWriteRoute` rather than
  // here.** It is a pure function in `ledger.mjs` so the one thing that must be right — an
  // adopted row takes a MERGE and never a REPLACE — is unit-tested, rather than pinned by
  // matching this file's source text from a test (which is what it took while the decision was
  // inline: `supervisor.mjs` cannot be imported, because it starts an MCP server). What stays
  // here is the execution and the logging.
  //
  // The rule: a rehydrated row was adopted from the endpoint, so its ledger record belongs to
  // the server that SPAWNED that worker — which may still be running and driving it. The
  // endpoint's `Post` REPLACES a whole row rather than merging a patch, so a CREATE from a
  // second server would not merely lose one field: it would rebuild the record from a row that
  // carries identity alone and blank everything the owner had written. A `PATCH` is the one
  // write that is safe from a second server, because the store merges the fields the caller
  // names and leaves every other field exactly as the owner wrote it.
  //
  // ⚠️ **Refusing both was the shape this had while the endpoint had no merge, and it cost
  // precisely the case the stop path exists for** — the worker a manager stops is usually the
  // one it did NOT spawn. The stop then went unrecorded on the durable half, and the sweep read
  // a deliberately stopped worker exactly as it reads a dead one: an orphan candidate, which the
  // drive leg would stop+recreate.
  //
  // ⚠️ **This is reachable, not hypothetical.** `send_agent_message` looks an agent up in
  // `currentRoster()` — which is the point, because the worker a manager messages is the one it
  // did not spawn — and the cluster arm of that handler writes `{ result }` onto it. Before the
  // guard, a manager messaging another server's cluster worker erased that worker's row.
  //
  // The refusal is logged rather than silent: the turn's `result` genuinely goes unrecorded for
  // a cluster worker this process does not own, and that loss must be readable rather than
  // discovered. `stampUnobservedWorkers` already skips these rows for the same reason; the
  // guard is what makes that one site's discipline a property of the writer.
  const route = ledgerWriteRoute(agent, patch)
  if (route === 'no-session-id') {
    log(`WARNING: no ledger record for ${agent.id} — its session id never resolved, so there is no key to file it under`)
    return null
  }
  if (route === 'refuse') {
    log(`WARNING: refusing to write the ledger row for ${agent.id} — it was adopted from the endpoint, so its record belongs to the server that spawned it`)
    return null
  }
  if (route === 'patch') {
    // ⚠️ **`agent.ledgerRecord` is deliberately NOT set here, and the patch is returned rather
    // than a merged row.** This process holds no copy of an adopted row — that is what "adopted"
    // means — so a local record built from the patch alone would be a PARTIAL row, and the next
    // wholesale publish of it would blank the owner's fields: the exact damage this branch
    // exists to avoid, deferred by one write rather than prevented. The merge lives in the
    // store, and a caller that needs the merged row reads it back.
    publishLedgerPatch(agent.sessionId, patch)
    return patch
  }
  try {
    // ⚠️ **The merge source is this process's OWN in-memory record, not a read-back.** The patch
    // arm used to merge into the file record, which meant a patch could land only while a file
    // was still there to merge into — and it made the endpoint row a copy of a copy, so a
    // vanished file froze that row silently. The record the create arm built is held on the agent
    // instead: one source for this process's own row, no round trip, and no read-modify-write
    // window for two in-flight patches to race in.
    //
    // ⚠️ The create arm always runs FIRST, for every mode — headless on the SDK's `init` message
    // (`agent-loop.mjs:58`), and cluster / a2a / interactive inside their own spawn paths — so a
    // patch that finds no record is a session id that resolved with no spawn behind it, which is
    // the same "no key to file it under" case the guard above reports.
    if (patch) {
      const merged = mergeRecord(agent.ledgerRecord, patch)
      if (!merged) {
        log(`WARNING: no ledger record for ${agent.id} (${agent.sessionId}) — the patch was not applied`)
        return null
      }
      agent.ledgerRecord = merged
      publishLedgerRow(merged)
      return merged
    }
    const record = buildRecord({
      sessionId: agent.sessionId,
      agentId: agent.id,
      label: agent.label,
      // Three answers, not two. A cluster worker is neither a tab nor an in-process query, and
      // folding it into `headless` would file it under a mode whose whole meaning is "a local
      // process that parks its gates on this server" — which is exactly what it is not.
      // Four answers, not two. An A2A worker is neither a tab, an in-process query, nor the
      // shared cluster pod: the controller created it and this server reaches it over A2A, so
      // folding it into `headless` would file it under a mode whose whole meaning is "a local
      // process that parks its gates on this server". The A2A answer comes from the spawn module
      // so the mapping has one home and is unit-tested — `buildRecord` THROWS on a mode outside
      // `MODES`, so a status this derivation did not know would lose the ledger record rather
      // than mis-file it, and a missing record is indistinguishable from a worker never spawned.
      mode:
        ledgerModeForStatus(agent.status) ??
        (agent.status === 'interactive' ? 'interactive' : agent.status === 'cluster' ? 'cluster' : 'headless'),
      modeSource: agent.modeSource ?? null,
      cwd: agent.cwd,
      launcher: agent.launcher ?? null,
      paneId: agent.paneId ?? null,
      resumedFrom: agent.resumedFrom ?? null,
      policy: agent.policyPath ?? null,
      shipping: agent.shipping === true,
      parentSession: agent.parentSession ?? null,
      // The role this spawn declared. Carried into the record because the spawn edge
      // cannot otherwise distinguish a manager from a worker: `gate-owner-filter.py`
      // reads a record as proof of "worker", so a manager spawned with
      // `role="manager"` was read as a worker by every peer manager.
      role: agent.role ?? null,
      // The finished result, when there already is one. Null for every local mode at spawn —
      // a tab or headless worker has not run yet, and its terminal patch supplies this later.
      // A cluster worker is the exception: its turn ends inside the spawn call, so the record
      // filed here is the only write its result will ever get, and hard-coding null in
      // `buildRecord` was silently discarding it from the durable half too.
      result: agent.result ?? null,
      // Set only for an A2A worker: the id the liveness mirror matches against the controller's
      // `list`, and the target whose name is the roster status. See `buildRecord`.
      workerId: agent.workerId ?? null,
      target: ledgerModeForStatus(agent.status) ? agent.status : null,
      spawnedAt: agent.createdAt,
    })
    agent.ledgerRecord = record
    publishLedgerRow(record)
    return record
  } catch (error) {
    log(`WARNING: could not write the ledger record for ${agent.id}: ${error.message}`)
    return null
  }
}

// Publish a record to the session-ledger endpoint.
//
// ⚠️ **This is the ONLY durable write now, and that is a change in kind rather than in address.**
// The endpoint was a second copy beside the file while the readers moved; the file write is gone,
// so a row that is not published here exists nowhere that outlives the process. A missed POST
// therefore costs a RECORD, not merely visibility — which is why the shutdown path below waits for
// the ones already in flight.
//
// ⚠️ **Still not awaited on the ordinary paths, on purpose.** A store outage must not turn a spawn
// that already produced a working worker into a failed spawn, and no ordinary caller has anything
// to do with a rejection. The spawn has already succeeded by the time this runs, and a refusal
// here cannot un-succeed it.
//
// ⚠️ **But "not awaited" must not mean "abandoned", and on the shutdown path it did.** Every
// publish is tracked so the signal handler can wait for the ones already in flight — see
// `SHUTDOWN_PUBLISH_TIMEOUT_MS` below for why that is the one caller where losing the POST is
// not merely lost visibility.
const inFlightPublishes = new Set()

// How long the shutdown path waits for in-flight endpoint writes before exiting anyway.
// Bounded because a wedged store must not hold the process open, and short because the only
// thing being waited on is a localhost round trip.
const SHUTDOWN_PUBLISH_TIMEOUT_MS = 2000

function publishLedgerRow(record) {
  const wire = recordToWire(record)
  if (!wire) return
  const pending = postSessionLedger(wire)
    .then((ok) => {
      if (!ok) log(`WARNING: the session-ledger endpoint did not take the row for ${wire.session_id}`)
    })
    .catch(() => {})
    .finally(() => inFlightPublishes.delete(pending))
  inFlightPublishes.add(pending)
  return pending
}

// Merge a patch into ONE endpoint row, from a server that did not spawn it.
//
// ⚠️ **Separate from `publishLedgerRow` because it writes a different SHAPE to a different
// ROUTE.** That one sends a whole record to the collection route; this one sends only the named
// fields to the per-id route and lets the store merge. Routing an adopted agent's patch through
// `recordToWire` would send a partial record to the REPLACING route — the destructive write the
// guard in `writeLedger` exists to prevent — so the two must not share a function however alike
// their plumbing looks.
//
// ⚠️ **The in-flight tracking is shared deliberately.** `inFlightPublishes` is what the shutdown
// path waits on, and a stop issued moments before the process exits is exactly the write whose
// loss matters: it is the difference between the sweep reading a worker as deliberately stopped
// and reading it as dead.
function publishLedgerPatch(sessionId, patch) {
  const pending = patchSessionLedgerRow(sessionId, patch)
    .then((ok) => {
      if (!ok) log(`WARNING: the session-ledger endpoint did not take the patch for ${sessionId}`)
    })
    .catch(() => {})
    .finally(() => inFlightPublishes.delete(pending))
  inFlightPublishes.add(pending)
  return pending
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
    // ⚠️ A rehydrated row is not this process's to stamp. Its ledger record belongs to the
    // server that spawned the worker — which may still be running and driving it — so
    // writing `unknown` here would assert an outcome this process never watched about a
    // worker it never had. It would also mis-derive `mode` for a rehydrated cluster row,
    // which the ternary below folds into `headless`.
    if (agent.rehydrated) continue
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
    // The self-stamp goes the same way. The handler already exists so a killed server does not
    // leave records asserting a liveness nobody can vouch for; the row describing the server's
    // OWN session is the same claim and belongs on the same path. `stopSelf` is idempotent, so
    // a server that never had a session id simply clears nothing.
    heartbeat.stopSelf()
    // ⚠️ **The exit WAITS for the endpoint writes already in flight.** `writeLedger` publishes
    // fire-and-forget by design, and `process.exit(0)` in the same tick killed every one of
    // them — `fetch` cannot complete a round trip synchronously, so this was deterministic
    // rather than racy. It matters here and nowhere else: the shutdown stamp is the one write
    // whose whole purpose is to stop a record asserting `running` forever, and the endpoint is
    // now the roster's source, so losing it left the two stores disagreeing on the one path
    // where they are guaranteed to diverge. Bounded, so a wedged store still exits.
    const pending = [...inFlightPublishes]
    const exit = () => process.exit(0)
    setTimeout(exit, SHUTDOWN_PUBLISH_TIMEOUT_MS).unref()
    Promise.allSettled(pending).finally(() => {
      log(`received ${signal} — stamped ${stamped} unobserved worker(s) as ${UNOBSERVED_STATUS}, after ${pending.length} endpoint write(s) settled`)
      exit()
    })
  })
}

// Spawn a worker as a real interactive session in a wezterm tab.
//
// The trade is deliberate: this worker's approval prompts are answered IN ITS TAB,
// so it is not supervised the way the headless path is. That also means no filter
// is needed in pending_permissions / await_permission — an interactive worker never
// calls canUseTool, so no permission record is ever created for it.
// The vaults this machine is configured for — the one source that knows each vault's
// directory and its `claude_script` together, and the same source /open reads.
//
// An unreadable list returns `[]`, which `resolveWorkerTarget` REFUSES on rather than
// resolving against nothing. Returning a fallback here instead is what produced the
// defect this replaced: `vaults.find(v => v.name === 'personal')` matched nothing once
// that vault was renamed, and the launcher then silently became the bare `claude` binary.
function loadVaults() {
  try {
    const out = spawnSync('vault-cli', ['config', 'list', '--output', 'json'], { encoding: 'utf8' })
    if (out.status !== 0) return []
    const parsed = JSON.parse(out.stdout)
    return Array.isArray(parsed) ? parsed : []
  } catch {
    return []
  }
}

// The model a launcher script starts its session with, or `null` when it cannot be read.
//
// Reported in the spawn response so a caller can tell "my worker came up on deepseek" from
// "it came up on Opus" without opening a pane. That verification is exactly what the
// 2026-10-03 batch could not perform: 27 workers ran on Opus under a launcher the response
// never named, and nothing in the reply distinguished them from a correct spawn.
function launcherModelFor(scriptPath) {
  if (!scriptPath) return null
  try {
    return parseLauncherModel(readFileSync(scriptPath, 'utf8'))
  } catch {
    return null
  }
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

async function spawnInteractiveAgent({ id, prompt, cwd, launcher, label, windowId, chip, env }) {
  // Handed in rather than resolved here. The target is resolved once, in spawnAgent, so a
  // bad path costs no tab — and so the tab path and the headless path cannot disagree about
  // which launcher this spawn uses, which is how a worker's `launcher` field came to mean
  // something different depending on which way it opened.
  const claudeCmd = launcher
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
  // The caller's per-call `env` is EXPORTED into this command line — the tab path's half of
  // the parameter, and the half a headless-only implementation leaves unexercised. It goes
  // before the `cd` so it survives the login shell's profile files; see `shellEnvExports`
  // for why it must be an `export` rather than a `VAR=value` prefix on the `cd`.
  const inner = `${shellEnvExports(env)}cd ${shellQuote(cwd)} && exec ${claudeCmd} -n ${shellQuote(tabName)} ${shellQuote(prompt)}`
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
// The fleet-wide concurrent-worker limit, as an error string or `null` when the spawn may open.
//
// Extracted so BOTH spawn paths answer to it — the tab/headless path below and the cluster
// path, which is a different SHAPE of worker but not a different population. A limit only one
// path consulted would be a second counter by omission, which is the defect this number's
// single home exists to prevent.
//
// The count is live WORKER SESSIONS — the spawn ledger joined to the **session-heartbeat
// endpoint**, minus auto-resumes. ⚠️ **The liveness source moved to the endpoint on 2026-10-09,
// and the join did not.** Until then this read the registry OR the local heartbeat store, and
// the move is the third correction of one instrument: the store alone answered 0 while 11
// interactive workers were live (measured 2026-10-01), the registry alone rendered every
// headless and cluster worker dead (corrected 2026-10-05, because the registry is pid-keyed and
// local-only and cannot see a worker with no pid of its own), and now the endpoint holds BOTH
// populations in one store, so the union rule goes with the channels. A cap counting one
// population while the managers' target counts another is a defect with no error on either side
// — and the target IS this number, so they must agree by construction.
// Definition and its two stated limits: `worker-sessions.mjs`.
// ⚠️ TWO THRESHOLDS SINCE 2026-10-04, ON THE OPERATOR'S RULING — *"lets start with 30 = soft
// cap and 50 = hard cap"*. The SOFT cap refuses ORDINARY spawns; the HARD cap refuses EVERY
// spawn. `operatorNamed` is the exemption that lives between them: a task the operator has
// named by hand may still open while the fleet sits at or above the soft cap, up to but not
// including the hard one. The band exists so the fleet never refuses the operator's own
// named work for a reason the operator did not choose.
//
// ⚠️ `operatorNamed` is the CALLER's assertion, resolved from the task's own operator-set
// flag and passed in — never inferred here. This function cannot read the vault, and a
// default of `true` would make the soft cap unenforceable, so an absent argument means an
// ordinary spawn. The marker and its provenance rule have one home in `commands/open.md`
// § Step 1.5; this is the enforcement, not a second definition.
// ⚠️ **Asynchronous since 2026-10-09, because the count's liveness source is the
// session-heartbeat endpoint** — an HTTP read, where it used to be two filesystem reads. Both
// callers are already `async` and must await it: a count resolved after the spawn has already
// spent the budget it protects is exactly the ordering this function's own comment forbids.
async function concurrentLimitError({ operatorNamed = false } = {}) {
  // Resolve and count HERE; decide THERE. The decision is `concurrentLimitRefusal` in
  // `spawn-mode.mjs` — pure, and therefore unit-testable, which this module is not, because
  // importing it starts an MCP server. Only the two side-effectful reads stay in this file.
  const limits = resolveMaxConcurrent({
    env: config.maxConcurrent,
    envHard: config.maxConcurrentHard,
    file: config.configFileContents,
    path: config.configFile,
  })
  // Skipped when the pair is off or unresolvable: `concurrentLimitRefusal` answers both of
  // those before it ever looks at the count, and the endpoint read is the expensive half.
  const counted =
    limits.error || limits.limit === null
      ? { workers: null, error: null }
      : await workerSessions()
  return concurrentLimitRefusal({
    limits,
    // `null` is "a store could not be read", which is NOT "no worker is live" — the refusal
    // turns that distinction into an error rather than a zero.
    liveCount: counted.workers === null ? null : counted.workers.length,
    // The cause travels with the count, exactly as it does for the manager cap below. Without it
    // the refusal can only say "the count could not be taken", which is what made 2026-10-19's
    // lost spawns undiagnosable.
    countError: counted.error,
    operatorNamed,
    configFile: config.configFile,
  })
}

// The fleet-wide MANAGER cap — consulted only for a `role: manager` spawn, beside the worker
// cap above and in the same split: resolve and count HERE, decide in `managerLimitRefusal`.
// The count is `managerSessions()` — the fleet board's own manager classification, which sees
// hand-started managers the spawn ledger never records. See `manager-sessions.mjs`.
// ⚠️ The limits come from `liveConfig`, NOT `config.configFileContents`. The boot snapshot is
// frozen for the process's life, so a spawn decided against it ignores every edit until the
// operator reconnects — observed 2026-10-14, `spawn.maxManagers` raised to 10 and still refused
// as 5. `liveConfig` re-reads the file when its mtime moves; see `config-reload.mjs`.
const liveConfig = mtimeReloadedConfig({ path: config.configFile })

async function managerLimitError({ operatorNamed = false } = {}) {
  const limits = resolveMaxManagers({ file: liveConfig.current().file, path: config.configFile })
  const counted = limits.error || limits.limit === null ? { ids: null, error: null } : await managerSessions()
  return managerLimitRefusal({
    limits,
    liveCount: counted.ids === null ? null : counted.ids.length,
    // The cause travels with the count. Without it the refusal can only say "unknown" beside a
    // limit, which is what sent the operator to the one value that was already correct.
    countError: counted.error,
    operatorNamed,
    configFile: config.configFile,
  })
}

// The session start rate — how fast sessions may be OPENED, as distinct from how many may be
// live at once. Enforced beside the concurrency cap, and for the same reason that cap is:
// a rate consulted after the spawn has already spent the budget it exists to protect.
//
// ⚠️ THIS ONE WAITS RATHER THAN ONLY REFUSING, and that is the deliberate difference from
// `concurrentLimitError` above. The callers are automated bulk paths, and refusing a 22-row
// batch outright fails half of it; pacing the same starts spreads the first-turn context
// loads that took the backend down on 2026-10-10. The wait is bounded — past the ceiling
// `startRateDecision` returns an error instead, so this cannot park a caller indefinitely.
//
// ⚠️ THE SEQUENCE IS `reserveSlot`'S, NOT THIS CALL SITE'S. Reading, deciding, writing and
// waiting are one ordering, and the WRITE-BEFORE-WAIT half of it is load-bearing: a caller
// that wrote after the wait would let a simultaneous batch read one stale stamp, compute one
// slot, and all sleep to the same instant — the burst arriving on the far side of the check
// meant to spread it. Stated here as well, the two copies drift, which is the defect this
// module family exists to refuse. This function resolves the config and hands off.
//
// ⚠️ CALLED AS LATE AS POSSIBLE — immediately before the session is created, not beside the
// other guards. The guards above it refuse a malformed call without creating anything, and a
// slot consumed by a call that was never going to spawn would charge the next legitimate
// start for a config typo.
async function reserveStart() {
  return reserveSlot({
    rate: resolveMaxStarts({
      env: config.maxStartsPerMinute,
      file: config.configFileContents,
      path: config.configFile,
    }),
    stateFile: join(config.stateDir, RATE_STATE_FILE),
    configFile: config.configFile,
    log: (message) => log(`${message}\n`),
  })
}

// The cluster target EVERY cluster call resolves — one home, so a spawn and a follow-up
// cannot disagree about which service they address or which token they present.
//
// Resolved here rather than in config.mjs, mirroring resolveSpawnMode: that module owns the
// environment surface and hands over the raw file, and the module that owns the MEANING
// decides precedence and refuses a bad value. See resolveClusterTarget for why the config
// file is the only source that can reach an already-running server.
//
// The token travels inside this value and is never echoed: every refusal on the path
// (`resolveClusterTarget`, `resolveAuthToken`, `startClusterSession`) names the variable or
// the file to fix, never the secret.
function clusterTarget() {
  return resolveClusterTarget({
    envUrl: config.clusterUrl,
    envToken: config.clusterAuthToken,
    file: config.configFileContents,
    path: config.configFile,
  })
}

// One finished cluster turn, in the shape `agent.result` and the tool result both carry.
//
// ⚠️ **`answer`, `outcome` and `truncated` are the three fields a caller cannot re-derive.**
// The response is gone by the time this returns, so a caller that drops them has nothing to
// reconstruct them from — which is exactly how three complete answers, one of them a
// success, were read from the outside as three empty turns on 2026-10-08. `answer` is the
// child's own words, bounded because its reader is a manager session's context; `truncated`
// is named rather than inferred from the length, so a cut answer is never read as a short one.
//
// The shape is deliberately NOT the SDK envelope `agent-loop.mjs` writes for a local
// worker. `subtype`, `is_error` and `num_turns` are facts about an SDK query and a cluster
// turn has none of them; borrowing the shape would put three invented values in a field
// readers already trust. The record's `mode: "cluster"` is what tells the two shapes apart.
function clusterTurnResult(started) {
  const answer = typeof started.answer === 'string' ? started.answer : ''
  return {
    outcome: started.outcome ?? classifyClusterAnswer(answer),
    answer: answer.slice(0, CLUSTER_ANSWER_MAX),
    truncated: answer.length > CLUSTER_ANSWER_MAX,
  }
}

// A cluster worker: a session inside the `claude-interactive` service, not a process here.
//
// A different shape from both paths below, rather than a third branch of them. It has no
// pane, so nothing here resolves a role, a window or a tab name. And it has no local
// process, so it holds no registry entry either — which is why the session id is MINTED and
// sent, instead of being discovered afterwards the way the tab path discovers one by polling
// the registry for its tab name.
//
// ⚠️ The binding is part of the spawn, not a follow-up. The task's `claude_session_id` is
// what makes the session reachable from the vault; a spawn that returned an id nobody wrote
// down is a worker the fleet cannot find. It is written only AFTER the call succeeds —
// stamping a task with an id the service refused would point it at a conversation that does
// not exist, which is worse than an empty field.

async function spawnClusterWorker({ id, prompt, label, task, vault, resume, policyPath, interactive, operatorNamed, env }) {
  // Arguments the cluster path cannot honour are REFUSED, never accepted and quietly ignored —
  // the same rule the tab path already carries for `resume` and `policy`. Silently dropping
  // them is how a caller ends up believing a worker was resumed when a fresh session was
  // opened, or that it runs under a policy nothing consults.
  if (env && Object.keys(env).length > 0) {
    return {
      error:
        `env is not supported with target "cluster": this path creates no local process, so there is no ` +
        `environment here to set — the session is created by the claude-interactive service and this server ` +
        `never runs it. Omit \`env\`, or spawn locally.`,
    }
  }
  if (resume) {
    return {
      error:
        'resume is not supported with target "cluster": this path mints a NEW session id, so it cannot ' +
        'continue an existing conversation. To send a further turn to a cluster session that already ' +
        'exists, call `send_agent_message` on that worker — the refusal names it because the old wording ' +
        'left a caller with no route at all. Omit `resume`, or spawn locally.',
    }
  }
  if (policyPath) {
    return {
      error:
        'policy is not supported with target "cluster": the policy layer answers permission requests raised on ' +
        'THIS server, and a cluster worker\'s gates never reach it. Omit `policy`, or spawn locally.',
    }
  }
  if (interactive !== undefined) {
    return {
      error:
        'interactive is not supported with target "cluster": it chooses between a local wezterm tab and a local ' +
        'headless worker, and the cluster target creates neither. Omit it.',
    }
  }

  if (!task) {
    return {
      error:
        'the cluster target requires `task`: it names the vault task the worker is opened for, and the task ' +
        'whose claude_session_id is bound to the created session. Pass the task name, plus `vault` when that ' +
        'name is not unique across the configured vaults.',
    }
  }

  // The limit is checked AFTER the argument refusals, so a malformed call is told what is
  // wrong with it rather than told the fleet is full — both refuse, but only one names the
  // thing the caller can actually fix. It is still checked BEFORE anything is created, which
  // is what matters: a cap consulted after the spawn has already spent the budget it protects.
  const limitError = await concurrentLimitError({ operatorNamed })
  if (limitError) return { error: limitError }

  const minted = newSessionId()
  if (minted.error) return { error: minted.error }

  const target = clusterTarget()
  if (target.error) return { error: target.error }

  // A cluster session is a session start like any other, so it answers to the same rate —
  // and it is paced HERE, after every refusal above, so a call that never reaches the
  // service does not spend a slot.
  const rateError = await reserveStart()
  if (rateError.error) return { error: rateError.error }

  const started = await startClusterSession({
    baseUrl: target.url,
    authToken: target.token,
    prompt,
    sessionId: minted.sessionId,
  })
  if (started.error) return { error: started.error }

  // ⚠️ **The cluster turn is SYNCHRONOUS and already OVER by this line.** `startClusterSession`
  // holds the request open until the pod answers, so there is a final answer here and no later
  // one. That is why this value is written to every surface at once rather than streamed, and
  // why none of them may be left null: the tool result is read by the caller NOW, `agent_status`
  // and `list_agents` by the manager later, and the ledger by a peer manager after this server
  // has exited. They are one fact seen by three readers, so a null in any of them puts the three
  // reads in disagreement about a turn that has already finished — which is exactly how three
  // complete answers, one of them a success, were read from the outside as three empty turns on
  // 2026-10-08 (sessions `9fac5053`, `38a2bdae`, `a3be5a1b`). The shape itself, and why it is
  // not the SDK envelope, is `clusterTurnResult`'s — one home, so a spawn and a follow-up
  // cannot report the same turn differently.
  const result = clusterTurnResult(started)

  const bound = bindSessionToTask({ task, vault, sessionId: started.sessionId })
  if (bound.error) {
    // The session exists and is running; only the vault's record of it failed. Reported with
    // the id, because that is the one thing a human needs to reconcile it — and reported
    // rather than swallowed, so the caller decides whether to keep a session nothing points at.
    //
    // ⚠️ The answer rides along even here. The turn RAN, and its answer is the one thing that
    // cannot be recovered from the id — dropping it on this path would reintroduce the exact
    // silence this change removes, on the rarer of the two paths and therefore the one nobody
    // would notice.
    return {
      error: `session ${started.sessionId} was created but binding it to task "${task}" failed: ${bound.error}`,
      answer: result.answer,
      outcome: result.outcome,
      truncated: result.truncated,
    }
  }

  // ⚠️ `transcript` and `permissions` are SEEDED, not optional, and getting this wrong is not
  // a cosmetic drift. `agentView` dereferences both unconditionally (`a.transcript.length`,
  // `a.permissions.filter`) and `list_agents` maps over EVERY agent — so one agent missing
  // either makes the whole roster unreadable, not just that row. The local paths always seed
  // them; a cluster worker has no transcript on this machine and parks no permissions here,
  // which is exactly why they must be EMPTY rather than absent.
  const agent = {
    id,
    label: label || id,
    prompt,
    cwd: null,
    status: 'cluster',
    rules: policy.rules,
    policyPath: null,
    sessionId: started.sessionId,
    resumedFrom: null,
    carriedDecision: null,
    parentSession: parentSessionId({ dir: config.sessionsDir }),
    modeSource: 'argument',
    role: null,
    windowId: null,
    launcher: null,
    paneId: null,
    transcript: [],
    permissions: [],
    // Not null, and not deferred to a later write: the turn is already over (see above), so
    // this is the finished result rather than a placeholder for one. It is what `agent_status`
    // and `list_agents` report, and what `writeLedger` files.
    result,
    error: null,
    task,
    createdAt: new Date().toISOString(),
  }
  agents.set(id, agent)
  writeLedger(agent)

  return {
    agent_id: id,
    label: agent.label,
    status: 'cluster',
    target: 'cluster',
    session_id: started.sessionId,
    task,
    vault: bound.vault,
    // The three fields this call exists to stop dropping. `answer` is the child's own words,
    // bounded and quoted rather than summarised; `outcome` separates a failed turn from a
    // silent one; `truncated` says whether `answer` is the whole of it. A caller that wants to
    // judge the turn itself has everything it needs here, with no second probe.
    answer: result.answer,
    outcome: result.outcome,
    truncated: result.truncated,
  }
}

// The A2A worker-controller spawn: locate the controller, create one worker for this task, read
// the worker's own Agent Card, bind the session, write the record — and return WITHOUT a turn.
//
// ⚠️ THE TURN IS NOT SENT HERE, AND THAT IS SC5 RATHER THAN AN OMISSION. `spawnClusterWorker`
// holds its request open until the pod answers and writes a FINISHED `result`, because a cluster
// turn is over by the time it returns. SC5 requires the opposite here — the send returns in
// **under 2 s** while the turn runs **over 60 s** — so this call stops once the worker EXISTS and
// is reachable, and the turn is a separate step. That is why the record it writes carries
// `result: null` rather than a placeholder: a roster read would take a placeholder for a finished
// turn that produced nothing, which is the exact silence `clusterTurnResult` exists to remove,
// arriving from the other direction.
//
// ⚠️ EVERY REFUSAL HAPPENS BEFORE `create`, because `create` is the side effect. A container that
// exists and is then rejected for a missing token is a container nothing reaps. The one thing
// that cannot be refused first is the worker itself: a `create` whose reply fails AFTER the
// executor started a container reports FAILED, which is why `createWorkerOnController` returns
// the controller's own text rather than a "nothing happened" claim.
async function spawnA2AWorker({ id, prompt, label, task, vault, resume, policyPath, interactive, windowId, role, operatorNamed, shipping, env, mounts, secrets, target }) {
  const refusal = a2aSpawnArgumentRefusal({
    target,
    resume,
    policy: policyPath,
    interactive,
    windowId,
    role,
    env,
    shipping,
    task,
  })
  if (refusal) return { error: refusal }

  const limitError = await concurrentLimitError({ operatorNamed })
  if (limitError) return { error: limitError }

  const minted = newSessionId()
  if (minted.error) return { error: minted.error }

  const controller = resolveA2AControllerTarget({
    target,
    envUrl: config.a2aUrls[target],
    envToken: config.a2aTokens[target],
    file: config.configFileContents,
    path: config.configFile,
  })
  if (controller.error) return { error: controller.error }

  // Paced after every refusal above, so a call that never reaches the controller does not spend
  // a rate slot — the same ordering `spawnClusterWorker` uses and for the same reason.
  const rateError = await reserveStart()
  if (rateError.error) return { error: rateError.error }

  // SC2's evidence: the exact `create` body, written at the send site when the operator sets
  // `a2a.captureDir` in the supervisor config. Off by default — nothing is written otherwise. The
  // body carries no credential (the token rides in a header the hook is never given).
  const captureDir = config.configFileContents?.a2a?.captureDir
  const capture = captureDir
    ? ({ url, body }) => {
        mkdirSync(captureDir, { recursive: true })
        const at = new Date().toISOString()
        writeFileSync(
          join(captureDir, `${at.replace(/[:.]/g, '-')}-${target}-create.json`),
          `${JSON.stringify({ at, target, task, url, body }, null, 2)}\n`,
        )
      }
    : undefined

  // ─────────────────────────────────────────────────────────────────────────────────────────
  // THIS WORKER'S STATUS CHANNEL, minted ONCE per spawn — deliberately outside `createOnce`
  // below. The retry there re-creates the SAME task's worker, so a token minted per attempt would
  // leave the earlier one unbound and the worker reporting under a credential nothing listens for.
  //
  // ⚠️ NOT SENT TO THE CONTROLLER, which is the correction this design turned on. The controller
  // creates the worker but never sees a task — this server's own first turn creates it, on the
  // WORKER's A2A server — so the registration goes there, once the turn has named the task. See
  // the `onTaskId` callback below.
  //
  // ⚠️ A RECEIVER THAT CANNOT BIND DOES NOT FAIL THE SPAWN. The channel is additive: a spawn
  // without it produces exactly the pre-change worker, and failing here would lose a live worker
  // to an optional transport. The degradation is visible where it matters — `status_messages`
  // stays empty, and the surfacing criterion is written so an empty array satisfies nothing.
  const pushBegun = await a2aPushRegistry.beginCreate()
  const push = pushBegun.error ? undefined : pushBegun.push

  // A fresh `messageId` and `contextId` per attempt: a retry is a NEW turn on the controller's
  // conversation, and reusing the first attempt's ids would make it read as a duplicate of the
  // call that just failed.
  const createOnce = () =>
    createWorkerOnController({
      endpoint: controller.url,
      task,
      token: controller.token,
      messageId: randomUUID(),
      mint: () => randomUUID(),
      // Forwarded verbatim: this path's job for them is to carry the caller's declaration to the
      // controller, which is the only party that can allow a path or resolve a key. A retry re-sends
      // them because a retry is a fresh `create` — dropping them would silently create a worker
      // WITHOUT the access the caller asked for, which is the failure this whole path refuses to make.
      mounts,
      secrets,
      capture,
    })

  let created = await createOnce()

  // ⚠️ A FAILED CREATE MAY HAVE A WORKER BEHIND IT, AND THE ERROR DOES NOT SAY SO. The controller
  // refuses a repeat create for a task that already has a worker, but a backend failure reaches
  // here as a bare `TASK_STATE_FAILED` with no detail attached — so the refusal cannot be told from
  // a transport failure by its text, and the worker id it names never arrives. `list` is what tells
  // the two apart: it is the only client-side read that answers whether this task already has a
  // worker, and when it does, that worker is what the create was refused over.
  //
  // ⚠️ THE RETRY IS BOUNDED TO ONE. A second create for a task whose worker was just stopped either
  // succeeds or fails for a reason a third attempt cannot fix, and an unbounded loop here would
  // mint and reap workers forever against a controller that keeps refusing.
  //
  // ⚠️ AND THE ORIGINAL ERROR IS WHAT IS REPORTED WHEN NOTHING MATCHED. A create that failed for
  // its own reasons — an unreachable controller, a rejected token — has no worker to reap, and
  // reporting a reaping failure there would hide the real one.
  // ⚠️ ONLY A CALL THE CONTROLLER ANSWERED AND REFUSED IS RECOVERED. A create that failed before
  // reaching it — an argument refusal here, a transport failure — carries no `controllerFailed`,
  // and running the reap for one would stop a healthy worker for a task whose create failed for
  // an unrelated reason. The refusal is the only failure the reap can actually resolve, so it is
  // the only one it is allowed to act on.
  if (created.error && created.controllerFailed) {
    const refusal = created.error
    const reaped = await reapExistingWorkerForTask({
      endpoint: controller.url,
      task,
      token: controller.token,
      messageId: randomUUID(),
      mint: () => randomUUID(),
    })
    if (reaped.error) {
      // ⚠️ THE ORIGINAL ERROR STAYS PRIMARY. A failed `list` does not establish that a worker
      // existed — the create may have failed for its own reasons — so reporting the reaping
      // failure alone would replace a real diagnosis with an invented one. It is carried as
      // context instead, because it is what stopped the recovery from being attempted.
      return { error: `${refusal} ⚠️ The recovery could not run: ${reaped.error}` }
    }
    if (reaped.none) return { error: refusal }

    created = await createOnce()
    if (created.error) {
      return {
        error:
          `worker ${reaped.stoppedWorkerId} already served task ${JSON.stringify(task)} and was ` +
          `stopped, but the create that followed it also failed: ${created.error}`,
      }
    }
  }

  // ⚠️ A FAILURE THAT NEVER REACHED THE CONTROLLER HAS NO RECOVERY TO ATTEMPT. Returning here
  // rather than falling through is load-bearing: the card read below dereferences
  // `created.worker`, so a create error carried past this point would throw instead of reporting.
  if (created.error) {
    // No worker exists, so nothing will ever push under this token — releasing it keeps a failed
    // spawn from leaving a dead credential in the registry for the life of the process.
    a2aPushRegistry.release({ token: pushBegun.token })
    return { error: created.error }
  }

  // The worker exists from here on, so its token is bound to the id the controller minted for it.
  // Binding is what re-keys anything the receiver accepted under the token — the only identity
  // this side held before the create answered — onto the key `agent_status` reads.
  //
  // ⚠️ A BIND FAILURE IS NOT FATAL AND NOT FOLDED INTO THE SPAWN'S ERROR. It can only fire on an
  // internal invariant break (the token was minted above; the controller always names a worker id,
  // which the error paths below already dereference), and failing a spawn over a bookkeeping slip
  // would lose a running container to it.
  if (push) a2aPushRegistry.bind({ token: pushBegun.token, workerId: created.worker.worker_id })

  // ⚠️ THE CARD IS READ BEFORE ANYTHING IS RECORDED. It is what proves the URL the controller
  // returned actually answers, and it is where the turn's endpoint comes from — rather than
  // trusting the returned string to be the endpoint as well. A worker whose card cannot be read
  // is reported WITH ITS ID, because it exists and something has to reap it; that is a different
  // fact from a create that never ran, and the two must not read alike.
  const card = await fetchWorkerCard({ a2aUrl: created.worker.a2a_url })
  if (card.error) {
    return {
      error: `worker ${created.worker.worker_id} was created but its Agent Card could not be read, so no turn could reach it: ${card.error}`,
      worker_id: created.worker.worker_id,
    }
  }

  const bound = bindSessionToTask({ task, vault, sessionId: minted.sessionId })
  if (bound.error) {
    return {
      error: `worker ${created.worker.worker_id} was created but binding session ${minted.sessionId} to task "${task}" failed: ${bound.error}`,
      worker_id: created.worker.worker_id,
    }
  }

  const built = a2aWorkerRecord({
    id,
    label,
    prompt,
    task,
    target,
    sessionId: minted.sessionId,
    workerId: created.worker.worker_id,
    a2aUrl: card.endpoint,
    workerToken: created.token,
    parentSession: parentSessionId({ dir: config.sessionsDir }),
    createdAt: new Date().toISOString(),
  })
  if (built.error) return { error: built.error }

  // ⚠️ A RECEIVER THAT NEVER BOUND IS THE FIRST THING THIS FIELD MUST SAY. Without it the spawn
  // proceeds, `push` is undefined, `onTaskId` returns at its first line, and the channel is silent
  // for a reason nothing records — the shape that cost a whole e2e run and could not be diagnosed
  // from `status_messages` afterwards.
  if (!push) {
    built.record.pushRegistration = {
      state: 'not-attempted',
      reason: `the status receiver is unavailable: ${pushBegun.error}`,
    }
  }

  agents.set(id, built.record)
  writeLedger(built.record)

  // ─────────────────────────────────────────────────────────────────────────────────────────
  // THE FIRST TURN — issued here, COLLECTED LATER, and that split is SC5 rather than a style.
  //
  // SC5 measures this call's latency against the turn's own duration (`< 2 s` against `> 60 s`)
  // and names its falsifier outright: *"a send whose latency tracks the turn's duration
  // (blocking)"*. Awaiting the collection below would BE that falsifier, so the SEND is awaited
  // and the COLLECTION is not — the turn keeps running against the worker while this call returns.
  //
  // ⚠️ A FAILED SEND IS NOT A FAILED SPAWN. The worker exists by this point and something has to
  // reap it, so the failure is filed against the record — where a roster read will find it —
  // rather than returned as though no worker had been created. That is the distinction the card
  // failure above draws, arriving in the opposite order.
  const sent = await openWorkerTurn({
    endpoint: card.endpoint,
    contextId: created.contextId,
    text: prompt,
    token: created.token,
    messageId: randomUUID(),
  })

  // The conversation the first turn opened — what `send_agent_message` continues. Held in memory
  // only, beside the token: a follow-up is possible only from the process holding both.
  if (!sent.error) built.record.contextId = sent.contextId

  if (sent.error) {
    const patch = { result: null, error: `the first turn could not be sent: ${sent.error}` }
    built.record.error = patch.error
    writeLedger(built.record, patch)
  } else {
    // ⚠️ `void`, AND THAT ONE KEYWORD IS THE CRITERION. The record is patched in memory as well as
    // on disk, so `agent_status` and `list_agents` read the same answer the ledger holds rather
    // than the one from spawn time.
    void collectWorkerTurn({
      res: sent.res ?? sent.pending,
      sentContextId: sent.contextId,
      // ───────────────────────────────────────────────────────────────────────────────────────
      // THE REGISTRATION, fired the instant the turn names its task.
      //
      // ⚠️ WHY IT CANNOT WAIT FOR THE COLLECTION TO FINISH. The SDK refuses a registration naming
      // a task its store does not hold, so this can only run after the turn has created the task —
      // and the mid-turn status it exists to catch fires a fixed ~10 s in. Registering when
      // `collectWorkerTurn` RESOLVES would arm the channel minutes after the event it was meant to
      // observe, and every symptom of that is silent: the turn completes, the outcome is filed,
      // and the only visible effect is a status channel that never carries anything.
      //
      // ⚠️ AND WHY A FAILURE IS SWALLOWED RATHER THAN SURFACED. The channel is reporting-only. A
      // refused registration must not be filed against the record as though the TURN had failed,
      // or a roster read would report a healthy worker as broken. `collectWorkerTurn` swallows a
      // throw from here for the same reason, so the two agree rather than relying on each other.
      //
      // ⚠️ THIS IS NOT THE ONLY TURN THAT REGISTERS — corrected 2026-10-18. It read "a follow-up
      // continues an existing task, so its config is already held and re-registering would add a
      // duplicate callback per message", and the premise is false: `openWorkerTurn` sends a
      // follow-up with a `contextId` and **no `taskId`**, so the worker's SDK creates a NEW task,
      // and the previous task's config does not apply to it. The `send_agent_message` call site
      // registers its own turn's task for exactly that reason — no duplicate is created, because
      // each config belongs to a distinct task.
      onTaskId: async (taskId) => {
        // ⚠️ A MISSING PUSH CONFIG IS RECORDED, NOT SWALLOWED — and this line used to swallow it.
        // `if (!push) return` left the SEED reason standing, which reads "the turn has not yet
        // named a task" — the opposite of what happened: the turn named one, which is the only way
        // this callback runs at all. Measured 2026-10-18 on a fresh docker spawn: the record showed
        // exactly that seed while the task id had been found, so the field named the wrong cause
        // for the third time in this task. `pushBegun.error` is the bind failure `beginCreate`
        // returned (it is the only way `push` is undefined); carrying it here turns the next run
        // into a diagnosis instead of another guess about which branch was taken.
        if (!push) {
          built.record.pushRegistration = {
            state: 'not-attempted',
            reason: pushBegun.error
              ? `no push config was minted for this spawn: ${pushBegun.error}`
              : 'no push config was minted for this spawn',
            at: new Date().toISOString(),
          }
          writeLedger(built.record)
          return
        }
        // ⚠️ RECORDED BEFORE THE AWAIT, SO THE FIELD NEVER READS AS "NOT STARTED" WHILE IN FLIGHT.
        // The seed says "the turn has not yet named a task" — the exact opposite of the truth once
        // this callback is running, and the timeout alone does not remove that: a bounded call
        // still leaves a window in which the field is wrong. `onDiagnostic`'s guard
        // (`state !== 'not-attempted'`) already refuses to overwrite a non-seed state, which is
        // what keeps the two writers from disagreeing mid-flight.
        built.record.pushRegistration = { state: 'attempting', taskId, at: new Date().toISOString() }
        writeLedger(built.record)
        const registration = await registerPushCallbackOnWorker({
          endpoint: card.endpoint,
          taskId,
          push,
          token: created.token,
        })
        // ⚠️ RECORDED, NOT DISCARDED — and that is the whole fix. `registerPushCallbackOnWorker`
        // returns its refusal precisely so the caller can record it, and this call used to drop
        // the value on the floor. The result was that a REFUSED registration and one that NEVER
        // HAPPENED left identical evidence: an empty channel and no explanation. Recording it
        // here keeps the turn's own outcome untouched — the refusal is filed as a property of the
        // CHANNEL, never as a turn failure, which is what the comment above requires.
        built.record.pushRegistration = registration.error
          ? { state: 'attempted-error', taskId, error: registration.error, at: new Date().toISOString() }
          : { state: 'attempted-ok', taskId, at: new Date().toISOString() }
        writeLedger(built.record)
      },
      // ⚠️ WHICH PATH THE COLLECTION TOOK — the field that separates the two ways `onTaskId` can
      // fail to run. `not-attempted` alone cannot: a whole-body collection structurally never
      // calls it, while an incremental one calls it unless no frame named a task. Recording the
      // path and the frame count turns the seed reason into a diagnosis.
      //
      // ⚠️ IT NEVER OVERWRITES A TERMINAL STATE. `onTaskId` owns `attempted-ok` / `attempted-error`,
      // and a diagnostic arriving afterwards must not undo it — hence the early return.
      onDiagnostic: (d) => {
        // ⚠️ TERMINAL STATES ARE FINAL; `not-attempted` AND `attempting` ARE NOT. The guard used to
        // return on anything that was not the seed, which would have discarded the throw report
        // once the pre-await `attempting` write had landed — the exact silence this task spent four
        // probes on.
        const state = built.record.pushRegistration?.state
        if (state === 'attempted-ok' || state === 'attempted-error') return
        // ⚠️ THE BRANCH LIVES IN `pushDiagnosticPatch`, AND THAT IS THE FIX. Deciding it here on
        // `d.taskIdFound === false` mislabelled every incremental run that found no task id as
        // "whole-body" — the announcement carries no `taskIdFound` key at all. A pure function is
        // where that can be tested; inline, it looked right and was wrong.
        const patch = pushDiagnosticPatch(d)
        if (!patch) return
        built.record.pushRegistration = patch
        writeLedger(built.record)
      },
    })
      .then((collected) => {
        const patch = a2aTurnPatch(collected, { at: new Date().toISOString() })
        built.record.result = patch.result
        built.record.error = patch.error
        writeLedger(built.record, patch)
      })
      .catch((cause) => {
        // `collectWorkerTurn` returns its failures rather than throwing, so reaching here means
        // something outside it broke. Filed rather than swallowed: a turn whose outcome was never
        // recorded leaves a worker that looks idle forever.
        const patch = { result: null, error: `collecting the turn failed: ${cause?.message ?? cause}` }
        built.record.error = patch.error
        writeLedger(built.record, patch)
      })
  }

  return {
    agent_id: id,
    label: built.record.label,
    status: built.record.status,
    target,
    session_id: minted.sessionId,
    task,
    vault: bound.vault,
    // ⚠️ THE URL, AND NOT THE TOKEN. The token authenticates every follow-up turn to this worker
    // and is held on the agent record; a tool result is a transcript write, so echoing it here
    // is the exact leak this task's own Definition of Done forbids — the one the sibling session
    // hit on 2026-10-13 by printing a create response body. A caller that needs a turn sent asks
    // this server, which already holds the credential.
    a2a_url: card.endpoint,
    worker_id: created.worker.worker_id,
    // ⚠️ THREE ANSWERS, NOT TWO, because those are the three `openWorkerTurn` distinguishes: "the
    // worker is thinking" and "the request never left" are different facts, and a caller unable to
    // tell them apart would send a second turn to a worker already working on the first. There is
    // deliberately no `answer` field — the result arrives in the background, and a null one would
    // read as a turn that produced nothing.
    turn: sent.error
      ? `not sent — ${sent.error}`
      : sent.res
        ? 'sent — the worker acknowledged; its result is collected in the background'
        : 'sent, not yet acknowledged — the request is outstanding and its result is collected in the background',
  }
}

// `bindSessionToTask` lives in task-binding.mjs — it is no longer a one-line `task set`,
// and the rule it implements (stamp only when the field is empty, otherwise record the
// session in `metrics_sessions`) is the kind of thing that needs a test beside it. See that
// module's header for why displacing an existing owner is the defect it exists to prevent.

async function spawnAgent({ prompt, cwd, label, interactive, resume, decision, policy: policyPath, windowId, role, target, task, vault, operatorNamed, shipping, env, mounts, secrets }) {
  // ⚠️ `seq` is per-process and restarts at 0 on every reconnect, while `agents` may already
  // hold rows adopted from the ledger whose ids a PREVIOUS process minted from this same
  // counter. Minting straight to `agent_${++seq}` would then land on an adopted id, and the
  // `agents.set` below would silently overwrite a live worker's row — `list_agents` dropping
  // it and `agent_status` answering `unknown agent`, which is the exact symptom rehydration
  // exists to remove. Advancing past anything already held makes the mint collision-free
  // whatever populated the Map, rather than only for the two-ledger-records case.
  let id
  do {
    id = `agent_${++seq}`
  } while (agents.has(id))

  // The caller's per-call environment, validated HERE with the other guards — before the
  // target short-circuits, because the cluster path has to be able to refuse it and a
  // refusal it cannot see is no refusal. Pure and cheap, so a malformed `env` costs a
  // rejected call and nothing else: no worker, no tab and no ledger record exists yet.
  //
  // REFUSED rather than filtered, matching `resolveWorkerTarget` and `resolveSpawnTarget`.
  // A key dropped silently is a value the worker does not have, and a worker missing one
  // value looks exactly like one that was never given it — which is the reading this
  // parameter exists to make possible in the first place.
  const envResolution = resolveEnvOverrides({ env })
  if (envResolution.error) return { error: envResolution.error }
  const envOverrides = envResolution.env

  // ⚠️ KEYS ONLY, never values — computed once here so neither spawn path can report the
  // other thing. A caller may legitimately pass a credential, and this response is quoted
  // into ledger records, PR bodies and `# Progress` entries; printing the value would put
  // the secret somewhere it must never appear, and a redaction filter over it is exactly
  // the pattern the fleet has already been burned by. The names carry the whole signal —
  // that the parameter was RECEIVED, which a caller otherwise cannot tell from a call that
  // accepted it and dropped it.
  const envReport = Object.keys(envOverrides).length > 0 ? { keys: Object.keys(envOverrides) } : null

  // The target is resolved before the mode, and it SHORT-CIRCUITS. A cluster worker is not
  // opened on this machine at all, so every guard below — the role/window resolution, the
  // tab-name join, the resume and policy support checks, the concurrent-worker limit — asks
  // about machinery it never touches. Running them first would spend a spawn's worth of work
  // on a worker that is never created here, and any error they raised would name the wrong
  // thing.
  //
  // ⚠️ The ROLE skip now has a consequence beyond the colour it used to decide, so it is
  // named here rather than left to be rediscovered: `gate-owner-filter.py` reads a ledger
  // record's `role` to tell a manager from a worker, and a cluster record is written with
  // `role: null` (the agent literal below), which reads as a worker. The consequence is that
  // the CLUSTER SESSION ITSELF is filed as a worker — NOT that its children's gates
  // re-escalate: a cluster worker's children run on the remote target and never enter the
  // local ledger, registry or event log, so the filter cannot see their panes at all. Kept
  // as-is deliberately: managers are local by design, and this short-circuit exists precisely
  // so a cluster spawn touches none of the local machinery. The consistent alternative is to
  // REFUSE the combination the way `shippingSupportError` refuses cluster+shipping two lines
  // below, which is a cluster-semantics decision rather than a fix to make silently here.
  //
  // Refused rather than defaulted, and that is the whole reason it is a separate question
  // from the mode: a spawn that fell back to `local` when the caller asked for the cluster
  // would look exactly like a working call, and where the worker was created is otherwise
  // discovered only by noticing it.
  const spawnTarget = resolveSpawnTarget({ target })
  if (spawnTarget.error) return { error: spawnTarget.error }
  if (spawnTarget.target === 'cluster') {
    const unsupportedShipping = shippingSupportError({ shipping, cluster: true })
    if (unsupportedShipping) return { error: unsupportedShipping }
    return spawnClusterWorker({ id, prompt, label, task, vault, resume, policyPath, interactive, operatorNamed, env: envOverrides })
  }
  // The A2A worker-controller targets, which create their worker somewhere this server does not
  // run it: a container on this Mac for `docker`, a pod in nuke dev for `k8s`. Both refuse the
  // arguments that name a LOCAL process, a local window or a local policy — see
  // `a2aSpawnArgumentRefusal`, which owns that rule and is unit-tested because this file cannot
  // be imported by a test at all.
  if (A2A_SPAWN_TARGETS.includes(spawnTarget.target)) {
    return spawnA2AWorker({
      id,
      prompt,
      label,
      task,
      vault,
      resume,
      policyPath,
      interactive,
      windowId,
      role,
      operatorNamed,
      shipping,
      env: envOverrides,
      // ⚠️ NOT filtered through `resolveEnvOverrides`, and not the same kind of value. `env` is a
      // raw map this server would have to transmit; these are a host path the CONTROLLER checks
      // against its own allowlist and a TeamVault KEY the CONTROLLER resolves — so nothing here is
      // a credential and the value of a secret never reaches this server at all. They are handed
      // through unchanged; the shape is validated in `declaredCreateArguments`, which is what puts
      // bytes on the wire.
      mounts,
      secrets,
      target: spawnTarget.target,
    })
  }

  // ⚠️ THE LAST GUARD BEFORE THE LOCAL PATH, and it sits here rather than beside
  // `resolveSpawnTarget` only because `cluster` returns above. Everything reaching this line
  // is either `local` or a KNOWN target with no client yet (`docker`, `k8s`), and without this
  // check the latter would FALL THROUGH into the local path below — opening an ordinary local
  // worker under a call that asked for a container or a pod, and reporting success. That is
  // the same failure `resolveSpawnTarget` refuses an unknown value to avoid, applied to a
  // value that IS known. The rule lives in `spawnTargetImplementationRefusal` so it is
  // unit-tested; this file cannot be imported by a test at all.
  const unimplementedTarget = spawnTargetImplementationRefusal(spawnTarget)
  if (unimplementedTarget) return { error: unimplementedTarget }

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

  // The fleet-wide concurrent-worker limit — resolved and enforced HERE, with the other
  // guards, before a worker, a tab or a ledger record exists. A limit checked after the
  // spawn has already spent the budget it exists to protect.
  //
  // The count is live WORKER SESSIONS — the spawn ledger joined to the session-heartbeat
  // endpoint, minus auto-resumes. The endpoint holds every population in one store, including
  // the headless and cluster workers the registry is structurally blind to (it is pid-keyed and
  // local-only). See the longer note on `concurrentLimitError` for why the source moved. A cap
  // counting one population while the managers' target counts another is a defect with no error
  // on either side — and the target IS this number, so they must agree by construction.
  // Definition and its two stated limits: `worker-sessions.mjs`.
  const limitError = await concurrentLimitError({ operatorNamed })
  if (limitError) return { error: limitError }

  // A manager also answers to the manager cap — a separate, smaller population bound. Checked
  // here, with the other guards, before a tab or a ledger record exists.
  if (roleResolution.role === 'manager') {
    const managerError = await managerLimitError({ operatorNamed })
    if (managerError) return { error: managerError }
  }

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
  // Same class again: shipping settings a tab worker would never load are refused.
  const unsupportedShipping = shippingSupportError({ shipping, interactive: opensInteractive })
  if (unsupportedShipping) return { error: unsupportedShipping }

  // Resolved before anything is spawned, so a bad path costs no worker, no ledger
  // record, and no half-started session running under rules nobody chose.
  //
  // REFUSED, not defaulted, and that is the whole fix. Both halves used to be silent: an
  // absent `cwd` became this server's own working directory, and an unresolvable launcher
  // became the vault named `personal` — since renamed, so it matched nothing — and then the
  // bare `claude` binary. Measured 2026-10-03: 27 workers spawned that way in one batch,
  // every one in the supervisor repo and on Opus instead of the vault's
  // `cc-private-deepseek`, with nothing in the reply to say so.
  const workerTarget = resolveWorkerTarget({ cwd, vault, vaults: loadVaults(), serverCwd: process.cwd() })
  if (workerTarget.error) return { error: workerTarget.error }
  const workerCwd = workerTarget.cwd
  // `SUPERVISOR_CLAUDE_CMD` stays the top override: an explicit operator setting rather
  // than a fallback, and the documented way to force a launcher for a one-off spawn.
  //
  // Below it, a task's own `launcher:` frontmatter, then its goal's, then the vault's
  // `claude_script` — see `resolveTaskLauncher`.
  let taskLauncher = { launcher: workerTarget.launcher, source: 'vault' }
  if (!config.claudeCmd && roleResolution.role === 'manager') {
    // A manager takes the manager rule, not the worker rule: the `-claude` sibling of the
    // vault launcher when it exists — see `resolveManagerLauncher`. No subject page is read
    // here; a topic's `manager_launcher:` is resolved by `/supervisor:open` Step 2C.
    taskLauncher = resolveManagerLauncher({ vaultLauncher: workerTarget.launcher, exists: existsSync })
  } else if (!config.claudeCmd && typeof task === 'string' && task) {
    const read = readTaskLaunchers({ task, vault: workerTarget.vault })
    if (read.error) return { error: read.error }
    taskLauncher = resolveTaskLauncher({ vaultLauncher: workerTarget.launcher, ...read })
    for (const w of read.warnings ?? []) log(`spawn ${task}: ${w}`)
    if (taskLauncher.error) return { error: taskLauncher.error }
  }
  const workerLauncher = config.claudeCmd || taskLauncher.launcher
  const workerModel = launcherModelFor(workerLauncher)
  let workerRules = null
  let resolvedPolicyPath = null
  if (policyPath) {
    // Checked before the file, because the mode decides whether the file could ever
    // matter. A policy that cannot be REACHED is the same failure as one that is never
    // read — accepted, reported as applied, and inert — which is the failure this repo
    // already shipped once, so it is refused rather than warned about.
    // A shipping worker's mode arrives inline in the `flag` tier, which the on-disk
    // resolution below cannot see — so resolving from disk would refuse a reachable
    // policy on a machine whose user tier says `auto`. Use the mode it will run under.
    const mode = shipping === true ? SHIPPING_PERMISSION_MODE : await effectivePermissionMode(workerCwd)
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

  let carriedDecision = null
  if (resume) {
    const { live, probes, reason } = checkLiveness(resume)
    if (live === true) {
      return {
        error: `session ${resume} is still running (${reason}) — close it before resuming, or you will have two writers on one conversation`,
      }
    }
    // The channel that answers for this server's own spawns: a headless worker here is
    // an in-process SDK `query()`, and its record in this table is authoritative for
    // *this* server. `status` genuinely closes — set at spawn and cleared to
    // `done`/`error` when the query ends — unlike the ledger's `running`, which never
    // closes at all.
    //
    // ⚠️ Corrected 2026-09-25 by measurement. This comment used to claim a headless
    // worker "has no pid for the registry and no argv for any process probe", and the
    // guard's scope was reasoned from that. Measured instead: it DOES have a pid — the
    // SDK's own `claude-agent-sdk-*` binary — and DOES register in `~/.claude/sessions/`
    // while it is alive, carrying `kind: interactive` like any tab worker, so `kind`
    // cannot tell the two apart. `checkLiveness` therefore answers `live: true` and
    // refuses a parked worker at the registry probe first. This guard is a second line,
    // not the first — it stays because it is the only one for a worker the registry
    // cannot see.
    //
    // Without this, a `false` from the registry would be read as "closed" and a live
    // worker resumed: two writers on one conversation, the corruption this guard
    // exists to prevent. Scoped to this server's own spawns by construction; a worker
    // spawned by a different server process is not visible here.
    //
    // The test is finished-vs-not, NOT `running` — see resume-guard.mjs. A parked
    // worker carries `blocked-on-permission` while it is blocked inside a tool call,
    // so matching only `running` let it through and left a parked worker resumable.
    // ⚠️ PRUNE FIRST, then let the guard see every row — do NOT filter rehydrated rows out.
    // The first cut excluded them wholesale, to stop a DEAD worker's row asserting it was
    // still mid-turn and refusing the resume that is its only recovery. `pruneRehydratedAgents`
    // deletes rows whose liveness has gone false, so a row still in the Map after it runs
    // SHOULD hold the guard. The prune previously ran on the read paths only, which is why the
    // resume path never saw it — and this guard's contract, which this comment states, had
    // drifted to the opposite of its own code.
    //
    // ⚠️ This is defence in depth, NOT the line that stops a live resume: `checkLiveness(resume)`
    // above already refuses one, over the same channels and the same TTL. What this guard is
    // the only line for is a worker in THIS process's `agents` that the registry cannot see.
    //
    // ⚠️ "Still in the Map" is an AGE PROXY, not proof of liveness — `liveSessionIds()` reads a
    // heartbeat stamp against HEARTBEAT_TTL_MS (60s) or a pid-checked registry entry. So a dead
    // worker's row can hold the guard for up to 60s after its owner dies: the deadlock the
    // deleted filter existed to prevent, now bounded and self-healing on retry rather than
    // immediate. And when `liveSessionIds()` answers `null`, every row is kept — the fail-closed
    // direction, but a weaker property than "live by construction".
    pruneRehydratedAgents()
    const holder = findLiveHolder(agents.values(), resume)
    if (holder) {
      return {
        // ⚠️ A rehydrated row's worker lives in the supervisor process that SPAWNED it, not
        // in this one (`registry-rehydrate.mjs` — adoption crosses processes), so naming
        // "this server" sends the operator to close the wrong thing.
        error: `session ${resume} is still running (worker ${holder.id} is mid-turn ${holder.rehydrated ? 'in the supervisor process that spawned it' : 'in this server'}) — close it before resuming, or you will have two writers on one conversation`,
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

    // A decision can only ride a resume, and only one that names the park on record.
    //
    // The record is this server's own evidence that a park happened: written at park
    // time and — in the case this exists for — never cleared, because the `settle` that
    // would clear it lives in the spawner that died. That is what makes it the durable
    // half of the answer, and why this check reads it rather than the attention store.
    if (decision) {
      // ⚠️ Read from the endpoint, which is where every reader looks now — and `null` means
      // exactly what `readRecord`'s `null` did: no park to tie this decision to, so the verdict
      // refuses. ⚠️ `PARK_FIELD` is `parked_permission`, spelled IDENTICALLY on the wire and in
      // the file record, so the field access below is unchanged rather than renamed — the one
      // field this path depends on is the one field the two vocabularies agree on.
      const record = await readSessionLedgerRow(resume)
      const verdict = validateDecision({ decision, parkRecord: record?.[PARK_FIELD] ?? null, sessionId: resume })
      if (!verdict.ok) {
        log(`refusing a decision on resume ${resume}: ${verdict.error}`)
        return { error: verdict.error }
      }
      // The decision is information for the worker's next turn, not a re-run of the
      // tool: the gate was already answered, and replaying the call would be a second
      // attempt at something the operator settled once.
      prompt = `${renderResumePrompt({ decision: verdict.decision, parkRecord: record[PARK_FIELD] })}\n\n${prompt ?? ''}`.trim()
      log(`resume of ${resume} carries a ${verdict.decision.behavior} for ${verdict.decision.item_id}`)
      // Carried on the AGENT, not just in the prompt. The decision has to reach the gate
      // the resumed worker re-raises — measured 2026-09-25: without this the worker came
      // back and parked on the identical tool call, so a carried `deny` re-asked instead
      // of stopping. There is no original promise left to settle: it died with the
      // spawner. The gate worth answering is the one raised in THIS server.
      carriedDecision = buildCarriedDecision({ decision: verdict.decision, parkRecord: record[PARK_FIELD] })
    }
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
    // Carried on the LIVE agent so `list_agents` can report it. ⚠️ **Not persisted** —
    // `buildRecord` has no `shipping` parameter, so no ledger record carries the flag and a
    // resume cannot recover it. `registry-rehydrate.mjs` hardcodes `shipping: false` for
    // exactly that reason. This comment previously claimed the opposite — that the flag WAS
    // recorded so a resume could recover it — which is the contradiction `agentView` below
    // already names. Corrected to the code's behaviour rather than the intent; making it
    // recoverable is a separate change (persist it, then read it back in `toAgentRecord`).
    shipping: shipping === true,
    sessionId: null,
    // Which conversation this one continues, when it is an adoption rather than a
    // fresh start. `sessionId` alone cannot say: on resume the SDK reports the SAME
    // id, so the two fields together are what tell the operator which conversation
    // they are now in.
    resumedFrom: resume ?? null,
    // The decision this resume carried, if any — consumed by the first matching gate.
    carriedDecision,
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
    // The mode ITSELF, beside the source that decided it. `status` cannot answer this and
    // must not be asked to: it carries liveness ('running', 'blocked-on-permission',
    // 'done'), and a tab worker's 'interactive' is assigned a few lines below — so a reader
    // deriving headless-ness from `status` would be inferring it from a field that means
    // something else, and would read a not-yet-assigned tab as headless. The attention
    // poll's release guard needs this value positively and refuses when it is absent.
    mode: spawnMode.mode,
    // What the role resolved to, and the window actually targeted. Carried on the agent so
    // the reply, agent_status and the ledger can all answer "where did this spawn go, and
    // why" from one value — and so a role-routed spawn is OBSERVABLE rather than inferred
    // from wherever the tab happened to land.
    role: roleResolution.role,
    windowId: targetWindowId ?? null,
    // Which vault this worker belongs to, the launcher it inherits, and the model that
    // launcher starts — carried on the agent so the reply, agent_status and the ledger all
    // answer "where did this spawn go, and under what" from one value rather than
    // re-deriving it from a config that may have changed since. The model rides along
    // because it is the half a caller cannot otherwise check: a wrong launcher and a wrong
    // model are the same defect seen from two sides, and the 2026-10-03 batch had neither.
    vault: workerTarget.vault,
    launcher: workerLauncher,
    model: workerModel,
    permissions: [],
    transcript: [],
    error: null,
    createdAt: new Date().toISOString(),
  }
  // The last guard before anything is created, and deliberately the last: every refusal above
  // it returns without a side effect, so a slot is only spent by a spawn that is actually
  // going ahead. See `reserveStart` for why this waits rather than only refusing.
  //
  // ⚠️ IT SITS ABOVE `agents.set`, AND THAT ORDER IS LOAD-BEARING. Registration is this
  // function's first side effect and it writes `status: 'running'` — so a guard below it
  // leaves a REFUSED start registered as a running worker that no session backs, for the life
  // of the process: the only reaper, `pruneRehydratedAgents`, skips every entry that is not
  // `rehydrated`. The same ordering would also report a DELAYED start as running for up to
  // the 5-minute ceiling, which is the entire window a paced batch spends waiting — i.e. the
  // common case, not the edge. `agents.delete(id)` on the refusal branch would patch the
  // first half and leave the second.
  const rateError = await reserveStart()
  if (rateError.error) return { error: rateError.error }

  agents.set(id, agent)

  if (opensInteractive) {
    agent.status = 'interactive'
    const res = await spawnInteractiveAgent({ id, prompt, cwd: agent.cwd, launcher: agent.launcher, label: agent.label, windowId: targetWindowId, chip: roleResolution.chip, env: envOverrides })
    if (res.error) {
      agents.delete(id)
      return { error: res.error }
    }
    agent.paneId = res.paneId
    agent.sessionId = res.sessionId ?? null
    agent.launcher = res.launcher ?? null
    agent.transcriptDir = transcriptDirFor(agent.cwd)
    writeLedger(agent)

    // ⚠️ **The bind is the tab path's missing half, and it is not bookkeeping.** A tab
    // worker's session id is learned only by polling the registry (above), and until it
    // is written to the task's `claude_session_id` the vault cannot resolve the worker
    // at all. That is what left a parked worker invisible to
    // `manager-attention-watch.py`: its `parked-on-unregistered-gate` cell fires only
    // when a LIVE session owns the row, and with no stamp the row falls to the orphan
    // path instead — measured 2026-10-05, a worker parked on a question sat unnoticed
    // for 30+ minutes.
    //
    // Reuses `bindSessionToTask` rather than writing the field directly: it already
    // implements the ownership rule (stamp only when the field is EMPTY, else append to
    // `metrics_sessions`), and a second writer here would be a second definition of who
    // owns a task — which is exactly the displacement that module's own docstring
    // records.
    //
    // ⚠️ **NOT fatal, unlike the cluster path's bind.** A cluster worker has no pane, so
    // a failed bind there leaves a session nothing points at and the caller is told. This
    // worker is reachable by pane and by ledger, so failing the spawn would destroy a live
    // worker to report a bookkeeping failure. Reported on the response instead, and logged
    // because the response is easy to drop.
    let bind = null
    // ⚠️ `typeof task === 'string'` is not defensive padding: the cluster branch
    // type-checks `task` at its own entry, but the local path does not, and a
    // non-string reaches `spawnSync('vault-cli', args)` as an argv element and throws
    // `ERR_INVALID_ARG_TYPE` — AFTER the tab is spawned and registered. That throw
    // would lose the response for a worker that is already live.
    if (typeof task === 'string' && task && agent.sessionId) {
      // ⚠️ `workerTarget.vault`, never the raw `vault` argument. The argument is
      // `undefined` for a cwd-only spawn — an explicitly supported shape — and
      // `bindSessionToTask` omits `--vault` when it is falsy, so the write would land
      // in vault-cli's DEFAULT vault rather than the one this worker resolved to. The
      // agent record above already carries `workerTarget.vault` for the same reason.
      try {
        const bound = bindSessionToTask({ task, vault: workerTarget.vault, sessionId: agent.sessionId })
        if (bound.error) {
          bind = { error: bound.error }
          log(`WARNING: worker ${id} spawned but binding it to task failed: ${String(bound.error).replace(/[\r\n]+/g, ' ')}`)
        } else {
          bind = { action: bound.action, vault: bound.vault }
        }
      } catch (error) {
        // Same reasoning as the non-fatal `bound.error` branch: the worker is live and
        // reachable, so a bind that throws must be reported, never allowed to escape
        // and discard the spawn response.
        bind = { error: String(error && error.message ? error.message : error) }
        log(`WARNING: worker ${id} spawned but the task bind threw: ${String(bind.error).replace(/[\r\n]+/g, ' ')}`)
      }
    }

    return {
      agent_id: id,
      label: agent.label,
      cwd: agent.cwd,
      status: agent.status,
      // The three fields the 2026-10-03 incident had no way to read. A caller could see
      // only the `cwd` it passed and nothing about where the worker went or what it ran on,
      // so a batch of 27 wrong workers returned exactly what a correct one returns.
      vault: agent.vault,
      launcher: agent.launcher,
      // Which rule picked it: `task` / `goal` frontmatter, the `vault` default, or the
      // `SUPERVISOR_CLAUDE_CMD` override.
      launcher_source: config.claudeCmd ? 'override' : taskLauncher.source,
      model: agent.model,
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
      // The names of the per-call environment this worker was given, or null. Reported on
      // both paths for the same reason `policy` and `color` are: "accepted and then inert"
      // is this repo's recurring failure, and a caller who passed `env` has no other way to
      // tell it was received. Keys only — see envReport.
      env: envReport,
      // The task this worker was opened for, and whether the vault's record of it was
      // written. `null` when the caller named no task (the stamp is optional for a tab
      // worker, unlike the cluster path). Reported rather than swallowed for the same
      // reason the cluster path reports it: a spawn whose owner was never recorded is a
      // worker the fleet cannot find, and it otherwise reads exactly like a clean one.
      bind,
    }
  }

  // `agent.launcher` is already set — resolved once, above, alongside the cwd it belongs
  // to. It used to be re-derived here from `agent.cwd` alone, which meant the tab path and
  // the headless path each resolved the launcher separately and could disagree about the
  // same spawn.

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
      // `overrides` is the caller's per-call `env`. It sits above the inherited environment
      // and below the mode handover — see workerEnvFor for why that order is the only
      // correct one.
      env: workerEnvFor({ mode: spawnMode.mode, source: spawnMode.source, env: config.baseEnv, overrides: envOverrides }),
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
      // A shipping worker runs under `acceptEdits` instead, so its edits and its three git
      // verbs never depend on the permission channel; see shipping-settings.mjs for why
      // that mode and not a wider one. The option and the flag-tier `settings` say the
      // same thing on purpose: either alone leaves a reader to wonder which one won.
      permissionMode: shipping === true ? SHIPPING_PERMISSION_MODE : PERMISSION_MODE,
      ...(shipping === true ? { settings: shippingSettings(true) } : {}),
      canUseTool: makeCanUseTool(agent),
      hooks: { PermissionRequest: [{ hooks: [makePermissionHook(agent)] }] },
    },
  })
  agent.query = q

  // Fire-and-forget on purpose: a spawn must return its agent id without waiting for
  // the worker's first turn. The loop itself lives in agent-loop.mjs so a test can
  // drive it with a synthetic stream and await its completion — this module cannot be
  // imported by a test at all, since it connects a stdio server at load.
  void runAgentLoop({ q, agent, writeLedger, log, heartbeat })

  return {
    agent_id: id,
    label: agent.label,
    cwd: agent.cwd,
    status: agent.status,
    // Same three fields as the tab path, and for the same reason: the launcher and the
    // model a worker runs under are not observable from the arguments that produced it.
    vault: agent.vault,
    launcher: agent.launcher,
    launcher_source: config.claudeCmd ? 'override' : taskLauncher.source,
    model: agent.model,
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
    shipping: agent.shipping === true,
    // Same field as the tab path, and here too it is not redundant: this is the half a
    // headless-only implementation would leave unexercised, so a caller reading it back is
    // how "both paths carry it" stops being a claim about the diff. Keys only — see envReport.
    env: envReport,
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
    shipping: a.shipping === true,
    // ⚠️ The discriminating signal for a row adopted from the ledger rather than spawned by
    // this process. Without it three fields a caller DOES see are boot-time or false readings
    // that read as measurements: `status` is never terminal, `shipping` is unrecoverable
    // (`buildRecord` never persisted the flag), and `pending_permissions: []` is
    // indistinguishable from "no gate outstanding" — the exact reading that cost a manager a
    // misdirected worker in the v0.110.4 incident. See registry-rehydrate.mjs.
    rehydrated: a.rehydrated === true,
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
    // ── the pushed status channel ────────────────────────────────────────────────────────────
    // ⚠️ BOTH FIELDS ARE ALWAYS PRESENT, AND THAT IS THE POINT. `status_messages` is `[]` and
    // `last_status` is `null` for every worker that is not an A2A one — the same shape a live A2A
    // worker has before its first push lands. Neither is ever `undefined`, so a caller can tell
    // "the channel exists and is quiet" from "this field was never wired", and only the second is
    // a defect. That is also why the surfacing criterion is written to reject an empty array: the
    // shape alone cannot distinguish a working channel from an unwired one.
    //
    // ⚠️ THE VALUES ORIGINATE AT THE WORKER. `ts` is the receive instant the receiver stamped, and
    // `state` / `text` come from the pushed event — nothing here is synthesised from the reply the
    // spawner already collected, which is what makes the field evidence of a push rather than of a
    // projection. See `a2a-receive.mjs` for why the receive instant and not a body-supplied one.
    status_messages: a2aPushRegistry.statusMessagesFor(a.workerId),
    last_status: a2aPushRegistry.lastStatusFor(a.workerId),
    // ⚠️ THE CHANNEL'S OWN OUTCOME, so an empty `status_messages` stops being ambiguous. It says
    // whether a registration was attempted and, when refused, why — the distinction the two
    // fields above cannot draw on their own. Always present, `null` only for a record written
    // before this field existed. Holds no credential: `taskId` is the task's own id.
    push_registration: a.pushRegistration ?? null,
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
        cwd: {
          type: 'string',
          description:
            'Working directory for the worker. It must resolve to a configured vault — the vault supplies the LAUNCHER, so a cwd outside every vault is refused rather than run under a guessed one. Prefer `vault` when the worker belongs to a vault rather than to a specific directory. Omitting BOTH `cwd` and `vault` is REFUSED: it used to default to this server\'s own working directory, which is how 27 workers came up in the supervisor repo on the wrong model in one batch (2026-10-03).',
        },
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
        target: {
          type: 'string',
          // ⚠️ A HARDCODED LITERAL, NOT DERIVED FROM `SPAWN_TARGETS` — this file imports only
          // `A2A_SPAWN_TARGETS`, so the two can drift and the enum is the surface a caller
          // actually reads. `cluster` was removed here in the same change that removed it from
          // `SPAWN_TARGETS` (2026-10-15), because leaving it would keep the published tool
          // OFFERING a target the server refuses.
          enum: ['local', 'docker', 'k8s'],
          description:
            'WHERE the worker is created. Omit for `local` — every worker this server opened before this argument existed: a wezterm tab, or with `interactive:false` an in-process SDK query. It is PER-CALL ONLY and deliberately NOT selectable from SUPERVISOR_SPAWN_MODE or `spawn.mode` in the config file, because the target is a second option rather than the fleet default — a config value able to default the whole fleet somewhere is exactly what that split forecloses. A value that is not a known target REFUSES the spawn rather than falling back, since a worker created somewhere the caller did not ask for is otherwise discovered only by noticing it. `docker` and `k8s` are the A2A worker-controller targets: `docker` asks the controller for one container per task on this Mac, `k8s` for one pod per task in nuke dev, and both then talk to the worker DIRECTLY over A2A. Both are PER-CALL ONLY for the same reason. ⚠️ With either, the local-only arguments `interactive`, `resume`, `policy` and `env` are REFUSED rather than ignored: the controller creates the worker, so there is no local process or window here to set one on, and a dropped `resume` would look like a resume while opening a fresh session. Omit all four on a docker or k8s call. ⚠️ A worker created this way is stopped with `stop_agent`, never with `send_agent_message` — the two are different planes (see that tool). ⚠️ `cluster` was RETIRED on 2026-10-15 (the shared long-lived `claude-interactive` pod in nuke dev); `k8s` is the route to a worker in that cluster now, and `cluster` is refused like any other unknown target.',
        },
        task: {
          type: 'string',
          description:
            'The vault task this worker is opened for. On every LOCAL path it also chooses the launcher: the task\'s `launcher:` frontmatter, else its goal\'s, else the vault\'s `claude_script` (rule: docs/fleet-surface.md § Spawn a worker item 8) — so a task that cannot be read REFUSES the spawn. Honoured on the TAB and CLUSTER paths, and there it is what binds the worker to the vault: the session id is written to this task\'s `claude_session_id` (through the ownership rule in `task-binding.mjs` — stamped only when the field is EMPTY, otherwise appended to `metrics_sessions`) and reported back as `bind`. REQUIRED with `target: "cluster"`. ⚠️ The local HEADLESS path (`target: "local"` with `interactive: false`) does NOT bind and reports no `bind` key at all, so a headless spawn carrying `task` leaves the field unwritten. Pass `vault` alongside it when the task name is not unique across the configured vaults.',
        },
        vault: {
          type: 'string',
          description:
            'The vault this worker belongs to. Resolves BOTH the working directory and the default launcher (that vault\'s `claude_script`, which a `task`\'s `launcher:` frontmatter can override) from one value, so the two cannot disagree — the reliable form of the required `cwd`/`vault` pair, and the one to prefer. An unknown vault, or a vault with no `claude_script`, is refused rather than falling back to another vault\'s launcher or to the bare `claude` binary. With `target: "cluster"` it instead names the vault the `task` lives in, because task names collide across boards.',
        },
        resume: {
          type: 'string',
          description:
            'Session id to continue. Requires interactive:false — a tab worker cannot honour it (the tab path launches the cc-* launcher, which is never handed the flag) and the call is refused rather than quietly opening a fresh conversation. The session MUST be closed: resuming a live one puts two writers on one conversation, so a session found still running is refused, and so is one whose liveness cannot be determined. The resumed worker is created here, so unlike the original session it IS supervised and its prompts park for the manager.',
        },
        decision: {
          type: 'object',
          description:
            'The answer to carry into the resumed worker — for a worker that was parked on a permission when its spawner died, which is the one case `resume` alone cannot help with. Shape: { item_id, behavior: "allow"|"deny", message? }. `item_id` must name the park on that session\'s ledger record: a bare `allow` with no `item_id` is refused, and so is a decision naming a different park. The worker is TOLD the decision; the tool is not re-run, because the gate was already answered once. Requires `resume`.',
          properties: {
            item_id: { type: 'string' },
            behavior: { type: 'string', enum: ['allow', 'deny'] },
            message: { type: 'string' },
          },
          required: ['item_id', 'behavior'],
        },
        shipping: {
          type: 'boolean',
          description:
            'Mark this worker as SHIPPING: it runs under `acceptEdits` with `git add` / `git commit` / `git push` pre-allowed, so its edits and commits never depend on the supervisor permission channel — a headless worker whose channel dies ("Stream closed") can otherwise neither edit nor commit. Every other tool still prompts, so the approval policy stays in force. Headless and local only: combining it with interactive:true or target:"cluster" is refused. Omit for an ordinary worker.',
        },
        policy: {
          type: 'string',
          description:
            'Path to a JSON file of approval rules for THIS worker, evaluated ahead of the user and bundled rules — first match wins, so a rule here beats both, while the bundled set still covers whatever it does not name. Full replacement is reachable by ending the file with a {"tool":"*","match":"*","action":"escalate"} catch-all. An absolute path is used as-is; a relative one resolves against the worker\'s cwd. The file must exist and parse — a named policy that cannot be read refuses the spawn rather than silently falling back to the server default. Headless only: a tab worker answers its own prompts in its tab, so combining this with interactive:true is refused. Omit to use the server policy.',
        },
        env: {
          type: 'object',
          description:
            'Environment variables for THIS worker only — a per-call override of the ambient environment, so one session can be launched against a non-production backend without redirecting every session in a vault. Keys are shell variable names; values are strings (a number or boolean is stringified). They are layered ON TOP of the environment this server already passes through, so a key given here WINS over the inherited one and every key NOT given here is inherited unchanged — PATH, HOME and ANTHROPIC_BASE_URL among them. Honoured on BOTH local paths: a tab worker gets them exported into the tab\'s own command line, a headless worker gets them set on the SDK process. ⚠️ **On the TAB path the VALUES are visible in the host process table** (`ps`, `/proc/<pid>/cmdline`) for the life of the pane — exporting into a command line is the only mechanism a tab worker has, so a credential passed here is exposed to every local user on the machine; the headless path has no such exposure, because the SDK sets the child environment directly. Pass a credential only if you accept that. The spawn response echoes the NAMES back as `env: {keys: [...]}` — never the values, since a caller may pass a credential and this response gets quoted into ledger records and PR bodies — so a caller can tell the parameter was received rather than accepted-and-dropped. ⚠️ REFUSED with target:"cluster": that path creates no local process, so there is no environment here to set. An illegal variable name, or a value that is not a string/number/boolean, REFUSES the spawn rather than silently dropping the key.',
        },
        mounts: {
          type: 'array',
          description:
            'Host paths to make visible inside a `docker` or `k8s` worker, passed through to the controller\'s `create` unchanged. Each entry is `{host_path, container_path, read_only?}`. ⚠️ THE CONTROLLER DECIDES, NOT THIS SERVER: it checks `host_path` against its own allowlist (`MOUNT_ALLOW` on the local docker controller) and REFUSES anything outside it with `mount_not_allowed: <path>`, creating no worker — so declaring a mount is a request, never a grant. A declared mount is WRITABLE unless you set `read_only: true`. ⚠️ A named `volume` is refused rather than accepted: the allowlist governs host paths only, so a volume would be an ungoverned surface. ⚠️ This is the sanctioned route to what the raw `env` argument cannot give you — `env` stays refused on these targets, and a mount carries a FILE rather than a value, so no credential travels from here. Ignored on the local paths, which have no controller.',
        },
        secrets: {
          type: 'array',
          description:
            'Secrets to deliver to a `docker` or `k8s` worker, passed through to the controller\'s `create` unchanged. Each entry is `{name, teamvault_key}` — ⚠️ A TEAMVAULT KEY, NEVER A VALUE: the controller resolves the key against TeamVault itself, so the secret\'s value never travels from this server, never appears in a tool result, and is never written to a ledger record. Delivery differs by backend and the value is protected in both: docker writes it to a 0600 host file bind-mounted READ-ONLY at `/run/secrets/<name>` (never an env var, so it is absent from `docker inspect` Config.Env), and k8s adds the key to the worker\'s own Secret object, which the pod already consumes via `envFrom`. The controller refuses a key it cannot resolve, creating no worker. ⚠️ This is the sanctioned route to what the raw `env` argument cannot give you — `env` stays refused on these targets precisely because a value passed that way would be ungoverned. Ignored on the local paths, which have no controller.',
        },
        operator_named: {
          type: 'boolean',
          description:
            // ⚠️ The two numbers are INTERPOLATED from the constants, never written. They were
            // written once, as "30 / hard 50", and were wrong within the hour: 30 is the value
            // the operator set in his config, not the code default, which is 20. A restated
            // default is a second counter a reader cannot tell from the real one, and this
            // surface is outside `check-worker-target.py`'s reach — so the fix is to make the
            // drift impossible rather than to correct it once.
            `Whether the OPERATOR named THIS task by hand — the exemption that lets one task open while the fleet sits between its soft and hard caps (default soft ${DEFAULT_MAX_CONCURRENT} / hard ${DEFAULT_MAX_CONCURRENT_HARD}). The caller resolves it from the task's own operator-set flag and passes it; the server never infers it and never reads the vault, because a default of true would make the soft cap unenforceable. Omit for an ordinary spawn, which is refused at the soft cap. An operator-named spawn is still refused at the hard cap, where the fleet is full for everyone.`,
        },
      },
      required: ['prompt'],
    },
  },
  {
    name: 'send_agent_message',
    description:
      'Send a follow-up message to a running worker — the send_to_agent this server has never had. TWO transports, chosen from the worker\'s own record. A TAB worker: activates its tab, waits for the input prompt, and types the message, so it STEALS FOCUS; returns an error rather than a false success when the pane is gone, the tab cannot be activated, or the prompt never appears. A CLUSTER worker: posts one more turn on its EXISTING session id through the claude-interactive service — the bearer token is resolved inside the server, so the caller passes no credential — and BLOCKS until the pod answers, so expect a long call on a long turn. ⚠️ A 2xx proves a turn ran, not that the SAME conversation ran: the pod\'s own log (`turn start id=<id>` / `turn end id=<id>`) is the only proof of that, so never read the returned `answer` as a delivery receipt. An A2A worker (`docker` / `k8s`): sends one more turn to the WORKER\'s own endpoint on the conversation its first turn opened — the controller is not in the path — and returns once the worker acknowledges, with `send_ms`; the result is collected in the background and read with `agent_status`. A row re-adopted after a reconnect holds no token and is refused. A headless worker has neither a pane nor a cluster session and is refused; answer its parked prompts with `answer_permission` instead.',
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
    name: 'stop_agent',
    description:
      'Stop an A2A worker (`docker` / `k8s`) — the one manager-facing way to remove a worker this fleet spawned, and the third of the controller\'s `create` / `list` / `stop` operations to be reachable from here. It calls the A2A worker controller\'s `stop` on the worker id `create` recorded, so the container (docker) or the pod and its `Config` CR (k8s) are removed. ⚠️ KEYED ON THE WORKER ID, NEVER A TASK NAME: one task can be spawned again after its first worker died, and a task-keyed stop would remove the successor. The id is read from the worker\'s own ledger record — pass `agent_id`, and the id is resolved for you. ⚠️ THIS IS A CONTROL-PLANE CALL TO THE CONTROLLER, authenticated with the controller token this server already holds — it is NOT a turn to the worker, so it needs no worker token. That is what makes it work on a row this process never spawned, including one re-adopted after a reconnect: those can be seen but not messaged, and they CAN be stopped. ⚠️ It applies to A2A workers only. A tab worker has no controller worker to stop (close its tab), and a cluster worker is the shared long-lived pod — out of scope here. ⚠️ A `TASK_STATE_FAILED` reply does NOT mean the worker is still running: the controller reports a store-side failure the same way, so reconcile against `list_agents` / the controller\'s own `list` rather than assuming the stop did not happen.',
    inputSchema: {
      type: 'object',
      properties: {
        agent_id: { type: 'string', description: 'The A2A worker to stop — the id `spawn_agent` returned, or one from `list_agents`.' },
      },
      required: ['agent_id'],
    },
  },
  {
    name: 'read_push_config',
    description:
      'Read a worker\'s registered A2A push config back FROM THE WORKER — `{ configs }` (a list, empty when the worker holds none) or `{ error }`. It lists rather than gets: the SDK\'s get is keyed by task id AND a config id the registration never sends, so a get can never match. This is the read half of the push round trip: `push_registration.state === "attempted-ok"` proves only that the worker ACCEPTED a registration, not that it holds one, and this call is the only thing that separates those two. ⚠️ It requires an accepted registration to exist for that agent and refuses otherwise, rather than reading an unregistered task. ⚠️ It returns the CONFIG — the url and notification token the worker stores — never the worker\'s bearer token, which authenticates the call and is never projected. ⚠️ Read-only: nothing on the worker changes.',
    inputSchema: {
      type: 'object',
      properties: {
        agent_id: {
          type: 'string',
          description: 'The A2A worker whose push config to read — the id `spawn_agent` returned, or one from `list_agents`.',
        },
      },
      required: ['agent_id'],
    },
  },
  {
    name: 'list_agents',
    description: 'All spawned sessions with status and how many permissions are pending. Each row also carries `current_tool_call` (the call in flight and how long it has been held) and `last_message`, so one sweep says what every worker is doing without a pane. Each row also carries `open_cards` — the session\'s open attention-store cards (`{item_id, answer_mechanism, payload, created_at}`), which is where a worker parked on a permission prompt shows up whoever spawned it, since `pending_permissions` covers only the parks this server holds. The tool name is the card\'s `payload` prefix. Answer one with `attention-answer.py answer <item_id> --decision allow|deny`. `open_cards` is null (not []) on every row when the store is unreachable or switched off. Each row also carries `last_status` — the newest pushed A2A status for that worker as `{worker_id, state, text, ts, age_s}`, or null when nothing has arrived — which is what lets one sweep show a docker worker\'s own mid-turn progress without opening its pane or waiting for its turn to end.',
    inputSchema: { type: 'object', properties: {} },
  },
  {
    name: 'agent_status',
    description: 'One session: status, last assistant message, the current tool call, result, pending permission ids. `current_tool_call` is `{name, input_summary, started_at, held_seconds}` — the call the worker is inside right now and how long it has been held — or null when nothing is in flight; it is read from the worker\'s transcript and is what tells a long tool call apart from a worker parked on a gate. For a tab worker the last message is read from its transcript on disk. `session_status` and `awaiting_input` come from the session registry; `awaiting_input` is null (not false) when the session is not listed, and means "blocked on input" rather than specifically "a permission gate is open". `open_cards` lists the session\'s open attention-store cards (`{item_id, answer_mechanism, payload, created_at}`) — for a tab worker this is where a live permission prompt shows, since `pending_permissions` covers headless parks only; answer a `permission` card with `attention-answer.py answer <item_id> --decision allow|deny`. `open_cards` is null (not []) when the store is unreachable or switched off. `status_messages` is the worker\'s pushed A2A status channel — `{worker_id, state, text, ts}` entries, oldest first, and empty for every worker that is not an A2A one; `last_status` is the newest of those with an added `age_s`, or null when nothing has arrived. Both fields are ALWAYS present, so an empty `status_messages` means the channel is quiet rather than unwired — a worker that has just spawned looks the same as one that is not an A2A worker at all.',
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
    description: 'Answer a parked permission prompt: allow or deny. This is what unblocks the worker. ⚠️ Only for a park THIS server holds — a request parked by another manager\'s server is refused, and the refusal names the worker and its spawning session plus the `attention-answer.py answer <item_id> --decision allow|deny` route by which that manager\'s server settles it. A cross-session `answer_permission` cannot release another server\'s park.',
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
          // Passed through raw and validated in spawn-mode.mjs, which owns the legal values and
          // the refusal message — the same split as the mode and the role above.
          target: args.target,
          task: args.task,
          vault: args.vault,
          resume: args.resume,
          decision: args.decision,
          // Passed through raw, like `target`: the caller asserts it from the task's own
          // operator-set flag, and `concurrentLimitError` reads only truthiness. Coercing an
          // omitted argument to a boolean here is what made the mode config unreachable.
          operatorNamed: args.operator_named,
          policy: args.policy,
          // Only a literal `true` opts in — see shippingSettings.
          shipping: args.shipping,
          // Passed through raw and validated in spawn-mode.mjs, which owns the legal shape
          // and the refusal message — the same split as the mode, the target and the role
          // above. A value that is not an object of string-ish values refuses there.
          env: args.env,
          // ⚠️ UNLIKE `env` ABOVE, THESE ARE NOT REFUSED ON THE A2A TARGETS — they are the
          // sanctioned route to what `env` cannot provide. Passed through raw; the shape is
          // validated in `declaredCreateArguments` (a2a-client.mjs), which is what puts bytes on
          // the wire, so a malformed entry is refused at the builder rather than reaching the
          // controller. A caller cannot pass a secret's VALUE through `secrets`: the entry names a
          // TeamVault key, and the controller resolves it.
          mounts: args.mounts,
          secrets: args.secrets,
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
      // ⚠️ The request-time roster, for the same reason `list_agents` reads it: the worker a
      // manager is being asked to message is the one it did NOT spawn — that is the whole
      // point of the endpoint — so a Map-only lookup refuses exactly the call this change
      // exists to make work. The transport still comes from the row (`pane_id`, `worker_id`),
      // which `toAgentRecord` carries. ⚠️ The `agent.messages` append below is a no-op for a
      // row built fresh from the endpoint: that array is this process's own in-memory log and
      // was never persisted for any worker, so nothing regresses — it is named so the missing
      // entry is not read as a delivery failure.
      const agent = (await currentRoster()).get(args.agent_id)
      if (!agent) return reply({ error: `unknown agent ${args.agent_id}` })
      if (typeof args.message !== 'string' || !args.message.trim()) {
        return reply({ error: 'message is required' })
      }
      // Which transport carries this, and why it is that one — `message-channel.mjs` owns
      // the decision, so the refusal names the right reason for each of the three worker
      // kinds. The guard this replaces read `status !== 'interactive' || !paneId`, which is
      // "has a pane?" wearing a general guard's clothes: true for a headless worker, and
      // accidentally true for every cluster worker, whose record carries `paneId: null` by
      // construction.
      const channel = resolveMessageChannel(agent)
      if (channel.error) return reply({ error: channel.error })

      if (channel.channel === 'cluster') {
        // Resolved per call, never cached on the agent record: the config file is the only
        // source that reaches an already-running server, and a value cached at spawn would
        // keep presenting a token the operator has since rotated.
        const target = clusterTarget()
        if (target.error) return reply({ error: target.error })
        // No `newSessionId()` here, and that is the whole change: the id comes from the
        // worker's own record, so the turn lands on the conversation the caller named
        // rather than opening a fresh one that would look identical.
        const started = await startClusterSession({
          baseUrl: target.url,
          authToken: target.token,
          prompt: args.message,
          sessionId: channel.sessionId,
        })
        if (started.error) return reply({ error: started.error })
        // The turn is already over — see `clusterTurnResult`. Recorded on the agent AND in
        // the ledger, because a cluster worker's `result` is its last finished turn: unlike
        // a local worker, whose result is written once at turn end, this one is overwritten
        // by each follow-up, and `agent_status`, `list_agents` and a peer manager reading
        // the ledger must all see the same latest turn.
        const result = clusterTurnResult(started)
        agent.messages = [...(agent.messages ?? []), { at: new Date().toISOString(), text: args.message }]
        agent.result = result
        writeLedger(agent, { result })
        log(`sent a follow-up message to cluster worker ${agent.id} (session ${channel.sessionId}, ${result.outcome})`)
        return reply({
          agent_id: agent.id,
          sent: true,
          session_id: channel.sessionId,
          answer: result.answer,
          outcome: result.outcome,
          truncated: result.truncated,
        })
      }

      if (channel.channel === 'a2a') {
        // A follow-up turn goes to the WORKER's own endpoint on the conversation its first turn
        // opened — the controller is not in this path (SC3). Sent the same way as the first turn:
        // the send is awaited, the collection is not (SC5), and `send_ms` is measured here so the
        // latency is this server's, not a caller's round trip.
        const t0 = Date.now()
        const sent = await openWorkerTurn({
          endpoint: channel.endpoint,
          contextId: channel.contextId,
          text: args.message,
          token: channel.token,
          messageId: randomUUID(),
        })
        const sendMs = Date.now() - t0
        if (sent.error) return reply({ error: `the turn could not be sent: ${sent.error}`, send_ms: sendMs })
        // ⚠️ THE FOLLOW-UP REGISTERS TOO, AND THE SEED IS RESET FIRST. A follow-up is sent with a
        // `contextId` and no `taskId`, so the worker creates a NEW task; the push config held for
        // the first turn's task does not apply to it. Without this the channel is silent for every
        // turn after the first, and `pushRegistration` keeps the FIRST turn's outcome — so the row
        // reports a registration that has nothing to do with the turn just sent.
        //
        // ⚠️ WHY THE SEED IS REWRITTEN RATHER THAN THE GUARD DROPPED. `onDiagnostic` refuses to
        // overwrite a terminal state, which is what stops it undoing `onTaskId` within a turn.
        // Resetting to the seed gives that guard the meaning it has at spawn, while keeping the
        // field per-turn — `result` is overwritten by each follow-up for exactly this reason.
        agent.pushRegistration = pendingPushRegistration()
        void collectWorkerTurn({
          res: sent.res ?? sent.pending,
          sentContextId: sent.contextId,
          onTaskId: async (taskId) => {
            const push = await a2aPushRegistry.pushFor(agent.id)
            if (!push) {
              // ⚠️ RECORDED, NOT SKIPPED. A worker whose token this registry no longer holds — a
              // row re-adopted after a reconnect, or a create that never bound — cannot register,
              // and saying so is the difference between that and a channel that is merely quiet.
              agent.pushRegistration = {
                state: 'not-attempted',
                reason: 'this process holds no push token for the worker, so no registration could be sent',
                at: new Date().toISOString(),
              }
              writeLedger(agent)
              return
            }
            // ⚠️ SAME PRE-AWAIT WRITE AS THE SPAWN SITE, and it matters more here: a follow-up is
            // the turn a reader is most likely to catch mid-flight, because it is issued against a
            // worker that already exists. Without this the field would say "not yet named a task"
            // for a task the stream named seconds ago.
            agent.pushRegistration = { state: 'attempting', taskId, at: new Date().toISOString() }
            writeLedger(agent)
            const registration = await registerPushCallbackOnWorker({
              endpoint: channel.endpoint,
              taskId,
              push,
              token: push.token,
            })
            agent.pushRegistration = registration.error
              ? { state: 'attempted-error', taskId, error: registration.error, at: new Date().toISOString() }
              : { state: 'attempted-ok', taskId, at: new Date().toISOString() }
            writeLedger(agent)
          },
          onDiagnostic: (d) => {
            // ⚠️ TERMINAL STATES ARE FINAL; `attempting` IS NOT — same rule as the spawn site, and
            // for the same reason: the pre-await write must not be able to hide a thrown call.
            const state = agent.pushRegistration?.state
            if (state === 'attempted-ok' || state === 'attempted-error') return
            const patch = pushDiagnosticPatch(d)
            if (!patch) return
            agent.pushRegistration = patch
            writeLedger(agent)
          },
        })
          .then((collected) => {
            const patch = a2aTurnPatch(collected, { at: new Date().toISOString() })
            agent.result = patch.result
            agent.error = patch.error
            writeLedger(agent, patch)
          })
          .catch((cause) => {
            const patch = { result: null, error: `collecting the turn failed: ${cause?.message ?? cause}` }
            agent.error = patch.error
            writeLedger(agent, patch)
          })
        return reply({
          agent_id: agent.id,
          channel: 'a2a',
          send_ms: sendMs,
          turn: sent.res
            ? 'sent — the worker acknowledged; its result is collected in the background'
            : 'sent, not yet acknowledged — the request is outstanding and its result is collected in the background',
        })
      }

      const res = await sendToPane(channel.paneId, args.message)
      if (res.error) return reply({ error: res.error })
      agent.messages = [...(agent.messages ?? []), { at: new Date().toISOString(), text: args.message }]
      log(`sent a follow-up message to ${agent.id} (pane ${channel.paneId}, tab ${res.tabId})`)
      return reply({ agent_id: agent.id, sent: true, tab_id: res.tabId, pane_id: res.paneId })
    }

    case 'stop_agent': {
      // The request-time roster, for the same reason `send_agent_message` reads it: the worker a
      // manager stops is often the one it did NOT spawn. ⚠️ Unlike a message, a stop needs no
      // worker-side handle — the controller token authenticates it and the worker id comes off the
      // row — so a re-adopted row this process cannot MESSAGE is still stoppable, and the refusal
      // below is scoped to what stop actually requires rather than to what a turn would.
      const agent = (await currentRoster()).get(args.agent_id)
      if (!agent) return reply({ error: `unknown agent ${args.agent_id}` })

      // Only an A2A worker has a controller worker behind it. Refused with the reason per kind
      // rather than a bare "not supported", so a caller told no is told what to do instead.
      if (!A2A_SPAWN_TARGETS.includes(agent.status)) {
        return reply({
          error:
            `agent ${agent.id} is not an A2A worker (status "${agent.status}"), so there is no controller worker ` +
            'to stop. A tab worker is closed in its own tab; a cluster worker is the shared long-lived pod and is ' +
            'out of this tool\'s scope. Only the `docker` and `k8s` targets are stopped here.',
        })
      }
      // ⚠️ THE WORKER ID COMES OFF THE RECORD, NEVER FROM A TASK. It is the id `create` recorded
      // (`ledger.mjs` `worker_id`), restored on rehydrate by `toAgentRecord` — so a row that was
      // re-adopted still carries it. A row without one is refused rather than stopped by a
      // re-derived handle: one task can be spawned again, and a task-keyed stop removes the successor.
      if (typeof agent.workerId !== 'string' || agent.workerId === '') {
        return reply({
          error:
            `agent ${agent.id} is an A2A worker with no worker id on its record, so there is no worker to name in a ` +
            'stop. The id is written at spawn; a row missing it cannot be stopped by task name, because one task ' +
            'can be spawned again and the stop would remove the successor.',
        })
      }

      // Resolved per call, never cached on the agent record — the config file is the only source
      // that reaches an already-running server, and a value cached at spawn would keep presenting
      // a token the operator has since rotated.
      // ⚠️ `agent.status` IS the target for an A2A row — the roster stores the spawn target there,
      // which is why the guard above reads it as one. So a stop resolves the same controller the
      // spawn did, from the record rather than from the caller's memory.
      const controller = resolveA2AControllerTarget({
        target: agent.status,
        envUrl: config.a2aUrls[agent.status],
        envToken: config.a2aTokens[agent.status],
        file: config.configFileContents,
        path: config.configFile,
      })
      if (controller.error) return reply({ error: controller.error })

      const stopped = await stopWorkerOnController({
        endpoint: controller.url,
        workerId: agent.workerId,
        token: controller.token,
        messageId: randomUUID(),
        mint: () => randomUUID(),
      })
      if (stopped.error) return reply({ error: stopped.error, agent_id: agent.id, worker_id: agent.workerId })

      // ⚠️ **The stop is RECORDED, not merely echoed — and the reply alone is not a record.**
      // The controller reports `stopped` only in the reply to the stop that removed it; `list`
      // never returns it. So a supervisor that returns the reply and files nothing leaves the
      // durable record silent, and the manager sweep then reads a deliberately stopped worker
      // exactly as it reads a dead one — an orphan candidate, which the drive leg would
      // stop+recreate, resurrecting work someone ended on purpose.
      //
      // ⚠️ **ONE record now, and it is the ledger row.** There used to be two: `writeLedger`
      // refused an adopted row outright, so a stop on a worker this server did NOT spawn reached
      // no durable store at all, and a machine-wide stop file carried exactly that case. It now
      // routes such a stop through the endpoint's PATCH, which MERGES where `Post` would blank
      // the owner's fields (see its guard), so the row is written for every stop and the file —
      // with the `worker_id` join that existed only to read it — is gone. Applied on the success
      // path only: a stop the controller refused is not a stop.
      writeLedger(agent, stoppedPatch())

      // ⚠️ The response is echoed as a whole rather than projected: it names the operation and the
      // worker's resulting state, which is what a caller checks — and it carries no credential
      // (the controller token rides in a header, never in the reply body).
      return reply({
        agent_id: agent.id,
        worker_id: agent.workerId,
        target: agent.status,
        state: stopped.state,
        response: stopped.response,
      })
    }

    case 'list_agents': {
      // ⚠️ The request-time roster, not the in-process Map: a worker another manager spawned —
      // or one resumed from a WezTerm pane — is not in this process's Map at all, and reading
      // only the Map is the defect this task exists to close. See `currentRoster`.
      const roster = [...(await currentRoster()).values()]
      // ⚠️ One store read for the whole roster, fanned out through the pure `openCardsOf` —
      // never one `fetchOpenCards` per worker. That would be N round-trips, and with the store
      // down N × the 3 s timeout, in front of every manager sweep.
      //
      // `null` (the store could not be read) stays distinct from `[]` (read, nothing open for
      // this session) on every row: collapsing them reports a parked worker as unblocked, which
      // is the false negative this field exists to remove.
      const items = await fetchAttentionItems({ storeUrl: config.attentionStoreUrl })
      return reply(
        roster.map((agent) => ({
          ...agentView(agent),
          open_cards: items === null ? null : openCardsOf(items, agent.sessionId),
        })),
      )
    }

    case 'agent_status': {
      const agent = (await currentRoster()).get(args.agent_id)
      if (!agent) return reply({ error: `unknown agent ${args.agent_id}` })
      // A tab worker's permission prompt never enters the park queue, so
      // `pending_permissions` is empty for it by construction. Its attention-store cards
      // are the gate a manager can see — and, via the plugin's PermissionRequest hook,
      // answer with `attention-answer.py answer <item_id>`. Null = store unreadable/off.
      const open_cards = await fetchOpenCards({ storeUrl: config.attentionStoreUrl, sessionId: agent.sessionId })
      return reply({ ...agentView(agent), open_cards })
    }

    case 'read_push_config': {
      // ⚠️ THE READ HALF OF THE ROUND TRIP. `attempted-ok` says the worker ACCEPTED a registration;
      // only a read says it HOLDS one. Gated on an accepted registration existing, because reading
      // a task the worker never accepted would report "no config" and read as a channel fault.
      const agent = (await currentRoster()).get(args.agent_id)
      if (!agent) return reply({ error: `unknown agent ${args.agent_id}` })
      const reg = agent.pushRegistration
      if (!reg || reg.state !== 'attempted-ok' || typeof reg.taskId !== 'string') {
        return reply({
          error:
            `agent ${args.agent_id} has no accepted push registration to read back (state: ${reg?.state ?? 'none'}). ` +
            `A read of a task the worker never accepted would report a missing config as a channel fault.`,
        })
      }
      if (!agent.a2aUrl || !agent.workerToken) {
        return reply({
          error:
            `agent ${args.agent_id} is an A2A worker whose endpoint or bearer token this process does not hold — ` +
            `a row re-adopted after a reconnect can be watched but not read back.`,
        })
      }
      const out = await readPushCallbackFromWorker({
        endpoint: agent.a2aUrl,
        taskId: reg.taskId,
        token: agent.workerToken,
      })
      if (out.error) return reply({ error: out.error, task_id: reg.taskId })
      return reply({ agent_id: agent.id, task_id: reg.taskId, configs: out.configs })
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
        // ⚠️ Never the bare `no pending permission <id>`: the commonest reason this misses is
        // that the park belongs to ANOTHER manager's server, and that caller can act on the
        // store route. See `park-refusal.mjs`.
        return reply({
          error: await foreignParkRefusal({ requestId: args.request_id, readLedger: readSessionLedger }),
        })
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

// Bind the A2A status receiver now, so its port is known before the first spawn rather than on
// that spawn's critical path.
//
// ⚠️ NOT AWAITED, NOT FATAL, AND REPORTED ONCE. The server is already connected and serving, and a
// port this process cannot take must not stop it — the channel is additive. Reporting once is
// enough because the registry CACHES the bind result: a later spawn reads the same cached failure
// rather than re-attempting the bind, so this line and the spawn's silent degradation cannot
// disagree, and neither turns into a per-spawn error.
void a2aPushRegistry.ensureListening().then((ready) => {
  if (ready.error) log(`a2a status receiver unavailable — workers spawn without a status channel: ${ready.error}`)
})

// Stamp the session this server runs inside, on the same interval as its workers. A server
// started outside a session has no id, so `startSelf` is a no-op there rather than a stamp
// under an invented key — see its comment.
heartbeat.startSelf(config.sessionId)

// The cluster half of the heartbeat store, on the same interval the headless workers use.
//
// A cluster worker has no process here and no registry entry, so the only way it becomes
// visible to the local readers is for this process to fold the cluster store into the local
// one. One timer for the whole server, not one per worker: the poll answers for every cluster
// session at once, and a per-worker timer would run the same query N times for one answer.
//
// ⚠️ **A failed poll is reported on TRANSITION, never per tick, and never as an error.**
// The cluster being unreachable is a state the readers already handle — the reachability
// marker goes stale, every cluster stamp reads `UNKNOWN`, and no resume is authorised — so
// logging it every 30s would bury the signal, and logging it as a failure would invite a
// reader to treat "could not tell" as "not live". That inversion is the one this whole store
// exists to prevent.
const clusterPollState = { ok: null, inFlight: false }
const clusterTimer = setInterval(async () => {
  // A hung reader must not stack polls. With the 20 s budget against a 30 s interval one poll
  // cannot outlive its own tick, but an injected `run` carries no such bound.
  if (clusterPollState.inFlight) return
  clusterPollState.inFlight = true
  try {
    const result = await pollCluster()
    if (!result.ok) {
      if (clusterPollState.ok !== false) {
        log(`cluster heartbeat: ${result.reason} — cluster stamps will read UNKNOWN until it clears`)
      }
    } else if (clusterPollState.ok === false) {
      log(`cluster heartbeat: cluster reachable again (${result.stamped.length} stamped)`)
    }
    clusterPollState.ok = result.ok
  } catch (error) {
    // An async interval callback that throws has no caller to reject to, so it must not throw.
    // ⚠️ Transition-gated exactly like the `ok: false` branch above, and for the reason the block
    // comment gives: a throw source that persists — ENOSPC on the marker write, say — would
    // otherwise log every 30 s and bury the signal this design exists to keep readable. Recording
    // `ok: false` is what makes the gate work: the next throw stays quiet, and a following success
    // logs "reachable again" through the branch above.
    if (clusterPollState.ok !== false) {
      log(`cluster heartbeat: poll failed: ${error?.message ?? error}`)
    }
    clusterPollState.ok = false
  } finally {
    clusterPollState.inFlight = false
  }
}, HEARTBEAT_INTERVAL_MS)
clusterTimer.unref?.()

// The A2A equivalent of the cluster mirror: list the controller's workers and stamp every ledger
// session it still runs, so an A2A worker reads live while it runs and dead within one sweep of
// being killed (SC7). See `pollA2A` in a2a-spawn.mjs for why there is no reachability marker.
//
// ⚠️ SILENT WHEN NO CONTROLLER IS CONFIGURED, and that is the common case rather than a fault: most
// servers never spawn an A2A worker, and logging a missing `SUPERVISOR_A2A_URL_<TARGET>` every
// 30 s would bury the signal. The ledger is read from disk each tick, never from the in-memory
// map, so a reconnected server keeps stamping workers a previous process spawned.
const a2aPollState = { ok: null, inFlight: false }
const a2aTimer = setInterval(async () => {
  if (a2aPollState.inFlight) return
  // ⚠️ ONE POLL PER CONFIGURED TARGET, NOT ONE POLL. The docker controller runs on this Mac and
  // the k8s controller in nuke dev, behind separate URLs, so a single list call can only ever see
  // half the fleet. That matters here and nowhere else on this path: `pollA2A` STAMPS what it
  // lists and never un-stamps what it does not, so a one-controller poll would leave the other
  // target's workers unstamped — reading as dead while they run.
  //
  // Fanning out is safe for exactly that reason: an unlisted session is left alone, never marked
  // stopped. A target whose controller is unreachable contributes `ok: false` rather than a false
  // "no workers", so the caller's existing warning still fires.
  const controllers = A2A_SPAWN_TARGETS.map((t) =>
    resolveA2AControllerTarget({
      target: t,
      envUrl: config.a2aUrls[t],
      envToken: config.a2aTokens[t],
      file: config.configFileContents,
      path: config.configFile,
    }),
  ).filter((c) => !c.error)
  // Silent when NO target is configured, and that is the common case rather than a fault: most
  // servers never spawn an A2A worker, and logging a missing `SUPERVISOR_A2A_URL_*` every 30 s
  // would bury the signal.
  if (controllers.length === 0) return
  // ⚠️ Read from the endpoint, which is where every reader looks now — and the filter
  // transfers UNCHANGED, because `readSessionLedger` returns the store's wire rows verbatim
  // and `mode`, `worker_id` and `ended_at` are spelled identically on both sides. The only
  // difference is where the array came from.
  // ⚠️ `null` (could not read it) becomes `[]` here, which is exactly what `listRecords` did
  // for a directory it could not read: the tick is skipped and the next one retries. That is
  // safe because the poll is idempotent — a skipped tick costs a cycle, never a stamp — and
  // the alternative, refusing the tick loudly, would log every 30 s through a store outage.
  const records = ((await readSessionLedger()) ?? []).filter(
    (r) => r?.mode === 'a2a' && r.worker_id && !r.ended_at,
  )
  if (records.length === 0) return
  a2aPollState.inFlight = true
  try {
    const results = []
    for (const controller of controllers) {
      results.push(
        await pollA2A({
          records,
          list: () =>
            listWorkersOnController({
              endpoint: controller.url,
              token: controller.token,
              messageId: randomUUID(),
              mint: () => randomUUID(),
            }),
          stamp: a2aStamp({ dir: heartbeatDir }),
        }),
      )
    }
    // ⚠️ `every`, not the last result: a target that went unreachable must still warn even when
    // the other answered. The warning names stamps going stale, and half the fleet going stale is
    // exactly that condition — reading only the final result would silence it whenever the k8s
    // controller happened to be the one that answered last.
    const ok = results.every((r) => r.ok)
    const stamped = results.flatMap((r) => r.stamped)
    const failed = results.find((r) => !r.ok)
    if (!ok && a2aPollState.ok !== false) {
      log(`a2a heartbeat: ${failed.reason} — A2A stamps will go stale until it clears`)
    } else if (ok && a2aPollState.ok === false) {
      log(`a2a heartbeat: controller reachable again (${stamped.length} stamped)`)
    }
    a2aPollState.ok = ok
  } catch (error) {
    if (a2aPollState.ok !== false) log(`a2a heartbeat: poll failed: ${error?.message ?? error}`)
    a2aPollState.ok = false
  } finally {
    a2aPollState.inFlight = false
  }
}, HEARTBEAT_INTERVAL_MS)
a2aTimer.unref?.()

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
