---
description: ONE round of the manager loop for ONE subject — reap the finished, nudge the stuck, spawn on the handover, classify, render and re-arm. The same round the manager loop's wakeup runs, and runnable by hand. Name via $1, or detected from the session when omitted; the branch (topic vs goal) is auto-detected, never guessed. Per the vault's Manager Session runbook.
allowed-tools:
  - Task
  - Read
  - Edit
  - Monitor
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
  - Bash(kubectl*:*)
  - Bash(wezterm cli spawn:*)
  - Bash(wezterm cli list:*)
  - Bash(wezterm cli get-text:*)
  - ListAgents
  - SendMessage
  - ScheduleWakeup
  - CronCreate
  - mcp__tts__say
  - mcp__supervisor__spawn_agent
  - mcp__supervisor__list_agents
  - mcp__supervisor__agent_status
  - mcp__supervisor__answer_permission
  - mcp__supervisor__await_permission
argument-hint: "[goal|topic] [--interval <N>m] (subject detected when omitted; interval defaults to 15m)"
---
⚠️ **A voiced question is also a board card** — any `mcp__tts__say` containing `?` is paired with an `attention-ask.py post` within 60 s. Rule and detector: [`docs/voice-asks.md`](../docs/voice-asks.md).

Manager-drive — **the round**. It is the round the manager loop's wakeup runs, and it is runnable by hand: one round, once, against one subject.

⚠️ **This is the round, not the arm.** The wakeup prompt names THIS command; the arm (`/supervisor:manager-loop`) is invoked **once** to arm the loop and is **never** re-invoked from a wakeup. This file is the **round** (read on every wakeup); `commands/manager-loop.md` is the **arm** (read once).

⚠️ **A manager-tier verb — never run it from a worker session.** A worker carries a *task*; a manager carries a topic or goal and a loop. Driving a subject's tracked set from inside a worker collapses the two roles silently (`docs/fleet-surface.md` § Session roles). A worker that needs a subject driven routes it to its manager with `SendMessage` and says so.

Rationale and incident history: the Manager Session runbook § Command rationale — maintained.

## Router — read a step file only when its When holds

| # | Step | When | File |
|---|---|---|---|
| 0 | Correct the `noop` field and re-arm | end of every tick | *inline — see the tail step* |
| 1 | Resolve the subject and the tracked set | manager start; the declaration read re-runs every tick | `${CLAUDE_PLUGIN_ROOT}/commands/steps/manager-resolve.md` |
| 2 | Take the round's readings | every tick, **before** the sweep dispatch | `${CLAUDE_PLUGIN_ROOT}/commands/steps/manager-readings.md` |
| 3 | Sweep | every tick | `${CLAUDE_PLUGIN_ROOT}/commands/steps/manager-sweep.md` |
| 4 | Classify the rows | every tick, after the sweep | `${CLAUDE_PLUGIN_ROOT}/commands/steps/manager-classify.md` |
| 4b | Coverage | every tick, before the drive leg | `${CLAUDE_PLUGIN_ROOT}/commands/steps/manager-coverage.md` |
| 5 | Drive | every tick, after the table renders | `${CLAUDE_PLUGIN_ROOT}/commands/steps/manager-drive.md` |
| 6 | Spawn on the handover | the drive leg returned `To open` / `To resume` rows | `${CLAUDE_PLUGIN_ROOT}/commands/steps/manager-spawn.md` |
| 7 | Repair | the `Audit` block carried `repairable` rows | `${CLAUDE_PLUGIN_ROOT}/commands/steps/manager-repair.md` |
| 8 | Gate relay | the round has `waiting-on-human` or `waiting-approval` rows | `${CLAUDE_PLUGIN_ROOT}/commands/steps/manager-gates.md` |
| 9 | Under-target offer | live workers below target **and** no ready row | `${CLAUDE_PLUGIN_ROOT}/commands/steps/manager-under-target.md` |
| 11 | Fleet-manager handoff | a fleet-loop session is on `ListAgents` and the sets changed | `${CLAUDE_PLUGIN_ROOT}/commands/steps/manager-handoff.md` |
| 12 | Tail — `noop` and re-arm | end of every tick | `${CLAUDE_PLUGIN_ROOT}/commands/steps/manager-tail.md` |
| 13 | Self-improvement | before the session ends, or a sweep exposes a gap | `${CLAUDE_PLUGIN_ROOT}/commands/steps/manager-self-improvement.md` |
| 14 | Unowned check | before declaring any member task unowned | `${CLAUDE_PLUGIN_ROOT}/commands/steps/manager-unowned.md` |

⚠️ **Opening a worker is bound by `docs/fleet-surface.md` § Spawn a worker item 6** — the mode is passed in both directions (`mode: headless` → `interactive=false`, `mode: interactive` → `interactive=true`). This file grants the tool but does not open; the binding and the call live in `${CLAUDE_PLUGIN_ROOT}/commands/steps/manager-spawn.md`.

