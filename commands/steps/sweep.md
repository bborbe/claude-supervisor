---
# Read by the router, never invoked — without this key Claude Code
# registers this file as /<plugin>:steps:<name>.
user-invocable: false
---

# Sweep

When: every tick

## Steps 0–3 — Read half, delegated to `supervisor:fleet-sweep-reader`

**Step 0 — roster.** Call `ListAgents` — the only place this command calls it. (Do not reimplement `/fleet-status`'s roster join here.)
**Steps 0b–3 — delegate.** Dispatch:

`Task(subagent_type: "supervisor:fleet-sweep-reader", prompt: <this round's ListAgents roster verbatim + SID + vault path and tasks dir + round timestamp>)`

The plugin prefix is required — a bare `fleet-sweep-reader` resolves to a personal `~/.claude/agents/` copy. The agent (Sonnet) reads the four channels and the ledger, loads the previous snapshot, stats each `busy`/`shell` task file, runs the orphan reverse index (`orphan-candidates.py --tasks-dir . --max-age-days 7`, park filter + 7-day upper bound, exit code checked), finds collision and unmanaged-topic candidates, classifies every session, persists the next snapshot through `fleet-snapshot.py`, and returns a **≤ 1 KB `key=value` hand-back** — the bucket counts plus the `payload=` path it wrote the full ≤ 40-line digest render to. It owns those rules — read them in `agents/fleet-sweep-reader.md`; this command does not restate them. ⚠️ **Keep the `payload=` path.** The render no longer rides the hand-back: Step 5 forwards this path to the drive leg, and Steps 3, 11 and 14 read the file for their rows. A round that drops the path has no rows to render and no drive input.
**When the delegation returns no usable hand-back** — it errored, came back empty, or resolved to something that returned no hand-back — run the reads yourself for this round (`/fleet-status`, then `/supervisor:open-items list`, `orphan-candidates.py` as above), compacting `fleet-sessions.py` through `grep -oE '\b[0-9a-f]{8}\b'`. Trigger on the missing hand-back, never on a matched error string.
