---
# Read by the router, never invoked — without this key Claude Code
# registers this file as /<plugin>:steps:<name>.
user-invocable: false
---

# Keep alive — the topic managers

When: every tick

⚠️ **This leg acts, and a read-only execution is not a run of it.** Both halves restart, resume,
record the resume and hand over the pane; a run that only prints `unactioned: <n>` has measured
nothing. It is one of the round's two acting legs, so the round's delegate rule carves it out —
a sub-agent may run it only with write tools (`commands/fleet-drive.md`, the delegate rule's
carve-out).

⚠️ **Gate this leg on its own debt — the `re-arm` step — and chain it, do not merely read it:**

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/manager-receipt.py" --debt --subject "Fleet Manager" --branch fleet --only re-arm \
  && <run this leg>
```

Exit **12** = the arm is still owed: print the `debt:` lines, skip this leg, and **carry on with the round — the tail still runs**, because the tail's `--check` is what reports the round's verdict to the operator. Exit **0** = no debt this leg consumes, run it. ⚠️ **Exit 2 = the gate could not read or write the debt file** — it prints nothing to stdout, only a reason to stderr; skip the leg and print that reason, never read 2 as "no debt". ⚠️ **`--debt` judges the round in progress**, re-reading the receipt, so a round that already armed is not blocked by a debt it has paid. ⚠️ **The exit code is the gate, never the printed line** — a leg that reads only the text is satisfied by an empty read.

⚠️ **Why `--only re-arm`, and not the whole round's debt.** None of this leg's five procedures reads the digest — they read `topic-keepalive.py`, `topicless-goal-keepalive.py`, `park-event.py`, `park-nudge.py` and `worker-keepalive.py`. Gated on the whole round, the leg was stopped by a step it cannot consume whenever the round reached row 5b with the sweep reader (row 2) still running in the background: measured 2026-11-05 on the Fleet Manager, 2 of 25 gate calls printed `debt: digest` and exited 12 (03:13:29, 06:57:39 — the second one's digest recorded at 07:05:35, after the leg). `--only` scopes the exit code, never the record: the debt file still carries the digest, so `--check` at the tail and the unscoped legs (`drive.md`, `flagged-open.md`) are unchanged. This narrows the 2026-10-31 trade — *a topic-manager resume is not urgent enough to outrank the step the round owes* — to the one step this leg needs: a round that has not armed still skips it, and a round whose sweep is merely in flight no longer does.

## Restart a lapsed topic manager

The goal tier has had a keep-alive since `topic-drive.md`'s § Keep alive; the topic tier has
had none, so a lapsed topic manager simply stayed lapsed and nothing reported it. This leg is
the missing rung: **restart is not open**, so resuming a topic manager that already owns its
subject decides nothing new (`docs/session-tiers.md` § The four tiers, rule 1).

**Population — every topic page carrying a session id.** Read the vault's topic
directory, not the roster: a topic manager whose session died is absent from the roster, which
is exactly the row this leg exists to recover. ⚠️ **Resolve the directory from the vault's own
config, never assume `23 Topics`** — a renamed directory would glob nothing and the leg would
report a clean round, the exact silent failure it exists to remove. ⚠️ **Match the config entry by the vault's PATH (`resolve-folders.py --vault`), never take the first entry carrying the key** — on the operator's machine the first `goals_dir` is `private-starcitizen`'s `23 Goals` and the first `tasks_dir` is `seibert-agent`'s `tasks` (measured 2026-11-08), so a first-match read refuses or globs nothing for private-personal on every round. Every section below resolves its folders the same way, requiring only the keys it reads, and an `UNKNOWN` answer records that section not run. The folders come back **absolute**, so glob `"$TOPICS"/*.md`, never `"<vault>/$TOPICS"`. `<vault>` must be the vault's configured root path — `vault-cli config list` shows it — not a symlink alias:

```bash
P=${CLAUDE_PLUGIN_ROOT:-$HOME/.claude/plugins/marketplaces/claude-supervisor}/scripts
IFS='|' read -r TOPICS GOALS TASKS _ < <(python3 "$P/resolve-folders.py" --vault "<vault>" --require topics_dir)
case "$TOPICS" in UNKNOWN*) echo "folders: $TOPICS"; exit 2;; esac   # record this section not run
for f in "$TOPICS"/*.md; do   # absolute — resolve-folders.py prints full paths
  name=$(basename "$f" .md)
  grep -qE '^claude_session_id: *[^ ]' "$f" || continue
  printf '%s\t' "$name"
  python3 "$P/topic-keepalive.py" --vault "<vault>" --subject "$name" --branch topic --dir "$TOPICS" | head -2 | tr '\n' '\t'
  echo
done
```

**Act on the verdict, exactly as the goal tier does.** `restart` → run the printed
`/supervisor:restart <session-id>`, then record the resume **once `session-liveness.py --check`
shows the id live** (`topic-keepalive.py --vault "<vault>" --subject "<name>" --branch topic
--record-resume <id>` — `--subject` is `required=True`, and omitting it makes argparse exit 2
so the cap silently never arms); `resume` → the
session is `ABSENT`, so open it with `/supervisor:open --topic "<name>"` and record the resume
the same way; `jump` → hand the operator the pane; `leave` → a deliberate stand-down, do
nothing; `cannot-tell` → report it and never act.

⚠️ **Never a fresh spawn while a recorded id exists.** A second manager on one topic is the
double-writer failure; `resume` is the only route and `restart` resumes in place.

⚠️ **The cap counts only resumes that took.** `--record-resume` refuses an id the instrument
does not read `LIVE`, so a resume that never landed does not arm the cap against its own
retry. The fourth resume inside 30 minutes prints `⚠️ MANAGER CRASH-LOOP: <topic>` — stop
restarting that subject for the round, say so, and let a later round retry.

⚠️ **`STOPPED` is not a lapse.** `topic-resolve.md` § 4c writes that marker for a deliberate
stand-down on a non-`execution` subject, and this leg must not undo it — restarting one would
turn a refused arm into a restart loop that still runs a round every tick.

⚠️ **`SLEEPING` is not a lapse either, and it is the state this leg would otherwise create.**
A date-gated subject waits on its own earliest `defer_date` with a deliberately frozen
cadence, so it reads as a fault to every signal this leg uses. `topic-keepalive.py` returns
`leave` for it, and this leg must do the same: a restart spends a full-context round to
rediscover that nothing is due, and a hand `--arm` would clear the marker and un-sleep it.
The fallback wake needs no branch here — once the instant passes the marker stops
suppressing anything, the subject reads `STALE`, and the ordinary restart path resumes it.

**Record the count — in the digest, not only in chat.** `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/fleet-legs.py" --digest "<this round's digest>" --leg lapsed-managers --count <n>`, with `<n>` the topic rows this leg found and did not act on — the `# Regression metric` on the task that installed this leg reads it, and a round that prints the number without moving it is the defect it exists to remove. The line's format, its not-run form and the dead-worker floor have their **single home in `commands/steps/output-shape.md` item 14** — read it there and never restate it here. ⚠️ **The reading must land in the digest, which is the file that outlives the round.** A count spoken into chat and nowhere else dies with the round: measured 2026-11-06, a real 69-line digest over 24 sessions carried **zero** `unactioned:` lines while this file instructed the round to print them.

## Restart a lapsed goal manager whose goal no topic declares

`docs/session-tiers.md` assigns the fleet manager *"every topic manager, and every goal with
no topic"*. The goal-branch keep-alive runs inside a topic manager's round
(`agents/topic-drive.md`), so before this section a goal manager whose goal no topic
declares was kept by **nobody**. Measured 2026-11-08 on private-personal: 29 non-terminal
goals carried a manager id and no topic declared any of them. This is the rung for that
population, and it is the same restart as above. **Restart is not open.**

**Population: `scripts/topicless-goal-keepalive.py` selects it, and also decides each verdict.**
It takes every goal page at `status: in_progress` + `phase: execution` that carries a
`claude_session_id` and that no topic page's `## Goals` list names (`crashed-managers.py`
`fleet_keeps`, one home). For each one it asks `topic-keepalive.py --branch goal` for the
verdict. It never acts and never writes the cap.
- ⚠️ **The phase filter is not optional.** `topic-keepalive.py` reads liveness and cadence
  only, so on its own it answers `resume` for a backlog goal. Measured 2026-11-08: 5 of the
  8 `resume` verdicts were outside `execution`.
- ⚠️ **A goal a topic declares is never selected, even while that topic's manager is down.**
  The section above restarts the topic manager, and the topic manager restarts its goal
  managers. Taking the goal here as well makes a second keeper, which is a double writer.

```bash
P=${CLAUDE_PLUGIN_ROOT:-$HOME/.claude/plugins/marketplaces/claude-supervisor}/scripts
IFS='|' read -r TOPICS GOALS TASKS _ < <(python3 "$P/resolve-folders.py" --vault "<vault>" --require goals_dir,topics_dir)
case "$TOPICS" in UNKNOWN*) echo "folders: $TOPICS"; exit 2;; esac   # record this section not run
python3 "$P/topicless-goal-keepalive.py" --vault "<vault>" --goals-dir "$GOALS" --topics-dir "$TOPICS"
```

Output:
- `selected: <n>`
- one `<goal>\t<verdict>\t<command>\t<reason>` row per goal
- `actionable: <n>`

Exit **2** means REFUSED: an unreadable directory, page or helper. Record it as not run (see
below). A refusal is never "nothing to resume".

**Act on the verdict exactly as the topic tier does.** Run the row's command column:
- `restart` → `/supervisor:restart <id>`.
- `resume` → `/supervisor:open --goal "<name>"`.
- After either, once `session-liveness.py --check` shows the id live, record the resume with
  `topic-keepalive.py --vault "<vault>" --subject "<name>" --branch goal --record-resume <id>`.
  That write is the 3-per-30-minute cap.
- `crash-loop` → print its `⚠️ MANAGER CRASH-LOOP` line and stop on that goal for the round.
- `jump` → hand the operator the pane.
- `leave` → nothing.
- `cannot-tell` → report it, never act.
The command column is `-` for every verdict that runs nothing.

⚠️ **Not an open.** A topic-less goal with no recorded id is not in the population. Opening
it is `commands/steps/flagged-open.md`'s job, and only when the goal is flagged.

**Record the count in the digest.**
`python3 "${CLAUDE_PLUGIN_ROOT}/scripts/fleet-legs.py" --digest "<this round's digest>" --leg topicless-goal-managers --count <n>`
- `<n>` is the rows that needed action and got none: every `crash-loop` and `cannot-tell`
  row, plus each `resume`/`restart` whose command failed. The script's own `actionable:`
  line counts only `resume`/`restart` rows — the work handed to the round — so the two
  numbers differ by design.
- On exit 2, write `--leg topicless-goal-managers --not-run "<reason>"` instead, so the other
  keep-alive readings stay intact.
- The line's format has its single home in `commands/steps/output-shape.md` item 14.

## Resume the park watches whose re-check has passed

A parked worker is not a dead one: it exited on purpose, on a clock. Its re-check
instant lives in `defer_date`, written by `park-event.py register` at park time and
honoured by the readers, so the task resurfaces on its own even if this step never
runs. What the store adds is the **event key**: which waits are the same wait, and
which sessions are already parked on one.

```bash
P=${CLAUDE_PLUGIN_ROOT:-$HOME/.claude/plugins/marketplaces/claude-supervisor}/scripts
python3 "$P/park-event.py" due
```

One line per watch whose time has passed, naming **the task** for every session
parked on it — `register --task` recorded the name for exactly this read, since
`/supervisor:open` opens a task and a bare session id leaves the round nothing to
open. A watch with several sessions is still **one line, one watch**: that is the
dedup `register` created, and resuming it is one act per named task, not one per
watch.

Resume each named task through `/supervisor:open --task "<name>"`, then drop the
member with `park-event.py release --event "<key>" --session "<id>"` — the `<id>` in
brackets is the **full** session id, which `release` compares verbatim; `register`
and `list` print only the first eight characters, so take it from the `due` line or
from `due --json`. The file is removed when the last member leaves. A line reading
`CORRUPT <file>` is a watch holding memberships that cannot be read — report it,
never treat it as empty.

**A watch that comes due and cannot be resumed is re-parked, not left due.** The
event has not fired, so bump the instant — and ⚠️ **always pass `--session <parked-id>`
and `--repark`**, never a bare re-register:

```bash
python3 "$P/park-event.py" register --task "<name>" --event "<key>" \
  --recheck "<new ISO-8601>" --session "<parked-id>" --repark
```

`register` adds whatever `--session` resolves to as a member, so a bare call from the
round would park the **round** on the watch and the next `due` would name it as a
session to resume. `--repark` does two things: it **moves `recheck_at` forward** to
the instant given (the earliest-wake rule that governs a plain join does not apply —
otherwise the watch stays due and re-fires every round), and it increments the
counter `due` prints. At **three** re-parks it stops and routes the wait to the
manager as a criterion challenge; the count is persisted on the record because the
parked session has exited and nothing else survives to remember it.

⚠️ **This half is the only path that resumes automatically — keep `defer_date` set
anyway.** The task's own `defer_date` resurfaces the row in every manager's open
queue, but surfacing is not resuming: no reader of that queue starts the parked
session, so a round that misses this step **strands** the worker until someone acts by
hand. Measured 2026-11-05: after ~12 rounds that never entered this step, `due --json`
listed 24 overdue watches, the oldest past its re-check by ~52 h, each with a past
`defer_date`. Keep `defer_date` set so the row stays visible, never as a reason this
step can be skipped.

## Nudge a watcher past the park threshold

The park rule is always loaded and was measured ignored: a session sat `watching` 31 min
while a 12-min one beside it was legal. `agents/fleet-drive.md` excludes
`parked-on-watcher` rows from nudge candidacy on purpose, so no other leg reaches this
population. This leg is the enforcement point; `scripts/park-nudge.py` holds the
selection, the suppression and the text, so the round runs it and does not re-derive it.

```bash
P=${CLAUDE_PLUGIN_ROOT:-$HOME/.claude/plugins/marketplaces/claude-supervisor}/scripts
python3 "<vault>/.claude/scripts/session-activity.py" --json | python3 "$P/park-nudge.py"
```

⚠️ **The instrument is vault-local, not part of this plugin.** If
`<vault>/.claude/scripts/session-activity.py` does not exist, print
`park-nudge: unknown — instrument absent` and skip the leg. Never render that as
`0 nudged`. Exit **2** prints `park-nudge: unknown — …`; render it verbatim.

**Act on each `NUDGE <session-id> quiet <N>m (<name>) :: <message>` line.** The field name
is historical — **`N` is the watching-state duration, not transcript silence** (see the
selection rule below). Send the text after `::` with
`SendMessage(to: <the roster name for that session id>, message: <text>)`. ⚠️ **Do not
apply the drive leg's busy/`shell` skip here.** A `watching` row is
`shell` because its watcher is running, and that watcher is what the nudge targets. A
send that fails, or a session with no roster name (headless, pane-less), is reported as
`undelivered: <id8>`. The ledger already counts it as nudged, so it is not retried every
tick. Never park, kill or exit the session yourself: the nudge asks, and the session acts.

The selection is `bucket == watching` AND `watching_min` strictly above
`park-event.py threshold`. `watching_min` is the span from the first `🟡 WAITING`
closer of the session's **current watching episode** to now, published by
`session-activity.py`; `gated`, `working`, `idle`
and `unclassified` rows never qualify, whatever their duration. ⚠️ **`quiet_min` must never
be substituted for it** — a watcher re-arm writes to the transcript, so a session parked
3 h 34 m read `quiet 12m` and was never nudged, which is the defect this selection exists
to remove. Suppression is one nudge per watching episode, keyed on the session id
**and the episode's first-closer timestamp (`watching_since`)** — never on the duration,
which grows every round, and never on the duration merely dropping, because a round can
land after one park ended and the next began. Leaving the bucket, leaving the fleet, or a
different `watching_since` re-arms it.

**Report the line.** Print the `park-nudge:` summary verbatim
(`<n> nudged · <s> suppressed · <w> within 15m · <m> watching · <l> live`) beside the
four counts that `commands/steps/output-shape.md` item 14 renders. ⚠️ **It is not one of
them** — item 14's block is the digest's `LEGS` section, which admits exactly four
`unactioned:` lines, so a `park-nudge:` line printed inside it would fail that item's own
`--check`.

## Resume a worker whose session died

⚠️ **This reverses `agents/fleet-drive.md`'s *"orphan → escalate, never nudge"*.** That note
made a dead worker the operator's problem every round; rule 1 says a crashed or lapsed worker
is restarted automatically, and a worker that already owns its task decides nothing new by
being resumed. The escalation stays for a worker that **cannot** be resumed (no recorded id,
a spent cap, an undecidable probe) — it is no longer the disposition for every orphan.

**Population — every task page carrying a non-empty `claude_session_id`.** The script narrows it: a terminal status, a parked one (`hold`/`blocked`, or `human_review`/`todo`/`done` phase), a future `defer_date`, no open boxes, and a live owning goal manager all return `jump`. Any other non-terminal status (`next`, for one) with an open box **is** resumed — the guard is the parks above, not `status: in_progress`. Run
the script per task; it decides, it never resumes. ⚠️ **Filter terminal pages in the shell before the call.** The script would answer `jump` for them anyway, but each call costs a process start (~0.7 s). Measured 2026-11-08 on private-personal: 4,172 id-carrying task pages, of which 288 are non-terminal. Unfiltered, the scan costs ~44 min per round. ⚠️ **Resolve the task directory from the
vault's config, never assume `25 Tasks`:**

```bash
P=${CLAUDE_PLUGIN_ROOT:-$HOME/.claude/plugins/marketplaces/claude-supervisor}/scripts
IFS='|' read -r TOPICS GOALS TASKS _ < <(python3 "$P/resolve-folders.py" --vault "<vault>" --require tasks_dir,goals_dir)
case "$TOPICS" in UNKNOWN*) echo "folders: $TOPICS"; exit 2;; esac   # record this section not run
for f in "$TASKS"/*.md; do   # absolute — resolve-folders.py prints full paths
  grep -qE '^claude_session_id: *[^ ]' "$f" || continue
  # Terminal pages answer `jump` in the script anyway. Skipping them here saves a process each.
  grep -qE '^status: *["'\'']?(completed|aborted)' "$f" && continue
  name=$(basename "$f" .md)
  printf '%s\t' "$name"
  python3 "$P/worker-keepalive.py" --vault "<vault>" --task "$name" --dir "$TASKS" --goals-dir "$GOALS" | head -2 | tr '\n' '\t'
  echo
done
```

**Act on the verdict.** `resume` → `/supervisor:open --task "<name>"`, then record the resume
**once `session-liveness.py --check` shows the id live** (`worker-keepalive.py --vault "<vault>" --task "<name>"
--record-resume <id>` — `--vault` is `required=True`, and omitting it exits 2 so the cap never arms); `jump` → ⚠️ **read its second line before acting** — the script prints `jump` for a live session, a terminal task, a parked one, a deferred one, one with no open boxes, and one whose goal has a live manager; only the live case has a pane, so render the reason rather than the pane action; `crash-loop` → stop resuming that
task and escalate once with the `⚠️ WORKER CRASH-LOOP` line; `cannot-tell` → report it and
never act.

⚠️ **This is a resume, not a fresh spawn.** `/supervisor:open --task` resumes the recorded id;
a second worker on one task is the double-writer failure. A task with **no** `claude_session_id`
prints `cannot-tell` and is reported — it is not opened here, because opening it is rule 3's
approved-work path, not this leg's.

⚠️ **Three worker mechanisms, three different populations — never merge them.**
`scripts/restart-worker.py` restarts a **live, idle** session whose loaded code is stale (it
refuses `busy-target` and `headless-target`); `scripts/stuck-heal.py` heals a **stuck** headless
worker whose permission channel died while the session lives; this leg resumes a worker whose
session is **gone**. Neither of the other two applies to a dead session, which has no pid left
to kill and no channel left to reopen.

⚠️ **`agents/goal-drive.md`'s orphan gate covers the same population, and `worker-keepalive.py`
enforces the exclusion itself rather than leaving it to this prose.** That gate runs on the
goal tier's tick with its own `last_auto_resume` cap; this leg runs on the fleet tick with the
`worker--<slug>` cap. Both caps record only **after** a resume the instrument confirms `LIVE`,
so a check-then-act race would let two resumes land before either cap arms. The script therefore
scans the task's frontmatter for wikilinks and, if any page one names has a session id
(`claude_session_id`) the liveness instrument reads `LIVE`, returns `jump` with *"goal manager <id> is live and owns this
task"* — the fleet leg never acts on a row the goal tier owns. This leg takes only the rows
whose goal has no live manager, the same fallback `docs/fleet-surface.md` § Which manager owns a
subject already states.

**Record the count — in the digest, and labelled as a floor while the scan cannot finish.**
`python3 "${CLAUDE_PLUGIN_ROOT}/scripts/fleet-legs.py" --digest "<this round's digest>" --leg dead-workers --count <n> --floor`
alongside the manager count. `--floor` is what renders `(lower bound, scan incomplete)`;
`--exact` asserts the scan completed. ⚠️ **The script refuses a bare `dead-workers` count** —
one of the two is mandatory, because a number with no label reads as a measurement, and while
the dead-worker scan cannot finish inside its own timeout the floor is the only honest form.
Both flags are refused on any other key. Format, not-run form and rationale:
`commands/steps/output-shape.md` item 14, the single home.

⚠️ **A leg that does not execute records WHY, in the same file — at the granularity that failed.**
When the debt gate skips the whole leg (exit 12 or 2), write
`fleet-legs.py --leg keep-alive --not-run "<reason>"`, which covers all three of its keys.
⚠️ **When only one section failed, name that KEY, never the leg** — a scan whose instrument is
absent is `fleet-legs.py --leg dead-workers --not-run "scan instrument absent"`, because
`--leg keep-alive` would wipe the readings the other sections already recorded. Never
leave the sweep's seeded `not run — no reading recorded this round` standing: `--check`
treats that text as "nobody has spoken for this key", so a round that renders it is reported
as incomplete. A suppressed leg and a leg that ran and found nothing must never print the same.

⚠️ **A failed write is reported, never swallowed.** If `fleet-legs.py` itself exits 2 the digest
could not be read or written (the sweep may not have written it yet); render
`legs: UNKNOWN — <reason>` for this round and retry the record once the digest exists. A
reading that never landed must not read as a leg that never ran.

