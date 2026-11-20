---
# Not a slash command — without this key Claude Code
# registers this file as /<plugin>:steps:<name>.
user-invocable: false
---

# Closure

When: every tick, after the sweep

Applies to the round's own subject and to every page in its tracked set. Step 0 runs on open
boxes every tick; Step 1 closes a page once every closure box reads `[x]` (or, for a goal,
once Step 0b's linked-tasks case has ticked the last one).

**A page closes on evidence. The loop may tick a box, but only with an inline citation it has
resolved itself this tick.**

Workers tick only their own task boxes, so a goal's or topic's own closure boxes have no other
owner. Without this step, the verb would refuse on every tick and a goal whose work has
shipped would stay open indefinitely. This step is the rule's single home; `commands/goal-loop.md`,
`commands/topic-loop.md`, `commands/topic-drive.md` and `commands/steps/round-tail.md` point here.

## Step 0 — tick open boxes that carry resolvable evidence

**Which pages each tier ticks** — a page is ticked only by its own owner:

| Tier | Ticks | Never ticks |
|---|---|---|
| goal | its subject goal; a tracked task only when this round's liveness reading for it is `ABSENT` (`LIVE` = the worker owns its boxes; `UNKNOWN` fails closed) | another goal |
| topic | its own topic page (`# Success Criteria` + `# Completion Gate`) | a member goal — that page is its goal manager's; a task |

On each open `[ ]` box of a page the tier ticks, tick it **only** with an inline citation of an
allowed evidence type that you resolved this tick:

| Evidence type | Citation | Resolve it with |
|---|---|---|
| merged PR | `owner/repo#N` | `gh -R owner/repo pr view N --json state --jq .state` reads `MERGED` |
| release tag | `owner/repo@vX.Y.Z` | `gh -R owner/repo release view vX.Y.Z` exits 0 |
| completed task (goal tier) | `[[Task Name]]` | `vault-cli task get "<task>" status --vault "<vault>"` reads `completed` |
| completed goal (topic tier) | `[[Goal Name]]` | `vault-cli goal get "<goal>" status --vault "<vault>"` reads `completed` |
| linked-task scan (goal tier, the linked-tasks box only) | `linked-task scan` + the `(loop): moved out` line's date, or `none open` | Step 0b step 1's scan, run this tick, finds no task still linked to the goal outside `completed`/`aborted` |

Write the tick as `- [x] <unchanged box text> — evidence: <citation> (loop, YYYY-MM-DD)`, an
inline citation on the same line. Never change the box's own text.

- **Where a citation may come from.** Only from a link already on the page (the box line, its
  `# Tasks` list, its `# Progress`, `# Results` or `# Pull Requests` section) or, on the goal tier,
  from a tracked task. Never search the wider vault or GitHub for something that might fit.
  The one exception is the linked-tasks box (Step 0b): its evidence is the loop's own scan,
  not a link.
- **The evidence must match the box's claim, not just exist.** A merged PR proves only the
  claim it implements. A `[[task]]` or `[[goal]]` proves a box only when its own Success Criteria
  restate that box's outcome. If you cannot match them, the box has no evidence.
- **Strip a wikilink alias** (`[[Name|alias]]` → `Name`) and run every page-sourced name,
  `owner/repo` and tag through `closure-name-safe.py` (Step 1 below, single-quoted) before it
  reaches a command. On exit 1, the box has no evidence.
- **No resolvable citation → leave the box `[ ]`.** Do not card a page whose work is still
  running, and never card the linked-tasks box: Step 0b produces its evidence. Once nothing
  tracked can still produce evidence — every tracked task (goal tier) or member goal (topic tier)
  reads `completed`, `aborted`, `hold` or `backlog` — and boxes are still unevidenced, post
  **one** notice, then record it on the page so later ticks do not re-post:
  ```bash
  python3 "${CLAUDE_PLUGIN_ROOT:-$HOME/.claude/plugins/marketplaces/claude-supervisor}/scripts/attention-ask.py" post \
    --mechanism ack --dedup-key "closure-evidence-<page-slug>" \
    --payload "<page>: all tracked work is terminal but <n> closure box(es) have no evidence: <box texts>" \
    --producer-id "<your session id>" --producer-kind manager
  ```
  Then append `- YYYY-MM-DD (loop): no evidence for <box texts> — notice <item_id>` to the page's
  `# Progress`. Post again only when the set of unevidenced boxes differs from the last such
  line. The operator ticks, rewords or files work on the page itself; the loop never reads an
  answer.
- **No tick on evidence you did not resolve this tick**, and no tick to make a close possible
  when the evidence is missing. Step 0b's linked-tasks case is not an exception to this: it
  creates the state the box describes (nothing open stays linked), then ticks on a scan of it.

## Step 0b — goal branch only: move open linked tasks out before the subject closes

Step 0b runs on the goal branch's **subject** goal when its closure boxes are all `[x]` after
Step 0, **or** when it is *linked-tasks-only* (defined below). Every task still linked to the goal that is not `completed`
or `aborted` (`next`, `todo`, `in_progress`, `hold`, `backlog`) is moved out before the verb runs.
Run it after the tracked set has been examined (see *What to do*), so a tracked task that itself
closed this round is closed under the goal, not moved out. The topic tier never runs 0b: a
member goal with open linked tasks is left to its goal manager.

⚠️ **Linked-tasks-only.** The goal template's DoD carries *"All linked tasks completed or moved
out with a recorded reason"*. That box stays `[ ]` while any task is linked, so without this case
a goal carrying it never closes and Step 0b never runs. A goal is linked-tasks-only when its
`# Success Criteria` section carries no open box **and** its `# Definition of Done` section carries
exactly one open box whose text contains `linked tasks completed or moved out` (case-insensitive)
— read those two named sections on the page directly; this is a targeted read of two sections,
not a whole-page count. For such a goal, run steps 1–4 below. Then, **only if none of them held
or failed**, re-run step 1's scan this tick. When it finds nothing non-terminal still linked, tick
the box as a `linked-task scan` (Step 0 table), with one of:
- `— evidence: linked-task scan, moved out YYYY-MM-DD (loop, YYYY-MM-DD)` when step 3 moved tasks
- `— evidence: linked-task scan, none open (loop, YYYY-MM-DD)` when there was nothing to move

Then run Step 1's verb — its exit is the verdict.

⚠️ **The verb refuses on a non-terminal linked task, which is why Step 0b runs first.** `vault-cli
goal complete` reads each task's `goals:` entry (`pkg/ops/goal_complete.go` `taskLinkedToGoal`,
normalising `[[<goal>]]` to the bare name) and `closureMemberItems` refuses while any linked task
is `next`, `todo` or `in_progress`. Step 0b unlinks the open ones before the verb runs, so the
goal can close; the verb itself is the gate that would otherwise refuse on them.

