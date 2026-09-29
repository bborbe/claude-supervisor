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

**The disarm contract — what must be disarmed, which harness surface reaches each driver and which it cannot, and what must be left running — is `${CLAUDE_PLUGIN_ROOT}/docs/fleet-surface.md` § Session end. Read it and execute it from there; do not restate it here.** The vault's `Manager Session` runbook § Guardrails item 6 — Session end carries the per-vault operating statement and points back at both. **What this file owns is the procedure and the report** — the knowledge is the contract doc, read directly at step 1; an agent behind this file would only add a hop between it and the doc it points at, and could not reach the drivers anyway, since they are session-scoped and a subagent has its own session.

<process>
1. **Read the contract, then disarm every model-waking driver it names** — in the order it gives, printing each driver's line as you go and *before* the call that removes it, since a deletion the operator cannot see is indistinguishable from a job that was never armed. Those lines **are** the report's `Disarmed` block — collect them, and do not print the set a second time.

   **Three forms, because the drivers do not share a read surface.** `✓` and `·` are claims a read surface confirmed; `~` is the honest form for a driver nothing can confirm — whether a stop was sent or none could be. Never print `✓` for a driver you could not read.

   | Form | Means | Available for |
   |---|---|---|
   | `✓ <driver> — <what it was>` | disarmed, confirmed | the enumerable driver — a cron job, which `CronList` reads |
   | `· <driver> — not armed` | read surface says it was never armed | the same one |
   | `~ <driver> — <what was done, or why nothing could be>` | **unconfirmed** — nothing can confirm it | every driver without a read surface — § Session end names them and why |

   ⚠️ **`~` carries a reason, and the reason must be true.** Two cases reach it and they are not the same sentence: a stop *was* sent (the driver was reachable but unreadable), or nothing *could* be sent (no id exists in this conversation to name it — the `Monitor` case, which § Session end bounds at ≤30 min for exactly this reason). Writing *"stop sent"* where no stop was sent is the same over-claim as a `✓` you could not read, one form down.

2. **Probe the state `stop` must not change** — read-only; the probe writes nothing and signals nothing:

   ```bash
   python3 ${CLAUDE_PLUGIN_ROOT:-$HOME/.claude/plugins/marketplaces/claude-supervisor}/scripts/stop-probe.py
   ```

   It prints the session id; the subject with its branch and vault; **one line per gate loop found**, each with its pid, uptime and command; the tick file's path, mtime and age; the gate ledger's path, last line and age; and the session ledger's open count. The report below has a slot for every one of those — if a line has no slot, that is this template's defect, not a value to drop.

2b. **Mark the stand-down as deliberate** — when the probe resolved a subject:

   ```bash
   python3 ${CLAUDE_PLUGIN_ROOT:-$HOME/.claude/plugins/marketplaces/claude-supervisor}/scripts/manager-liveness.py --stop --topic "<subject>"
   ```

   It writes `<slug>.stopped` into the liveness state dir, so a deliberate stand-down is not reported as a lapse. The next `/supervisor:manager-loop` re-arm clears it. No subject → skip and say so; there is no slug to mark. Report it as a `✓ stop marker — <path>` line under `Disarmed`.

3. **Print the report** — this command's own output format, and the only surface the contract does not own:

   ```
   ⏹️ STOP — <subject> (<branch> · <vault>) · session <sid8>
     Disarmed
       ✓ <driver> — <what it was>
       · <driver> — not armed
       ~ <driver> — <what was done, or why nothing could be>
     Left running — fleet-surface.md § Session end owns this contract
       ● gate loop   pid <pid>, up <etime>  <command>
       ● tick file   <path>  mtime <iso>  (<age>s ago)
       ● gate ledger <path>  <last line>  (<age>s ago)
     Session alive
       ● ledger <path> — <n> open of <m>, unchanged by this command
     Restart: /supervisor:manager-loop "<subject>"
   ```

   Print only the forms that occurred. A `●` line whose read came back absent takes its own form from `<error_handling>` instead of a fabricated value — the tick file and the gate loop each have one, and a dropped line reads as "checked and clean" when the truth may be "never looked".
</process>

<constraints>
- **Write no page, and no state file except the stop marker (step 2b).** `stop` is a disarm, not a bookkeeping act; the marker exists only so a deliberate stop is not reported as a lapse.
- End the turn with the session's normal closer. This report is not one.
</constraints>

<error_handling>
- **A driver that refuses to disarm** — print the failure verbatim together with the driver still standing, and say the loop is **not** fully stood down. Reporting success over a live driver is worse than reporting the failure.
- **No gate loop found**: print `⚠️ no gate loop found` in place of the gate-loop line, and say plainly that it may never have been armed. A fact to report, not to repair.
- **Tick file or gate ledger absent**: print `· tick file — absent` / `· gate ledger — absent` in place of that line, and say which of the two reasons it is — no subject recorded, so no path resolves, or the loop has not written one yet. Neither is a fault in this command.
- **No session id resolves**: the header prints `session (unknown)` verbatim, and the ledger line names the path it looked for. Say the id could not be resolved rather than leaving the slot empty — an empty slot reads as a value that was not printed, not as one that does not exist.
- **No subject recorded**: print the session id and carry on — the loop may have been armed before the subject was written. The branch and vault render as `—` with it, since all three come from the same record.
</error_handling>
