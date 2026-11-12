---
# Read by the router, never invoked — without this key Claude Code
# registers this file as /<plugin>:steps:<name>.
user-invocable: false
---

# Re-arm

When: every tick — arm `ScheduleWakeup` FIRST, before the read half

- **Open the round's receipt FIRST, before the arm.** The round's tail checks that this step and the digest both ran (`commands/steps/fleet-round-tail.md`), and its window is `round_started_at` — so the window must open **before** the arm writes its epoch. Opened after, the round's own arm lands *before* the window and `rearm_shape` rejects it, printing `skipped: re-arm` on every round that armed correctly: the exact false red the check exists to prevent.

  ```bash
  python3 "${CLAUDE_PLUGIN_ROOT}/scripts/manager-receipt.py" --start --subject "Fleet Manager" --branch fleet
  ```

  ⚠️ **`--start` clears the fields**, so a round is judged only on artifacts it produced itself, never on a previous round's. It is bookkeeping, not a step in the round — it does not displace `ScheduleWakeup`, which is still the first *action* below.

- **Re-arm FIRST — before the read half, not after it:** `ScheduleWakeup` with the `/supervisor:fleet-drive --interval <N>m` prompt — **name the ROUND, never the arm** — at the **clamped** interval. Default **15 min**; floor **15 min** — a below-floor value is **clamped up and announced** (`⚠️ --interval 5m is below the 15m floor — clamped to 15m`), never rejected. **Never tighter than 15 min, never self-re-invoking** — cadence belongs to the scheduler. ⚠️ **It belongs at the head of the round:** `ScheduleWakeup` is **one-shot per call**, so a long round (a drive leg, an escalation, a gate relay) never reaches an end-of-round re-arm and the loop then stops **silently**. Arming first makes a missed re-arm **impossible**; a round that later discovers a reason to slow down or stop re-arms again at that point. ⚠️ **`noop` is required by `ScheduleWakeup` and cannot be honest at the head** — the round has not run yet — so arm with `noop: false`. ⚠️ **This side DOES keep a liveness record, and it is written here — at every round's head, beside the `ScheduleWakeup`.** It was previously absent "rather than added for symmetry", and that absence was not neutral: `scripts/answer-path-check.py` reads a session's registration, and a session it cannot find reads as having **no answer path**, so the spawn guard refused **every** headless spawn the Fleet Manager made (measured 2026-10-23). The fleet round genuinely answers headless workers' parked prompts — it runs the wrangler and watches the queue — so the record states a fact that was always true.

   ```bash
   python3 "${CLAUDE_PLUGIN_ROOT}/scripts/fleet-register.py" --vault "$(basename "$PWD")" \
     && python3 "${CLAUDE_PLUGIN_ROOT}/scripts/manager-liveness.py" --arm \
          --subject "Fleet Manager" --branch fleet --interval "<clamped-seconds>"
   ```

   ⚠️ **`&&`, not two independent lines, and the quoting is load-bearing.** A registration that is refused must NOT be followed by an arm: that is precisely the registered-nowhere-but-armed state this step exists to prevent. Unquoted, `--vault <vault>` parses as a `<` redirection and `--interval <clamped seconds>` splits at the space into a redirection plus a stray word — the block would not run at all. ⚠️ **`$(basename "$PWD")` is the vault source**, and it is the right one: the fleet session runs with a vault as its cwd (that is what `commands/open.md` Step 0 resolves too), and the value is stored on the registration and read back by `scripts/reset.py` and `scripts/who-needs-my-help.py` to scope the session — a guessed or omitted value files a registration those readers mis-scope. ⚠️ **If `fleet-register.py` exits 1** (a missing `CLAUDE_CODE_SESSION_ID`, an unwritable state dir) it prints `REFUSED\t<reason>`; report that line and **skip the arm** — the session has no answer path, so the round must say so rather than run headless spawns it cannot serve. ⚠️ **If the arm exits non-zero**, the session is registered but **UNARMED**, which `--state` refuses exactly like no registration at all — report it and do not treat the round as healthy. The usual cause is a bad `--interval`: it takes bare **seconds** (`900`), never `15m` or `900s`.

   ⚠️ **Both lines, in that order, and both are load-bearing.** `fleet-register.py` writes `~/.claude/state/worker-manager/<session-id>.json` — the registration `answer-path-check.py` gates on, idempotent and safe to rewrite every round. `--arm` writes the cadence that `--state` then classifies, so a session registered but never armed reads `UNARMED` and is refused exactly like an unregistered one. ⚠️ **The cadence expires**: `--state` calls it STALE past the observed period plus `SLACK` (180 s) — the declared `interval` plus the same `SLACK` before four arms exist — so an arm written once at the first round goes stale roughly one period later and every later spawn is refused again. This step is the round, so this is where it must be refreshed. ⚠️ **`--branch fleet` is its own tier**, not a stand-in for `goal` or `topic`: the Fleet Manager manages no subject page, and the slug `fleet--fleet-manager` is what keeps it out of those namespaces. The interval is the **clamped** one, in **seconds**, never the requested minutes. ⚠️ **A second `ScheduleWakeup` supersedes the first rather than stacking** — so a later re-arm in the same round is a correction, never a second loop.

