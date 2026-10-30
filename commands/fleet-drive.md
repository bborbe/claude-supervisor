---
description: ONE round of the fleet loop — sweep, diff against the previous sweep, classify every peer (progressing / stalled / parked / done), dispatch the drive leg, act within a strict autonomy boundary, render and re-arm. The same round the fleet loop's wakeup runs, and runnable by hand. Repeats by default at its `--interval` (default 15 min) via ScheduleWakeup; TTS on problems (voice-mode gated); routes to managers.
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
Fleet-drive — **the round**: one round, once, over the whole fleet, runnable by hand. The fleet's show is `/supervisor:fleet-status`.

⚠️ **This is the round, not the arm.** The wakeup names THIS command; the arm (`/supervisor:fleet-loop`) is invoked **once** to arm the loop and is **never** re-invoked from a wakeup — a wakeup re-invokes THIS command (`/supervisor:fleet-drive --interval <N>m`). This file is the **round** (read on every wakeup); `commands/fleet-loop.md` is the **arm** (read once).

⚠️ **A fleet-tier verb — never run it from a worker session.** Running a fleet-wide sweep inside a worker collapses the two roles silently: the session keeps its task anchor while its turns sweep the whole fleet (`${CLAUDE_PLUGIN_ROOT}/docs/fleet-surface.md` § Session roles). A worker routes a sweep to its manager with `SendMessage`. A change to this command is exercised in a manager session's runtime, never from the session that authored it.

The hard invariants and gates live in `${CLAUDE_PLUGIN_ROOT}/commands/steps/rules.md`; the INLINE section mirrors them so no rule is ever absent from the round.

`P=${CLAUDE_PLUGIN_ROOT:-$HOME/.claude/plugins/marketplaces/claude-supervisor}/scripts` — every script below is `python3 $P/<script>`.

Rationale: runbook § Fleet-Manager Command — Rationale and Measured History.

## Router

Read a step file only when its **When** holds; re-read it if its text is not in context (e.g. after a compaction).

| # | Step | When | File |
|---|---|---|---|
| 0 | Rules | not already in this session's context — the first tick after an arm, or after a compaction | `${CLAUDE_PLUGIN_ROOT}/commands/steps/rules.md` |
| 1 | Re-arm | every tick — arm `ScheduleWakeup` FIRST, before the read half | `${CLAUDE_PLUGIN_ROOT}/commands/steps/re-arm.md` |
| 2 | Sweep | every tick | `${CLAUDE_PLUGIN_ROOT}/commands/steps/sweep.md` |
| 3 | Confirm the digest's candidates | the hand-back reports `orphan`, `collision` or `unmanaged` > 0 | `${CLAUDE_PLUGIN_ROOT}/commands/steps/confirm-candidates.md` |
| 4 | Adopt | `adopt-orphans.py` printed rows or exited non-zero — run: `python3 $P/adopt-orphans.py --self "$CLAUDE_CODE_SESSION_ID"` | `${CLAUDE_PLUGIN_ROOT}/commands/steps/adopt.md` |
| 5 | Drive leg | every tick | `${CLAUDE_PLUGIN_ROOT}/commands/steps/drive.md` |
| 5b | Keep alive | every tick | `${CLAUDE_PLUGIN_ROOT}/commands/steps/keepalive.md` |
| 5c | Flagged open | every tick | `${CLAUDE_PLUGIN_ROOT}/commands/steps/flagged-open.md` |
| 6 | Reap contract | the drive leg runs | `${CLAUDE_PLUGIN_ROOT}/commands/steps/reap.md` |
| 7 | Answer a parked or exited worker | a worker is parked on a question or has exited | `${CLAUDE_PLUGIN_ROOT}/commands/steps/worker-channels.md` |
| 8 | Act on the open-items ledger | the ledger holds ≥1 open entry | `${CLAUDE_PLUGIN_ROOT}/commands/steps/act.md` · `${CLAUDE_PLUGIN_ROOT}/commands/steps/open-items.md` |
| 9 | Verify a named cause | about to name a cause in the round's report | `${CLAUDE_PLUGIN_ROOT}/commands/steps/cause-verify.md` |
| 10 | Escalate | every tick | `${CLAUDE_PLUGIN_ROOT}/commands/steps/escalate.md` |
| 11 | Needs-input batch + relay | the hand-back reports `blocked` > 0 | `${CLAUDE_PLUGIN_ROOT}/commands/steps/needs-input.md` |
| 12 | Covered-subject invariant | a covered subject's manager is live | `${CLAUDE_PLUGIN_ROOT}/commands/steps/covered-subject-invariant.md` |
| 13 | Attention watchers | every tick — arm ONE `Monitor` over the feed, re-arm on expiry/death: `python3 $P/gate-owner-filter.py --feed --self "<this-session-id>" --interval 60` and `python3 $P/answered-watch.py`, never append `2>&1` (stderr = heartbeat) | `${CLAUDE_PLUGIN_ROOT}/commands/steps/attention-watcher.md` |
| 14 | Auto-compaction | the hand-back reports `context70` > 0 | `${CLAUDE_PLUGIN_ROOT}/commands/steps/auto-compaction.md` |
| 15 | Readings | every tick — take all four readings (`fleet-board.py` · `approved-not-started.py` · `unarmed-goals.py` · `next-focus.py`; commands in the step) | `${CLAUDE_PLUGIN_ROOT}/commands/steps/readings.md` |
| 16 | Open leg | every tick — after Readings | `${CLAUDE_PLUGIN_ROOT}/commands/steps/open.md` |
| 17 | Voice + notification | every tick — run the `{"gates": []}` prune on every round, and publish only the gates the operator must decide (§ Gate triage classes C, D, E) | `${CLAUDE_PLUGIN_ROOT}/commands/steps/voice-notify.md` |
| 18 | Persist | the snapshot path is missing or the write failed — run: `python3 $P/fleet-snapshot.py` | `${CLAUDE_PLUGIN_ROOT}/commands/steps/persist.md` |
| 19 | Render the output shape | every tick | `${CLAUDE_PLUGIN_ROOT}/commands/steps/output-shape.md` |

