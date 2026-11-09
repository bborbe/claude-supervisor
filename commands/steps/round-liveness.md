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

  ⚠️ **Skip a stood-down or sleeping manager.** `--arm` **clears** the `<slug>.sleeping` marker (`scripts/manager-liveness.py`), so arming a sleeping subject silently un-sleeps it — handing `topic-keepalive.py` a live cadence to keep. Read `--state` first and arm only when it is neither `STOPPED` nor `SLEEPING`. ⚠️ **A stand-down is no longer cleared by an arm, and this step is why:** an arm runs at every round, so it is the liveness *reading*; a reading that cleared the marker destroyed the evidence that would have prevented the wrong action (measured 2026-11-07 — stood down 12:13, read 12:51, marker gone, Fleet Manager read `STALE` and restarted at 13:14). Only `--arm --resume` resumes, and only the loop-start arms (`goal-loop` / `topic-loop` / `fleet-loop`) pass it. ⚠️ **Never pass `--resume` from this step** — it is the round's reading, not a restart. ⚠️ **The sleep is the case that bites without a warning:** a date-gated subject is SUPPOSED to have an ageing cadence, so the arm looks like the ordinary every-round write and quietly cancels the sleep the same round just armed.

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

- **Emit the two answer reaches, every round, beside the liveness write.** The `mcp` and `wrapper` reaches are what `scripts/answer-path-check.py` reads instead of trusting an armed loop (measured 2026-11-09: spawner `0110c4a3` read exit 0 while its 27 workers' parks were released 56 times by the store and never by it). They **age out**, so they belong in the ROUND, not the one-shot arm: a manager that writes them once at loop start has a `wrapper` leg that expires one period later.

  ⚠️ **This is the goal and topic tiers' half of a contract the fleet round writes in `commands/steps/re-arm.md`.** All three tiers can open headless workers and all three answer parks, so all three must refresh. The fleet round does it per tick in its own step; this one covers `commands/goal-drive.md` and `commands/topic-drive.md`, which run this step every round. A tier that emitted neither would have its headless spawns refused once the probe's verdict flips onto the reaches.

  **Leg `mcp` needs nothing here** — the server records it on `answer_permission`'s existing foreign-park reply, which a call with an unknown `request_id` already gets.

  **Leg `wrapper` is two calls, deliberately separate** so the curl keeps the exact command shape the permission classifier judges:

  ```bash
  curl -s -o /dev/null -w '%{http_code}' -X POST \
    "${SUPERVISOR_WRAPPER_URL:-http://localhost:18090}/api/1.0/sessions/${CLAUDE_CODE_SESSION_ID}/answer" \
    -H 'content-type: application/json' \
    -d '{"request_id":"answer-path-reach-sentinel","behavior":"allow"}'
  ```

  ```bash
  python3 "${CLAUDE_PLUGIN_ROOT}/scripts/answer-reach.py" --status "<the status printed above>"
  ```

  ⚠️ **`SUPERVISOR_WRAPPER_URL` must be an ORIGIN, with no path.** `server/config.mjs` resolves it to `url.origin`, so a value carrying a path would have the server call `/api/1.0/...` while this curl posts to `<path>/api/1.0/...` — a route-level `404`, which the recorder accepts as a valid reach and which would therefore certify a route that was never reached.

  ⚠️ **`409` or `404` are the only statuses the recorder accepts**, and the only ones that mean the request arrived and was refused harmlessly. Any other status writes nothing and says why: a `5xx`, an unreachable wrapper, or a command the classifier denied all leave this session with **no** wrapper reach. **Do not retry a refused curl into a passing status** — the refusal is the finding.

## A second beat at the tail

**Run the block above a SECOND time as the tick's last write.** The head arm is what keeps a by-hand round that stops at the pre-dispatch gate from going unrecorded; this second run is what stops a round that outruns `period + SLACK` from reaching its own end already reading `STALE` — the reading that made a live, working manager look lapsed to every session checking `--state` mid-round.

⚠️ **After `commands/steps/round-tail.md` step 8's receipt check, never before it.** That check is what proves the round ran its legs; a beat written ahead of it would make a skipped leg invisible.

⚠️ **Same allow-list, same arguments, no `--resume`.** Only `OK` / `STALE` / `UNARMED` / `INVALID` arm; `STOPPED`, `SLEEPING`, `WRONG-BRANCH` and an empty `STATE` do not. A `SLEEPING` subject is the one that bites: a date-gated manager is *supposed* to have an ageing cadence, so an unconditional arm looks like the ordinary write and silently cancels the sleep the round just armed.

⚠️ **It is a liveness RECORD, not the `ScheduleWakeup` arm.** `commands/steps/round-tail.md` step 7 says *"do not re-arm here"*, and that is the **wakeup**, one-shot per call. The paragraph above already separates the two (*"two files, two jobs"*).

⚠️ **It is a ninth item in `commands/steps/round-tail.md`, not a line in this file.** That placement is deliberate: a mandatory tail action stated only here, at the head, has to survive a whole round in the reader's memory while the file it is actually following at the tail says nothing — which is the "a round that skips a step reads as complete" failure this beat exists to prevent. The cost is that `round-tail.md` now carries 31626 bytes against its 30720 body budget, and `scripts/budget-baseline.json` records that size. ⚠️ **That entry is a ratchet, not an allowance:** `scripts/check-budget.py` fails when the file grows past it, so any further addition to `round-tail.md` needs its own justification, not this one.

⚠️ **What it costs, stated rather than implied.** Two beats per round split each round gap into `D` (head→tail) and `I−D` (tail→next head), so the `.arms` population becomes `[D, I−D, …]` and `observed_period()`'s p90 rises toward `max(D, I−D)`. The staleness limit therefore **grows with the round**: the false `STALE` is removed by re-measuring the cadence, not by shortening the gap. A round longer than the limit still reads `STALE` **during** it — only the post-round reading is fixed. A mid-round beat would shorten the gap instead; that is a different change and is not made here.
