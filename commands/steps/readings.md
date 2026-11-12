---
# Read by the router, never invoked — without this key Claude Code
# registers this file as /<plugin>:steps:<name>.
user-invocable: false
---

# Readings

When: every tick — take all seven readings (commands in the step)

## The plugin staleness reading — who is executing an old body

Every tick prints the fleet's spread across plugin versions:

    plugin-stale: 21 of 43 live (8 versions · installed 0.274.1)

Read the counts from the script's default render — `installed: <X> · live: <N> · stale: <S>` (plus `· unknown: <U>` when any live session carries no marker) — and the per-session rows from `python3 $P/plugin-version-census.py --json`, whose shape is `{"installed": …, "rows": [{"sid8", "name", "loaded", "state", "pane"}]}`. ⚠️ **`live` and `stale` are render counts, not JSON keys** — derive them from `rows[]` (`state == "stale"`) only if you need them programmatically, and never cite a key the payload does not carry.

⚠️ **This is the only reading whose observer is not its subject, and that is the whole point.** A rule that lives inside a step body cannot detect that its own body is stale: the session holding an old body is exactly the one that never prints the new rule. Measured 2026-11-10: eight plugin versions in flight across one fleet, with the in-body staleness rules shipped in every one of them — the fleet was still spread over all eight. So the fleet round reports the spread from a session that can see it, rather than asking each session to notice itself.

⚠️ **The lever is `--reload`, and this reading never runs it.** It types `/reload-plugins` into every stale session whose pane resolves — a production-touching act on live sessions, several of them mid-turn — so the round **reports the number and names the lever**; pulling it is the operator's call, not the round's.

⚠️ **`unknown` is never `stale`.** A headless worker and a cluster worker may hold no local `.in_use` marker, and reporting them stale would name a fix for a session the lever cannot reach. The script draws that line itself; never re-derive it here.

## The saturation reading — the ratio, not tok/s

Every tick prints the fleet working ratio beside the marker line:

    saturation: 3/6 (busy + shell over live sessions)

Read it from `python3 $P/fleet-board.py --json` → `saturation`, which carries `numerator`, `denominator` and `ratio`. The numerator counts the registry **status** (`busy` + `shell`), not the board's `running` bucket — the buckets apply a precedence, so a `busy` session holding an open gate is bucketed `needs-input` and would be dropped from a bucket-based count. `ratio` is `None` for an empty fleet; print it as absent, never as `0%`.

**The ratio is the signal, not tok/s.** Measured 2026-09-20 (Fleet Manager session `b700c650`): output throughput swung 629 → 905 tok/s in 45 minutes while the ratio moved only 44% → 42%. Throughput tracks whichever model happens to be mid-response at the moment of the read; the ratio does not. Print the tok/s figure beside it for context only — `~/.claude/scripts/claude-metrics.sh` reads `out_tok/s` — and never act on it as the number.

**Open Questions 1 and 2 — ANSWERED 2026-10-01, and neither answer is a ratio.** Both asked whether a *ratio threshold* should trigger a spawn. The operator's design of 2026-10-01 replaces the ratio as the capacity signal outright: the trigger is the fleet-wide **worker target** (`spawn.maxWorkerSoft`, whose single home is `${CLAUDE_PLUGIN_ROOT}/docs/fleet-surface.md` § Spawn a worker item 5 — **read the number there and never restate it here**), read as live workers against that number and printed as `workers: <live>/<target>` (Output shape item 1).

- **Open Question 1 is superseded rather than answered with a threshold.** No ratio value triggers a spawn, and none is to be inferred from this line. The ratio stays a *diagnostic* — it is what tells you the fleet is saturated, or human-queued — while the target is what decides whether a manager proposes work.
- **Open Question 2 is answered: capacity does drive action — but through the target, not through this ratio.** A manager tick below the target with no ready row proposes the next most important rows to approve (`commands/goal-loop.md` step 4). **This line itself stays read-only**, exactly as the measurement argued.

## The approved-not-started reading

Every tick prints this line with the marker line:

    approved, not started: 3 · oldest 7h35m (Build claude-interactive)
      never started: 1 → open · orphaned: 2 → reopen

Read it from `python3 $P/approved-not-started.py --tasks-dir "<vault>/<tasks-dir>"`. A row counts when its task-file frontmatter carries `approved_at` with a non-terminal `status` and no **live** `claude_session_id`. Rows waiting longer than one tick (**30 min, a script constant** — never a per-call flag, because two rounds disagreeing about the same row is the defect this line exists to remove) are listed beneath it with their class and their owning manager.

