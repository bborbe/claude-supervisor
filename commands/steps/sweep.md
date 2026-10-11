# Sweep

When: every tick

## Steps 0–3 — Read half, delegated to `supervisor:fleet-sweep-reader`

**Step 0 — roster.** Call `ListAgents` — the only place this command calls it. (Do not reimplement `/fleet-status`'s roster join here.)
**Steps 0b–3 — delegate.** Dispatch:

`Task(subagent_type: "supervisor:fleet-sweep-reader", prompt: <this round's ListAgents roster verbatim + SID + vault path and tasks dir + round timestamp>)`

The plugin prefix is required — a bare `fleet-sweep-reader` resolves to a personal `~/.claude/agents/` copy. The agent (Sonnet) reads the four channels and the ledger, loads the previous snapshot, stats each `busy`/`shell` task file, runs the orphan reverse index (`orphan-candidates.py --tasks-dir . --max-age-days 7`, park filter + 7-day upper bound, exit code checked), finds collision and unmanaged-topic candidates, classifies every session, persists the next snapshot through `fleet-snapshot.py`, and returns a **≤ 40-line digest**. It owns those rules — read them in `agents/fleet-sweep-reader.md`; this command does not restate them.
**When the delegation returns no usable digest** — it errored, came back empty, or resolved to something that returned no digest — run the reads yourself for this round (`/fleet-status`, then `/supervisor:open-items list`, `orphan-candidates.py` as above), compacting `fleet-sessions.py` through `grep -oE '\b[0-9a-f]{8}\b'`. Trigger on the missing digest, never on a matched error string.
