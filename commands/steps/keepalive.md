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

**Population — every topic page carrying a `manager_session_id`.** Read the vault's
`23 Topics/` directory, not the roster: a topic manager whose session died is absent from the
roster, which is exactly the row this leg exists to recover.

```bash
P=${CLAUDE_PLUGIN_ROOT:-$HOME/.claude/plugins/marketplaces/claude-supervisor}/scripts
for f in "<vault>/23 Topics"/*.md; do
  name=$(basename "$f" .md)
  grep -q '^manager_session_id:' "$f" || continue
  printf '%s\t' "$name"
  python3 "$P/topic-keepalive.py" --vault "<vault>" --subject "$name" --branch topic | head -2 | tr '\n' '\t'
  echo
done
```

**Act on the verdict, exactly as the goal tier does.** `restart` → run the printed
`/supervisor:restart <session-id>`, then record the resume **once `session-liveness.py --check`
shows the id live** (`topic-keepalive.py --record-resume <id> --branch topic`); `resume` → the
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