**The total is two populations, and the second line splits it.** *Never started* — no `claude_session_id` at all: nobody ever opened the row. *Orphaned* — a `claude_session_id` that names a session the registry does not hold live: the row was started and its worker died. Each is counted on its own predicate, never as the other's remainder, so the two sum to the total only while every row is classified — a mismatch is visible on the line rather than absorbed into a derived number (an unclassified row also renders as `unclassified` in the over-tick block); a row whose id is live counts in neither, because it is not in the total. Measured 2026-10-30: 81 of 122 rows were orphans, invisible as a class inside the single number. `--json` carries the same split as `never_started` and `orphaned`, and each row's `class`.

⚠️ **`orphaned` is not `adopt-orphans.py`'s population.** That script adopts the **live** workers an exited manager left behind; an orphaned row here is the inverse — the worker is gone and the task remains.

⚠️ **`unknown` is a third answer, not `0`.** If the session registry cannot be read the script prints `approved, not started: unknown` and `never started: unknown → open · orphaned: unknown → reopen`, and exits non-zero — *"no approved rows are waiting"* and *"I could not ask"* must not render the same (`docs/pane-reads.md` § The rule). Print it as it comes back; never round `unknown` down to a number.

⚠️ **The line reports; the open leg opens, and the reopen rule reopens.** This reading adds visibility, not authority. The arrows name the *kind* of lever each class needs, not which leg is guaranteed to act on a given row. The open leg — `commands/steps/open.md` (router row 16), fed by `scripts/open-queue.py` — selects the **flagged** rows of this reading, both classes, ranks them, and opens them through `/supervisor:open` up to the free slots, and the owning manager's where one is live; an unflagged row is skipped as `not flagged` and is never opened by this leg. For an orphaned row that open is a resume of its recorded session. The reopen rule — `commands/steps/keepalive.md` § Resume a worker whose session died, through `scripts/worker-keepalive.py`, which owns the resume cap and the crash-loop escalation — is the second path for orphaned rows, and it decides per row: a parked, deferred or manager-owned row returns `jump`, not a resume. A reading that opened or reopened anything itself would be the measure acting on its own measurement — and the flag gate, the rank, the guards and the cap are the legs' to apply, never this line's.

## The unarmed-goals reading

Every tick prints this line with the marker line:

    unarmed goals: 1 unarmed goal · Build Rapid Strategy Development Pipeline (2 sessions)

Read it from `python3 $P/unarmed-goals.py --vault "<vault>" --tasks-dir "<vault>/<tasks-dir>"`. A goal counts when it has **both** a live session — a non-terminal task whose `claude_session_id` or `metrics_sessions` id the registry reports alive — **and** no manager record under either the `goal--…cadence` or the pre-split `…cadence` name. A task carrying neither `goals:` nor `topics:` is listed as a `NO-SUBJECT` row: `docs/subject-resolution.md` forbids deriving a subject from `themes:`, so it is reported rather than dropped. ⚠️ **A dangling `goals:` link is not an unarmed goal.** The reader passes `--vault` to `manager-liveness.py --state`, which answers `UNARMED` only once the subject's page resolves; a name that resolves no page and no record is refused (exit 3) and drops out of this reading — a typo in a `goals:` field is a page defect, not a manager gap.

⚠️ **The conjunction is the whole point, in both directions.** A goal with no live session is never reported — 318 goals in the primary vault, a handful with live sessions — and a goal that *has* a record is never reported, even a lapsed or stood-down one. Those are `manager-liveness.py`'s `STALE` / `STOPPED` classes and belong to `docs/sweep-output.md`; folding them into "never armed" is the defect this reading ends.

⚠️ **`unknown` is a third answer, not `0`.** An unreadable session registry prints `unarmed goals: unknown` and exits non-zero. Print it as it comes back; never round it down to a number.

⚠️ **The line reports; it never arms.** Arming a manager is either the operator's answer to a `start manager` card (`commands/steps/propose-manager.md`) or a manager opening one through `/supervisor:open` (`docs/session-tiers.md` § The four tiers); this reading does neither. ⚠️ **This is about MANAGERS, and the open leg does not touch it.** Opening an approved *worker* row (router row 16) arms no manager: a goal with ready rows and no live manager gets its workers opened and its manager state unchanged — the operator's ruling of 2026-10-30 is explicit that auto-opening topic and goal managers is out, so "the round opens" and "the round arms" are different acts with different boundaries.

## The next-focus reading

Every tick takes this reading; the block below is rendered as **Output shape item 2**, not at the marker line:

    Next focus (3/30)
      1. Write the Gate Contract for ORB XAUUSD V8 … — <vault>/60 Periodic Notes/Weekly/2026-W43.md:65 — …
      2. ORB DE40 W43 Sunday Review … — <vault>/60 Periodic Notes/Weekly/2026-W43.md:79 — …
      left for the next round (cap 5)

