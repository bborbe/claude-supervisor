---
description: One pass over every live Claude Code session — classify each into CLOSED / WORKING / EXTERNAL / OPERATOR / STUCK / LIVE-IDLE HEADLESS, close the finished (headless workers included, via `close-worker.py`), drive everything that is not genuinely blocked on something outside the fleet, post a card for every question that has none, and hand the operator only what only they can release. Ends by re-sweeping and asserting every session is working, closed, or parked on a named producer or an open card — with the stuck, uncarded and finished-open counts all zero.
allowed-tools:
  - Read
  - ListAgents
  - SendMessage
  - mcp__supervisor__agent_status
  - Bash(python3:*)
  - Bash(wezterm cli list:*)
  - Bash(wezterm cli get-text:*)
  - Bash(vault-cli:*)
  - Bash(echo:*)
  - Bash(tail:*)
  - Bash(ls:*)
  - Bash(grep:*)
  - Bash(jq:*)
  - Bash(ps:*)
  - Bash(date:*)
argument-hint: "[--dry-run]"
---
Fleet triage — one pass over the whole fleet, ending with **nothing idle that could be moving**.

⚠️ **The sweep and its repairs are the run; this file is the router.** Read a step file only when its **When** holds, and re-read it if its text is not in context (e.g. after a compaction). The hard rules are mirrored INLINE below so no rule is ever absent from the run.

## The invariant

> **At the end of a run, every session is doing one of four things: working, closed,
> genuinely waiting on a producer outside the fleet, or held by an operator gate that
> carries a card. A session that is idle with work left is a failure of this command.**

The same invariant stated as the exit condition, which is what Step 6 asserts:

> **Every live session ends the run either WORKING, or CLOSED, or parked with a named
> valid reason to wait — a named external producer with an ETA, or an open attention card
> carrying the question. "Idle, finished, still open" and "parked on a question with no
> card" are both failures, and both are counted.**

That is the whole point. A good-looking table with three idle workers on it is a failed
run. The stuck list is the deliverable, and the target is zero.

**The same invariant applies to managers, and it is checked separately.** A manager with a live,
non-terminal subject carrying runnable work and no loop is a failure of this command — its whole
subject is unmanaged and every worker under it is waiting on a loop that will never tick. But a
manager stopped because its subject is terminal, or stood down because it has nothing runnable,
is **correct**, and counting those as failures sends the operator to fix what is already right.
Step 3.6 owns the three-way split.

This is **not** any of its neighbours:

| Command | Question it answers |
|---|---|
| `/supervisor:fleet-status` | what is everyone doing (snapshot, zero side effects) |
| `/supervisor:who-needs-my-help` | what is blocking each session (report-only on gates) |
| `/supervisor:fleet-drive` | the manager's recurring round |
| **`/supervisor:fleet-triage`** | **what is stuck, and then unstick it** |

`--dry-run` classifies and repairs nothing, but still prints the stuck list.

## The six states

Exactly one per session, first match wins, evaluated in this order. `4b` is a distinct state,
not a sub-case of `4`: a live-idle headless session is **not STUCK**, because the drive verb
that would clear a STUCK row does not exist for it.

| # | State | Meaning | Action |
|---|---|---|---|
| 1 | ⚪ **CLOSED** | task terminal on disk, not mid-turn | close via `close-worker.py` — pane kill or SIGTERM, never a hand-rolled `kill` |
| 2 | 🔵 **OPERATOR** | a gate only the operator can release | one jump-linked row + a card |
| 3 | 🟡 **EXTERNAL** | parked on a producer outside the fleet | name the watcher and ETA |
| 4 | 🟠 **STUCK** | idle with work left, nothing external blocking | **drive it now** |
| 4b | 🔒 **LIVE-IDLE HEADLESS** | live, idle, headless, no pane — no *supervisor* verb reaches it, but a cross-session `SendMessage` does | **`SendMessage` first; card a kill only if no reply** |
| 5 | 🟢 **WORKING** | everything else | nothing |

