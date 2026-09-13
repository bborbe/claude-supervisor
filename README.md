# claude-supervisor

Supervise many Claude Code worker sessions from **one** manager session — spawn them, watch them, and **answer their permission prompts**.

The problem it solves: every agent session normally owns a private channel to you (its terminal tab), so you become the multiplexer, and every permission prompt demands a tab switch. Here, workers are sessions the supervisor owns, and their prompts are answered programmatically by the manager — no tab, no code, no `--dangerously-skip-permissions`.

## Install

```
/plugin marketplace add bborbe/claude-supervisor
/plugin install supervisor@claude-supervisor
```

The server runs on **bun** (installs its own node dependencies on first start) and executes on **node**. Both must be on `PATH` in the session that loads the plugin.

## What you get

| Piece | Job |
|---|---|
| MCP server `supervisor` | spawns and owns the workers; parks their permission prompts |
| skill `supervising-workers` | when to spawn a worker instead of doing the work yourself |
| `/supervisor:spawn` `/supervisor:workers` `/supervisor:answer` `/supervisor:drain` | the operator surface |
| agent `worker-wrangler` | runs the routine approval loop on a cheap model, escalating only real forks |

## Tools

| Tool | Purpose |
|---|---|
| `spawn_agent(prompt, cwd?, label?)` | start a worker — one `query()` session |
| `list_agents()` | every worker with status and pending-permission count |
| `agent_status(agent_id)` | one worker: status, last message, result |
| `pending_permissions()` | prompts awaiting an answer, across all workers |
| `await_permission(timeout_ms?)` | block until any worker asks — one call instead of polling |
| `answer_permission(request_id, behavior, message?)` | `allow` / `deny` — this unblocks the worker |

## How it works

Each worker is a `query()` session from `@anthropic-ai/claude-agent-sdk`, started with `permissionMode: 'default'`, so every approval-requiring tool lands in the server's `canUseTool` callback. The callback parks the request and returns a promise; the manager resolves it through `answer_permission`.

```
manager session ──MCP──► supervisor server ──query()×N──► workers
                              ▲                              │
                              └──── canUseTool ──────────────┘
```

**Escalation chain:** worker → policy → `worker-wrangler` → manager → human. Routine approvals should never reach the manager, and genuine forks should never reach the human.

## Layout

```
.claude-plugin/marketplace.json          marketplace manifest
.claude-plugin/plugin.json               plugin manifest
.mcp.json                                starts the server (${CLAUDE_PLUGIN_ROOT}/server)
skills/supervising-workers/SKILL.md
commands/{spawn,workers,answer,drain}.md
agents/worker-wrangler.md
server/supervisor.mjs                    the MCP server
server/policy.json                       approval policy (NOT wired yet — see Status)
```

## Status

Extracted from a working prototype proven end-to-end on 2026-09-13: one manager session spawned two workers concurrently, answered both `Write` prompts, and both workers completed (`permission_denials: 0`).

Known gaps, tracked rather than hidden:

- **`policy.json` is not consumed yet** — every prompt currently reaches the manager. The policy layer is the next increment.
- **`send_to_agent` is missing** — an SDK string-prompt session is single-shot; multi-turn needs streaming input (`AsyncIterable<SDKUserMessage>`).
- **Status lags after an allow** — `agent_status` can still read `running` for a few seconds; never treat one post-allow check as final.
- **Cost figures are meaningless off-Anthropic** — they are priced from Anthropic's table; ignore them when traffic is routed elsewhere.
- **Unanswered prompts auto-deny** after 15 minutes.