Read it from `python3 $P/next-focus.py --vault "<vault>" --tasks-dir "25 Tasks"`; `--tasks-dir` is **vault-relative** (default `25 Tasks`) — the script joins it to `--vault`, so pass the bare directory name; an absolute path is used as-is rather than joined. ⚠️ **Capacity: the script reads it itself** (`worker-sessions.py --count`, `worker-target.py`), so a round reaching this reading first — it is router row 15, ahead of both the voice step at 17 and the render at 19 — passes nothing, and that is correct. `--live`/`--target` override it for a round that already holds the numbers (a later re-read of the table), and passing them is what keeps this block's head and Output shape item 1's `workers:` line from disagreeing. `--now <ISO>` pins the period. ⚠️ **It executes the vault's own `.claude/scripts/prometheus-alerts.sh`** — the operator's declared alert source, and the vault is worker-writable, so that is a deliberate trust boundary; a round that keeps its alert script outside the vault passes `--alerts-script <path>` and no vault content is executed. ⚠️ **Run it ONCE with `--json`**: that form carries both the rendered block (`render`) and the proposals' dedup keys and payloads (`proposed[].card_key`, `proposed[].payload`), so a card-posting round never needs a second invocation. **The script owns the parse, the tier order, the cap and the dedup key** — read the cap off the block it prints, never from this page, and never re-derive any of them at this call site. Read its header for the order and why it is what it is.

**It is the round's answer to "what should the free slot do?".** The sweep classifies live sessions and never reads the pages the operator writes to rank work, so without this reading a free slot goes to whatever was filed last. Measured 2026-10-21: `grep -rln 'Next focus'` over the repo returned nothing — the capability did not exist, so every round chose blind.

⚠️ **A `phase: todo` row renders `needs your approval` and carries no action at all** — not an empty one. Such a row is reported and opens nothing — no spawn, no resume, no nudge. The rule's single home is `agents/goal-sweep-reader.md` § step 4's *Waiting-approval* bullet, with `the Manager Session runbook` § Step 4 carrying the operator's copy.

⚠️ **The held line is not an omission.** At or above the target the block reads `Next focus: held — at target (<live>/<target>)`; an unreadable capacity store reads `held — workers unknown`. Both still print. A suppressed round and a round that found nothing must never render the same, and a fleet that cannot count itself is not an idle fleet — `unknown` is a third answer, never `0`.

⚠️ **Proposals are capped and the remainder is named.** **Every** item under `Proposed for this week's plan:` gets its own card — the cap is the number on the `left for the next round (cap <n>)` line that **follows the `Proposed for this week's plan:` rows** (the block also carries a `(cap <n>)` line for the ranked candidates, so take the one after the proposals), never from this page — each with its own stable `card_key` from the script (`proposed[].card_key` in `--json`), posted as **one card each — never a batch**: `python3 $P/attention-ask.py post --producer-kind manager --dedup-key "<that row's card_key>" --payload <that row's payload, verbatim and UNQUOTED> --context "proposed for this week's plan" --option "add it to the plan" --option "leave it out" --recommend "add it to the plan"`. ⚠️ **`payload` arrives already shell-quoted from the script — paste it bare, never inside `"…"`.** The text it carries is an alert row's raw `prometheus-alerts.sh` line, or a task/goal filename any worker can choose, so hand-assembling `"<name> — <why> — <file>:<line>"` lets a `"` break the quoting and a `$(…)` or backtick **execute** in this session's shell. Never rebuild the string at this call site. The rest print `left for the next round (cap <n>): <name>, <name>, … and <n> more` — **named, not a bare count**: the first five are listed and the rest are counted (`and <n> more`), so the operator can see what the cap is holding back rather than only that something is. ⚠️ **A firing prod alert is ranked FIRST and is never in that remainder** — it is the one proposal that is time-critical, and behind the P1 rows it was capped out entirely whenever three or more unplanned P1 tasks existed. 38 open `priority: 1` tasks existed on 2026-10-21, so an unbounded rule would have put 38 cards on the board in one round. ⚠️ **The card is an ASK, never an act** — posting it edits no plan page and adds no item. **The answer is consumed by `next-focus-answer.py`, which the round runs once per tick BEFORE this reading**: `python3 $P/next-focus-answer.py --vault "<vault>" --consume`. It applies every answered proposal card not yet consumed — `add it to the plan` writes **both** the `## ✅ Picked` row and the `### N.` block carrying `**Task:** [[<name>]]`, which is the only shape `parse_weekly` reads (measured 2026-10-27: the Picked row alone left the row proposed forever); `leave it out` writes a durable dismissal that `next-focus.py` then skips and reports in `dismissed[]` — and prints one line per consumption, or `next-focus consume: nothing to consume (0 answered card(s) awaiting; <n> open)`. ⚠️ **A refused consume (exit 2) is a REFUSAL, never a quiet round** — render it as `next-focus consume: refused — <reason>`, because a round that could not look and a round that found nothing must not look the same. ⚠️ **The script owns both records and their paths** — never hand-write a Picked row or a `### N.` block at this call site.

