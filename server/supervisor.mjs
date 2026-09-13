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
import { query } from '@anthropic-ai/claude-agent-sdk'
import { appendFileSync, mkdirSync, readFileSync } from 'fs'
import { homedir } from 'os'
import { join } from 'path'
import { realpathSync } from 'fs'
import { spawnSync } from 'child_process'
import { decide as decideWith, inputKey } from './policy.mjs'

const PERMISSION_TIMEOUT_MS = 15 * 60 * 1000

const agents = new Map() // id -> agent record
const pending = new Map() // requestId -> permission record
const waiters = new Set() // resolvers waiting for the next permission
let seq = 0

// stderr is the transport-safe channel (stdout carries MCP frames). A file log is
// opt-in via SUPERVISOR_LOG so nothing depends on a machine-local path by default.
const LOG_FILE = process.env.SUPERVISOR_LOG

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

const CONFIG_DIR = join(
  process.env.XDG_CONFIG_HOME || join(homedir(), '.config'),
  'claude-supervisor',
)
const STATE_DIR = join(
  process.env.XDG_STATE_HOME || join(homedir(), '.local', 'state'),
  'claude-supervisor',
)

const USER_POLICY = process.env.SUPERVISOR_POLICY || join(CONFIG_DIR, 'policy.json')
const BUNDLED_POLICY = new URL('./policy.json', import.meta.url).pathname
const PERMISSION_LOG =
  process.env.SUPERVISOR_PERMISSION_LOG === 'off'
    ? null
    : process.env.SUPERVISOR_PERMISSION_LOG || join(STATE_DIR, 'permissions.jsonl')

const PERMISSION_MODES = ['default', 'acceptEdits', 'bypassPermissions', 'plan', 'dontAsk', 'auto']

// Validated once, at the boundary. An unvalidated mode would silently fall through
// to the SDK's default and make a typo indistinguishable from a decision.
const PERMISSION_MODE = (() => {
  const raw = process.env.SUPERVISOR_PERMISSION_MODE
  if (!raw) return 'default'
  if (!PERMISSION_MODES.includes(raw)) {
    log(`WARNING: SUPERVISOR_PERMISSION_MODE="${raw}" is not a known mode — falling back to "default". Known: ${PERMISSION_MODES.join(', ')}`)
    return 'default'
  }
  if (raw !== 'default') log(`WARNING: permissionMode "${raw}" — see README; 'auto' bypasses the hook and canUseTool entirely, leaving workers unsupervised.`)
  return raw
})()

// Distinguish "no such file" (normal — the user file is optional) from "file exists
// but is unreadable or malformed" (never normal — it means real rules are being
// silently ignored). Collapsing the two would let a corrupted policy look empty.
function readRules(path) {
  let raw
  try {
    raw = readFileSync(path, 'utf8')
  } catch (error) {
    if (error.code !== 'ENOENT') log(`ERROR: cannot read policy ${path}: ${error.message} — its rules are NOT in effect`)
    return []
  }
  try {
    const parsed = JSON.parse(raw)
    const rules = Array.isArray(parsed.rules) ? parsed.rules : []
    for (const rule of rules) {
      if (!rule?.action || !['allow', 'deny', 'escalate'].includes(rule.action)) {
        log(`ERROR: policy ${path} has a rule with an unknown action ${JSON.stringify(rule?.action)} — it will never match`)
      }
    }
    return rules
  } catch (error) {
    log(`ERROR: policy ${path} is not valid JSON: ${error.message} — its rules are NOT in effect`)
    return []
  }
}

// Overlay, not replace: your rules are evaluated first, and the bundled set fills
// in whatever you have not decided. Growing the list means appending, never copying.
const policy = { rules: [...readRules(USER_POLICY), ...readRules(BUNDLED_POLICY)] }

