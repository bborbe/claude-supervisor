---
# Not a slash command — without this key Claude Code
# registers this file as /<plugin>:steps:<name>.
user-invocable: false
---

# Closure

When: every tick, after the sweep

Applies to the round's own subject and to every page in its tracked set. Step 0 runs on open
boxes every tick; steps 1–3 close a page once every closure box reads `[x]` (or, for a goal,
once Step 0b's linked-tasks case has ticked the last one).

**A page closes on evidence. The loop may tick a box, but only with an inline citation it has
resolved itself this tick.**

Workers tick only their own task boxes, so a goal's or topic's own closure boxes have no other
owner. Without this step, the reader would return `OPEN` on every tick and a goal whose work has
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
  `owner/repo` and tag through `closure-name-safe.py` (step 2 below, single-quoted) before it
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

Step 0b runs on the goal branch's **subject** goal when it reads `CLOSE`, **or** when it is
*linked-tasks-only* (defined below). Every task still linked to the goal that is not `completed`
or `aborted` (`next`, `todo`, `in_progress`, `hold`, `backlog`) is moved out before the verb runs.
Run it after the tracked set has been examined (see *What to do*), so a tracked task that itself
closed this round is closed under the goal, not moved out. The topic tier never runs 0b: a
member goal with open linked tasks is left to its goal manager.

⚠️ **Linked-tasks-only.** The goal template's DoD carries *"All linked tasks completed or moved
out with a recorded reason"*. That box stays `[ ]` while any task is linked, so without this case
a goal carrying it never reads `CLOSE` and Step 0b never runs. A goal is linked-tasks-only when the
reader prints `Success Criteria: … 0 open` and `Definition of Done: … 1 open`, **and** that one
open DoD box, read from the page outside code fences, contains `linked tasks completed or moved
out` (case-insensitive). For such a goal, run steps 1–4 below. Then, **only if none of them held
or failed**, re-run step 1's scan this tick. When it finds nothing non-terminal still linked, tick
the box as a `linked-task scan` (Step 0 table), with one of:
- `— evidence: linked-task scan, moved out YYYY-MM-DD (loop, YYYY-MM-DD)` when step 3 moved tasks
- `— evidence: linked-task scan, none open (loop, YYYY-MM-DD)` when there was nothing to move

Then re-read with the reader and continue to the verb on `CLOSE`.

⚠️ **Do not rely on the verb to catch these tasks.** `vault-cli goal complete` compares each
task's raw `goals:` entry with the goal name (`pkg/ops/goal_complete.go` `taskLinkedToGoal`).
The usual link shape `- '[[<goal>]]'` never equals the bare name, so the verb passes over those
tasks without refusing. Checked 2026-11-15 against vault-cli source.

1. List the linked tasks yourself. Run every task name through `closure-name-safe.py`
   (single-quoted, step 2 below). A name that fails holds the close: report it and close
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
5. Then run the verb (step 2 below). Never `--force`.

## What to do

For the subject, then each page of the tracked set: Step 0, then steps 1–3. **Exception on the
goal branch:** when the subject goal reads `CLOSE` or is linked-tasks-only (Step 0b), hold its
step 2 until every tracked page has been examined. Then run Step 0b, then the subject's step 2.

1. **Read it with the reader script — never a whole-page count:**
   ```bash
   python3 "${CLAUDE_PLUGIN_ROOT:-$HOME/.claude/plugins/marketplaces/claude-supervisor}/scripts/goal-closure-read.py" --kind goal  "<path to the goal page>"
   python3 "${CLAUDE_PLUGIN_ROOT:-$HOME/.claude/plugins/marketplaces/claude-supervisor}/scripts/goal-closure-read.py" --kind task  "<path to the task page>"
   python3 "${CLAUDE_PLUGIN_ROOT:-$HOME/.claude/plugins/marketplaces/claude-supervisor}/scripts/goal-closure-read.py" --kind topic "<path to the topic page>"
   ```
   ⚠️ **The argument is a page PATH, not a name** — the reader opens it as a file and exits 2
   with `cannot read` on anything else. The resolution step prints the subject's path
   (`Branch: goal (24 Goals/<Goal>.md)`), so take it from there. A name from `Tracked (N):`
   resolves to `<goals-dir>/<name>.md` or `<tasks-dir>/<name>.md` by the same **exact
   basename, case-insensitive** match the resolution step uses — never a substring glob, and
   never a hand-built join that misses a filename typed in a different case.
   ⚠️ **`--kind` is not cosmetic — the tiers close on different evidence.** A goal and a
   task close on `# Success Criteria` + `# Definition of Done`; a **topic closes on
   `# Success Criteria` + `# Completion Gate`** (`docs/session-progress.md:37`,
   `docs/sweep-output.md:95` — a topic is the only level carrying both sets). Measuring a
   topic against a DoD it does not have reads UNMEASURABLE forever; measuring it against the
   SC alone and ignoring the Gate closes it on the wrong evidence. A Gate mixing checkboxes
   with an ordered list is **out of compliance**, and the reader refuses it.

   It prints the frontmatter `status`, each section's done/open split, and a verdict, and
   exits 0 only on `CLOSE`:

   | Verdict | Meaning | Action |
   |---|---|---|
   | `TERMINAL` | the page already reads `completed`/`aborted` | **skip it** — nothing to close |
   | `UNMEASURABLE` | a section is absent, or carries no recognised box | report the gap, close nothing |
   | `OPEN` | at least one box is open — for `--kind task`, anywhere on the page (`Whole page` line), since `vault-cli task complete` refuses on any open box | report which, close nothing — except a subject goal that is linked-tasks-only, which goes to Step 0b |
   | `CLOSE` | both sections present, every box `[x]` — or an absent DoD on a grandfathered goal (see below) | step 2 |

   ⚠️ **The `Whole page` line prints on every `--kind task` run, `0 open` included.** It is
   the verb's own gate, not a section tally: `vault-cli task complete` refuses on any open
   box anywhere on the page, `# Tasks` included. `Whole page (task complete's gate): 0 open`
   is the **passing** reading. The line's absence from a `--kind task` output means the run
   never reached the tally — it never means the gate does not apply.

   ⚠️ **`TERMINAL` is what stops a page being re-closed every tick.** The verb writes
   `status: completed` and leaves every box at `[x]`, so a page closed last tick satisfies the
   same predicate this tick — without this guard the round reports a close attempt forever.

   ⚠️ **`UNMEASURABLE` is not complete.** The predicate is *every box reads `[x]`*, which a
   section holding no box satisfies vacuously — that is how an empty `# Definition of Done`
   reached the verb. An absent section and an empty one are the same answer: refuse.

   ⚠️ **A grandfathered goal is the one exception, and it is narrow.** The vault requires a
   `# Definition of Done` only on goals created on or after **`DOD_REQUIRED_AS_OF` =
   `2026-06-26`**; a goal created before that date, or carrying no `created` at all, is
   **grandfathered** — an absent DoD is an accepted gap, not a violation. (A page whose
   frontmatter does not parse at all is **refused**, not grandfathered: the reader cannot
   tell a missing `created` from an unreadable page, so it fails closed.) Without the
   exception this rule made every grandfathered goal permanently unclosable: closable by
   every other reader in the vault, unclosable by the one that actually closes it. The
   reader applies it, so an absent DoD on a grandfathered goal reads `CLOSE` and this step
   closes it normally.
   The exception covers **absence only** — a DoD heading that exists but holds no box is
   still `UNMEASURABLE` at every cutoff — and **goals only**: a task's DoD requirement is
   not part of that decision, and a topic closes on its `# Completion Gate`, which has no
   cutoff. The date is the same constant **the vault's** `agents/goal-auditor.md` § 12 carries —
   not a path in this repo, which ships no `goal-auditor.md` — and it is restated here, in
   `scripts/goal-closure-read.py`, in the CHANGELOG and in that agent, because a plugin script
   cannot import from a vault agent; change them together. On such a goal the
   reader prints `Definition of Done: ABSENT — grandfathered (created <date> < 2026-06-26)`
   where it would otherwise print `MISSING`, so a close it now permits is never mistaken for
   a section it failed to find.

2. **Run the tier's own verb, `--vault` passed explicitly, and quote it verbatim:**
   ```bash
   vault-cli task  complete "<task>"  --vault "<vault>"
   vault-cli goal  complete "<goal>"  --vault "<vault>"
   vault-cli topic complete "<topic>" --vault "<vault>"
   ```
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

   ⚠️ **The verb's own gate differs by tier, and only the task tier's covers checkboxes.**
   `vault-cli task complete` refuses on any open box anywhere on the page, so for a task the
   verb's verdict is the authority and a disagreement means this step's read was stale.
   `vault-cli goal complete` re-checks **only** its open-task gate and reads no success
   criterion at all, so for a goal **this step's read is the authority**: the verb can only
   disagree by refusing on a linked open task — a surface this step does not measure — and a
   close it accepts is no evidence that any criterion was checked. `vault-cli topic complete`
   carries no gate at all, so the same holds there.
   Measured 2026-11-09: on a scratch goal with an open `# Success Criteria` box, `goal complete`
   printed `✅ Goal completed` and wrote `status: completed` (exit 0); on a scratch task
   carrying the same open box, `task complete` exited 1 with `Error: incomplete subtasks`.

3. **Never pass `--force`.** A close that needs it is a close this step does not make.

## Only after the whole set is examined — ending the round

⚠️ **Steps 1–3 run over EVERY page before anything stops.** Ending the tick from inside the
loop would close the subject and quit before the tracked set was read, so the tasks a finished
goal owns would never be closed — and on the topic branch it would also preempt row 12, whose
tail (`commands/steps/round-tail.md`) owns the topic's post-close sequence: reconcile the
Status Summary → `/vault-cli:sync-progress` → stop the loop → `/vault-cli:session-close`.

Once every page in the set has been examined, and **only** if the SUBJECT itself reported
`CLOSE`:

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
  a grandfathered goal**, which reads `CLOSE` by design (see the cutoff note in step 1); an
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
closure: <goal> — ticked linked-tasks box — linked-task scan → re-read, verb on CLOSE
closure: <goal> — CLOSE held by live/unknown [[<task>]] → nothing closed this tick
closure: <goal> — unlink of [[<task>]] failed: <line>   → nothing closed this tick
closure: <page> — <n> open (SC <a>, DoD <b>)            → not a candidate
closure: <page> — TERMINAL (already <status>)           → skipped
closure: <page> — UNMEASURABLE (<which section>)        → gap reported
closure: <page> — 0 open → <tier> complete → <verdict>  → closed
closure: <page> — 0 open → <tier> complete → REFUSED: <the verb's own line>
```

⚠️ **A refusal is reported verbatim and leaves the page open.** It is not smoothed into
"closed", and it is not retried with `--force`.
