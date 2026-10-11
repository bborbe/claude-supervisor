---
description: Stateful fleet round — snapshot, diff against the previous sweep, classify every peer (progressing / stalled / parked / done), dispatch the drive leg, and act within a strict autonomy boundary. Composes /fleet-status; repeats by default at its `--interval` (default 15 min) via ScheduleWakeup, TTS on problems (voice-mode gated), routes to managers. (Renamed from /fleet-sweep 2026-09-10 when the two were merged.)
allowed-tools:
  - ListAgents
  - SendMessage
  - Monitor
  - AskUserQuestion
  - ScheduleWakeup
  - mcp__supervisor__spawn_agent
  - mcp__supervisor__list_agents
  - mcp__supervisor__agent_status
  - mcp__supervisor__answer_permission
  - mcp__tts__say
  - Bash(python3:*)
  - Bash(date:*)
  - Bash(vault-cli:*)
  - Bash(ls:*)
  - Bash(cat:*)
  - Bash(mkdir:*)
  - Bash(echo:*)
  - Bash(grep:*)
  - Bash(pgrep:*)
  - Bash(comm:*)
  - Bash(sort:*)
  - Bash(kubectl*:*)
  - Bash(gh:*)
  - Bash(git log:*)
  - Bash(wezterm cli list:*)
  - Task
  - Read
  - Write
  - Edit
argument-hint: "[--interval <N>m] (interval defaults to 15m; the round repeats by default)"
---
⚠️ **A voiced question is also a board card** — any `mcp__tts__say` containing `?` is paired with an `attention-ask.py post` within 60 s. Rule and detector: [`docs/voice-asks.md`](../docs/voice-asks.md).

`P=${CLAUDE_PLUGIN_ROOT:-$HOME/.claude/plugins/marketplaces/claude-supervisor}/scripts` — every script below is `python3 $P/<script>`.

This is the **arm**, invoked once. The stateful fleet round remembers the last sweep, diffs against it, and acts only on evidence. It has side effects — a message consumes a peer's turn — so it never runs tighter than its 15-minute floor and is never invoked back-to-back by a polling loop. The arm re-arms itself; every wakeup runs `${CLAUDE_PLUGIN_ROOT}/commands/fleet-tick.md`, the per-round router.

The hard invariants and gates live in `${CLAUDE_PLUGIN_ROOT}/commands/steps/rules.md` — their single normative home. This file and the tick do not restate them.

## Interval

`--interval <N>m` sets the tick — default **15 min**, floor **15 min**; any other argument is ignored. A below-floor value is **clamped up and announced** (`⚠️ --interval 5m is below the 15m floor — clamped to 15m`), never rejected, because a loop that refuses its own interval stops silently. The round repeats by default; there is no one-sweep flag — a single round still dispatches the drive leg, sends and writes back, so it was never a read-only look.

## Arm procedure

1. **Arm `ScheduleWakeup` FIRST — before the read half, not after it.** It is one-shot per call, so a long round (a drive leg, an escalation, a gate relay) that re-arms at its end is exactly the round that never reaches the re-arm and stops silently. Arm at the **clamped** interval with the prompt `/supervisor:fleet-tick --interval <N>m` — **name the TICK, never the arm** — and `noop: false` (the round has not run yet, so `noop` cannot be honest at the head). A second `ScheduleWakeup` supersedes the first rather than stacking.
2. **Then run the round** — read `${CLAUDE_PLUGIN_ROOT}/commands/fleet-tick.md` and follow its router table. Every step file it names is under `${CLAUDE_PLUGIN_ROOT}/commands/steps/`.

## Router

The tick's router, repeated here so the arm is self-contained; `${CLAUDE_PLUGIN_ROOT}/commands/fleet-tick.md` is its home.

| # | Step | When | File |
|---|---|---|---|
| 0 | Rules | not already in this session's context — the first tick after an arm, or after a compaction | `${CLAUDE_PLUGIN_ROOT}/commands/steps/rules.md` |
| 1 | Re-arm | every tick — arm `ScheduleWakeup` FIRST, before the read half | `${CLAUDE_PLUGIN_ROOT}/commands/steps/re-arm.md` |
| 2 | Sweep | every tick | `${CLAUDE_PLUGIN_ROOT}/commands/steps/sweep.md` |
| 3 | Confirm the digest's candidates | the digest carries an orphan, collision or unmanaged-topic candidate | `${CLAUDE_PLUGIN_ROOT}/commands/steps/confirm-candidates.md` |
| 4 | Adopt | `adopt-orphans.py` printed rows or exited non-zero — run: `python3 $P/adopt-orphans.py --self "$CLAUDE_CODE_SESSION_ID"` | `${CLAUDE_PLUGIN_ROOT}/commands/steps/adopt.md` |
| 5 | Drive leg | every tick | `${CLAUDE_PLUGIN_ROOT}/commands/steps/drive.md` |
| 6 | Reap contract | the drive leg runs | `${CLAUDE_PLUGIN_ROOT}/commands/steps/reap.md` |
| 7 | Answer a parked or exited worker | a worker is parked on a question or has exited | `${CLAUDE_PLUGIN_ROOT}/commands/steps/worker-channels.md` |
| 8 | Act on the open-items ledger | every tick | `${CLAUDE_PLUGIN_ROOT}/commands/steps/act.md` · `${CLAUDE_PLUGIN_ROOT}/commands/steps/open-items.md` |
| 9 | Verify a named cause | about to name a cause in the round's report | `${CLAUDE_PLUGIN_ROOT}/commands/steps/cause-verify.md` |
| 10 | Escalate | every tick | `${CLAUDE_PLUGIN_ROOT}/commands/steps/escalate.md` |
| 11 | Needs-input batch + relay | every tick | `${CLAUDE_PLUGIN_ROOT}/commands/steps/needs-input.md` |
| 12 | Covered-subject invariant | a covered subject's manager is live | `${CLAUDE_PLUGIN_ROOT}/commands/steps/covered-subject-invariant.md` |
| 13 | Attention watchers | an arm has expired or been found dead — re-arm: `python3 $P/gate-owner-filter.py --feed --self "<this-session-id>" --interval 60` and `python3 $P/answered-watch.py` | `${CLAUDE_PLUGIN_ROOT}/commands/steps/attention-watcher.md` |
| 14 | Auto-compaction | the digest's CONTEXT section lists sessions over 70% | `${CLAUDE_PLUGIN_ROOT}/commands/steps/auto-compaction.md` |
| 15 | Voice + notification | every tick | `${CLAUDE_PLUGIN_ROOT}/commands/steps/voice-notify.md` |
| 16 | Persist | the snapshot path is missing or the write failed — run: `python3 $P/fleet-snapshot.py` | `${CLAUDE_PLUGIN_ROOT}/commands/steps/persist.md` |
| 17 | Render the output shape | every tick | `${CLAUDE_PLUGIN_ROOT}/commands/steps/output-shape.md` |
| 18 | Readings | either reading returned `unknown` or a non-zero exit — run: `python3 $P/fleet-board.py --json` · `python3 $P/approved-not-started.py --tasks-dir "<vault>/<tasks-dir>"` | `${CLAUDE_PLUGIN_ROOT}/commands/steps/readings.md` |

## Rationale and runbook

Why each rule exists — the incidents, measurements and superseded readings — lives in the Fleet Manager Session runbook (per-vault) § Fleet-Manager Command — Rationale and Measured History. Read it before changing a rule; the command bodies carry only what a round executes.
