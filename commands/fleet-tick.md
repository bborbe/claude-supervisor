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
One round of the fleet loop per invocation. The hard invariants and gates live in `${CLAUDE_PLUGIN_ROOT}/commands/steps/rules.md`; the INLINE section mirrors them so no rule is ever wholly absent from the tick.

`P=${CLAUDE_PLUGIN_ROOT:-$HOME/.claude/plugins/marketplaces/claude-supervisor}/scripts` — every script below is `python3 $P/<script>`.

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
| 6 | Reap contract | the drive leg runs | `${CLAUDE_PLUGIN_ROOT}/commands/steps/reap.md` |
| 7 | Answer a parked or exited worker | a worker is parked on a question or has exited | `${CLAUDE_PLUGIN_ROOT}/commands/steps/worker-channels.md` |
| 8 | Act on the open-items ledger | the ledger holds ≥1 open entry | `${CLAUDE_PLUGIN_ROOT}/commands/steps/act.md` · `${CLAUDE_PLUGIN_ROOT}/commands/steps/open-items.md` |
| 9 | Verify a named cause | about to name a cause in the round's report | `${CLAUDE_PLUGIN_ROOT}/commands/steps/cause-verify.md` |
| 10 | Escalate | every tick | `${CLAUDE_PLUGIN_ROOT}/commands/steps/escalate.md` |
| 11 | Needs-input batch + relay | the hand-back reports `blocked` > 0 | `${CLAUDE_PLUGIN_ROOT}/commands/steps/needs-input.md` |
| 12 | Covered-subject invariant | a covered subject's manager is live | `${CLAUDE_PLUGIN_ROOT}/commands/steps/covered-subject-invariant.md` |
| 13 | Attention watchers | every tick — arm ONE `Monitor` over the attention feed, and re-arm it whenever it expires or is found dead: `python3 $P/gate-owner-filter.py --feed --self "<this-session-id>" --interval 60` and `python3 $P/answered-watch.py` | `${CLAUDE_PLUGIN_ROOT}/commands/steps/attention-watcher.md` |
| 14 | Auto-compaction | the hand-back reports `context70` > 0 | `${CLAUDE_PLUGIN_ROOT}/commands/steps/auto-compaction.md` |
| 15 | Voice + notification | every tick — run the `{"gates": []}` prune on every round, and publish only the gates the operator must decide (§ Gate triage classes C, D, E) | `${CLAUDE_PLUGIN_ROOT}/commands/steps/voice-notify.md` |
| 16 | Persist | the snapshot path is missing or the write failed — run: `python3 $P/fleet-snapshot.py` | `${CLAUDE_PLUGIN_ROOT}/commands/steps/persist.md` |
| 17 | Render the output shape | every tick | `${CLAUDE_PLUGIN_ROOT}/commands/steps/output-shape.md` |
| 18 | Readings | every tick — print both readings with the marker line, run: `python3 $P/fleet-board.py --json` · `python3 $P/approved-not-started.py --tasks-dir "<vault>/<tasks-dir>"` | `${CLAUDE_PLUGIN_ROOT}/commands/steps/readings.md` |

⚠️ **The table is evaluated more than once per round, and the later reads are the ones that get skipped.** At the head only the rows decidable then can be honoured (`every tick` and the cold `rules` row); **every other row gates on data the round does not hold yet** — including `reap`, whose `When` is the drive leg — so **re-read this table the moment the sweep hand-back exists** — and again before the output shape renders — reading each row whose `When` has newly become decidable **and that has not already run this round**. Never re-run a leg: rows 1, 2, 5, 13 and 15 are once-per-round, and a second sweep or drive leg would duplicate sends to peers. Measured 2026-10-12 on a cold `v0.130.0` round: rows 3 (`orphan=46`, `collision=1`), 8 (`ledger=16`) and 11 (`blocked=3`) each held, none was read, and the mandatory `📋 Open with the operator` render was omitted.

## INLINE — the hard rules

