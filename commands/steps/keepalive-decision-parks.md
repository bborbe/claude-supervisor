---
# Read by the router, never invoked — without this key Claude Code
# registers this file as /<plugin>:steps/<name>.
user-invocable: false
---

# Resume a decision-parked worker whose wait cleared

The clock leg (`agents/fleet-keepalive-actor.md` § 2) resumes waits that wrote themselves a
`defer_date`. **Two** waits write none: an **approval** that landed after the worker was closed, and
a **`blocked_by` set** that has gone terminal. A reaped worker (`scripts/reap-idle-headless.py`)
keeps its transcript and its task's `claude_session_id`, so such a row is resumable — and nothing
resumed it.

⚠️ **A third wait — an answered attention card — is deliberately not here.** It ships:
[[An Answered Card Resumes the Session Waiting On It]] resumes a dead session on its answered card
today. A second implementation would be a second owner of one wait, which is how these legs drift.

**The decision is a script, not this prose.** Run it and act on its verdicts:

```bash
P=${CLAUDE_PLUGIN_ROOT:-$HOME/.claude/plugins/marketplaces/claude-supervisor}/scripts
python3 "$P/decision-park-resume.py" --vault "<vault>" --tasks-dir "<vault>/<tasks-dir>"
```

`--tasks-dir` is the vault's own, resolved from `vault-cli config` — never a hardcoded directory
name, which would glob nothing and report a clean round. The script prints one line per row and a
summary carrying `decision-candidates=`, `decision-resumed-a=`, `decision-resumed-d=` and
`decision-unread=`. Print the
summary; it is the hand-back's three counts and none of them is a digest key.

⚠️ **This was prose for six review rounds, and every round found a different real defect** — an
`approved_at` read the population already guarantees, an empty `blocked_by` that is vacuously "all
terminal", no consumption bound, a counter nothing wrote, and an all-or-nothing `unread` rule that
stranded half-read rows. Each is a named test in `scripts/tests/test_decision_park_resume.py`.
**Do not restate the rules here** — a second home is how the next one appears.

## Acting on a verdict

| Verdict | Act |
|---|---|
| `resume kind=a` / `kind=d` | `/supervisor:open --task "<name>"`, then record it (below) |
| `⚠️ WORKER CRASH-LOOP` | stop resuming that task; escalate once with the line as printed |
| `unread <task> — <reason>` | report it, resume nothing — an unreadable wait is never "not cleared" |

**Recording is what arms the bound, and it must go to the script's own store.** After a resume that
actually took, record it with the script — not with `worker-keepalive.py`, whose counter is a
different store and a different population:

```bash
python3 "$P/decision-park-resume.py" --vault "<vault>" --record "<task>" --sid "<the id the resume produced>"
```

⚠️ **`<id>` is the id the resume produced, not the task's stored one.** Take it from the call's own
return; a stale id records a resume that did not happen and leaves `RESUME_CAP` unarmed against the
retry it is there to stop. If the resume returns no id, record nothing and report the row.

⚠️ **Writing `worker-keepalive.py --record-resume` here instead is a silent no-op for this bound.**
That store is keyed on a different branch, so this script's `spent` never sees the write and the
`⚠️ WORKER CRASH-LOOP` row is unreachable — measured in review round eight. One counter, one writer,
both in the script.

⚠️ **A worker session may never open or resume another session** (`docs/session-tiers.md`) — this
half lives in the fleet round, which is where it runs.
