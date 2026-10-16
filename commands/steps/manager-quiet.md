---
# Read by the router, never invoked — without this key Claude Code
# registers this file as /<plugin>:steps:<name>.
user-invocable: false
---

# Quiet check

When: every tick, after the readings

2b. **A subject with nothing left to drive stands itself down, instead of spending a full-context tick every interval until the operator stops it by hand.** Run the check for this round's subject:

   ```bash
   python3 ${CLAUDE_PLUGIN_ROOT}/scripts/manager-quiet.py \
     --subject "<subject>" --vault "<vault>"
   ```

   It prints one machine line — `<verdict> TAB <n_runnable> TAB <n_quiet> TAB <consecutive_quiet>` — and, when it has stood the subject down, a second line naming the reopen trigger. It reads task frontmatter and nothing else, and it writes nothing but its own `<slug>.quiet` counter in the liveness state dir.

   ⚠️ **`armed` is the answer for every subject that still has work, and it is what you should expect almost always.** The stand-down needs `QUIET_TICKS_TO_STAND_DOWN` — **2** — **consecutive** quiet ticks, and a single runnable row resets the counter to 0. So a subject caught between two writes reads `armed 1 6 0`, and standing it down on that reading would retire a live manager. **On `armed`, continue the round normally and read nothing else here.**

   **On `stood-down`, the round ENDS — do not sweep, do not drive, do not spawn.** Disarm via the `/supervisor:stop` contract in `commands/stop.md`, which is the one home for *what* must be disarmed, which harness surface reaches each driver and which it cannot, and what must be left running. **Read it and execute it from there rather than from this file.** Two things are this step's own, because they are what differs from the operator running `stop` by hand:

   - **The marker is written before the report, not after it:** `python3 ${CLAUDE_PLUGIN_ROOT}/scripts/manager-liveness.py --stop --topic "<subject>"`. `commands/stop.md` step 2b writes it for the operator; a self-stop writes the same file for the same reason — so a deliberate stand-down is not later reported as a lapsed manager. Run it with **this session's** subject, which this round already resolved, never by re-deriving one from a probe that may not have been written.
   - **Print the script's second line verbatim** as the round's last output: `Stood down: zero runnable tasks; reopen on <trigger>`. The trigger is computed from the **same read that produced the verdict** — recomputing it here could name a different date than the one the stand-down was decided on. ⚠️ **The line names the trigger's content, never just the words `reopen on`**: the check reports the earliest future `defer_date` when a quiet row carries one, otherwise the blockers by name, otherwise the literal `no named trigger`. A stand-down whose line cannot say what would reopen the subject is the one case a reader cannot act on, so it says so in words rather than trailing off.

   ⚠️ **Never `ScheduleWakeup {stop:true}` to disarm.** It is not a driver of its own, and it has been measured returning *"there was no pending wakeup to cancel"* while `CronList` showed the job still armed and firing (2026-09-30) — a stand-down that used it left the loop running. `CronList` → `CronDelete` is what reaches a `ScheduleWakeup`-armed loop; `commands/stop.md` § process step 1 owns the full sweep and the `✓` / `·` / `~` reporting forms.

   ⚠️ **A subject stood down here is quiet, not finished, and nothing reopens it on its own.** The reopen trigger is a date or a blocker for the operator, or for a later round on that subject, to act on — auto-reopening is deliberately out of scope. That is exactly why the line names the trigger instead of reporting completion: a reader must be able to tell *"this subject is done"* from *"this subject is waiting for 2026-10-22"*.

   ⚠️ **Two bounds on arming this, both measured on the frozen-tree rule this one sits beside** (`65 Runbooks/Manager Session.md` § Cadence mechanics). **Never stand down over an unpublished `asked-of-you`** — it lives on this session's screen and in the open-items ledger and nowhere else, so disarming removes the one place the operator reads the question. And **the frozen-tree rule is a different predicate, not a second spelling of this one**: it fires on *no tracked file's mtime moving for ≥2 intervals*, while this fires on *zero runnable tasks*. A subject can satisfy either without the other, and neither may be read as evidence for the other.
