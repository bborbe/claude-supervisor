---
# Read by the router, never invoked — without this key Claude Code
# registers this file as /<plugin>:steps:<name>.
user-invocable: false
---

# Persist

When: this round's `.digest` marker is missing — run: `python3 $P/fleet-snapshot.py`

⚠️ **The snapshot itself is no longer written by this path.** `bborbe/claude-wrapper`'s sweep tick owns `~/.claude/state/fleet-snapshot.json` (verified 2026-11-20: 2496 ids vs 49 live, 0 missing). This script now stamps only the round's own `.digest` marker. A missing SNAPSHOT is the wrapper's to fix, not this round's — never hand-write it.

  - **Persist:** Step 6, via `fleet-snapshot.py` only — never hand-write `~/.claude/state/fleet-snapshot.json`.
## Step 6 — Persist the new snapshot

The sweep reader persists it (its hand-back carries `snapshot=written|skipped|UNKNOWN` — `UNKNOWN` means the persist pass did not run, never that it failed). On the fallback path, persist it yourself — pipe the sessions JSON (schema: `agents/fleet-sweep-reader.md` step 9) into `python3 $P/fleet-snapshot.py`.