- **Emit the two answer reaches, beside the arm — this is the evidence the spawn gate will read.** `scripts/answer-path-check.py` records them because an armed loop is not an answer channel. Measured 2026-11-09, spawner `0110c4a3` read `exit 0` while its 27 workers' parks were released 56 times by the attention store and **never once by it** — an armed loop that had never answered anything, certified as able to.

  ⚠️ **Stage 1 only REPORTS a reach; the verdict is still the loop state.** `check()` passes on `OK` and appends `reach: mcp=… wrapper=…` to its `detail`. The verdict flips onto the reaches in stage 2, gated on a coverage reading. Emit them now regardless — the coverage reading is what licenses the flip.

  ⚠️ **This file is the FLEET tier's copy. The goal and topic tiers emit the same two reaches from `commands/steps/round-liveness.md`, every round.** All three tiers open headless workers, so all three must refresh; a tier that emitted neither would have its headless spawns refused once stage 2 ships.

  **Leg `mcp` is ONE tool call.** `answer_permission` with a sentinel `request_id` that no worker holds; the server finds no park, consults the wrapper, replies with the foreign-park message, and **that reply writes the reach**. If the call is refused before it reaches the server — a classifier denial, the outage case the Impact names — no record is written, which is the correct outcome.

  ```
  mcp__supervisor__answer_permission(request_id="answer-path-reach-sentinel", behavior="deny")
  ```

  ⚠️ **On the current server this leg is written by ANY `answer_permission` call, real park or sentinel.** The Go handler (`claude-wrapper/pkg/roster_mcp.go` `answerPermission`) records the reach **on entry**, before it decodes its arguments or looks the request id up — so a manager that answered a real park this round already wrote the leg. ⚠️ **This paragraph previously claimed the opposite**, carrying over the JS server's settle-path reasoning ("a real park never reaches the foreign-park arm"); that was true of the retired `server/supervisor.mjs` and is **false of the server the fleet runs**. The sentinel is still required, because a manager that answers no parks would otherwise write nothing.

  **Leg `wrapper` is a Bash `curl`, in its own call, then the recorder in another.** Two calls, deliberately: folding the recorder onto the same line changes the command shape the permission classifier judges, and a green reach would then prove the wrong thing.

  ```bash
  curl -s -o /dev/null -w '%{http_code}' -X POST \
    "${SUPERVISOR_WRAPPER_URL:-http://localhost:18090}/api/1.0/sessions/${CLAUDE_CODE_SESSION_ID}/answer" \
    -H 'content-type: application/json' \
    -d '{"request_id":"answer-path-reach-sentinel","behavior":"allow"}'
  ```

  ```bash
  python3 "${CLAUDE_PLUGIN_ROOT}/scripts/answer-reach.py" --status "<the status printed above>"
  ```

  ⚠️ **`SUPERVISOR_WRAPPER_URL` must be an ORIGIN, with no path.** `server/config.mjs` resolves it to `url.origin`, so a value carrying a path would have the server call `/api/1.0/...` while this curl posts to `<path>/api/1.0/...` — a route-level `404`, which the recorder accepts as a valid reach and which would therefore certify a route never reached.

  ⚠️ **`409` or `404` are the only statuses the recorder accepts**, and they are the only ones that mean the request arrived and was refused there harmlessly — 409 no open question, 404 unknown session. Any other status writes nothing and says why: a `5xx`, an unreachable wrapper, or a command the classifier denied all leave this session with **no** wrapper reach, which is exactly what the gate should then refuse. ⚠️ **Do not retry a refused curl into a passing status** — the refusal is the finding, and a session that cannot reach the wrapper route must not be certified as able to.

  ⚠️ **A `--sleep`ped manager stops re-arming, so its reaches age out and it is refused.** That is correct rather than a regression: a manager that has stopped its loop is not answering parks. Re-arm before spawning headless workers.

  ⚠️ **The two legs are keyed on two resolutions of "this session", and they can disagree.** `mcp` uses `parentSessionId()` — what the spawn gate itself uses — while `wrapper` uses this session's `$CLAUDE_CODE_SESSION_ID`, since a shell command has nothing else. They agreed in 41 of 42 measured servers. A disagreement fails **closed**: the probe finds at most one leg and refuses, which is why it reports each leg separately — so a refusal naming only `wrapper=absent` on a session that did run the curl means the ids diverged, not that the route was unreachable.

- **Record the arm on the receipt, after it lands.** The window was opened above, before the arm, for the reason stated there.

  ```bash
  python3 "${CLAUDE_PLUGIN_ROOT}/scripts/manager-receipt.py" --record --subject "Fleet Manager" --branch fleet \
    --step re-arm --path "${MANAGER_LIVENESS_STATE_DIR:-$HOME/.claude/state/sweep-gate}/fleet--fleet-manager.arms"
  ```

  ⚠️ **`--branch fleet` is what makes the receipt read the same `fleet--fleet-manager` files this step just wrote** — without it the check looks for a bare `fleet-manager` slug nothing ever writes, and reports `skipped: re-arm` on a round that armed perfectly. ⚠️ **Recording a ring path after a refused arm is safe**: `rearm_shape` validates an epoch *inside* the window, so a skipped arm still reports `skipped: re-arm` at the tail rather than passing on the file's existence.
