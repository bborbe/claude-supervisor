---
name: supervising-workers
description: Decide when work belongs in a supervisor worker rather than in this session, choose the spawn mode that matches who should answer its permission prompts, and drive that worker to a terminal status. Use when the supervisor MCP server is loaded in this session (mcp__supervisor__* tools are available) and work could run unattended.
---

If the `mcp__supervisor__*` tools are not in this session, ignore this skill — you are not a manager session. The word "worker" is overloaded (dark-factory workers, Task sub-agents), so activate only on the MCP tools being present.

Work that can run unattended belongs in a worker, not in this session.

## Prerequisites

- The supervisor MCP server is loaded and its tools are visible.
- The worker gets its own `cwd` and cannot see this conversation — the brief must be self-contained.
- A task anchors the work. Anchor it before spawning, or the worker becomes untracked work.

## When to spawn

Spawn when the task is bounded, verifiable, and does not need this conversation's context: a fix in one repo, a file set to produce, a check to run. Do it yourself when the work is a decision, a quick read, or needs what was just discussed.

## Choose the mode before you spawn

`spawn_agent` opens a worker one of two ways, and **the default is the tab**. The tooling is identical in both — same plugin skills, same MCP servers, same launcher-resolved environment. The only difference is who answers the worker's permission prompts, and whether anyone can watch it.

| | `interactive: true` — **default** | `interactive: false` |
|---|---|---|
| What runs | a real `claude` session in a wezterm tab | a `query()` session inside the server |
| Its prompts go to | **its own tab** | **the manager**, via `await_permission` |
| Watchable | yes — tab and scrollback | no tab; tail its transcript |
| `list_agents` shows it as | `interactive` | `running` (then `done` / `error`) |
| The session roster shows it | yes — it is a real session, tab named `⚙ <label>` | **no** — it has no socket |
| Steerable mid-run | yes — `send_agent_message`, or close the tab | **no** — nothing reaches a running SDK session |

Pass `interactive: false` when the point is that **you** answer the prompts — that is the only mode this skill's approval loop applies to. Leave the default when a human wants to watch the worker, or correct it mid-run.

⚠️ **The default mode makes `await_permission` blind to that worker.** A tab worker answers its own prompts in its tab, so no request is ever parked and the call simply times out — it still serves any *headless* worker running alongside. If you intend to supervise, say `interactive: false` explicitly — never spawn the default and then wait on it.

## The operator surface

**Headless worker** (`interactive: false`) — the manager answers its prompts:

`spawn_agent` → `await_permission` → `answer_permission` → `list_agents`

⚠️ The `answer_permission` step is gated by **the manager's own session mode, not the worker's**. Under `auto` the classifier can refuse the outgoing call before the fleet is involved (measured 2026-09-19) — fix it with **Shift+Tab → `accept edits`** in the manager session, then retry. Never by changing the worker's mode or `defaultMode`.

**Tab worker** (default) — its own tab answers them:

`spawn_agent` → `agent_status` / `send_agent_message` → read the tab

The slash commands wrap the same calls: `/supervisor:manager-spawn`, `/supervisor:fleet-workers`, `/supervisor:manager-answer`, and `/supervisor:manager-drain` for bulk approvals. The approval *policy* lives in the `manager-wrangler` agent — do not restate it here.

`spawn_agent` takes `prompt` (required) plus optional `cwd`, `label`, `interactive` and `resume`. `await_permission(timeout_ms)` blocks until a **headless** worker asks; it returns `null` on timeout, so treat a null as "nothing to do", never as an approval.

## After a worker stops

Read its result and report a one-line outcome to the human. If it failed or was denied, say why before spawning a replacement. Immediately after an allow, status can still read `running` for a few seconds — never treat one check as final.

## Escalation

Routine approvals are yours. Escalate to the human when the request is outside the task's stated scope, touches production or credentials, or repeats an approval already denied once.

## Success Criteria

- Every worker reached a terminal status, or is reported with the reason it did not.
- Nothing is left pending at the end except requests deliberately escalated.
- Every denial carried a reason the worker could act on.

## Honest limits

- Cost figures the server reports are priced from Anthropic's table and mean nothing when traffic is routed elsewhere — never quote them.
- An unanswered permission auto-denies after 15 minutes — **headless workers only**. A tab worker's prompt waits for its tab indefinitely.
- A headless worker is single-shot: one prompt, one conversation. A tab worker takes follow-ups through `send_agent_message` (typed into its pane, stealing focus), and so does a **cluster** worker — there the same tool posts one more turn on the session id that worker already holds and blocks until the pod answers.
- A headless worker is invisible to the **session roster** (`/fleet-status`, the `●` set) — it has no socket — so a roster-only sweep reads its task as unowned and may spawn a duplicate onto it. `list_agents` does show it; check there before concluding a task has no owner.
