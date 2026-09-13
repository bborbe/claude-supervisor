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
import { appendFileSync } from 'fs'

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

function spawnAgent({ prompt, cwd, label }) {
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

  const q = query({
    prompt,
    options: { cwd: agent.cwd, permissionMode: 'default', canUseTool: makeCanUseTool(agent) },
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

const server = new Server({ name: 'supervisor', version: '0.1.0' }, { capabilities: { tools: {} } })

server.setRequestHandler(ListToolsRequestSchema, async () => ({ tools: TOOLS }))

server.setRequestHandler(CallToolRequestSchema, async (request) => {
  const args = request.params.arguments ?? {}
  const reply = (payload) => ({ content: [{ type: 'text', text: JSON.stringify(payload, null, 2) }] })

  switch (request.params.name) {
    case 'spawn_agent':
      if (typeof args.prompt !== 'string' || !args.prompt.trim()) {
        return reply({ error: 'prompt is required' })
      }
      return reply(spawnAgent({ prompt: args.prompt, cwd: args.cwd, label: args.label }))

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
      if (args.behavior === 'allow') {
        record.settle({ behavior: 'allow' })
        log(`permission ${args.request_id} ALLOWED by manager`)
        return reply({ answered: args.request_id, behavior: 'allow', agent_id: record.agentId })
      }
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
