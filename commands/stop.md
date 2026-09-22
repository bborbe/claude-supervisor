---
description: Stand this session's manager loop down — disarm the model-waking cadence, leave the model-free gate loop running, keep the session (and its asks ledger) alive, and report what was disarmed. The disarm contract is docs/fleet-surface.md § Session end.
allowed-tools:
  - CronList
  - CronDelete
  - ScheduleWakeup
  - TaskStop
  - Bash(python3:*)
argument-hint: "(no argument)"
---

Stand the manager loop **this session** is running down. This is the operator's verb; it is not a close. It takes no argument — the cadence is session-scoped, so there is nothing to name.

**The disarm contract is `${CLAUDE_PLUGIN_ROOT}/docs/fleet-surface.md` § Session end** — what must be disarmed, which harness surface reaches each driver and which it cannot, and what must be left running. **Read it, and do not restate it here.** The vault's `Worker Manager Session` runbook § Guardrails item 6 carries the per-vault operating procedure and points back at both.

<process>
1. **Disarm the cadence drivers.**

   | Driver | Reach it with |
   |---|---|
   | a `CronCreate` job held by this session | `CronList` → `CronDelete` per job id |
   | a `ScheduleWakeup` loop | `ScheduleWakeup` with `stop: true` |
   | a `Monitor` / background task this session armed | `TaskStop` with its task id |

   `CronList` **first**, and print every job before deleting it — id, schedule, prompt prefix. A deletion the operator cannot see is indistinguishable from a job that was never armed. Delete **every** job this session holds: a session-scoped cron job in a manager session is part of the loop by construction, and if one is not, the printed line is how the operator notices. Then `ScheduleWakeup` with `stop: true` and no other field. Then `TaskStop` for each background task **you can name**.

2. **Probe the state `stop` must not change** — read-only, writes nothing, signals nothing:

   ```bash
   python3 ${CLAUDE_PLUGIN_ROOT:-$HOME/.claude/plugins/marketplaces/claude-supervisor}/scripts/stop-probe.py
   ```

   It prints the subject, the gate loop's pid and uptime, the tick file's mtime and age, and the ledger's open count.

3. **Print the report.**

   ```
   ⏹️ STOP — <subject> (<branch>) · session <sid8>
     Disarmed
       ✓ CronDelete <id>  <schedule>  "<prompt prefix>"
       ✓ ScheduleWakeup — dynamic loop ended
       ✓ TaskStop <id> — <what it was>
     Left running — fleet-surface.md § Session end owns this contract
       ● gate loop  pid <pid>, up <etime>
       ● tick file  <path>  mtime <iso>  (<age>s ago)
     Session alive, not closed
       ● ledger <path> — <n> open of <m>, unchanged by this command
     Restart: /supervisor:worker-manager "<subject>"
   ```

   Print `·` for a driver that was **not** armed rather than dropping the line — a missing line reads as "checked and clean" when the truth may be "never looked".
</process>

<constraints>
- **Write nothing.** No task, topic or goal page; no state file; no `open-items.py` mutation. `stop` is a disarm, not a bookkeeping act.
- **Never end with `/vault-cli:session-close`, and never offer it.**
- **Never signal the gate loop** — the probe prints it; § Session end states why it must survive.
- End the turn with the session's normal closer. This report is not one.
</constraints>

<error_handling>
- **A driver that refuses to disarm** — `CronDelete` errors, `ScheduleWakeup` rejects the stop: print the failure verbatim together with the driver still standing, and say the loop is **not** fully stood down. Reporting success over a live driver is worse than reporting the failure.
- **No gate loop found**: print `⚠️ no gate loop found` and say plainly that it may never have been armed. That is a fact to report, not to repair.
- **No subject recorded**: print the session id and carry on — the loop may have been armed before the subject was written.
</error_handling>
