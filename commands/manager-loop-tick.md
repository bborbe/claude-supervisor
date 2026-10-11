---
description: One tick of the manager loop — the per-round steps, read from the step files named here.
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

This is the **tick**, not the arm. The wakeup prompt names THIS command; the arm (`/supervisor:manager-loop`) is invoked **once** to arm the loop and is **never** re-invoked from a wakeup.

## Router — read a step file only when its When holds

| # | Step | When | File |
|---|---|---|---|
| 0 | Correct the `noop` field and re-arm | end of every tick | *inline — see the tail step* |
| 1 | Resolve the subject and the tracked set | manager start; the declaration read re-runs every tick | `${CLAUDE_PLUGIN_ROOT}/commands/steps/manager-resolve.md` |
| 2 | Take the round's readings | every tick, **before** the sweep dispatch | `${CLAUDE_PLUGIN_ROOT}/commands/steps/manager-readings.md` |
| 3 | Sweep | every tick | `${CLAUDE_PLUGIN_ROOT}/commands/steps/manager-sweep.md` |
| 4 | Classify the rows | every tick, after the sweep | `${CLAUDE_PLUGIN_ROOT}/commands/steps/manager-classify.md` |
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

⚠️ **The rules do not live here, and that is deliberate.** Every always-hold invariant and gate is **inline in the arm** (`commands/manager-loop.md`), and every per-round rule lives in **exactly one** step file above. Restating a rule in this command would give it two homes, and a rule with two homes drifts — one copy is amended and the other silently keeps the old behaviour. So this command is the **map**: it names the steps and the condition under which each is read, and nothing else. If you find yourself needing a rule that is not in a step file you have read, read the arm or the step file — do not act from memory of a previous tick.

⚠️ **The arm is not in context, and a tick never assumes context left over from tick N−1.** State lives on disk (the tracked-set file, the snapshot, the verdicts cache); re-read what a step needs rather than carrying it forward.
