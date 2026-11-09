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

  ⚠️ **Skip a stood-down or sleeping manager.** `--arm` **clears** the `<slug>.sleeping` marker (`scripts/manager-liveness.py`), so arming a sleeping subject silently un-sleeps it — handing `topic-keepalive.py` a live cadence to keep. Read `--state` first and arm only when it is neither `STOPPED` nor `SLEEPING`. ⚠️ **A stand-down is no longer cleared by an arm, and this step is why:** an arm runs at every round, so it is the liveness *reading*; a reading that cleared the marker destroyed the evidence that would have prevented the wrong action (measured 2026-11-07 — stood down 12:13, read 12:51, marker gone, Fleet Manager read `STALE` and restarted at 13:14). Only `--arm --resume` resumes, and only the two loop-start arms pass it. ⚠️ **Never pass `--resume` from this step** — it is the round's reading, not a restart. ⚠️ **The sleep is the case that bites without a warning:** a date-gated subject is SUPPOSED to have an ageing cadence, so the arm looks like the ordinary every-round write and quietly cancels the sleep the same round just armed.

  ⚠️ **`--interval` is required and must be > 0** — `manager-liveness.py` hard-errors otherwise (`--arm needs --interval > 0`), and neither round command otherwise reads it. Take the seconds the wakeup prompt carried, clamped exactly as the arm clamps it (default **15 min**, floor **5 min**), and pass **bare seconds** (`900`), never `15m` or `900s`. A by-hand round whose prompt carries no interval uses the default.

  ⚠️ **`--branch` is explicit, never auto-detected here.** `goal` from `commands/goal-drive.md`, `topic` from `commands/topic-drive.md`. Omitting it falls back to auto-detection, which keeps a subject in whichever namespace its existing record or page happens to match — the one place an explicit tier is cheaper than a correct guess.

  ```bash
  PLUGIN=${CLAUDE_PLUGIN_ROOT:-$HOME/.claude/plugins/marketplaces/claude-supervisor}
  STATE=$(python3 "$PLUGIN/scripts/manager-liveness.py" --state --subject "<subject>" --branch "<goal|topic>" --vault "<vault-root>" | awk '{print $1}')
  case "$STATE" in
    OK|STALE|UNARMED|INVALID) python3 "$PLUGIN/scripts/manager-liveness.py" --arm --subject "<subject>" --branch "<goal|topic>" --interval "<clamped-seconds>" ;;
    STOPPED) echo "liveness: STOPPED — no arm (deliberately stood down)" ;;
    SLEEPING) echo "liveness: SLEEPING — no arm (date-gated; the wake ends it)" ;;
    *) echo "liveness: ${STATE:-refused (subject unresolvable — see stderr)} — no arm" ;;
  esac
  ```

  ⚠️ **Arm on an allow-list, never on a fallback.** Only `OK` / `STALE` / `UNARMED` / `INVALID` arm; every other answer — `STOPPED`, `SLEEPING`, `WRONG-BRANCH`, and an **empty** `STATE` — does not. `--state` prints its verdict as the first field and **exits 0 for every state**, but it **exits 3 with nothing on stdout** when the subject resolves neither a record under any branch nor a page under `--vault` (an abbreviated or mistyped name), so `STATE` comes back empty. `WRONG-BRANCH` means the record lives on another tier. Both are misreads of the subject, and arming on a misread re-arms the wrong record — a live loop's cadence history reset, or a stand-down reversed. The previous `*) --arm` fallback did exactly that for any word it did not name; `scripts/tests/test_round_liveness_step.py` pins this block.

  ⚠️ **`--vault` is the vault ROOT**, the same value the round's other scripts take; it is what lets `--state` resolve a subject that has no record yet.
