---
# Read by the router, never invoked — without this key Claude Code
# registers this file as /<plugin>:steps:<name>.
user-invocable: false
---

# Parked

When: every tick, after the drive leg returns

⚠️ **This file is the lever row's ONE home.** `commands/manager-drive.md` names it in the
router and restates none of it; the drive leg's report is the input, never a second
definition.

- **What a parked row is.** One line per parked **tracked** worker — a worker whose turn
  has ended holding an open gate: `waiting-on-human`, `waiting-approval`, and the
  `parked-on-unregistered-gate` class. A worker inside a tool call is not parked; a
  terminal task is not parked; an untracked session is not this round's to report.

- **The row — four fields, always, in this order:**
  `<jump link> · waits: <what it is parked on> · lever: <the one act that clears it> · owner: <manager|operator|machine: <watcher>>`
  - **Jump link** — `python3 ${CLAUDE_PLUGIN_ROOT:-$HOME/.claude/plugins/marketplaces/claude-supervisor}/scripts/jump-link.py <pane-id>`. A headless worker has no pane: render `no pane (headless)`, never a dead link.
  - **Waits** — the gate in the worker's own words, never a paraphrase.
  - **Lever** — the ONE act that clears it. Not a list, not a plan, not a sequence.
  - **Owner** — `manager`, `operator`, or `machine: <watcher>`. Nothing else.

- **Render through `scripts/parked-lever.py`; never hand-assemble a row.** It emits the
  four fields and **refuses a lever that names no lever**: `the fleet`, `CI`, `the team`,
  `wait`, `someone` are rejected outright, because they read exactly like an answer and
  leave the operator opening the pane anyway. ⚠️ **A refusal (exit 2) is a reportable
  gap, not a formatting problem** — name the worker and say the lever is unknown; never
  print a row the script refused.

- **A manager-owned lever is executed in this round, not offered.** Nudge, re-arm a
  watcher, reap, resume, ready a row — these are the round's own work: perform them, then
  render the row with `owner: manager` and the act already done. ⚠️ **Never present one as
  an `AskUserQuestion` option** — the rule's home is `commands/manager-loop.md:87`
  (execute, then report as a table line), and a lever offered back is a lever the round
  declined to pull.

- **An operator-owned lever feeds the round's ONE card.** It is a row on the existing
  decision card posted by `commands/steps/manager-gates.md`, never a second modal and
  never a per-row menu. This step supplies the rows; the card is not this file's.

- **A `machine:` lever names the watcher AND the condition it waits on.** *"a cron will
  pick this up"* is not an answer; `machine: attention-watcher — fires on the next poll`
  is. An unnamed watcher is rejected by the renderer.

- **Voice the block in one pass, below the table.** It is a scan, not a report: one line
  per parked worker, no preamble, no per-worker narration. ⚠️ **Voice is the caller's**
  (`mcp__tts__say`, voice-mode gated) — a subagent has none.