⚠️ **The order is the design.** CLOSED first, or a finished session reports as working
forever. OPERATOR before EXTERNAL, or a question gets filed as a machine wait. **STUCK is
the bucket this command exists to empty** — anything reaching state 4 must leave the run
moving or with a card.

## Router

| # | Step | When | File |
|---|---|---|---|
| 0 | Load path | every run — before the sweep | `${CLAUDE_PLUGIN_ROOT}/commands/steps/fleet-triage-load-path.md` |
| 1 | Sweep | every run — before any classification | `${CLAUDE_PLUGIN_ROOT}/commands/steps/fleet-triage-sweep.md` |
| 2 | Tier | the sweep's JSON parsed | `${CLAUDE_PLUGIN_ROOT}/commands/steps/fleet-triage-tier.md` |
| 3 | State | every session in the sweep — derive exactly one state | `${CLAUDE_PLUGIN_ROOT}/commands/steps/fleet-triage-state.md` |
| 4 | Act | every row whose state is not WORKING, unless `--dry-run` | `${CLAUDE_PLUGIN_ROOT}/commands/steps/fleet-triage-act.md` |
| 5 | Render | every run — after the act phase | `${CLAUDE_PLUGIN_ROOT}/commands/steps/fleet-triage-render.md` |
| 6 | Verify | every run — the last step, always | `${CLAUDE_PLUGIN_ROOT}/commands/steps/fleet-triage-verify.md` |
| 7 | Rules | not already in this session's context | `${CLAUDE_PLUGIN_ROOT}/commands/steps/fleet-triage-rules.md` |

Steps 0–6 run in order on every pass; step 7 is read once. `--dry-run` changes step 4 to a
no-op, and it also changes step 6's rendering — the verdict block prints its buckets as
*would-be* (`0 (n closable)`), because a dry run has nothing to report as done.

## INLINE — the hard rules

One imperative per rule; the full text, the measurements and the `⚠️` clauses live in
`steps/fleet-triage-rules.md`, in `commands/steps/rules.md` (the three fleet-wide rules), and in
the step that owns each.

- **One pass, then one verification sweep** — no polling loop.
- **Never type into another session's pane** — `wezterm cli send-text` into another session turns a question into a menu selection.
- **Never run `/vault-cli:sync-progress` or `/vault-cli:session-close` for another session** — both are session-scoped.
- **Never relay an approval** for a production-touching or irreversible action, and never drive a selection modal.
- **A peer's claim is testimony; the task file and a live read are the evidence.**
- **A measured state beats a reported one** — the board, `details`, a task page's Status Summary and a closer panel are all asserted.
- **Report the refusals** — a close that refused and was not reported reads as a close that happened.
- **A headless worker is closed by `close-worker.py`, not by a hand-rolled `kill`.**
- **A permission park is not a stuck session** — route the gate, never drive it, never close it.
- **A signal is the last resort of the close path, never of the drive path.**
- **Every question ends the run carded** — a session classified OPERATOR with no open card is the run's failure, named in Step 6's `uncarded` count.
- **A card is not a ping** — it carries the question verbatim and a recommended answer; if you cannot state both, the honest output is a STUCK row naming what is missing.
- **A `STALE` manager reading is arithmetic, not proof of a lapsed loop**, and **`session-liveness.py --check` returning `ABSENT` is not a death verdict** — both are `commands/steps/rules.md` rules; corroborate against a registry pid, a transcript mtime or pane text before either reaches a report or an action.
- **Never recommend OPENING a manager** — the only subject this fleet opens is one carrying `flag: true` with no loop armed (`commands/steps/rules.md` § No manager-proposal row).
- **Prove a card does not already exist before posting one** — `ls ~/.claude/state/attention/<session-id>*.open.json`, then **read each matched file's `item_id` and poll that** — `ls` prints paths, not ids.
