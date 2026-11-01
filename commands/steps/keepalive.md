---
# Read by the router, never invoked — without this key Claude Code
# registers this file as /<plugin>:steps:<name>.
user-invocable: false
---

# Keep alive — the topic managers

When: every tick

⚠️ **Gate this leg on the round's debt — chain it, do not merely read it:**

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/manager-receipt.py" --debt --subject "Fleet Manager" --branch fleet \
  && <run this leg>
```

Exit **12** = a step is still owed: print the `debt:` lines, skip this leg, and **carry on with the round — the tail still runs**, because the tail's `--check` is what reports the round's verdict to the operator. Exit **0** = no debt, run it. ⚠️ **Exit 2 = the gate could not read or write the debt file** — it prints nothing to stdout, only a reason to stderr; skip the leg and print that reason, never read 2 as "no debt". ⚠️ **`--debt` judges the round in progress**, re-reading the receipt, so a round that already swept is not blocked by a debt it has paid. ⚠️ **The exit code is the gate, never the printed line** — a leg that reads only the text is satisfied by an empty read. ⚠️ **Resuming a topic manager is not urgent enough to outrank the step the round owes** — that is the trade the 2026-10-31 rounds made in the other direction, and it is why the debt gates the cheap legs rather than only the expensive one.

## Step 5b — Restart a lapsed topic manager

The goal tier has had a keep-alive since `topic-drive.md`'s § Keep alive; the topic tier has
had none, so a lapsed topic manager simply stayed lapsed and nothing reported it. This leg is
the missing rung: **restart is not open**, so resuming a topic manager that already owns its
subject decides nothing new (`docs/session-tiers.md` § The four tiers, rule 1).

**Population — every topic page carrying a `manager_session_id`.** Read the vault's topic
directory, not the roster: a topic manager whose session died is absent from the roster, which
is exactly the row this leg exists to recover. ⚠️ **Resolve the directory from the vault's own
config, never assume `23 Topics`** — a renamed directory would glob nothing and the leg would
report a clean round, the exact silent failure it exists to remove:

```bash
P=${CLAUDE_PLUGIN_ROOT:-$HOME/.claude/plugins/marketplaces/claude-supervisor}/scripts
TOPICS=$(vault-cli config list --output json | python3 -c "
import json,sys
for x in json.load(sys.stdin):
    if x.get('topics_dir'):
        print(x['topics_dir']); break")
TOPICS="${TOPICS:-23 Topics}"
for f in "<vault>/$TOPICS"/*.md; do
  name=$(basename "$f" .md)
  grep -qE '^manager_session_id: *[^ ]' "$f" || continue
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

**Report the count.** Print `unactioned: <n> lapsed-managers` with the topic rows this leg
found and did not act on — the `# Regression metric` on the task that installed this leg reads
it, and a round that prints the number without moving it is the defect it exists to remove.

## Step 5b, first half — resume the park watches whose re-check has passed

A parked worker is not a dead one: it exited on purpose, on a clock. Its re-check
time lives in `park_recheck`, written by `park-event.py register` at park time, and
**`defer_date` cannot carry it** — every reader truncates that field to its date, so
a time written there resurfaces the task at the start of the day, hours early.

```bash
P=${CLAUDE_PLUGIN_ROOT:-$HOME/.claude/plugins/marketplaces/claude-supervisor}/scripts
python3 "$P/park-event.py" due
```

One line per watch whose time has passed, naming every session parked on it —
**one watch, not one per session**, which is the dedup the register call created.
Resume each named session through `/supervisor:open --task "<name>"`, then drop the
member with `park-event.py release --event "<key>" --session "<id>"`; the file is
removed when the last member leaves. A watch that comes due and cannot be resumed
(the event has not fired) is **re-parked, not left due**: bump `park_recheck` with a
fresh `register` call, and after the third re-park on the same event route the wait
to the manager as a criterion challenge rather than re-parking a fourth time.

⚠️ **This half is the fast path, never the only path.** The task's own `defer_date`
surfaces the row in every manager's open queue, so a missed round delays a resume and
never strands it. Never let this step become the sole record that a task is parked.

## Step 5b, second half — resume a worker whose session died

⚠️ **This reverses `agents/fleet-drive.md`'s *"orphan → escalate, never nudge"*.** That note
made a dead worker the operator's problem every round; rule 1 says a crashed or lapsed worker
is restarted automatically, and a worker that already owns its task decides nothing new by
being resumed. The escalation stays for a worker that **cannot** be resumed (no recorded id,
a spent cap, an undecidable probe) — it is no longer the disposition for every orphan.

**Population — every task page carrying a non-empty `claude_session_id`.** The script narrows it: a terminal status, a parked one (`hold`/`blocked`, or `human_review`/`todo`/`done` phase), a future `defer_date`, no open boxes, and a live owning goal manager all return `jump`. Any other non-terminal status (`next`, for one) with an open box **is** resumed — the guard is the parks above, not `status: in_progress`. Run
the script per task; it decides, it never resumes. ⚠️ **Resolve the task directory from the
vault's config, never assume `25 Tasks`:**

```bash
P=${CLAUDE_PLUGIN_ROOT:-$HOME/.claude/plugins/marketplaces/claude-supervisor}/scripts
TASKS=$(vault-cli config list --output json | python3 -c "
import json,sys
for x in json.load(sys.stdin):
    if x.get('tasks_dir'):
        print(x['tasks_dir']); break")
TASKS="${TASKS:-25 Tasks}"
GOALS=$(vault-cli config list --output json | python3 -c "
import json,sys
for x in json.load(sys.stdin):
    if x.get('goals_dir'):
        print(x['goals_dir']); break")
GOALS="${GOALS:-24 Goals}"
for f in "<vault>/$TASKS"/*.md; do
  grep -qE '^claude_session_id: *[^ ]' "$f" || continue
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
scans the task's frontmatter for wikilinks and, if any page one names has a `manager_session_id`
the liveness instrument reads `LIVE`, returns `jump` with *"goal manager <id> is live and owns this
task"* — the fleet leg never acts on a row the goal tier owns. This leg takes only the rows
whose goal has no live manager, the same fallback `docs/fleet-surface.md` § Which manager owns a
subject already states.

**Report the count.** Print `unactioned: <n> dead-workers` alongside the manager count.

