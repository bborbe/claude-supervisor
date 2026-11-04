---
# Read by the router, never invoked — without this key Claude Code
# registers this file as /<plugin>:steps:<name>.
user-invocable: false
---

# Step 1 — Sweep

When: every run — before any classification

```bash
python3 "${CLAUDE_PLUGIN_ROOT:?set by the loaded plugin}/scripts/fleet-board.py" --json > /tmp/fleet-triage.json
echo "EXIT=$?"
```

⚠️ **A non-zero exit or unparseable JSON is a hard stop.** An incomplete sweep reads exactly
like a clean one, and acting on it is worse than not acting.

Per `sessions[]`: `session_id`, `role`, `label`, `subject`, `bucket`, `unblocks`,
`task_title`, `age`, `parent`.

`details` is a `{session_id: "<why it is parked>"}` map from the board's own gate join — the
richest signal in the document, and load-bearing for OPERATOR:

- names a **human answer** (`keystroke`, `decide`, `say \`go\``, `approve`, `pick`) → OPERATOR
- names a **producer** (CI, bot review, deploy, tag, clock) → EXTERNAL
- absent → fall through to 3.4

⚠️ **`bucket` is not the state.** It reports most of the fleet as `running` while several
are parked. Key on `unblocks`, `details`, and the checks below.

⚠️ **The board is a snapshot, not a measurement.** Measured 2026-10-28: a row read
`DECISION — say go` while its pane was mid-turn at 1m09s with an empty composer, and two of
three "needs you" rows had started working by the time they were rendered. **Re-read the
pane before acting on any row** (3.2c).