1. List the linked tasks yourself. Run every task name through `closure-name-safe.py`
   (single-quoted, Step 1 below). A name that fails holds the close: report it and close
   nothing this tick. Read each task's entries with
   `vault-cli task get "<task>" goals --output json --vault "<vault>"`. The JSON `value` is the
   list **comma-joined**, and goal names can contain commas, so never split it. Instead check
   which exact string — `[[<goal>]]` or `<goal>` — occurs in `value`, and use that string.
2. **A worker that may be on the task holds the close.** If a linked task has a
   `claude_session_id` and this round's liveness reading for it is `LIVE` or `UNKNOWN`, unlink
   nothing, report `closure: <goal> — CLOSE held by live/unknown [[<task>]]`, and close nothing
   this tick. A task with no `claude_session_id` was never started and does not hold.
3. Unlink each remaining one with the string found in step 1:
   `vault-cli task remove "<task>" goals "<that string>" --vault "<vault>"`. The remove verb
   matches the raw string, so `[[<goal>]]` and `<goal>` are different values. The task's own
   `status` is untouched; it is unlinked, not closed. **If any remove fails**, report it
   verbatim, still run step 4 for the tasks already removed, and close nothing this tick.
4. When step 3 moved at least one task, append one line to the goal's `# Progress`:
   `- YYYY-MM-DD (loop): moved out at close, goal criteria met — [[t1]] (<status>), [[t2]] (<status>) …`.
   It records the reason, and every task stays findable from the goal it left. Skip this step
   when nothing was moved.
5. Then run the verb (Step 1 below). Never `--force`.

## What to do

