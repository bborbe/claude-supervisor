---
# Read by the router, never invoked — without this key Claude Code
# registers this file as /<plugin>:steps:<name>.
user-invocable: false
---

# Readings

When: every tick — take all four readings (`fleet-board.py` · `approved-not-started.py` · `unarmed-goals.py` · `next-focus.py`; commands in the step)

## The saturation reading — the ratio, not tok/s

Every tick prints the fleet working ratio beside the marker line:

    saturation: 3/6 (busy + shell over live sessions)

Read it from `python3 $P/fleet-board.py --json` → `saturation`, which carries `numerator`, `denominator` and `ratio`. The numerator counts the registry **status** (`busy` + `shell`), not the board's `running` bucket — the buckets apply a precedence, so a `busy` session holding an open gate is bucketed `needs-input` and would be dropped from a bucket-based count. `ratio` is `None` for an empty fleet; print it as absent, never as `0%`.

**The ratio is the signal, not tok/s.** Measured 2026-09-20 (Fleet Manager session `b700c650`): output throughput swung 629 → 905 tok/s in 45 minutes while the ratio moved only 44% → 42%. Throughput tracks whichever model happens to be mid-response at the moment of the read; the ratio does not. Print the tok/s figure beside it for context only — `~/.claude/scripts/claude-metrics.sh` reads `out_tok/s` — and never act on it as the number.

**Open Questions 1 and 2 — ANSWERED 2026-10-01, and neither answer is a ratio.** Both asked whether a *ratio threshold* should trigger a spawn. The operator's design of 2026-10-01 replaces the ratio as the capacity signal outright: the trigger is the fleet-wide **worker target** (`spawn.maxConcurrent`, whose single home is `${CLAUDE_PLUGIN_ROOT}/docs/fleet-surface.md` § Spawn a worker item 5 — **read the number there and never restate it here**), read as live workers against that number and printed as `workers: <live>/<target>` (Output shape item 1).

- **Open Question 1 is superseded rather than answered with a threshold.** No ratio value triggers a spawn, and none is to be inferred from this line. The ratio stays a *diagnostic* — it is what tells you the fleet is saturated, or human-queued — while the target is what decides whether a manager proposes work.
- **Open Question 2 is answered: capacity does drive action — but through the target, not through this ratio.** A manager tick below the target with no ready row proposes the next most important rows to approve (`commands/goal-loop.md` step 4). **This line itself stays read-only**, exactly as the measurement argued.

## The approved-not-started reading

Every tick prints this line with the marker line:

    approved, not started: 3 · oldest 7h35m (Build claude-interactive)

Read it from `python3 $P/approved-not-started.py --tasks-dir "<vault>/<tasks-dir>"`. A row counts when its task-file frontmatter carries `approved_at` with a non-terminal `status` and no **live** `claude_session_id`. Rows waiting longer than one tick (**30 min, a script constant** — never a per-call flag, because two rounds disagreeing about the same row is the defect this line exists to remove) are listed beneath it with their owning manager.

⚠️ **`unknown` is a third answer, not `0`.** If the session registry cannot be read the script prints `approved, not started: unknown` and exits non-zero — *"no approved rows are waiting"* and *"I could not ask"* must not render the same (`docs/pane-reads.md` § The rule). Print it as it comes back; never round `unknown` down to a number.

⚠️ **The line reports; it never opens.** These rows are the drive leg's and the owning manager's to act on, under the standing mandate and its approval boundary — this reading adds visibility, not authority.

## The unarmed-goals reading

Every tick prints this line with the marker line:

    unarmed goals: 1 unarmed goal · Build Rapid Strategy Development Pipeline (2 sessions)

Read it from `python3 $P/unarmed-goals.py --vault "<vault>" --tasks-dir "<vault>/<tasks-dir>"`. A goal counts when it has **both** a live session — a non-terminal task whose `claude_session_id` or `metrics_sessions` id the registry reports alive — **and** no manager record under either the `goal--…cadence` or the pre-split `…cadence` name. A task carrying neither `goals:` nor `topics:` is listed as a `NO-SUBJECT` row: `docs/subject-resolution.md` forbids deriving a subject from `themes:`, so it is reported rather than dropped.

⚠️ **The conjunction is the whole point, in both directions.** A goal with no live session is never reported — 318 goals in the primary vault, a handful with live sessions — and a goal that *has* a record is never reported, even a lapsed or stood-down one. Those are `manager-liveness.py`'s `STALE` / `STOPPED` classes and belong to `docs/sweep-output.md`; folding them into "never armed" is the defect this reading ends.