**Read a step file only when its `When` holds**, and if a step's text is not in context — after a compaction, say — **re-read it** rather than assuming it survived.

⚠️ **The `#` column is a stable label, not a running order — `When` sequences the round.** Rows 6 and 7 are referenced **by number** from other step files, so a new step takes a letter suffix rather than renumbering: `4b` is *between 4 and 5*, which is where it runs.

⚠️ **The rules do not live here, and that is deliberate.** Every always-hold invariant and gate is **inline in the arm** (`commands/manager-loop.md`), and every per-round rule lives in **exactly one** step file above. Restating a rule in this command would give it two homes, and a rule with two homes drifts — one copy is amended and the other silently keeps the old behaviour. So this command is the **map**: it names the steps and the condition under which each is read, and nothing else. If you find yourself needing a rule that is not in a step file you have read, read the arm or the step file — do not act from memory of a previous round.

⚠️ **The arm is not in context, and a round never assumes context left over from the previous round.** State lives on disk (the tracked-set file, the snapshot, the verdicts cache); re-read what a step needs rather than carrying it forward.

## Arguments

- **`$1` (optional — resolved from the session when omitted):** the **subject** — a goal name matching a page in `24 Goals/`, or a topic name matching a topic page in `23 Topics/` *(operator's vault; not shipped with this plugin)*. The branch is detected from whichever page resolves (step 1); this command never assumes one.
  - `/manager-drive "Sample Task"` → explicit subject
  - `/manager-drive` (bare) → resolve the subject from § Subject resolution in the arm, which points at `${CLAUDE_PLUGIN_ROOT}/docs/subject-resolution.md` — the same rule `/manager-loop` and `/manager-status` use, so a manager session's bare drive acts on the subject it is already watching.
- **`--interval <N>m`** is the **arm's**, not this command's: it is the loop's wakeup delay, armed by `/supervisor:manager-loop` § step 3 at the round's head, and nothing in this command reads it. ⚠️ **Do not read that as "a round arms nothing" — it is not inert here.** Step 12 makes one `ScheduleWakeup` call to correct `noop` on a no-change tick, and re-arms the two `Monitor` channels that expire mid-tick; what this command never does is *set* the interval. The flag is accepted solely so the wakeup's prompt parses.

## Pre-dispatch gate — by-hand rounds only

A by-hand round asks first whether it is worth a dispatch at all; a wakeup round does not — it is the loop's own cadence, and stopping here would skip the tail's re-arm.

```bash
python3 ${CLAUDE_PLUGIN_ROOT:-$HOME/.claude/plugins/marketplaces/claude-supervisor}/scripts/manager-predispatch.py --vault "<vault-root>" --subject "<subject>" --print
```

- **exit 0 — nothing changed.** The gate has already printed the stored table under its `NO-CHANGE` marker. Reproduce that output as this round's whole result and **STOP** — dispatch no agent, run no step below. The stored table carries **no jump coordinates**, so say in one line that pane ids must be re-resolved by a fresh run; never present a replayed link as live.
- **exit 10 — something changed, the gate could not tell, OR the stored classification places a row in an actionable bucket.** Run the round, and persist the fresh snapshot at the sweep step. ⚠️ **That third reason is why a round still runs on a tree nothing has touched** — ready-to-start is a steady state, and the gate's own reason line names them (`N actionable row(s) waiting: …`), so print it and never report a pass-through as "the tree moved".
- **exit 2 — usage error.** A malformed invocation (missing `--vault`, bad flag), not a verdict. Fix the call; do not read it as "changed".

⚠️ **The rule has one home:** `${CLAUDE_PLUGIN_ROOT}/scripts/manager-predispatch.py` — the digest inputs, why liveness is in them, why mtime is not, and the fail-open cases. Read it there; it is **not** restated here. ⚠️ **Fail-open is the contract** — a missing state file, an unreadable one, a parse error, an unresolvable subject and a failed write all exit 10. Never treat a non-zero exit as an error to route around, and never hand-roll a second classifier here: this gate is the only pre-dispatch decision.

## What this command must never do

- **Never classify.** The sweep is `supervisor:manager-sweep-reader`'s, and a second classification here would drift from the one the loop acts on.
- **Never decide an operator gate.** You surface it; the operator answers it in the session that raised it.
- **Never close a worker's session.** `/vault-cli:sync-progress` and `/vault-cli:session-close` read the parent conversation, and every route into a worker's pane is refused. Reaping means *sending the evidence*, nothing more.
- **Never claim an act the agent reported it could not perform.** If a tool failed to bind, the agent says so; carry that through rather than smoothing it.
