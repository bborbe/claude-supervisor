---
# Read by the router, never invoked — without this key Claude Code
# registers this file as /<plugin>:steps/<name>.
user-invocable: false
---

# Resume a decision-parked worker whose wait cleared

The clock leg (`agents/fleet-keepalive-actor.md` § 2) resumes waits that wrote themselves a
`defer_date`. Three waits write none: an **approval** that landed after the worker was closed, an
**answered attention card** whose worker exited, and a **`blocked_by` set** that has gone terminal.
A reaped worker (`scripts/reap-idle-headless.py`) keeps its transcript and its task's
`claude_session_id`, so such a row is resumable — and nothing resumed it.

**The decision is a script, not this prose.** Run it and act on its verdicts:

```bash
P=${CLAUDE_PLUGIN_ROOT:-$HOME/.claude/plugins/marketplaces/claude-supervisor}/scripts
python3 "$P/decision-park-resume.py" --vault "<vault>" --tasks-dir "<vault>/<tasks-dir>"
```

`--tasks-dir` is the vault's own, resolved from `vault-cli config` — never a hardcoded directory
name, which would glob nothing and report a clean round. The script prints one line per row and a
summary carrying `decision-candidates=`, `decision-resumed=` and `decision-unread=`. Print the
summary; it is the hand-back's three counts and none of them is a digest key.

⚠️ **This was prose for six review rounds, and every round found a different real defect** — a
resume carrying no prompt, an `approved_at` read the population already guarantees, an empty
`blocked_by` that is vacuously "all terminal", no consumption bound, an id the resume never
produced, and a cap that was armed but never read. Each is now a named test in
`scripts/tests/test_decision_park_resume.py`. **Do not restate the rules here** — a second home
is how the seventh appears.

## Acting on a verdict

| Verdict | Act |
|---|---|
| `resume kind=a` / `kind=d` | `/supervisor:open --task "<name>"` |
| `resume kind=b` | **`spawn_agent(prompt=<the answer the line carries>, resume=<sid>, interactive=false, cwd=<explicit>)`** — never `/supervisor:open`, which carries no prompt. An exited worker still holds its unanswered question, and an empty resume re-parks it at the same gate |
| `⚠️ WORKER CRASH-LOOP` | stop resuming that task; escalate once with the line as printed |
| `unread <task> — <reason>` | report it, resume nothing — an unreadable wait is never "not cleared" |

⚠️ **Record the resume against the id the resume produced, not the task's stored one.** Take it
from the call's own return; `worker-keepalive.py --record-resume` refuses an id the liveness
instrument does not read `LIVE`, so a stale id leaves the bound unarmed. If the resume returns no
id, record nothing and report the row.

⚠️ **A worker session may never open or resume another session** (`docs/session-tiers.md`) — this
half lives in the fleet round, which is where it runs.