⚠️ **`unknown` is a third answer, not `0`.** An unreadable session registry prints `unarmed goals: unknown` and exits non-zero. Print it as it comes back; never round it down to a number.

⚠️ **The line reports; it never arms.** Arming a manager is the operator's answer to a `start manager` card (`commands/steps/propose-manager.md`), never this reading's act — `docs/fleet-surface.md` bars a manager from starting one on its own initiative.

## The next-focus reading

Every tick takes this reading; the block below is rendered as **Output shape item 2**, not at the marker line:

    Next focus (3/30)
      1. Write the Gate Contract for ORB XAUUSD V8 … — <vault>/60 Periodic Notes/Weekly/2026-W43.md:65 — …
      2. ORB DE40 W43 Sunday Review … — <vault>/60 Periodic Notes/Weekly/2026-W43.md:79 — …
      left for the next round (cap 5)

Read it from `python3 $P/next-focus.py --vault "<vault>" --tasks-dir "25 Tasks"`; `--tasks-dir` is **vault-relative** (default `25 Tasks`) — the script joins it to `--vault`, so pass the bare directory name; an absolute path is used as-is rather than joined. ⚠️ **Pass `--live`/`--target` from the round's own capacity read** — the script otherwise re-runs `worker-sessions.py --count` and `worker-target.py`, and two independent reads can disagree inside one round. `--now <ISO>` pins the period. ⚠️ **Run it ONCE with `--json`**: that form carries both the rendered block (`render`) and the proposals' dedup keys and payloads (`proposed[].card_key`, `proposed[].payload`), so a card-posting round never needs a second invocation. **The script owns the parse, the tier order, the cap and the dedup key** — read the cap off the block it prints, never from this page, and never re-derive any of them at this call site. Read its header for the order and why it is what it is.

**It is the round's answer to "what should the free slot do?".** The sweep classifies live sessions and never reads the pages the operator writes to rank work, so without this reading a free slot goes to whatever was filed last. Measured 2026-10-21: `grep -rln 'Next focus'` over the repo returned nothing — the capability did not exist, so every round chose blind.

⚠️ **A `phase: todo` row renders `needs your approval` and carries no action at all** — not an empty one. Such a row is reported and opens nothing — no spawn, no resume, no nudge. The rule's single home is `agents/goal-sweep-reader.md` § step 4's *Waiting-approval* bullet, with `the Manager Session runbook` § Step 4 carrying the operator's copy.

⚠️ **The held line is not an omission.** At or above the target the block reads `Next focus: held — at target (<live>/<target>)`; an unreadable capacity store reads `held — workers unknown`. Both still print. A suppressed round and a round that found nothing must never render the same, and a fleet that cannot count itself is not an idle fleet — `unknown` is a third answer, never `0`.

⚠️ **Proposals are capped and the remainder is named.** **Every** item under `Proposed for this week's plan:` gets its own card — the cap is whatever the block's own `left for the next round (cap <n>)` line reports, so read it there, never from this page — each with its own stable `card_key` from the script (`proposed[].card_key` in `--json`), posted as **one card each — never a batch**: `python3 $P/attention-ask.py post --dedup-key "<that row's card_key>" --payload <that row's payload, verbatim and UNQUOTED> --context "proposed for this week's plan" --option "add it to the plan" --option "leave it out" --recommend "add it to the plan"`. ⚠️ **`payload` arrives already shell-quoted from the script — paste it bare, never inside `"…"`.** The text it carries is an alert row's raw `prometheus-alerts.sh` line, or a task/goal filename any worker can choose, so hand-assembling `"<name> — <why> — <file>:<line>"` lets a `"` break the quoting and a `$(…)` or backtick **execute** in this session's shell. Never rebuild the string at this call site. The rest print `left for the next round (cap <n>): <name>, <name>, … and <n> more` — **named, never a bare count**, so a dropped item cannot read as one the reading never found. ⚠️ **A firing prod alert is ranked FIRST and is never in that remainder** — it is the one proposal that is time-critical, and behind the P1 rows it was capped out entirely whenever three or more unplanned P1 tasks existed. 38 open `priority: 1` tasks existed on 2026-10-21, so an unbounded rule would have put 38 cards on the board in one round. ⚠️ **The card is an ASK, never an act** — no plan page is edited, no item added; the operator's answer is read back with `attention-ask.py poll <item-id>`.

⚠️ **`2` is a REFUSAL, never "nothing to do".** An unreadable vault or a bad `--now` exits 2 with the reason on stderr; an empty plan set exits 0 with `note:` lines. The two must not render the same.

⚠️ **The reading reports; it never opens.** Opening the approved candidate is the operator's answer to the card, executed through `/supervisor:open` — never this reading's act.
