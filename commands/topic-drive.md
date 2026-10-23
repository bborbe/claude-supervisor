---
description: ONE round of the TOPIC manager loop — read the goals this topic declares and the goal managers on them, keep the live ones alive, start what the ranking admits, render the goal table and re-arm. The round the topic loop's wakeup runs, and runnable by hand. Never manages a worker.
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
argument-hint: "[topic] [--interval <N>m] (topic detected when omitted)"
---
⚠️ **A voiced question is also a board card** — any `mcp__tts__say` containing `?` is paired with an `attention-ask.py post` within 60 s. [`docs/voice-asks.md`](../docs/voice-asks.md).

Topic-drive — **the topic tier's round**. The wakeup names THIS command; the arm
(`/supervisor:topic-loop`) is invoked **once** and is **never** re-invoked from a wakeup.

⚠️ **A topic-tier verb — never run it from a worker session.** A worker carries a task; a
manager carries a subject and a loop. A worker that needs a subject driven routes it to its
manager with `SendMessage` and says so.

⚠️ **The rows are GOAL MANAGERS.** The topic tier may open a worker, but it does so on demand
with `/supervisor:open --task`, never by sweeping one here (`docs/session-tiers.md` § The four
tiers). A worker row is reported downward.

## Router — read a step file only when its When holds

| # | Step | When | File |
|---|---|---|---|
| 1 | Resolve the topic and its declared goals | manager start; the declaration read re-runs every tick | `${CLAUDE_PLUGIN_ROOT}/commands/steps/topic-resolve.md` |
| 2 | Take the round's readings | every tick, **before** the sweep dispatch | `${CLAUDE_PLUGIN_ROOT}/commands/steps/round-readings.md` |
| 2b | Quiet check | every tick, after the readings | `${CLAUDE_PLUGIN_ROOT}/commands/steps/round-quiet.md` |
| 3 | Sweep — the goal rows and their manager state | every tick | `${CLAUDE_PLUGIN_ROOT}/commands/steps/topic-sweep.md` |
| 5 | Keep alive — resume, restart or leave, per goal | every tick, after the sweep | *inline — § Keep alive below* |
| 6 | Start — the ranking, then `topic-spawn` | the keep-alive left a slot free and a startable goal is held | *inline — § Start below* |
| 12 | Tail — `noop` and re-arm | end of every tick | `${CLAUDE_PLUGIN_ROOT}/commands/steps/round-tail.md` |
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
- **`--interval <N>m`** is the **arm's**, not this command's: it is accepted solely so the
  wakeup's prompt parses. Step 12 still makes one `ScheduleWakeup` call to correct `noop`.

## Keep alive — inline, because it runs every tick

For **each goal this topic declares**, one call decides:

```bash
python3 ${CLAUDE_PLUGIN_ROOT:-$HOME/.claude/plugins/marketplaces/claude-supervisor}/scripts/topic-keepalive.py \
  --vault "<vault-root>" --goal "<goal>"
```

The **first line is the verdict**:

| verdict | meaning | what this round does |
|---|---|---|
| `jump` | live session, loop `OK` **or `UNARMED`** | render the row. ⚠️ `UNARMED` means no record exists under either name — the loop was **never armed**, not lost; the lever is `/supervisor:goal-loop "<goal>"`, never `restart` (a restart resumes a session that never armed anything) |
| `resume` | no session (or `ABSENT`) | run `/supervisor:open --goal "<goal>"`, then `topic-keepalive.py --record-resume <the id>` **once the liveness instrument shows it live** |
| `restart` | live session, `STALE`/`INVALID` cadence | `/supervisor:restart <session-id>`; its `busy-target` refusal means mid-turn — wait a tick, never kill in-flight work |
| `leave` | `STOPPED` — a deliberate stand-down | nothing; the operator stood it down |
| `cannot-tell` | the probe could not decide | render `UNVERIFIED` and name the input; **never act** |
| `crash-loop` | 3 resumes in 30 min already spent | print `⚠️ MANAGER CRASH-LOOP: <goal>` and escalate **once** to the operator |

⚠️ **Never a fresh spawn while a recorded id exists.** A second manager on one goal is the
double-writer failure `commands/open.md` § Step 2C exists to prevent.

⚠️ **`UNKNOWN`/`AMBIGUOUS`/`MALFORMED` are cannot-tell, never death.** An undecidable probe
reported as a dead manager is how a working session gets a second writer.

## Start — inline, because it is this tier's whole product

Only when a keep-alive slot is free (`resume`/`restart` did not consume it) and a startable
goal is held:

1. `topic-spawn.py --topic "<topic>" --rank` → the ranked rows and which take the N slots.
2. For each `start` row not already running: `/supervisor:topic-spawn "<goal>"`. Its tokens
   (`goal-not-in-scope`, `goal-not-startable`, `manager-cap`, the worker refusal) are the
   gate — never re-derive them here.
3. ⚠️ **Never exceed N = 3 per topic and never exceed the fleet ceiling.** The cap is never
   raised to make room; rank and wait, or say which goal is starved and why.

⚠️ **A `phase: todo` goal is never started** — `goal-not-startable` is the operator's
approval boundary, not a bug.

## What this round must never do

- **Never manage a worker** — no `goal-spawn`, no `SendMessage` to a worker, no nudging a
  task. A worker row is reported downward to its goal manager.
- **Never classify a task.** This tier's rows are goals and their managers; the task buckets
  are the goal manager's, in `agents/goal-sweep-reader.md`.
- **Never decide an operator gate.** You surface it; the operator answers it.
- **Never close a goal.** Closure is `/vault-cli:complete-goal`'s.
- **Never claim an act the script reported it could not perform.** If a tool failed to bind,
  carry that through rather than smoothing it.
