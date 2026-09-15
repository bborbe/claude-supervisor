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

## Two spawn modes

`spawn_agent` opens a worker one of two ways. **They have the same tooling** — same plugin skills, same MCP servers, same launcher-resolved environment (measured: 12 MCP prefixes in both, verified 2026-09-14). The only difference is who answers the worker's permission prompts, and whether you can watch it.

| | `interactive: true` — **default** | `interactive: false` |
|---|---|---|
| What runs | a real `claude` session in a wezterm tab | a `query()` session inside this server |
| Launcher | the `cc-*` script, resolved from `vault-cli config` | same env, via `settingSources` + the launcher's `--mcp-config` |
| Its prompts go to | **its own tab** | **the manager**, via `await_permission` |
| Visible | yes — tab, scrollback, drivable by hand | no tab; tail its transcript instead |
| In `ListAgents` | yes, prefixed `⚙` (it is a real session) | **no** — it has no socket |
| Manager can steer it | yes — `send_agent_message`, or close the tab | **no** — no `interrupt`, and no channel into an SDK session |

**Choose interactive when you want to see or steer it. Choose headless when you want the manager answering its prompts.** Neither is more capable; the trade is visibility against supervision.

## Reaching a running tab worker

`send_agent_message(agent_id, message)` types a follow-up into a running tab worker and submits it. This is the `send_to_agent` the server never had: a worker that has gone wrong can be corrected, and one that has stalled can be nudged, without a human at the tab.

Three things about it are measured rather than assumed, and each one is a trap:

- **It only works on a tab worker.** A headless worker has no pane, so the call is refused rather than attempted — typing into a pane that does not exist is how a channel reports success while delivering nothing.
- **It activates the worker's tab, which steals focus.** Not silent by design; there is no way to type into a wezterm pane without making its tab active.
- **It waits for the input prompt, it does not sleep.** A fixed delay is a race that fails on a loaded machine. Readiness is the `❯` glyph in the pane, which appears about a second after spawn.

**The colour is set the same way, and that is a bug fix.** `SUPERVISOR_WORKER_COLOR` (default `/color pink`) used to be seeded as the first line of the worker's prompt, and it never worked: Claude Code parses one submitted message as one command, and `/color` takes the *entire* trimmed argument, so the colour swallowed the blank line and the whole task — `Invalid color "pink\n\n<task>"`. It is now sent as its own message once the pane is up, and the spawn response reports `color: {applied: true}` only when it actually landed.

⚠️ **`transcript_dir` on a tab worker is unreliable, and known to be.** It is derived from the `cwd` you passed, but the `cc-*` launcher does `cd` into its own vault — so a worker spawned with `cwd: "/tmp"` runs in `~/Documents/Obsidian/Personal` and writes its transcript there, while the spawn response still says `/tmp`. Measured 2026-09-14. Tail the directory the launcher's vault implies, or read the pane, until this is resolved.

## The spawn ledger

Every worker this server spawns gets a record at `~/.local/state/claude-supervisor/sessions/<uuid>.json`, keyed by its session uuid and written at spawn.

Two stores already exist and neither answers the question this one does:

| store | keyed by | lifetime | says |
|---|---|---|---|
| `~/.claude/sessions/<pid>.json` | pid | **deleted when the session exits** | who is running *right now* |
| `~/.claude/projects/<cwd>/<uuid>.jsonl` | uuid | permanent | the conversation |
| **the ledger** | uuid | permanent | **who started it, in what mode, from which manager, and how it ended** |

Measured 2026-09-14: the live registry held 13 entries against 13 live processes with **zero stale** — it tracks liveness, not history, so it forgets a session exactly when a record would first be useful.

```json
{
  "session_id": "b26cb46e-…", "agent_id": "agent_1", "label": "ledger-drill-tab",
  "mode": "interactive", "launcher": "…/cc-personal-deepseek", "pane_id": "1714",
  "resumed_from": null, "parent_session": "0096a027-…",
  "spawned_at": "2026-09-14T06:10:31.153Z", "ended_at": null,
  "status": "running", "result": null
}
```

