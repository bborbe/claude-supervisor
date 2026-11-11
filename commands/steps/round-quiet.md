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

   It prints one machine line — `<verdict> TAB <n_runnable> TAB <n_quiet> TAB <consecutive_quiet>` — and a second line naming what happens next: `Sleep until <instant>` on `sleep-until`, `Stood down: … reopen on <trigger>` on `stood-down`. It reads task frontmatter and nothing else, and it writes nothing but its own `<slug>.quiet` counter in the liveness state dir.

   ⚠️ **`armed` is the answer for every subject that still has work, and it is what you should expect almost always.** The stand-down needs `QUIET_TICKS_TO_STAND_DOWN` — **2** — **consecutive** quiet ticks, and a single runnable row resets the counter to 0. So a subject caught between two writes reads `armed 1 6 0`, and standing it down on that reading would retire a live manager. **On `armed`, continue the round normally and read nothing else here.**

   **On `sleep-until`, the round ENDS and the subject sleeps to the named instant — do not sweep, do not drive, do not spawn, and never `/supervisor:stop`.** This is the date-gated case: every quiet row carries a future `defer_date`, so the subject is not finished and not blocked, it is waiting on a clock it already knows. Standing it down here is what put a human in the loop every morning (measured 2026-11-07, the Several Clusters Manager re-armed by hand twice in one day).

   - **Record the sleep BEFORE arming the wakeup:** `python3 ${CLAUDE_PLUGIN_ROOT}/scripts/manager-liveness.py --sleep --topic "<subject>" --until "<instant>"`, with `<instant>` copied from the script's own second line. It writes `<slug>.sleeping`, which makes `--state` read `SLEEPING` instead of `STALE` while the instant is future — without it the frozen cadence file reads as a lapse and the fleet restarts the subject.
   - **Arm the wakeup for that same instant — by the right spelling for its distance — on the topic tier only.** ⚠️ **The goal tier arms no wakeup at all, and the gate cannot recover a sleep:** `manager-predispatch.digest_of` carries no `defer_date`, and its actionable clause reads the stored bucket sets, so a `defer_date` arriving moves neither input and the gate never fires. A goal-tier sleep is recovered instead by `topic-keepalive.py` restarting the manager off the **`STALE`** reading once the `.sleeping` marker expires — the same recovery the old cadence relied on. `manager-liveness.py` deliberately does **not** apply its gate-driven `OK` to an expired sleep marker, so that `STALE` survives. Skip the arm block below on the goal branch; the `--sleep` record above makes `--state` read `SLEEPING` until the instant, and the next arm clears `<slug>.sleeping`, which is what ends the sleep.

     ⚠️ **`ScheduleWakeup` clamps `delaySeconds` to [60, 3600], silently.** A sleep is usually hours or a day out, so passing `instant − now` to it does not sleep the subject — it **wakes it every hour** for the whole gap, and each of those rounds re-reads the same `sleep-until` and re-sleeps. That is exactly the tick-burn this verdict exists to remove, produced by the instruction that was meant to prevent it. Measured 2026-11-08: a 69 426 s (~19 h) sleep became an hourly wake.

     - **`instant − now` ≤ 3600** → `ScheduleWakeup` with that delay.
     - **anything longer** → a one-shot `CronCreate` pinned to the instant's **minute** (`recurring: false`). `ScheduleWakeup` takes `delaySeconds`; `CronCreate` takes a `cron` expression, which is what carries a date.

     ⚠️ **A one-shot wakeup only fires when the REPL is idle** — measured three times on 2026-11-05 — so this is the primary wake, not the only one: `scripts/topic-keepalive.py` re-arms a sleep whose instant has passed with no arm recorded.
   - **Print the script's second line verbatim** as the round's last output: `Sleep until <instant>`.

   **On `stood-down`, the round ENDS — do not sweep, do not drive, do not spawn.** Disarm via the `/supervisor:stop` contract in `commands/stop.md`, which is the one home for *what* must be disarmed, which harness surface reaches each driver and which it cannot, and what must be left running. **Read it and execute it from there rather than from this file.** Two things are this step's own, because they are what differs from the operator running `stop` by hand:

   - **The marker is written before the report, not after it:** `python3 ${CLAUDE_PLUGIN_ROOT}/scripts/manager-liveness.py --stop --topic "<subject>"`. `commands/stop.md` step 2b writes it for the operator; a self-stop writes the same file for the same reason — so a deliberate stand-down is not later reported as a lapsed manager. Run it with **this session's** subject, which this round already resolved, never by re-deriving one from a probe that may not have been written.
   - **Print the script's second line verbatim** as the round's last output: `Stood down: zero runnable tasks; reopen on <trigger>`. The trigger is computed from the **same read that produced the verdict** — recomputing it here could name a different date than the one the stand-down was decided on. ⚠️ **The line names the trigger's content, never just the words `reopen on`**: the check reports the earliest future `defer_date` when a quiet row carries one, otherwise the blockers by name, otherwise the literal `no named trigger`. A stand-down whose line cannot say what would reopen the subject is the one case a reader cannot act on, so it says so in words rather than trailing off.

   ⚠️ **Never `ScheduleWakeup {stop:true}` to disarm.** It is not a driver of its own, and it has been measured returning *"there was no pending wakeup to cancel"* while `CronList` showed the job still armed and firing (2026-09-30) — a stand-down that used it left the loop running. `CronList` → `CronDelete` is what reaches a `ScheduleWakeup`-armed loop; `commands/stop.md` § process step 1 owns the full sweep and the `✓` / `·` / `~` reporting forms.

   ⚠️ **A subject stood down here is quiet, not finished, and nothing reopens it on its own** — **unless it slept instead.** That split is the point: `sleep-until` auto-reopens at its named instant — on the topic tier through its own armed wakeup, on the goal tier through `topic-keepalive.py`'s `STALE` recovery, since the gate cannot see a `defer_date` arrive — because the only thing it was waiting on is a clock; `stood-down` needs a human or a later round, because its trigger is a date someone must act on or a blocker someone must clear. So a subject with a future `defer_date` **must** reach `sleep-until` and never `stood-down`; a stand-down whose trigger is a date is the defect this split removes. Either way the line names the trigger rather than reporting completion, so a reader can tell *"this subject is done"* from *"this subject is waiting for 2026-10-22"*.

   ⚠️ **Two bounds on arming this, both measured on the frozen-tree rule this one sits beside** (`65 Runbooks/Manager Session.md` § Cadence mechanics). **Never stand down over an unpublished `asked-of-you`** — it lives on this session's screen and in the open-items ledger and nowhere else, so disarming removes the one place the operator reads the question. And **the frozen-tree rule is a different predicate, not a second spelling of this one**: it fires on *no tracked file's mtime moving for ≥2 intervals*, while this fires on *zero runnable tasks*. A subject can satisfy either without the other, and neither may be read as evidence for the other.