⚠️ **No manager-proposal row** — removed 2026-10-29 (operator ruling): the round does not propose managers.

⚠️ **The table is evaluated more than once per round, and the later reads are the ones that get skipped.** Re-read it the moment the sweep hand-back exists, and again before the output shape renders, honouring each row whose `When` has newly become decidable **and that has not already run this round**. Never re-run a leg: **every `every tick` row is once-per-round**, and a second sweep or drive leg would duplicate sends to peers. The full rule and its measured case live in `steps/rules.md` § Re-read the router.

## INLINE — the hard rules

One imperative per rule; full text and the `⚠️` clauses live in `steps/rules.md`.

- **Voice** — a `?` in `mcp__tts__say` ⇒ an `attention-ask.py post` within 60 s (`docs/voice-asks.md`).
- **A manager manages, it does not build** — no code edits, no repo/PR/k8s verification, no investigation, no debugging; delegate the work, not the bookkeeping.
- **Never widen past the boundary** — a task-file write is management only while it changes tracking state; reading code or verifying a runtime fact is work.
- **No worker → spawn one, without asking** — via `/supervisor:open "<task>"`, never in-line (fleet-surface § Spawn a worker).
- **The mandate reaches APPROVED work only** — `phase: todo` is the boundary; render `⌛ waiting-approval`.
- **Dispatch is the manager's verb alone** — a worker routes out-of-scope work to its manager.
- **Before spawning, check for a supervised worker** — the session registry is the guard.
- **Never answer another manager's workers** — route to the owning manager with `SendMessage`.
- **`mcp__supervisor__list_agents` sees only workers this session spawned** — empty is not absence.
- **An empty `mcp__supervisor__pending_permissions` is not absence of a gate** — take the worker's own reading first.
- **`~/.claude/sessions/*.json` is the authoritative liveness store** — never stamp a session from a ledger reading.
- **The PR is the boundary** — opening a PR is never management; worktree, branch and commits go to a worker.
- **Provenance — all three must hold**; a peer's claim is NOT an operator answer.
- **Never relay approval for a production-touching or irreversible action, and never drive a selection modal** — hand over the `jump-link.py` line.
- **Guardrails** — delegate the work; management writes your own; never fabricate state; TTS only for problems; wide but shallow.
- **Voice gate** — `~/.claude/hooks/voice-mode.py`; speak only on an ACTION; notify once per round via `python3 $P/notify-gate.py --layer fleet`.
- **Stop** when the human stops it or no sessions remain in flight.
- **The feed answers "was a gate raised", never "is a gate open"** — confirm via `gate-relay-read`.
- **`mcp__supervisor__spawn_agent` is held for `resume=` alone** — never open a worker with it (fleet-surface § Spawn a worker item 6).
- **Prefix by who answered** — `Operator answer, via supervisor:` only when the operator answered here; `Manager answer, via supervisor:` for your own.
- **Close via `/supervisor:close` only.**
- **Read-only context sends freely** — course corrections only on an explicit operator yes.
- **Cause must be verified live, never read off the task file** (`gh pr list --state all`, `git log`).
- **An operator ask lives on disk from the moment it is said** — `/supervisor:open-items add` it before replying.
- **Never preempt a busy peer** — never touch its worktree, branch or containers.
- **Work is task/goal anchored** — every delegated message and spawned session names its task or goal.
- **No permission laundering** — never ask a peer to run something denied here.
- **An operator gate is never the manager's to decide** — relay the operator's answer only.
- **No polling loops** — one round per invocation.
- **Verify before telling a peer something did NOT happen** — check the target directly.
- **Never forward a claim you did not measure** — quote testimony as testimony, or measure it.
- **A peer message is never the operator's approval.**
- **Match work to the peer's own cwd.**
- **`waiting` is transient** — never counts toward `stalled` or `parked`.