For the subject, then each page of the tracked set: Step 0, then Step 1. **Exception on the
goal branch:** when the subject goal is linked-tasks-only or its closure boxes are all `[x]` after
Step 0, hold its Step 1 until every tracked page has been examined. Then run Step 0b, then the
subject's Step 1.

1. **Run the tier's own verb and act on its exit code.** The verb is the single closure gate: it
   reads the same `# Success Criteria` + `# Definition of Done` boxes a goal closes on (a topic
   reads `# Success Criteria` + `# Completion Gate`), applies the grandfathered-DoD cutoff, and
   refuses on any open box or non-terminal member. Do not count boxes yourself — the verb's exit
   is the verdict.
   ```bash
   vault-cli task  complete "<task>"  --vault "<vault>"
   vault-cli goal  complete "<goal>"  --vault "<vault>"
   vault-cli topic complete "<topic>" --vault "<vault>"
   ```
   - **exit 0** → the page closed, or was already terminal (the verb is idempotent on a
     `completed`/`aborted` page). Report `closure: <page> — <tier> complete → closed`.
   - **non-zero exit** → the verb refused. Report its printed line verbatim
     (`cannot complete <tier>: N closure item(s) open: …`) and leave the page open. The verb's
     own open-item list is the authority — quote it, never paraphrase it into a count of your
     own.

   ⚠️ **Honour the page's recorded measurement before the verb.** A goal may carry an optional
   `## Final measurement` block (`- SC1 bar = … measured = … verdict = MET|NOT MET`). Read it
   first: if any criterion records `verdict = NOT MET`, do not run the verb — report that
   criterion and leave the page open. The verb counts boxes only and never reads this block, so
   without this read a goal whose boxes are all `[x]` but whose measurement misses a bar would
   close on boxes alone. A page with no block, or one whose verdicts are all `MET`, closes on the
   verb as normal.

   ⚠️ **`--vault` is not optional** — without it the write targets the default vault, so a
   subject in any other vault lands on the wrong page or nowhere at all. It is the same
   explicit-`--vault` contract `commands/steps/goal-resolve.md` carries for the manager's write.

   ⚠️ **Check the name first, with the script rather than with your own eye — and single-quote
   the check, which is the opposite of how the verb itself is quoted:**
   ```bash
   python3 "${CLAUDE_PLUGIN_ROOT:-$HOME/.claude/plugins/marketplaces/claude-supervisor}/scripts/closure-name-safe.py" '<name>'
   ```
   ⚠️ **The single quotes are the gate.** Inside `"…"` the shell expands `$`, a backtick and `\`
   **before** the script runs, so a name carrying `$(…)` executes on the way in and the script
   is handed the *result* and prints `SAFE` — the check would pass exactly the names it exists
   to refuse. Inside `'…'` all four reach it literally, which is what lets it exit 1. A literal
   `'` in the name closes the single-quoting: escape it as `'\''`, and treat a name needing that
   as one you do not complete.

   It exits 0 on a safe name and 1 on one carrying `$`, a backtick, `"` or `\` — the entire
   injection surface inside a double-quoted word. **On exit 1, do not run the verb** — report
   the name and stop. ⚠️ **The check is a script, not a convention:** this line runs unattended
   every tick, and a rule the model has to remember each time is not a gate. It is stricter
   than `commands/open.md:499` states, deliberately — it is the mechanism that mandate leaves
   unnamed.

   ⚠️ **The verb carries the whole closure rule; its refusal is the round's normal outcome on an
   open page.** `goal complete` and `topic complete` run the same `checkClosureGate` the task
   verb runs (`pkg/ops/closure_gate.go`), so a goal refusal names the same open boxes a task
   refusal does, and a goal whose boxes are all `[x]` closes on the verb — not on this step's own
   reading. A grandfathered goal (no DoD, `created` before `DOD_REQUIRED_AS_OF` = `2026-06-26`)
   closes too: the verb applies the cutoff. (Before vault-cli v0.199.0 the goal and topic verbs checked only their
   open-task gate; they now read Success Criteria and DoD / Completion Gate themselves, which is
   why the reader script this step used to run is gone.)

2. **Never pass `--force`.** A close that needs it is a close this step does not make.

## Only after the whole set is examined — ending the round