`parent_session` is the **spawn edge** — the manager session that called `spawn_agent`, resolved once from this server's own parent pid. Nothing else records it. A worker whose session id never resolved gets no record rather than one filed under a key nothing would look up, and the server logs that rather than staying quiet.

⚠️ **This is not a liveness source.** An entry here must never be read as proof a session is alive — `liveness.mjs` owns that question, and it answers from the live registry plus `pgrep`. The ledger is deliberately the durable half.

`SUPERVISOR_LEDGER_DIR` overrides the location. It is deliberately *not* `SUPERVISOR_SESSIONS_DIR`, which already means the live registry — one variable meaning two stores is how a reader ends up pointing this one at Claude Code's directory.

⚠️ **A headless worker is invisible to `ListAgents` and `/fleet-status`.** A roster-only sweep will declare its task unowned and may spawn a duplicate onto it. Check `list_agents` before concluding a task has no owner.

⚠️ **`permissionMode: 'auto'` bypasses supervision entirely** — the hook never fires and `canUseTool` is never called. Measured: the same `kubectl create secret` that parked for approval under `'default'` ran with **zero** permission requests under `'auto'`. Leave it unset.

## Adopting an existing session

The supervisor can only supervise sessions **it created** — `canUseTool` is attached when a session starts and cannot be grafted onto a running process. That makes its control forward-looking by default: sessions started elsewhere are observable and messageable, but their prompts stay theirs.

`resume` inverts that. Pass a session id and the supervisor opens a **new** session continuing that conversation — and because *this* call creates it, the permission callback applies.

```
spawn_agent({ prompt, resume: "<session-id>", interactive: false })
  → continues the old conversation
  → its prompts park for the manager
```

**Verified 2026-09-14:** a session killed hours earlier was resumed, recalled its own prior task unprompted, parked a `Write` prompt for the manager, and completed on approval. Same session id — resume continues rather than forks.

**Why this matters:** it reaches what the permission *channel* exists for, with no worker-side plugin, no `--channels` flag and no marketplace dependency. The channel was ruled out as a Non-goal precisely to avoid those.

⚠️ **The session must be closed, and a live one is refused.** Two writers on one conversation corrupt it, so `spawn_agent` probes before resuming and returns an error rather than opening the session: a session found running is refused, and so is one whose liveness cannot be determined. Two probes, because neither is enough alone — the session registry at `~/.claude/sessions/<pid>.json`, which is the only one that finds a session started *fresh* (its id is in no process's command line, so `pgrep` has nothing to match), plus `pgrep -fl`, which catches a process the registry does not list. `SUPERVISOR_SESSIONS_DIR` overrides the registry location.

**Which conversation am I in?** `agent_status` reports `resumed_from` and `continued` — the latter `true` when the session id came back the same (continued) and `false` when it did not (forked), so an adoption is never mistaken for a fresh start.

⚠️ **Unmeasured:** `forkSession` and `resumeSessionAt`. Supported by the SDK; their interaction with `resume` has not been tested, and no run has yet produced a `continued: false`.

## Tools

| Tool | Purpose |
|---|---|
| `spawn_agent(prompt, cwd?, label?, interactive?, resume?)` | start a worker — a real session in a tab by default, or headless with `interactive: false` |
| `send_agent_message(agent_id, message)` | type a follow-up into a running **tab** worker and submit it |
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

- **A headless worker cannot be corrected or stopped once running** — only waited out. `send_agent_message` reaches a *pane*, so it does not apply here, and an SDK string-prompt session is single-shot; multi-turn needs streaming input (`AsyncIterable<SDKUserMessage>`). A tab worker can be steered with `send_agent_message` or stopped by closing its tab.
- **Status lags after an allow** — `agent_status` can still read `running` for a few seconds; never treat one post-allow check as final.
- **Cost figures are meaningless off-Anthropic** — they are priced from Anthropic's table; ignore them when traffic is routed elsewhere.
- **Unanswered prompts auto-deny** after 15 minutes — headless workers only; a tab worker's prompt waits for its tab.
