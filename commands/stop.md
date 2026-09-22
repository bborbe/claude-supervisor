---
description: Stand this session's manager loop down — disarm the model-waking cadence, leave the model-free gate loop running, keep the session (and its asks ledger) alive, and report what was disarmed. The disarm contract is docs/fleet-surface.md § Session end.
allowed-tools:
  - Read
  - CronList
  - CronDelete
  - ScheduleWakeup
  - TaskStop
  - Bash(python3:*)
argument-hint: "(no argument)"
---

Stand the manager loop **this session** is running down. This is the operator's verb; it is not a close. It takes no argument — the cadence is session-scoped, so there is nothing to name.

**The disarm contract — what must be disarmed, which harness surface reaches each driver and which it cannot, and what must be left running — is `${CLAUDE_PLUGIN_ROOT}/docs/fleet-surface.md` § Session end. Read it and execute it from there; do not restate it here.** The vault's `Worker Manager Session` runbook § Guardrails item 6 — Session end carries the per-vault operating statement and points back at both. **What this file owns is the procedure and the report** — the knowledge is the contract doc, read directly at step 1; an agent behind this file would only add a hop between it and the doc it points at.

<process>
1. **Read the contract, then disarm every model-waking driver it names** — in the order it gives, printing each driver's line as you go and *before* the call that removes it, since a deletion the operator cannot see is indistinguishable from a job that was never armed. Print `·` for a driver that was **not** armed rather than dropping the line: a missing line reads as "checked and clean" when the truth may be "never looked". Those lines **are** the report's `Disarmed` block — collect them, and do not print the set a second time.

2. **Probe the state `stop` must not change** — read-only; the probe writes nothing and signals nothing:

   ```bash
   python3 ${CLAUDE_PLUGIN_ROOT:-$HOME/.claude/plugins/marketplaces/claude-supervisor}/scripts/stop-probe.py
   ```

   It prints the subject, the gate loop's pid and uptime, the tick file's mtime and age, and the ledger's open count.

3. **Print the report** — this command's own output format, and the only surface the contract does not own:

   ```
   ⏹️ STOP — <subject> (<branch>) · session <sid8>
     Disarmed
       ✓ <driver> — <what it was>
       · <driver that was not armed>
     Left running — fleet-surface.md § Session end owns this contract
       ● gate loop  pid <pid>, up <etime>
       ● tick file  <path>  mtime <iso>  (<age>s ago)
     Session alive
       ● ledger <path> — <n> open of <m>, unchanged by this command
     Restart: /supervisor:worker-manager "<subject>"
   ```

   The gate-loop line takes the absent form below rather than printing a pid the probe did not find.
</process>

<constraints>
- **Write no page and no state file.** `stop` is a disarm, not a bookkeeping act.
- End the turn with the session's normal closer. This report is not one.
</constraints>

<error_handling>
- **A driver that refuses to disarm** — print the failure verbatim together with the driver still standing, and say the loop is **not** fully stood down. Reporting success over a live driver is worse than reporting the failure.
- **No gate loop found**: print `⚠️ no gate loop found` in place of the gate-loop line, and say plainly that it may never have been armed. A fact to report, not to repair.
- **No subject recorded**: print the session id and carry on — the loop may have been armed before the subject was written.
</error_handling>
