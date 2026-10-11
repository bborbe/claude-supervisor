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

Answer one question: **does anything in the fleet need attention right now?**

Not `/supervisor:worker-drive` (session-scoped) and not `/fleet-status` (stateless). This is the **arm**, invoked once. The stateful fleet round remembers the last sweep, diffs against it, and acts only on evidence. It has side effects — a message consumes a peer's turn — so it never runs tighter than its 15-minute floor (§ Interval) and is never invoked back-to-back by a polling loop. The arm re-arms itself; every wakeup runs `${CLAUDE_PLUGIN_ROOT}/commands/fleet-tick.md`, the per-round router.

The hard invariants and gates live in `${CLAUDE_PLUGIN_ROOT}/commands/steps/rules.md` — their single normative home. This file and the tick do not restate them.

⚠️ This file's frontmatter grants `mcp__supervisor__spawn_agent`; the mode rule that governs every spawn — the classifier, the `mode:` field and both headless constraints — has its single home in `${CLAUDE_PLUGIN_ROOT}/docs/fleet-surface.md` § Spawn a worker item 6. Read it there; never restate it here.

## Interval

`--interval <N>m` sets the tick — default **15 min**, floor **15 min**; any other argument is ignored. A below-floor value is **clamped up and announced** (`⚠️ --interval 5m is below the 15m floor — clamped to 15m`), never rejected, because a loop that refuses its own interval stops silently. For one look without a loop, run `/fleet-status` (read-only); for one act pass, run `/fleet-drive`. The round repeats by default; there is no one-sweep flag — a single round still dispatches the drive leg, sends and writes back, so it was never a read-only look.

## Arm procedure

1. **Arm `ScheduleWakeup` FIRST — before the read half, not after it.** It is one-shot per call, so a long round (a drive leg, an escalation, a gate relay) that re-arms at its end is exactly the round that never reaches the re-arm and stops silently. Arm at the **clamped** interval with the prompt `/supervisor:fleet-tick --interval <N>m` — **name the TICK, never the arm** — and `noop: false` (the round has not run yet, so `noop` cannot be honest at the head). A second `ScheduleWakeup` supersedes the first rather than stacking.
2. **Then run the round** — read `${CLAUDE_PLUGIN_ROOT}/commands/fleet-tick.md` and follow its router table. Every step file it names is under `${CLAUDE_PLUGIN_ROOT}/commands/steps/`.

## Router

The router table has one home: `${CLAUDE_PLUGIN_ROOT}/commands/fleet-tick.md` § Router. The arm carries no copy of it — read the table there and follow it.

## Rationale and runbook

Why each rule exists — the incidents, measurements and superseded readings — lives in the Fleet Manager Session runbook (per-vault) § Fleet-Manager Command — Rationale and Measured History. Read it before changing a rule; the command bodies carry only what a round executes.
