---
# Read by the router, never invoked — without this key Claude Code
# registers this file as /<plugin>:steps:<name>.
user-invocable: false
---

# Drive — the topic round's act leg

When: every tick, after the sweep

Rationale and incident history: the Manager Session runbook § Command rationale — maintained.

- Act:
  - **dispatch the act leg** — `Task(subagent_type: "supervisor:topic-drive", prompt: <topic name + the declared goal names the sweep read + the vault root + this tick's timestamp + this sweep's `ListAgents` roster verbatim + the park threshold (`2 ×` the clamped interval seconds) + the vault's tasks directory>)`. The plugin prefix is required. `agents/topic-drive.md` runs the **goal round's sweep → drive for each declared goal**: it dispatches `supervisor:goal-sweep-reader` (producing the tracked-set path and the bucket classification), saves the payload, then dispatches `supervisor:goal-drive` with them. ⚠️ **The sweep is not optional** — a drive leg dispatched without a tracked-set path and classification has no population and drives nothing (the round-6 finding).
  - ⚠️ **The agent holds no spawn tool**: its `To resume` / `To open` lines are **yours to execute**, through `/supervisor:open`, never a second dispatch.
  - ⚠️ **Never perform the act inline** — reaping, nudging, messaging or resuming a worker from this round is the double-writer failure `agents/goal-drive.md` exists to prevent.
  - **record the act artifact on the TOPIC receipt** — write the agent's aggregate report to `~/.claude/state/manager-receipts/<topic-slug>.drive`, then:

    ```bash
    python3 ${CLAUDE_PLUGIN_ROOT:-$HOME/.claude/plugins/marketplaces/claude-supervisor}/scripts/manager-receipt.py \
      --record --topic "<topic>" --tier topic --step drive --path ~/.claude/state/manager-receipts/<topic-slug>.drive
    ```

    ⚠️ **`--tier topic` is required** — the topic tail grades `TOPIC_STEPS`, which now carries `drive`; a goal-tier record would leave the topic round reporting `skipped: drive` while it drove. ⚠️ **The file must carry the leg's own headers** (`To open` / `To resume` / `Not resumed` / `Escalated` / `Nudged` / `Reaped`), not a summary; a leg that found nothing still reports. ⚠️ **No usable report → record nothing and let the tail say so**; a stub written to turn the check green is the defect.

- ⚠️ **Next: the tail (row 12), before the self-improvement row.** The drive report is the tail's input; a round that skipped this dispatch renders a complete-looking table while driving nothing.