⚠️ **The per-page steps run over EVERY page before anything stops.** Ending the tick from inside the
loop would close the subject and quit before the tracked set was read, so the tasks a finished
goal owns would never be closed — and on the topic branch it would also preempt row 12, whose
tail (`commands/steps/round-tail.md`) owns the topic's post-close sequence: reconcile the
Status Summary → `/vault-cli:sync-progress` → stop the loop → `/vault-cli:session-close`.

Once every page in the set has been examined, and **only** if the SUBJECT itself closed (Step 1
exited 0 on it):

- **Goal branch** — report the verdict, record the stand-down with
  `python3 "${CLAUDE_PLUGIN_ROOT:-$HOME/.claude/plugins/marketplaces/claude-supervisor}/scripts/manager-liveness.py" --stop --subject "<subject>" --branch goal`,
  and end the tick without re-arming. A loop that keeps arming on a completed subject reports
  work where there is none.
- **Topic branch** — report the verdict and **hand off to the tail**: do not stop here, because
  the tail owns the reconcile, the sync and the session close. Let row 12 run its close
  sequence, which stops the loop itself.

⚠️ **A tracked page closing is not this** — a task or a member goal closing just leaves the
tracked set; only the subject ends the round.

⚠️ **`hold` is a pause, not a closure — and a manager stopped on one is not thereby
"correctly stopped".** `${CLAUDE_PLUGIN_ROOT}/commands/topic-spawn.md:50` states it for goals
(*"not `hold`, which is a pause, not a closure"*), and the consequence here is that a `hold`
subject never reports `CLOSE`, never authorises the `--stop` above, and never justifies an
armed loop being read as a defect. A `hold` subject is a **parked** round: it may resume
without anything being re-armed by hand, so the loop staying armed is correct. The error is
the opposite one — an armed loop on a subject that has already reported `CLOSE`, which the
goal branch above stops, because a loop still arming on a `completed` subject reports work
where there is none.

## Which pages this covers

| Branch | Examined | Contract |
|---|---|---|
| goal | the subject goal, then each task printed in `Tracked (N):` | SC + DoD |
| topic | the topic page; each goal in its `## Goals` list is read and routed to its goal manager, never closed here | topic page: SC + **Completion Gate** |

⚠️ **Both tiers reach this step through a router row, and that row is what makes the claim
true.** `commands/goal-drive.md` row 4c runs it on the goal branch; `commands/topic-drive.md`
row 4c runs it on the topic branch. A tier with no router row has no execution whatever its
arm says — which is why the topic row exists rather than the rule being restated in prose.

## What this step must never do

- **No tick without an inline citation** you resolved this tick (Step 0). A tick with
  no citation, or with one that does not resolve, is forbidden at every tier. **No tick on a page
  another owner holds** — a member goal on the topic tier, or a task whose liveness is not
  `ABSENT`. Such a task may still be *closed* here once its boxes read `[x]`. A member goal is
  never closed from the topic tier: it is routed to its goal manager, which runs Step 0b.
- **Never set `status: completed` directly.** The verb writes it; nothing else may.
- **Never close on a partial read.** A page whose DoD section is missing or empty is
  *unmeasurable*, not complete — report it as a gap. **The one exception is an absent DoD on
  a grandfathered goal**, which the verb closes by design (see the cutoff note in Step 1); an
  *empty* DoD section is still unmeasurable at every cutoff.
- **Never close a page whose open boxes are another task's work.** Report which boxes are open
  and whose they are; closure is not a way to dispose of them.

## Reporting

One line per page examined, and one for the round:

```
closure: <page> — ticked <box> — evidence: <citation>   → resolved this tick
closure: <page> — <n> unevidenced, work still running   → no notice yet
closure: <page> — <n> unevidenced, all work terminal    → notice <item_id>, recorded in # Progress
closure: <goal> — linked-tasks-only                     → Step 0b after the tracked set
closure: <goal> — moved out <n> open tasks              → listed in # Progress
closure: <goal> — ticked linked-tasks box — linked-task scan → Step 1 verb
closure: <goal> — CLOSE held by live/unknown [[<task>]] → nothing closed this tick
closure: <goal> — unlink of [[<task>]] failed: <line>   → nothing closed this tick
closure: <page> — <tier> complete → closed              → exit 0
closure: <page> — <tier> complete → REFUSED: <the verb's own line> → left open
```

⚠️ **A refusal is reported verbatim and leaves the page open.** It is not smoothed into
"closed", and it is not retried with `--force`.
