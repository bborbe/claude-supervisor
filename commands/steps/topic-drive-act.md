---
# Read by the router, never invoked — without this key Claude Code
# registers this file as /<plugin>:steps:<name>.
user-invocable: false
---

# Drive — the topic round's act leg

When: every tick, after the sweep

Rationale and incident history: the Manager Session runbook § Command rationale — maintained.

- Act:
  - **dispatch the act leg** — `Task(subagent_type: "supervisor:topic-drive", prompt: <topic name + the declared goal names the sweep read + each goal's task rows + the topic page path + the vault root + this tick's timestamp + the sweep's classification>)`. The plugin prefix is required. `agents/topic-drive.md` drives each declared goal's rows through `agents/goal-drive.md` — it reaps the finished, nudges the stuck and decides the auto-resume gate under goal-drive's rules. ⚠️ **The agent holds no spawn tool**: its `To resume` / `To open` lines are **yours to execute**, through `/supervisor:open`, never a second dispatch.
  - ⚠️ **Never perform the act inline.** Reaping, nudging, messaging or resuming a worker from this round is the double-writer failure `agents/goal-drive.md` exists to prevent; the agent is the only home of those rules.
  - **record the act artifact** — write the agent's report to `~/.claude/state/manager-receipts/<slug>.drive`, then:

    ```bash
    python3 ${CLAUDE_PLUGIN_ROOT:-$HOME/.claude/plugins/marketplaces/claude-supervisor}/scripts/manager-receipt.py \
      --record --topic "<topic>" --step drive --path ~/.claude/state/manager-receipts/<slug>.drive
    ```

    ⚠️ **The file must carry the leg's own headers**, not a summary — `drive_shape()` accepts `To open` / `To resume` / `Not resumed` / `Escalated` / `Nudged` / `Reaped`. A leg that found nothing still reports, so an empty row set is not a failure. ⚠️ **No usable report → record nothing and let the tail say so**; a stub written to turn the check green is the defect.

- ⚠️ **Next: the tail (row 12), before the self-improvement row.** The drive report is the tail's input; a round that skipped this dispatch renders a complete-looking table while driving nothing.
