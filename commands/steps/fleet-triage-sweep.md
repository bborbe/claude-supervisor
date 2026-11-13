---
# Read by the router, never invoked — without this key Claude Code
# registers this file as /<plugin>:steps:<name>.
user-invocable: false
---

# Step 1 — Sweep

When: every run — before any classification

```bash
mkdir -p ~/.claude/state/fleet-triage
python3 "${CLAUDE_PLUGIN_ROOT:-$HOME/.claude/plugins/marketplaces/claude-supervisor}/scripts/fleet-board.py" --json > ~/.claude/state/fleet-triage/sweep-${CLAUDE_CODE_SESSION_ID:-$CLAUDE_PID}.json
echo "EXIT=$?"
```

⚠️ **A non-zero exit or unparseable JSON is a hard stop.** An incomplete sweep reads exactly
like a clean one, and acting on it is worse than not acting.

⚠️ **The snapshot lives under `~/.claude/state/fleet-triage/`, never in `/tmp`.** A `/tmp` path
is world-writable and the `>` redirect follows a pre-created symlink, so a guessable name (the
`$CLAUDE_PID` fallback is readable from `ps`) is a file-write primitive on a multi-user host.
`~/.claude/state/` is user-owned and already holds this command's nudge records; `mkdir -p` is
granted for the one directory this command owns.

⚠️ **The path is keyed on the session id, and Step 6 must use the same one.** A fixed
`sweep.json` is shared across every concurrent Claude session, so two triage runs
clobber each other's before/after snapshots and the delta in Step 6 compares this run's "after"
against another run's "before" — a clean-looking verdict built on someone else's fleet. The
`${CLAUDE_CODE_SESSION_ID}` suffix keeps each run's file its own; two runs *within* one session
still collide, which is why this command is one pass per session and not run twice at once.
⚠️ **The fallback is `$CLAUDE_PID`, never a literal `default`** — a fixed fallback would put every
session that lacks the variable back on one shared name, which is the clobber this paragraph
exists to prevent. `$CLAUDE_PID` is set in every `Bash` call and is unique per live process.

⚠️ **This file is the raw board document, not a triage verdict.** Its rows carry
`bucket` ∈ {`problem`, `needs-input`, `running`, `idle`} and **no STUCK classification** —
STUCK is derived by § 3.4 from the task files. Step 6 re-derives its before-count from this
snapshot by re-applying § 3.4, never by counting a column that does not exist.

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