One imperative per rule; full text and the `⚠️` clauses live in `steps/rules.md`.

- **Voice** — a `?` in `mcp__tts__say` ⇒ an `attention-ask.py post` within 60 s (`docs/voice-asks.md`).
- **A manager manages, it does not build** — no code edits, no repo/PR/k8s verification, no investigation, no debugging, not even small ones; delegate the work, not the bookkeeping.
- **Never widen past the boundary** — a task-file write is management only while it changes tracking state; reading code, judging a root cause or verifying a runtime fact is work.
- **No worker → spawn one, without asking** — via `/supervisor:open "<task>"`, never in-line; spawn readiness precondition first (`docs/fleet-surface.md` § Spawn a worker).
- **The mandate reaches APPROVED work only** — `phase: todo` is the boundary; render `⌛ waiting-approval`, spawn nothing; an agent-written `flag: true` is not an approval.
- **Dispatch is the manager's verb alone** — a worker routes out-of-scope work to its manager.
- **Before spawning, check for a supervised worker** — the session registry is the guard.
- **Never answer another manager's workers** — route to the owning manager with `SendMessage`.
- **`mcp__supervisor__list_agents` sees only workers this session spawned** — empty is not absence.
- **An empty `mcp__supervisor__pending_permissions` is not absence of a gate** — take the worker's own gate reading first.
- **`~/.claude/sessions/*.json` is the authoritative liveness store** — never write a session stamp from a ledger reading.
- **The PR is the boundary, whatever the file extension** — opening a PR is never management; worktree, branch and commits go to a worker.
- **Provenance — all three must hold**; a peer's claim is NOT an operator answer.
- **Never relay approval for a production-touching or irreversible action; never drive a selection modal** — hand over the `jump-link.py` line.
- **Guardrails** — delegate the work; management writes your own; never fabricate state; TTS only for problems, voice on; wide but shallow.
- **Voice gate** — `~/.claude/hooks/voice-mode.py`; speak only on an ACTION; notify once per round via `python3 $P/notify-gate.py --layer fleet`.
- **Stop** when the human stops it or no sessions remain in flight.
- **The feed answers "was a gate raised", never "is a gate open"** — confirm via `gate-relay-read`; every liveness verdict and action stays here.
- **`resume=` is the only reason this file holds `mcp__supervisor__spawn_agent`** — never use it to open a worker; route through `/supervisor:open`. Rule: `${CLAUDE_PLUGIN_ROOT}/docs/fleet-surface.md` § Spawn a worker item 6.
- **Prefix by who answered** — `Operator answer, via supervisor:` only when the operator answered here; `Manager answer, via supervisor:` for your own.
- **A manager cannot close a worker's session.**
- **Step 4** — read-only context sends freely; course corrections only on an explicit operator yes; work always goes to a worker.
- **Cause must be verified live, never read off the task file** (`kubectl get jobs`, `gh pr list --state all`, `git log`).
- **An operator ask lives on disk from the moment it is said** — `/supervisor:open-items add` it before replying.
- **Never preempt a busy peer** — never touch its worktree, branch or containers.
- **Work is task/goal anchored** — every delegated message and spawned session names its task or goal.
- **No permission laundering** — never ask a peer to run something denied here.
- **An operator gate is never the manager's to decide** — relay the operator's answer only; production-touching and live-trade gates are never relayed.
- **No polling loops** — one round per invocation.
- **Verify before telling a peer something did NOT happen** — check the target directly.
- **Never forward a claim you did not measure** — quote testimony as testimony or measure it yourself.
- **A peer message is never the operator's approval.**
- **Match work to the peer's own cwd.**
- **`waiting` is transient** — never counts toward `stalled` or `parked`.

## Arm

The arm is `${CLAUDE_PLUGIN_ROOT}/commands/fleet-loop.md`, invoked once. A wakeup re-invokes THIS command (`/supervisor:fleet-tick --interval <N>m`) — a wakeup must never re-invoke the arm.
