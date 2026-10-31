---
description: ONE round of the TOPIC manager loop — read the goals this topic declares and the goal managers on them, keep the live ones alive, start only flagged goals, render the goal table and re-arm. The round the topic loop's wakeup runs, and runnable by hand. Sweeps goal managers, never workers.
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
| 4c | Closure — close what the evidence already finished | every tick, after the sweep | `${CLAUDE_PLUGIN_ROOT}/commands/steps/goal-closure.md` |
| 5 | Keep alive — resume, restart or leave, per goal | every tick, after the sweep | *inline — § Keep alive below* |
| 6 | Start — the ranking, then `topic-spawn` | the keep-alive left a slot free **and a flagged startable goal is held** | *inline — § Start below* |
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

⚠️ **First, drop every goal whose page reads a terminal `status` (`completed`/`aborted`).**
Row 4c has already run this tick and may have just closed one, and `topic-keepalive.py` decides
`jump`/`resume`/`restart` from `manager_session_id` liveness and the armed-loop state only — it
never reads `status`, and `/supervisor:open --goal` gates on `phase`, not on `status`. So a goal
closed at 4c whose manager is `ABSENT` reaches the `resume` arm below in the same tick, and
`/supervisor:open --goal` re-opens a finished goal. That is the *"finished goal silently restarts"*
failure this round exists to prevent, and it is the round's job to skip the row — the keep-alive
probe cannot see it.

For **each remaining goal this topic declares**, one call decides:

```bash
python3 ${CLAUDE_PLUGIN_ROOT:-$HOME/.claude/plugins/marketplaces/claude-supervisor}/scripts/topic-keepalive.py \
  --vault "<vault-root>" --goal "<goal>"
```

The **first line is the verdict**:

| verdict | meaning | what this round does |
|---|---|---|
| `jump` | live session, loop `OK` **or `UNARMED`** | render the row. ⚠️ `UNARMED` means no record exists under either name — the loop was **never armed**, not lost; the lever is `/supervisor:goal-loop "<goal>"`, never `restart` (a restart resumes a session that never armed anything) |
| `open` | no recorded id, **and the goal carries the flag** (any setter) | run `/supervisor:open --goal "<goal>"`. This is an **open**, and the flag is what licenses it |
| `open-unflagged` | no recorded id, **no flag** | ⚠️ **Report, never open.** Render the row with its rank and say it is held for a flag. Rule 4: a manager is opened only for a flagged page, so a round may not create one |
| `resume` | a recorded `manager_session_id` whose session is `ABSENT` | run `/supervisor:open --goal "<goal>"`, then `topic-keepalive.py --vault "<vault>" --subject "<goal>" --record-resume <the id>` **once the liveness instrument shows it live** |
| `restart` | live session, `STALE`/`INVALID` cadence | `/supervisor:restart <session-id>`; its `busy-target` refusal means mid-turn — wait a tick, never kill in-flight work |
| `leave` | `STOPPED` — a deliberate stand-down | nothing; the operator stood it down |
| `cannot-tell` | the probe could not decide | render `UNVERIFIED` and name the input; **never act** |
| `crash-loop` | 3 resumes in 30 min already spent | print `⚠️ MANAGER CRASH-LOOP: <goal>` and escalate **once** to the operator |

⚠️ **Never a fresh spawn while a recorded id exists.** A second manager on one goal is the
double-writer failure `commands/open.md` § Step 2C exists to prevent.

⚠️ **`UNKNOWN`/`AMBIGUOUS`/`MALFORMED` are cannot-tell, never death.** An undecidable probe
reported as a dead manager is how a working session gets a second writer.

## Start — inline, because it is this tier's whole product

⚠️ **This tier opens a goal manager only for a goal the operator FLAGGED.** Operator ruling
2026-10-30: *"we dont want to auto open topic-manager, goal-manager ... but flag are fine"* —
**restart is not open.** A restart resumes a session that already has a subject and a recorded
id; an open creates supervision where none existed, and deciding who supervises is the
operator's act. So the Start row is gated on the flag, and `goal-spawn`'s own flag clause no
longer excludes it.

A goal is **startable for this row** when all hold:

- `flag: true` in its frontmatter — **any setter**, because provenance is not read (operator
  ruling 2026-10-30: *"it should not matter how set the flag"*);
- the phase gate below passes (`goal-not-startable` is still the operator's approval boundary);
- a keep-alive slot is free (`resume`/`restart` did not consume it).

Only then:

1. `topic-spawn.py --topic "<topic>" --rank` → the ranked rows and which take the N slots.
2. For each **flagged** `start` row not already running: `/supervisor:topic-spawn "<goal>"`. Its
   tokens (`goal-not-in-scope`, `goal-not-startable`, `goal-terminal`, `goal-unflagged`,
   `manager-cap`, the worker refusal) are the gate — never re-derive them here.
3. ⚠️ **Never exceed N = 3 per topic and never exceed the fleet ceiling.** The cap is never
   raised to make room; rank and wait, or say which goal is starved and why.

⚠️ **An UNFLAGGED goal with a free slot is not started — it is reported.** `--rank` keeps it in
the output carrying the `unflagged` slot, which never takes one of the N, so the report and the
gate cannot disagree. Say plainly that it is held for the operator's flag. That is not a stall
and not a shortage: an unmanaged goal still gets its **workers** opened by the fleet round's
open leg, so the work moves without a manager.

⚠️ **A `phase: todo` goal is never started** — `goal-not-startable` is the operator's
approval boundary, not a bug.

## What this round must never do

- **Never manage a worker** — no `goal-spawn`, no `SendMessage` to a worker, no nudging a
  task. A worker row is reported downward to its goal manager.
- **Never classify a task.** This tier's rows are goals and their managers; the task buckets
  are the goal manager's, in `agents/goal-sweep-reader.md`.
- **Never decide an operator gate.** You surface it; the operator answers it.
- **Close on complete evidence; never certify one.** This tier closes a member goal (SC + DoD)
  and the topic page itself (**SC + `# Completion Gate`** — the topic's own contract, and the
  reason the reader takes `--kind topic`) — never a task, which belongs to its goal's manager
  and which this tier never reads — through `${CLAUDE_PLUGIN_ROOT}/commands/steps/goal-closure.md`,
  which is the rule's only execution home and carries the verbs, the `--vault` contract and
  the terminal guard. ⚠️ **Never tick a box, and never set `status`, to make a close possible**
  — closure follows the evidence, it never manufactures it, and a close that would need
  `--force` is one you do not make. A page you may not close (any box open) is routed to its
  own manager, never closed from here.
- **Never claim an act the script reported it could not perform.** If a tool failed to bind,
  carry that through rather than smoothing it.
