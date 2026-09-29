# claude-supervisor — agent context

How to change this repo safely. The user-facing documentation is [README.md](README.md) — install, usage and configuration. This file is for agents working **in** the tree: the architecture map, the operational rules that are not a user's business, and the known gaps.

Audience split follows `coding/docs/readme-guide.md`: README answers *"can a new user get this running in 5 minutes?"*, this file answers *"how do I change this without breaking it?"*.

## Session tiers

Every command's prefix names the tier of the session that runs it: `fleet-*` for the fleet manager, `manager-*` for a goal/topic manager, `worker-*` for a worker; `jump`, `who-needs-me` and `read-guides` are cross-tier. A worker never opens or spawns sessions (`/supervisor:open` is manager-only) and routes anything out of scope to its manager. The full command → tier table, with "never run from" guidance, is [`docs/session-tiers.md`](docs/session-tiers.md) — the single source; run `/supervisor:read-guides` to load it.

## How it works

Each worker is a `query()` session from `@anthropic-ai/claude-agent-sdk`, started with `permissionMode: 'default'`, so every approval-requiring tool reaches the server: the `PermissionRequest` hook answers what the policy knows, and anything it does not cover falls through to `canUseTool`, which parks the request for the manager to resolve through `answer_permission`.

⚠️ `permissionMode: 'default'` is a **request, not a guarantee**. An escalating `permissions.defaultMode` from a trusted settings tier wins over it and bypasses the hook and `canUseTool` both, which makes the policy inert — see README § The approval policy.

⚠️ **The reverse direction has its own gate.** The manager's *outgoing* `answer_permission` call is itself subject to the **manager session's** permission mode. Under `auto` the classifier can refuse that call before the fleet is involved — the refusal reads like a worker-side denial but is not one (measured 2026-09-19, corrected here from an earlier reading that called it a structural gate). The fix is in the manager session: Shift+Tab → `accept edits`, then retry the same call.

```
manager session ──MCP──► supervisor server ──query()×N──► workers
                              ▲                              │
                              └──── canUseTool ──────────────┘
```

**Escalation chain:** worker → policy → `manager-wrangler` → manager → human. Routine approvals should never reach the manager, and genuine forks should never reach the human.

## Reaching a running tab worker

`send_agent_message(agent_id, message)` types a follow-up into a running tab worker and submits it. This is the `send_to_agent` the server never had: a worker that has gone wrong can be corrected, and one that has stalled can be nudged, without a human at the tab.

Three things about it are measured rather than assumed, and each one is a trap:

- **It only works on a tab worker.** A headless worker has no pane, so the call is refused rather than attempted — typing into a pane that does not exist is how a channel reports success while delivering nothing.
- **It activates the worker's tab, which steals focus.** Not silent by design; there is no way to type into a wezterm pane without making its tab active.
- **It waits for the input prompt, it does not sleep.** A fixed delay is a race that fails on a loaded machine. Readiness is the `❯` glyph in the pane, which appears about a second after spawn.

**The colour is set the same way, and that is a bug fix.** `SUPERVISOR_WORKER_COLOR` (default `/color pink`) used to be seeded as the first line of the worker's prompt, and it never worked: Claude Code parses one submitted message as one command, and `/color` takes the *entire* trimmed argument, so the colour swallowed the blank line and the whole task — `Invalid color "pink\n\n<task>"`. It is now sent as its own message once the pane is up, and the spawn response reports `color: {applied: true}` only when it actually landed.

⚠️ **`transcript_dir` on a tab worker is unreliable, and known to be.** It is derived from the `cwd` you passed, but the `cc-*` launcher does `cd` into its own vault — so a worker spawned with `cwd: "/tmp"` runs in `~/Documents/Obsidian/my-vault` and writes its transcript there, while the spawn response still says `/tmp`. Measured 2026-09-14.

**Resolved 2026-09-19 for reading a worker's state.** `agent_status` resolves a worker's transcript by **session id** — `<projectsDir>/*/<session-id>.jsonl` — and never from a cwd, so the derivation above is not on that path and is not trusted there. It still describes where the spawn response *claims* the transcript is, and is still wrong for a launcher-`cd` worker: nothing should read it as a location.

### Reading a worker

`agent_status(agent_id)` reports four fields that answer "what is it doing", none of which the write half could tell you:

| field | source | means |
|---|---|---|
| `last_message` | the worker's transcript JSONL | the last thing it said. Not "the last record" — most assistant records carry a `tool_use` block and no text (measured on a live session: 127 assistant records, 27 with text), so this is the last one carrying text. |
| `current_tool_call` | the worker's transcript JSONL | the call it is inside **right now** — `{name, input_summary, started_at, held_seconds}` — or `null` when nothing is in flight. This is the field that separates a worker executing a long tool call from one parked on a gate: `session_status` says both are not-idle, and this says *which* call and *for how long*. `input_summary` is capped at 200 chars and whitespace-flattened; the full input is `pending_permissions`' job for a parked call. `started_at`/`held_seconds` are `null` together when the record carries no timestamp — `name` is still reported, because "which tool" and "for how long" fail independently. |
| `session_status` | the session registry | the raw registry status: `idle`, `busy`, `waiting` or `shell`. |
| `awaiting_input` | derived from the status above | `true` only when the status is `waiting`. **`null`, not `false`, when the registry does not list the session** — unlisted is a different fact from not-waiting, and reporting it as `false` would be a guess wearing a measurement's clothes. |

