---
# Read by the router, never invoked — without this key Claude Code
# registers this file as /<plugin>:steps:<name>.
user-invocable: false
---

# Sweep — the topic's goal rows

When: every tick

Rationale: `agents/topic-sweep-reader.md` is the plugin's canonical procedure for this half.

1. **Take the readings first — before the dispatch, never batched with it.** For every goal
   the topic declares, read its page's `claude_session_id` and probe it:

   ```bash
   python3 ${CLAUDE_PLUGIN_ROOT:-$HOME/.claude/plugins/marketplaces/claude-supervisor}/scripts/session-liveness.py --check <id>
   ```

   Record `<goal> → LIVE | ABSENT | UNKNOWN`. ⚠️ **`UNKNOWN` is not `ABSENT`** — an
   unreadable source cannot prove a session dead, so it stays `UNKNOWN` and the row renders
   `UNVERIFIED`.

2. **Take the cadence verdicts the same way, and before the dispatch for the same reason.**

   ```bash
   python3 ${CLAUDE_PLUGIN_ROOT:-$HOME/.claude/plugins/marketplaces/claude-supervisor}/scripts/manager-liveness.py \
     --state --subject "<goal>" --branch goal
   ```

   Record `<goal> → OK | STALE | STOPPED | SLEEPING | INVALID`. ⚠️ **`--branch goal` is not optional** —
   the state slug carries the tier (`goal--<base>`), so a topic and a goal that share a name
   never overwrite each other's cadence record.

   ⚠️ **Write no reading into this dispatch that you have not taken this tick.** A prompt
   written in the same message as its own input has no output to quote and fills the slot
   from the previous tick instead — and a stale value has the same shape, position and
   confidence as a fresh one, so it silently disables the check it feeds. **When a read has
   not completed, leave the slot unstated.**

3. **Delegate the computation to `supervisor:topic-sweep-reader`.**

   `Task(subagent_type: "supervisor:topic-sweep-reader", prompt: <topic page path + the declared goal names + vault + mode: tick + timestamp + the liveness verdicts + the cadence verdicts + today + payload path>)`

   ⚠️ **Pass the payload path — the reader is required to write the rendered table and has nowhere to put it otherwise.** Its constraint says *"Your one write is the rendered payload, at the path the caller's gate reads"*, but this step passed no path, so the write had no destination and the leg's only artifact could not exist. Name `~/.claude/state/manager-receipts/<slug>.sweep`, where `<slug>` is the receipt's own slug (`scripts/manager-receipt.py`), so the file sits beside the receipt it belongs to.

   ⚠️ **The `supervisor:` prefix is required** — a bare name resolves to a personal
   `~/.claude/agents/` copy, never the plugin agent. The agent owns the goal-row read, the
   per-goal classification and the table render; it returns one compact report.

4. **Never sweep a task here.** The goal tier's `agents/goal-sweep-reader.md` classifies a
   goal's **task set** into buckets. A task row reaching this step is a tier violation: report
   it downward to the goal manager that owns it, and render no row for it.

5. **Render the table** — from the agent, or with
   `python3 .../scripts/box-table.py` reading `${CLAUDE_PLUGIN_ROOT}/docs/sweep-output.md`
   for the frame. **Never hand-draw the box.** Say so in the tick when the fallback fires: a
   table rendered through it is one whose delegation failed.

6. **Record the sweep on the round's receipt — the leg's own payload, not a copy you wrote.**

   ```bash
   python3 ${CLAUDE_PLUGIN_ROOT:-$HOME/.claude/plugins/marketplaces/claude-supervisor}/scripts/manager-receipt.py \
     --record --topic "<subject>" --step sweep --path ~/.claude/state/manager-receipts/<slug>.sweep
   ```

   ⚠️ **Without this call the topic tuple cannot honestly carry `sweep`.** The topic round sweeps every tick (router row 3), so dropping the step from the tuple would let a round that skipped its sweep report complete — the very defect the receipt exists to catch. The record call is what makes keeping it honest. ⚠️ **A delegation that failed leaves no payload** — record nothing and let the tail print `skipped: sweep`; a table rendered through the `box-table.py` fallback is still the leg's output and is recorded normally.
