---
# Read by the router, never invoked — without this key Claude Code
# registers this file as /<plugin>:steps:<name>.
user-invocable: false
---

# Keep alive — the topic managers

When: every tick

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
  grep -q '^manager_session_id:' "$f" || continue
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

## Step 5b, second half — resume a worker whose session died

⚠️ **This reverses `agents/fleet-drive.md`'s *"orphan → escalate, never nudge"*.** That note
made a dead worker the operator's problem every round; rule 1 says a crashed or lapsed worker
is restarted automatically, and a worker that already owns its task decides nothing new by
being resumed. The escalation stays for a worker that **cannot** be resumed (no recorded id,
a spent cap, an undecidable probe) — it is no longer the disposition for every orphan.

**Population — `in_progress` tasks with open boxes that record a `claude_session_id`.** Run
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
for f in "<vault>/$TASKS"/*.md; do
  grep -q '^claude_session_id:' "$f" || continue
  name=$(basename "$f" .md)
  printf '%s\t' "$name"
  python3 "$P/worker-keepalive.py" --vault "<vault>" --task "$name" --dir "$TASKS" | head -2 | tr '\n' '\t'
  echo
done
```

**Act on the verdict.** `resume` → `/supervisor:open --task "<name>"`, then record the resume
**once `session-liveness.py --check` shows the id live** (`worker-keepalive.py --record-resume
<id> --task "<name>"`); `jump` → hand the operator the pane; `crash-loop` → stop resuming that
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

⚠️ **`agents/goal-drive.md`'s orphan gate covers the same population, and the two must not both
act in one window.** That gate runs on the goal tier's tick and resumes a dead worker with its
own `last_auto_resume` cap; this leg runs on the fleet tick with the `worker--<slug>` cap. Both
caps record only **after** a resume the instrument confirms `LIVE`, so a race lets two resumes
land before either cap arms. **Act only on a row the goal tier has not already claimed** — a
worker whose goal has a live manager is that manager's to resume; this leg takes the ones whose
goal has no live manager (the same fallback `docs/fleet-surface.md` § Which manager owns a
subject already states).

**Report the count.** Print `unactioned: <n> dead-workers` alongside the manager count.