⚠️ **`awaiting_input` means "blocked on input", not specifically "a permission gate is open".** It is the superset; whether a permission prompt specifically produces `waiting` is what the live A/B pins.

⚠️ **`current_tool_call` names the pending call; it does not say whether the worker is parked.** A pending `tool_use` with no matching `tool_result` reads identically whether the worker is executing a tool or parked on a prompt — measured 2026-09-19 across 25 live sessions, 3 `busy` sessions carried exactly that pending call, indistinguishable by transcript from the 2 `waiting` ones. The two fields are complements: the transcript answers *which call*, the registry answers *is it moving*.

**In flight means the LAST `tool_use` with no `tool_result` carrying its id — not "any unmatched `tool_use`".** An interrupted call leaves an unmatched `tool_use` behind and the conversation carries on, so a scan that reports any of them keeps naming a call the worker abandoned; measured 2026-09-20 over 1 667 live transcripts, 58 carried an unmatched `tool_use` and only 36 had it as their last one. The record's own `timestamp` is the start, so `held_seconds` is measured from the transcript and does not reset when a manager reconnects or polls again.

The read is bounded: a transcript is read from its **tail** (256 KB). The last message is at the end by definition, and `list_agents` renders every worker — reading each file whole would turn one status call into a read of the fleet's entire history. A window that finds nothing reports `null` rather than falling back to a full read.

**That bound costs the tool-call field a blind spot the message field does not have, and it is named rather than hidden.** A `tool_result` always follows its `tool_use`, so a call found inside the window cannot have its result outside it — but a call **older than the whole window** (256 KB of records written after it while it runs) is invisible, and reports as `null`, i.e. as "no call in flight". The message read falls back to a full read on a miss; this one deliberately does not, because a miss there is common (an idle worker has no call to find) and an unbounded read on the status path is the cost the window exists to avoid.