⚠️ **Any non-zero exit is a REFUSAL, never "nothing to do".** An unreadable vault or a bad `--now` exits 2 with the reason on stderr, and an unexpected failure is mapped to the same 2 rather than escaping as a traceback — render it as `Next focus: refused — <that reason>`, per Output shape item 2; an empty plan set exits 0 with `note:` lines. The two must not render the same.

⚠️ **The reading reports; it never opens.** What this block proposes is still the operator's to approve — the card is the ask, and `attention-ask.py` is how it is answered. Once a row carries that approval, opening it is the **open leg's** act (router row 16), never this reading's and never the card's: the card answers *what to approve*, the leg answers *which approved row opens now*, and collapsing the two would turn a proposal into a spawn.

## The service-guard reading

Every tick runs `python3 $P/service-guard.py --apply` and prints its lines. Unlike the four readings above, this one acts: a breach on a terminal owning row reopens it. The contract — config, instrument, the three states, and why it reopens but never restarts — lives in `${CLAUDE_PLUGIN_ROOT}/commands/steps/service-guard.md`.

## The keeper-census reading

Every tick runs `python3 $P/keeper-census.py --vault "<vault>"` and prints its block. It is
the reading that asserts the fleet's invariant — **every live session has exactly one
keeper** — against the contract in `docs/session-tiers.md` § Keep-alive, which is the single
home of the rule set. It reads and never acts: it resumes nothing, closes nothing, adopts
nothing.

```
keeper-census: <vault>
  legs: topic=yes goal=yes worker=yes · live=80 rows=405 · unrecorded=20 · cannot-tell=1
  RED unkept      goal-mgr   <goal>    — declaring topic(s) have no live armed manager: <topic> [STOPPED]
      live        topic-mgr  <topic>   keeper: fleet round
      not-live    worker     <task>    keeper: —
MISMATCH live session <id> (<name>) — no page records it
MISMATCH page "<task>" records session <id> — the registry does not hold it
  card: <item-id>
keeper-census: unkept=0 double-kept=0 sessions=80
```

⚠️ **Three answers, not two, and the count line carries only one of them.**
`unkept` / `double-kept` are the invariant broken by a row **a page records**. A live session
**no page records** is `unrecorded` — reported, named in the `MISMATCH` block, counted in the
header, and **never counted red and never carded**: it is outside the session tree, and
`docs/session-tiers.md` § Adopt or close says nothing about it (an operator-opened ad-hoc tab,
a drill, a worker that never stamped its page). Counting those as gaps made `unkept=0`
unreachable and fired the card every episode-day forever. `cannot-tell` is the third: a keeper
read that failed.

⚠️ **The count line is the block's last line, always** — the `card:` line sits above it, and
that line is what the goal's own criterion reads.

⚠️ **The exit code is three-way, and only one of them is a finding.** `0` clean; `1` at
least one red row — the reading's own result, **not** a refusal, so the round carries on and
prints the block; `2` the registry or the vault could not be read — including a vault with
no topic, goal or task directory, and any internal error — and the census says so on stderr
rather than reporting a fleet of zero. Render `2` as
`keeper census: refused — <reason>`; a refusal and a clean fleet must never print the same.

⚠️ **`cannot-tell` is never counted red.** A row whose keeper could not be read (an
unreadable loop probe, a page naming a manager page that does not exist) prints that state
with its reason and is counted in the header — a failed read is not a definite gap.

⚠️ **"Is the keeper running" is read only for manager keepers.** A `fleet round` keeper is the
round itself, running by construction — re-reading it would turn every fleet-kept row red
whenever the fleet loop idles.

⚠️ **The script posts its own card — the round posts none.** One card per red *episode*, its
dedup key carrying the episode's start and day: a second consecutive red round asks nothing, a
fleet that goes green and red again is asked again, and a fleet still red after 24 h is asked
again rather than left with an expired card. Do not re-post it, do not add an option to it,
and do not card individual red rows.

⚠️ **A red row is a report, never a work item for this round** — the task that owns the gap
fixes it.
