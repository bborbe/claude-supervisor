---
# Read by the router, never invoked — without this key Claude Code
# registers this file as /<plugin>:steps:<name>.
user-invocable: false
---

# Re-arm

When: every tick — arm `ScheduleWakeup` FIRST, before the read half

- **Re-arm FIRST — before the read half, not after it:** `ScheduleWakeup` with the `/supervisor:fleet-tick --interval <N>m` prompt — **name the TICK, never the arm** — at the **clamped** interval. Default **15 min**; floor **15 min** — a below-floor value is **clamped up and announced** (`⚠️ --interval 5m is below the 15m floor — clamped to 15m`), never rejected. **Never tighter than 15 min, never self-re-invoking** — cadence belongs to the scheduler. ⚠️ **It belongs at the head of the round:** `ScheduleWakeup` is **one-shot per call**, so a long round (a drive leg, an escalation, a gate relay) never reaches an end-of-round re-arm and the loop then stops **silently**. Arming first makes a missed re-arm **impossible**; a round that later discovers a reason to slow down or stop re-arms again at that point. ⚠️ **`noop` is required by `ScheduleWakeup` and cannot be honest at the head** — the round has not run yet — so arm with `noop: false`. ⚠️ **Unlike `/manager-loop`, this side has no `manager-liveness.py --arm` record to move, and none must be added to force symmetry:** the fleet loop carries no liveness store, and nothing here keys on `noop`. ⚠️ **A second `ScheduleWakeup` supersedes the first rather than stacking** — so a later re-arm in the same round is a correction, never a second loop.