⚠️ **Two scope limits are unchanged by this read, and are named here rather than left to be discovered.** `agent_status` resolves by `agent_id` only — a label returns `unknown agent <label>` — and it reads an in-memory Map inside the caller's own server process, so it only knows workers **that session** spawned. Peers spawned elsewhere are invisible to it entirely, and an empty result is never evidence of absence. The new fields inherit both limits; they do not widen them.

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
  "mode": "interactive", "launcher": "…/cc-private-deepseek", "pane_id": "1714",
  "resumed_from": null, "policy": null, "parent_session": "0096a027-…",
  "spawned_at": "2026-09-14T06:10:31.153Z", "ended_at": null,
  "status": "running", "result": null
}
```

`parent_session` is the **spawn edge** — the manager session that called `spawn_agent`, resolved once by walking up from this server's own pid to the **nearest ancestor the live registry knows**. It is *not* the direct parent pid: `.mcp.json` starts this server through a `bun run` wrapper, so the direct parent is that wrapper and a bare `process.ppid` lookup named nothing — which is why this field was `null` in every record written before the walk existed. When no ancestor is registered — an exited manager, or a chain that never passed through a session — the field stays `null` rather than carrying a guess. Nothing else records it. A worker whose session id never resolved gets no record rather than one filed under a key nothing would look up, and the server logs that rather than staying quiet.

**Why the id may never resolve.** For a tab worker the id is found *by name* — `findRegisteredByName("⚙ " + label)`, an exact match, polled for up to 8s — so the name is the join back to the session. A name is not unique, and `find` returns the first holder, so a label reused while an earlier worker still answered to it resolved the new spawn to **that** worker and filed the old session's id as the new one's. The spawn therefore derives a free name before starting the process (`⚙ <label> (2)`, … until nothing holds it) and refuses poll matches against holders that existed before the spawn, so the join can only resolve to the session it created. A suffixed tab title is that guard working. See `docs/fleet-surface.md` § *The tab name is the join*.

⚠️ **This is not a liveness source.** An entry here must never be read as proof a session is alive — `liveness.mjs` owns that question, and it answers from the session registry plus the server's in-process record of workers it spawned. The ledger is deliberately the durable half.

`SUPERVISOR_LEDGER_DIR` overrides the location. It is deliberately *not* `SUPERVISOR_SESSIONS_DIR`, which already means the live registry — one variable meaning two stores is how a reader ends up pointing this one at Claude Code's directory.

⚠️ **A headless worker is invisible to `ListAgents` and `/fleet-status`.** A roster-only sweep will declare its task unowned and may spawn a duplicate onto it. Check `list_agents` before concluding a task has no owner.

⚠️ **`permissionMode: 'auto'` bypasses supervision entirely** — the hook never fires and `canUseTool` is never called. Measured: the same `kubectl create secret` that parked for approval under `'default'` ran with **zero** permission requests under `'auto'`. Leave it unset.

## Layout

```
.claude-plugin/marketplace.json          marketplace manifest
.claude-plugin/plugin.json               plugin manifest
.mcp.json                                starts the server (${CLAUDE_PLUGIN_ROOT}/server)
skills/supervising-workers/SKILL.md
skills/open-items/SKILL.md               the operator-asks ledger — rules + /supervisor:open-items (script stays at scripts/open-items.py)
commands/{manager-spawn,fleet-workers,manager-answer,manager-drain}.md
commands/{jump,who-needs-me}.md                  find and reach a session
commands/attention-next.md + scripts/attention-answer.py  answer an attention item, route it to the asker
commands/{fleet-loop,fleet-status,fleet-drive}.md  fleet surface — many sessions
commands/{manager-loop,manager-status}.md       one goal or topic
commands/manager-drive.md                        one goal or topic — the act leg, by hand
commands/fleet-verify.md                         fleet manager — verify the fleet layer's contract, suggest fixes
commands/manager-verify.md                       goal/topic manager — verify one subject, suggest fixes
commands/worker-drive.md                         one worker session — drive its anchored task to done
commands/worker-restart.md                       restart ONE named worker — kill, resume, re-orient in one verb
scripts/restart-precheck.py                      its read-only pre-kill probe: worktree, transcript, cause of death
scripts/restart-worker.py                        the kill+resume leg — seven refusals, never a broad `kill`
commands/open.md                                 resolve a name → jump / resume / spawn (manager-only)
commands/stop.md                                 stand that loop down
commands/reset.md + scripts/reset.py             re-discover its state; never deletes the ledger
docs/fleet-surface.md                            spawn shape + table render spec (canonical)
docs/session-tiers.md                            session tiers + command → tier table (canonical)
docs/subject-resolution.md                      the four-source subject chain the four manager commands share
scripts/manager-predispatch.py                   the pre-dispatch gate both manager commands run first (state: ~/.claude/state/manager-predispatch/)
commands/read-guides.md                          load every guide in docs/
scripts/{jump,who-needs-me}.py                   their helpers
agents/manager-wrangler.md                routine approval loop over headless workers
agents/manager-sweep-reader.md            the worker sweep's read-only half (called by both worker commands)
agents/fleet-sweep-reader.md             the fleet sweep's read half, Steps 0b–3 (called by /fleet-loop, read-only by /fleet-drive)
agents/manager-drive.md                    the worker sweep's act leg (composed by manager-loop, runnable by hand)
agents/manager-verify.md                  the seven-step verify fork + report shape (dispatched by /manager-verify)
agents/fleet-drive.md                    the fleet drive leg — revive/blocked split, re-nudge ledger (called by /fleet-drive and by /fleet-loop every round)
agents/gate-relay-read.md                the manager's gate relay, read leg — pane id in, ≤15-line per-pane summary out; declares no write tool at all
agents/gate-relay-send.md                the manager's gate relay, send leg — the operator's answer into a tab worker's pane; never drives a selection modal
server/supervisor.mjs                    the MCP server
server/policy.json                       bundled approval rules (see README § The approval policy)
```

## Status

Extracted from a working prototype proven end-to-end on 2026-09-13: one manager session spawned two workers concurrently, answered both `Write` prompts, and both workers completed (`permission_denials: 0`).

Known gaps, tracked rather than hidden:

- **A headless worker cannot be *steered* mid-task by the supervisor tool — but it is not unreachable.** `send_agent_message` reaches a *pane*, so it does not apply here, and an SDK string-prompt session is single-shot; multi-turn needs streaming input (`AsyncIterable<SDKUserMessage>`). ⚠️ **Read that as a limit on the *tool*, never on the worker:** ordinary cross-session `SendMessage` reaches a headless worker mid-task, in both directions (measured 2026-09-20), and a worker whose question is parked is answered over the permission channel. **A headless worker that has *exited*** — turn end, or a ~11 min question timeout — is **continued, not restarted**; see § Spawn a worker in [`docs/fleet-surface.md`](docs/fleet-surface.md) for the shape and its two traps. A tab worker can be steered with `send_agent_message` or stopped by closing its tab.
- **The policy layer is inert under `auto` / `bypassPermissions`** — both answer tool calls without consulting the `PermissionRequest` hook, so no rule can take effect. The server warns at startup and refuses a per-spawn policy under either. Measured 2026-09-15 on a machine whose `defaultMode` resolved to `auto` from `~/.claude/settings.json`. See README § The approval policy.
- **Status lags after an allow** — `agent_status` can still read `running` for a few seconds, because the server marks a worker running the moment it *answers* the prompt, not when the SDK confirms it resumed. That is the most the server can honestly know at that point, so the fix is to stop treating one post-allow check as a verdict: `answer_permission` already returns the outcome, and a worker that was about to finish will read `done` a moment later.
- **Unanswered prompts auto-deny** after 15 minutes — headless workers only; a tab worker's prompt waits for its tab.
