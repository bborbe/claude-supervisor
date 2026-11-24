---
description: ONE round of the TOPIC manager loop — read the goals this topic declares, drive each goal's rows through the goal-drive act leg, render the goal table and re-arm. The round the topic loop's wakeup runs, and runnable by hand. Drives goals, never workers.
allowed-tools:
  - Task
  - Read
  - Edit
  - Monitor
  - Skill
  - AskUserQuestion
  - Bash(grep:*)
  - Bash(ls:*)
  - Bash(cat:*)
  - Bash(awk:*)
  - Bash(mkdir:*)
  - Bash(vault-cli:*)
  - Bash(pgrep:*)
  - Bash(ps:*)
  - Bash(stat:*)
  - Bash(find:*)
  - Bash(date:*)
  - Bash(python3:*)
  - Bash(echo:*)
  - Bash(gh:*)
  - Bash(git tag:*)
  - Bash(wezterm cli list:*)
  - ListAgents
  - SendMessage
  - ScheduleWakeup
  - CronCreate
  - mcp__tts__say
  - mcp__supervisor__list_agents
  - mcp__supervisor__agent_status
  - mcp__supervisor__answer_permission
argument-hint: "[topic] [--interval <N>m] (topic detected when omitted)"
---
⚠️ **A voiced question is also a board card** — any `mcp__tts__say` containing `?` is paired with an `attention-ask.py post` within 60 s. [`docs/voice-asks.md`](../docs/voice-asks.md).

Topic-drive — **the topic tier's round**. The wakeup names THIS command; the arm
(`/supervisor:topic-loop`) is invoked **once** and is **never** re-invoked from a wakeup.

⚠️ **A topic-tier verb — never run it from a worker session.** A worker carries a task; a
manager carries a subject and a loop. A worker that needs a subject driven routes it to its
manager with `SendMessage` and says so.

⚠️ **The rows are GOALS.** The topic tier may open a worker, but it does so on demand
with `/supervisor:open --task`, never by sweeping one here (`docs/session-tiers.md` § The
three tiers). A worker row is reported upward to this topic manager.

## Router — read a step file only when its When holds

| # | Step | When | File |
|---|---|---|---|
| 0a | Write the liveness record | every round, before the pre-dispatch gate | `${CLAUDE_PLUGIN_ROOT}/commands/steps/round-liveness.md` |
| 1 | Resolve the topic and its declared goals | manager start; the declaration read re-runs every tick | `${CLAUDE_PLUGIN_ROOT}/commands/steps/topic-resolve.md` |
| 2 | Take the round's readings | every tick, **before** the sweep dispatch | `${CLAUDE_PLUGIN_ROOT}/commands/steps/round-readings.md` |
| 2b | Quiet check | every tick, after the readings | `${CLAUDE_PLUGIN_ROOT}/commands/steps/round-quiet.md` |
| 3 | Sweep — the goal rows and their manager state | every tick | `${CLAUDE_PLUGIN_ROOT}/commands/steps/topic-sweep.md` |
| 4c | Closure — close what the evidence already finished | every tick, after the sweep | `${CLAUDE_PLUGIN_ROOT}/commands/steps/goal-closure.md` |
| 5 | Drive — dispatch the act leg for every declared goal | every tick, after the sweep | `${CLAUDE_PLUGIN_ROOT}/commands/steps/topic-drive-act.md` |
| 6 | Start — retired with the goal tier | never | *inline — § Start below* |
| 12 | Tail — re-arm the `Monitor` channels | end of every tick | `${CLAUDE_PLUGIN_ROOT}/commands/steps/round-tail.md` |
| 13 | Self-improvement | before the session ends, or a sweep exposes a gap | `${CLAUDE_PLUGIN_ROOT}/commands/steps/round-self-improvement.md` |

⚠️ **The rules do not live here, and that is deliberate.** Every always-hold invariant and
gate is **inline in the arm** (`commands/topic-loop.md`); every per-round rule lives in
**exactly one** step file above. Restating a rule here would give it two homes, and a rule
with two homes drifts. If you need a rule that is not in a step file you have read, read the
arm or the step file — do not act from memory of a previous round.

## Arguments

- **`$1` (optional — resolved from the session when omitted):** the **topic name**. A goal
  name is the wrong tier — print `❌ "<name>" is a goal, not a topic — use
  /supervisor:goal-drive.` and stop.
- **`--interval <N>m`** is the **arm's**, not this command's: accepted so the wakeup's
  prompt parses. Step 12 re-arms only the `Monitor` channels (no `noop` correction).

## Drive — the act leg, dispatched from step 5

⚠️ **Row 0a — this topic manager's own liveness record — runs here each round, before the drive.** It records the **topic's** cadence, not a goal's; a loop-sourced subject would arm a goal under the topic branch. Rule: `${CLAUDE_PLUGIN_ROOT}/commands/steps/round-liveness.md` — `--branch topic`.

⚠️ **The per-goal keep-alive is retired (2026-11-24).** There is no goal-manager session to
keep alive or resume. The round's act is the **goal-drive act leg**, dispatched per declared
goal from `commands/steps/topic-drive-act.md` (router row 5). `topic-keepalive.py --branch
goal` answers `goal-branch-retired`; never call it, and never record a goal manager's verdict.

⚠️ **Drop every goal whose page reads a terminal `status` before driving it** — the goal-drive
act leg reaps a finished worker; it never re-opens a closed goal.

## Start — retired with the goal-manager tier

There is no goal-manager session to start (merged into the topic tier 2026-11-24). The topic round drives each declared goal's rows directly — the goal-drive act leg — instead of starting a goal manager and handing off. A goal that no manager owns is a **row**, not a start; `commands/topic-status.md` renders it `—`.

⚠️ **`topic-spawn.py --rank` and `/supervisor:topic-spawn "<goal>"` are gone.** The script is now the topic ceiling check only (`--topic-open`, `commands/topic-spawn.md`), and there is no goal to start. The per-topic N ranking and the five goal refusals retired with the tier.

## What this round must never do

- **Never manage a worker** — no `goal-spawn`, no `SendMessage` to a worker, no nudging a
  task. A worker row is reported to this topic manager; the act leg drives it.
- **Never classify a task.** This tier's rows are goals and their managers; the task buckets
  are the goal round's, in `agents/goal-sweep-reader.md`.
- **Never decide an operator gate.** You surface it; the operator answers it.
- **Close on complete evidence; tick only with an inline citation.** This tier closes only
  the topic page itself (**SC + `# Completion Gate`** — the topic's own contract, and the
  reason the reader takes `--kind topic`) — never a task, which belongs to its goal's manager
  and which this tier never reads — through `${CLAUDE_PLUGIN_ROOT}/commands/steps/goal-closure.md`,
  which is the rule's only execution home and carries the verbs, the `--vault` contract and
  the terminal guard. ⚠️ **Tick only on a resolved citation (Step 0); never set `status` to close**
  — closure follows the evidence, and a close that would need
  `--force` is one you do not make. A member goal, closable or not, is routed to its own
  manager (it runs Step 0b), never closed from here.
- **Never claim an act the script reported it could not perform.** If a tool failed to bind,
  carry that through rather than smoothing it.