// Evaluation is pure and lives in policy.mjs; this binds it to the loaded rule set.
const decide = (toolName, input, cwd) => decideWith(policy.rules, toolName, input, cwd)

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
    const { action, key, rule } = decide(toolName, hookInput?.tool_input, agent.cwd)
    const base = {
      ts: new Date().toISOString(),
      agent: agent.id,
      label: agent.label,
      tool: toolName,
      key,
      matched_rule: rule ? `${rule.tool}:${rule.match}` : null,
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
  return join(
    process.env.CLAUDE_CONFIG_DIR || join(homedir(), '.claude'),
    'projects',
    resolved.replace(/\//g, '-'),
  )
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
  if (process.env.SUPERVISOR_CLAUDE_CMD) return process.env.SUPERVISOR_CLAUDE_CMD
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
  const explicit = process.env.SUPERVISOR_MCP_CONFIG
  let path = explicit
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

function spawnInteractiveAgent({ id, prompt, cwd, label }) {
  const claudeCmd = resolveClaudeCmd(cwd)
  // Prefix the tab TITLE so a supervised worker is identifiable at a glance in the
  // tab bar and the fleet roster — the two places a normal session is otherwise
  // indistinguishable. The agent's own `label` stays unprefixed, so list_agents
  // joins to ListAgents by stripping the marker.
  // A colour command runs before the task so managed workers are visually distinct
  // in their own tab. Configured, not hardcoded, because whether "/color" exists as
  // a built-in is unverified; set SUPERVISOR_WORKER_COLOR=off to drop it.
  const colorCmd = process.env.SUPERVISOR_WORKER_COLOR ?? '/color pink'
  const seeded = colorCmd && colorCmd !== 'off' ? `${colorCmd}\n\n${prompt}` : prompt
  const inner = `cd ${shellQuote(cwd)} && exec ${claudeCmd} -n ${shellQuote(`⚙ ${label}`)} ${shellQuote(seeded)}`
  const res = spawnSync('wezterm', ['cli', 'spawn', '--', 'bash', '-lc', inner], { encoding: 'utf8' })

  if (res.error || res.status !== 0) {
    const why = res.error?.message || res.stderr?.trim() || `exit ${res.status}`
    return { error: `could not open a tab: ${why}` }
  }
  const paneId = (res.stdout || '').trim()
  log(`interactive worker ${id} opened in a tab (pane ${paneId || 'unknown'})`)
  return { paneId: paneId || null }
}

// Interactive is the DEFAULT. A worker must come up with the same tooling a normal
// session has — the launcher's env, plugin skills, MCP servers, settings.json
// permissions. A worker missing its normal tooling is not a cheaper worker, it is
// one that fails in unfamiliar ways and cannot be watched. Opt OUT with
// interactive:false for the headless path, where the manager answers instead.
// `resume` opens a NEW session continuing a CLOSED one's conversation. The session
// must be closed: resuming a live one puts two writers on one conversation, so a
// live id is refused below rather than silently producing that.
function spawnAgent({ prompt, cwd, label, interactive = true, resume }) {
  const id = `agent_${++seq}`
  const agent = {
    id,
    label: label || id,
    prompt,
    cwd: cwd || process.cwd(),
    status: 'running',
    sessionId: null,
    permissions: [],
    transcript: [],
    error: null,
    createdAt: new Date().toISOString(),
  }
  agents.set(id, agent)

  if (interactive) {
    agent.status = 'interactive'
    const res = spawnInteractiveAgent({ id, prompt, cwd: agent.cwd, label: agent.label })
    if (res.error) {
      agents.delete(id)
      return { error: res.error }
    }
    agent.paneId = res.paneId
    agent.transcriptDir = transcriptDirFor(agent.cwd)
    return {
      agent_id: id,
      label: agent.label,
      cwd: agent.cwd,
      status: agent.status,
      interactive: true,
      pane_id: agent.paneId,
      transcript_dir: agent.transcriptDir,
    }
  }

  const q = query({
    prompt,
    options: {
      cwd: agent.cwd,
      // Load the same settings an interactive session gets. Without this the SDK
      // starts from nothing — no plugin skills, no settings.json permissions, no
      // user MCP servers — and a worker missing its normal tooling is not a cheaper
      // worker, it is one that fails in unfamiliar ways.
      settingSources: ['user', 'project', 'local'],
      // Same reason as settingSources: parity with a tab worker. Spawning is rare, so
      // the per-spawn launcher lookup is not a hot path.
      mcpServers: resolveMcpServers(resolveClaudeCmd(agent.cwd)),
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

  ;(async () => {
    try {
      for await (const message of q) {
        agent.transcript.push(message)
        if (message.type === 'system' && message.subtype === 'init') agent.sessionId = message.session_id
        if (message.type === 'result') {
          agent.result = {
            subtype: message.subtype,
            is_error: message.is_error ?? false,
            result: typeof message.result === 'string' ? message.result.slice(0, 4000) : undefined,
            num_turns: message.num_turns,
            total_cost_usd: message.total_cost_usd,
            permission_denials: message.permission_denials?.length ?? 0,
          }
          agent.status = message.is_error ? 'error' : 'done'
        }
      }
      if (agent.status === 'running') agent.status = 'done'
    } catch (error) {
      agent.status = 'error'
      agent.error = String(error)
      log(`agent ${id} threw: ${error}`)
    }
  })()

  return { agent_id: id, label: agent.label, cwd: agent.cwd, status: agent.status }
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

const agentView = (a) => ({
  agent_id: a.id,
  label: a.label,
  status: a.status,
  cwd: a.cwd,
  session_id: a.sessionId,
  created_at: a.createdAt,
  pending_permissions: a.permissions.filter((id) => pending.has(id)),
  last_message: agentView.lastText?.(a) ?? lastAssistantText(a),
  result: a.result ?? null,
  error: a.error,
})

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
        interactive: {
          type: 'boolean',
          description:
            'Default true: open the worker as a real session in a wezterm tab, so it has the same tooling a normal session has — launcher env, plugin skills, MCP servers, settings.json permissions — and can be watched and driven by hand. Its approval prompts are answered IN THAT TAB, so it will never appear in pending_permissions. Pass false for a headless worker that the manager supervises instead, accepting the narrower toolchain.',
        },
        resume: {
          type: 'string',
          description:
            'Session id to continue. The session MUST be closed — resuming a live one puts two writers on one conversation. The resumed worker is created here, so unlike the original session it IS supervised and its prompts park for the manager.',
        },
      },
      required: ['prompt'],
    },
  },
  {
    name: 'list_agents',
    description: 'All spawned sessions with status and how many permissions are pending.',
    inputSchema: { type: 'object', properties: {} },
  },
  {
    name: 'agent_status',
    description: 'One session: status, last assistant message, result, pending permission ids.',
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
        spawnAgent({
          prompt: args.prompt,
          cwd: args.cwd,
          label: args.label,
          interactive: args.interactive !== false,
          resume: args.resume,
        }),
      )

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
