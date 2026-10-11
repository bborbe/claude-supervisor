## When

The snapshot path is missing or the write failed.

  - **Persist:** Step 6, via `fleet-snapshot.py` only — never hand-write `~/.claude/state/fleet-snapshot.json`.
## Step 6 — Persist the new snapshot

The sweep reader persists it (its digest quotes `snapshot written: <swept_at>`). On the fallback path, persist it yourself — pipe the sessions JSON (schema: `agents/fleet-sweep-reader.md` step 9) into `python3 $P/fleet-snapshot.py`.
