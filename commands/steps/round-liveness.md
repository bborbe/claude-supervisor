---
# Read by the router, never invoked — without this key Claude Code
# registers this file as /<plugin>:steps:<name>.
user-invocable: false
---

# Liveness record

When: every round, before the pre-dispatch gate

- **Write the liveness record every round, and before anything can stop the round.** `manager-liveness.py --arm` writes the `<slug>.arms` cadence that `--state` classifies and that `answer-path-check.py` gates a headless spawn on. A round that skips it leaves a live manager reading `STALE` one period later, and the fleet sweep then reports a healthy manager as lapsed. This step is that write.

  ⚠️ **It runs BEFORE the pre-dispatch gate.** A no-change by-hand round stops at that gate (`commands/goal-drive.md` § Pre-dispatch gate, exit 0) and never reaches the router's later steps — so a record placed after the gate is absent on exactly the rounds that most often go stale. That is the gap this step closes; do not move it below the gate.

  ⚠️ **The record is the ROUND's; the `ScheduleWakeup` arm stays one-shot.** `commands/goal-loop.md` § step 3 arms the loop once, and `commands/goal-drive.md` opens with *"This is the round, not the arm."* Both mean the **wakeup**. This step writes the liveness **record** every round and never arms a wakeup — two files, two jobs. Reading the one-shot rule as covering the record is what left the record unwritten.

  ⚠️ **Skip a stood-down or sleeping manager.** `--arm` **clears** both the `<slug>.stopped` and the `<slug>.sleeping` marker (`scripts/manager-liveness.py`), so arming a subject the operator deliberately stopped silently un-stands it down, and arming a sleeping one silently un-sleeps it — handing `topic-keepalive.py` a live cadence to keep in both cases. Read `--state` first and arm only when it is neither `STOPPED` nor `SLEEPING`. ⚠️ **The sleep is the case that bites without a warning:** a date-gated subject is SUPPOSED to have an ageing cadence, so the arm looks like the ordinary every-round write and quietly cancels the sleep the same round just armed.

  ⚠️ **`--interval` is required and must be > 0** — `manager-liveness.py` hard-errors otherwise (`--arm needs --interval > 0`), and neither round command otherwise reads it. Take the seconds the wakeup prompt carried, clamped exactly as the arm clamps it (default **15 min**, floor **5 min**), and pass **bare seconds** (`900`), never `15m` or `900s`. A by-hand round whose prompt carries no interval uses the default.

  ⚠️ **`--branch` is explicit, never auto-detected here.** `goal` from `commands/goal-drive.md`, `topic` from `commands/topic-drive.md`. Omitting it falls back to auto-detection, which keeps a subject in whichever namespace its existing record or page happens to match — the one place an explicit tier is cheaper than a correct guess.

  ```bash
  PLUGIN=${CLAUDE_PLUGIN_ROOT:-$HOME/.claude/plugins/marketplaces/claude-supervisor}
  STATE=$(python3 "$PLUGIN/scripts/manager-liveness.py" --state --subject "<subject>" --branch "<goal|topic>" --vault "<vault-root>" | awk '{print $1}')
  case "$STATE" in
    STOPPED) echo "liveness: STOPPED — no arm (deliberately stood down)" ;;
    SLEEPING) echo "liveness: SLEEPING — no arm (date-gated; the wake ends it)" ;;
    *) python3 "$PLUGIN/scripts/manager-liveness.py" --arm --subject "<subject>" --branch "<goal|topic>" --interval "<clamped-seconds>" ;;
  esac
  ```

  ⚠️ **`--state` always exits 0 and prints the verdict as its first field** (`OK` / `STALE` / `STOPPED` / `SLEEPING` / `UNARMED` / `INVALID`), so read the field, never the exit code. `UNARMED` — no record yet — is armed like any other state; only `STOPPED` and `SLEEPING` skip.

  ⚠️ **`--vault` is the vault ROOT**, the same value the round's other scripts take; it is what lets `--state` resolve a subject that has no record yet.
