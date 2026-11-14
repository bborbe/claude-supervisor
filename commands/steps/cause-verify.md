---
# Read by the router, never invoked — without this key Claude Code
# registers this file as /<plugin>:steps:<name>.
user-invocable: false
---

# Verify a named cause

When: about to name a cause in the round's report

**A session-scoped cause is resolved by script, never by reading.** Run:

```
python3 $P/cause-resolve.py --session <sid>
```

It reads exactly two things — that session's own `sessions/<sid>/events.jsonl` and the state ledger — and answers one of:

- `asserted: <cause>` plus the quoted `line:` — the session's own log carries a line that **names it and states the action**. Quote that line in the report.
- `unverified: <reason>` — no such line exists in its own sources. Render the cause as `unverified` (`agents/fleet-drive.md:199` mandates the marker) and say what the resolver answered. **Do not** substitute a line from another session's log, and do not infer a cause from the ledger's `absent-from-sources` row: the ledger records that a session was judged absent, never the mechanism that removed it.

⚠️ **A line that names the session is necessary and not sufficient.** `drain: session <sid> at turn boundary` names it and asserts nothing — the drain observes a boundary and never kills (`claude-wrapper/pkg/supervisor.go`: *"It never kills a child"*). The resolver rejects it as `names-but-does-not-assert`. Measured 2026-11-14, round 60: a round read `reaper` off a sibling session's reap tick, and the manager then read `drain` off a turn-boundary line — two false causes for one session in one day, both in a confident register.

**A cause that is not about one session still delegates.** When the candidate is a system's state rather than a session's end — a repo, a release, a cluster — one sub-agent per candidate (`Explore` or `general-purpose`, **Sonnet**), the cause as a yes/no question plus the systems to check; take back only the verdict:

> "Is `github-update-go-agent` actually broken right now? Check merged PRs on `bborbe/github-update-go-agent`, the latest released tag, and live Job states in `kubectl<wrapper> -n prod`. Answer: broken / fixed / can't tell, with the evidence line for each."

Independent causes go in one message so they run concurrently. **Never delegate** course-correction drafts. **Keep every `SendMessage` in this session** — a sub-agent has no address, so replies land here regardless.
