---
description: Stand a manager loop down — bare, disarm this session's own model-waking cadence; by name, resolve a topic, goal or task, refuse unless this session owns it, and write its stand-down marker. The disarm contract is docs/fleet-surface.md § Session end.
allowed-tools:
  - Read
  - CronList
  - CronDelete
  - TaskStop
  - Bash(python3:*)
  - Bash(vault-cli:*)
  - Bash(find:*)
argument-hint: "[<topic|goal|task name>] [--task|--goal|--topic] [--vault <name>]"
---

Stand a manager loop down. Two forms:

- **Bare — this session's own loop.** Disarm the model-waking cadence, leave the model-free gate loop running, keep the session (and its asks ledger) alive, and report what was disarmed.
- **By name — a topic, goal or task.** Resolve the name to its subject page, refuse unless this session owns it, then write that subject's stand-down marker. This is the operator's verb; it is not a close, and it does not touch another session's pane.

The bare form is the default and is unchanged. The by-name form exists so a stand-down is **named rather than implied**: the subject is resolved from its page, the caller's ownership is checked against that page, and the marker is written for a subject the caller actually owns. It does **not** widen who may stop what — a session can still stand down only its own subject; what changes is that it can now name it, and that a refusal is a printed verdict rather than a silent no-op.

**The disarm contract — what must be disarmed, which harness surface reaches each driver and which it cannot, and what must be left running — is `${CLAUDE_PLUGIN_ROOT}/docs/fleet-surface.md` § Session end. Read it and execute it from there; do not restate it here.** The vault's `Manager Session` runbook § Guardrails item 6 — Session end carries the per-vault operating statement and points back at both. **What this file owns is the procedure and the report** — the knowledge is the contract doc, read directly at step 1; an agent behind this file would only add a hop between it and the doc it points at, and could not reach the drivers anyway, since they are session-scoped and a subagent has its own session.

<by_name>
Run this section only when a name survives argument parsing. A bare call skips it entirely and runs `<process>`.

**Parse `$ARGUMENTS` yourself — never positional argument references.** A title is multi-word, so a positional resolves to one word of it.

- **name (optional):** everything that is not a recognised flag. May contain spaces; never word-split it, and quote it when passing to `vault-cli`.
- **`--task` / `--goal` / `--topic`:** force the type, skipping resolution. Use when a name matches more than one kind.
- **`--vault <name>`:** target vault, by name. Overrides cwd detection.

1. **Resolve the vault twice, because two readers want two shapes.** `vault-cli` takes the vault **name**; `owner.py --vault` takes the vault **root path** (`locate()` joins the subject folder onto it). Resolve the name first (`--vault` → the vault whose `path` contains the cwd → `default_vault` from config), then read its `path` from `vault-cli config list --output json`. If either arm fails, STOP. ⚠️ Passing the **name** to `owner.py` joins `private-personal/24 Goals`, which resolves nowhere — every by-name stop would refuse with a false `not-owned`.

2. **Resolve the type — never guess.** Skip when a type flag was given. Otherwise probe all three and report which matched:

   ```bash
   vault-cli --vault "<vault-name>" task get "<name>" status  2>/dev/null
   vault-cli --vault "<vault-name>" goal get "<name>" status  2>/dev/null
   find "<topics_dir>" -maxdepth 1 -iname "<name>.md"
   ```

   `<topics_dir>` is the vault's topics folder — `23 Topics` unless `vault-cli config list --output json` says otherwise; read it, never hardcode it. Use `find -iname`, never a case-sensitive shell glob. Exactly one match → act on it and say which type matched. More than one → print every match with its type and STOP, telling the operator to re-run with `--task` / `--goal` / `--topic`. None → print the closest candidates and STOP; never create anything from an unmatched name.

3. **Ownership guard — refuse, never gate.**

   ```bash
   python3 ${CLAUDE_PLUGIN_ROOT:-$HOME/.claude/plugins/marketplaces/claude-supervisor}/scripts/owner.py \
     --session "${CLAUDE_CODE_SESSION_ID}" --subject "<name>" --branch "<goal|topic|task>" --vault "<vault-root>"
   ```

   The first tab-separated field is the verdict; the second is the reason. `owner` → continue. **Every other verdict refuses, and each prints its own first line — they must never be collapsed into one another:**

   ```
   not-owned: <name> is not this session's subject        # verdict: not-owner
   cannot-tell: <name> — <the reason from field 2>        # verdict: cannot-tell
   ```

   Then STOP without writing a marker. ⚠️ **`cannot-tell` is not `not-owner`** — the page was unreadable, recorded no session, or its folder could not be listed, so nothing was established about ownership. `owner.py` refuses to collapse them and this command must not either: a definite "not yours" printed over an unread page is a claim the probe never made. Both refuse; only one of them is a fact about ownership.

4. **Write the marker** — `manager-liveness.py --stop --subject "<name>" --branch "<resolved branch>"`. It writes `<branch>--<base>.stopped` plus `<branch>--<base>.reset`. ⚠️ `--subject`, never the deprecated `--topic` alias, and always the explicit `--branch`: a branch-less call defaults to `goal` and marks the wrong record. Report it as `✓ stop marker — <path>`.

5. **Print the report** — the header form `⏹️ STOP — <subject> (<branch> · <vault>) · session <sid8>` that `<process>` step 3 owns, then only the rows this run can actually fill:

   ```
   ⏹️ STOP — <subject> (<branch> · <vault>) · session <sid8>
     Disarmed
       ✓ stop marker — <path>
     Session alive
       ● ledger <path> — <n> open of <m>, unchanged by this command
   ```

   ⚠️ **The bare form's `Left running` rows do not appear here** — `● gate loop`, `● tick file` and `● gate ledger` all come from `stop-probe.py`, which this path never runs; `<error_handling>` forbids printing a slot whose read did not happen, and a fabricated `●` line reads as "checked and clean". ⚠️ **`Restart:` is `/supervisor:goal-loop "<subject>"` for a goal or topic and is omitted for a task** — nothing re-arms a worker, so offering a restart line would name a verb that does not exist for that tier.
</by_name>

<process>
Run this section for the **bare** form only.

1. **Read the contract, then disarm every model-waking driver it names** — in the order it gives, printing each driver's line as you go and *before* the call that removes it, since a deletion the operator cannot see is indistinguishable from a job that was never armed. Those lines **are** the report's `Disarmed` block — collect them, and do not print the set a second time.

   **Three forms, because not every driver has a read surface.** `✓` and `·` are claims a read surface confirmed; `~` is the honest form for a driver nothing can confirm — whether a stop was sent or none could be. Never print `✓` for a driver you could not read.

   | Form | Means | Available for |
   |---|---|---|
   | `✓ <driver> — <what it was>` | disarmed, confirmed | the enumerable driver — a cron job, which `CronList` reads |
   | `· <driver> — not armed` | read surface says it was never armed | the same one |
   | `~ <driver> — <what was done, or why nothing could be>` | **unconfirmed** — nothing can confirm it | the one driver with no read surface — § Session end names it and why |

   **Disarm by `CronList` → `CronDelete`, and that sweep is what reaches a `ScheduleWakeup`-armed loop** — it is a cron job, so it appears in `CronList` like any other and takes the `✓`/`·` rows above. ⚠️ **Never `ScheduleWakeup {stop:true}` for it:** measured 2026-09-30, that returned *"there was no pending wakeup to cancel"* while the job kept firing, so a stand-down that used it left the loop armed. § Session end owns why the two are one driver; this is the procedure that follows from it.

   ⚠️ **`~` carries a reason, and the reason must be true.** Two cases reach it and they are not the same sentence: a stop *was* sent (the driver was reachable but unreadable), or nothing *could* be sent (no id exists in this conversation to name it — the `Monitor` case, which § Session end bounds at ≤30 min for exactly this reason). Writing *"stop sent"* where no stop was sent is the same over-claim as a `✓` you could not read, one form down.

2. **Probe the state `stop` must not change** — read-only; the probe writes nothing and signals nothing:

   ```bash
   python3 ${CLAUDE_PLUGIN_ROOT:-$HOME/.claude/plugins/marketplaces/claude-supervisor}/scripts/stop-probe.py
   ```

   It prints the session id; the subject with its branch and vault; **one line per gate loop found**, each with its pid, uptime and command; the tick file's path, mtime and age; the gate ledger's path, last line and age; and the session ledger's open count. The report below has a slot for every one of those — if a line has no slot, that is this template's defect, not a value to drop. ⚠️ **One line is a directive rather than a fact to report** — the `⚠️ no registration for this session …` hint the probe prints when it resolves no subject. It is step 2b's trigger, not a slot: act on it there, and do not carry it into the report as a value.

2b. **Decide the tier, then mark the stand-down.** The marker differs by tier, and choosing the wrong one is the defect this step exists to prevent: a **worker** that writes a subject marker stands down a goal manager's loop it does not own, and a **manager** that writes only a page field is restarted as a lapsed manager.

   **The discriminator — two signals, either one means manager.** The session is a **manager** when `~/.claude/state/worker-manager/<session-id>.json` exists **or** the probe in step 2 found a gate loop for it. Otherwise it is a **worker**. ⚠️ **Both limbs are needed, and neither is the subject the probe printed.** The registration can be missing while the loop is genuinely armed — the case step 2b's fallback exists for — so registration alone would misroute a live manager; and a worker runs no gate loop, so the gate loop alone would leave a registration-bearing manager with an armed loop looking like a worker. Take the probe's own output for the second limb rather than re-deriving it.

   **Worker limb** — stamp the anchored task, and write **no** state file:

   ```bash
   python3 ${CLAUDE_PLUGIN_ROOT:-$HOME/.claude/plugins/marketplaces/claude-supervisor}/scripts/task-standdown.py --session "$CLAUDE_CODE_SESSION_ID"
   ```

   It resolves the task by `claude_session_id` and writes `stopped: true` + `stop_date` on that page. That pair is what stops the row being **reopened** by `open-queue.py` and **resumed** by `worker-keepalive.py`. ⚠️ **Do not run step 2b's subject marker for a worker**, and do not fall back to the subject on a `no-task` verdict: a worker whose task cannot be resolved has nothing to stand down, and the fallback would resolve to its task's **goal**. Report the verdict line, and on `no-task` say so plainly.

   **Manager limb** — the subject marker, unchanged:

   ```bash
   python3 ${CLAUDE_PLUGIN_ROOT:-$HOME/.claude/plugins/marketplaces/claude-supervisor}/scripts/manager-liveness.py --stop --subject "<subject>" --branch "<branch>"
   ```

   It writes `<branch>--<base>.stopped` into the liveness state dir, so a deliberate stand-down is not reported as a lapse, plus `<branch>--<base>.reset` (the epoch the next arm consumes to start a fresh cadence sample). Only `/supervisor:goal-loop` — whose step 3 arms with `--resume` — clears the marker; a plain `--arm` is a round's *reading* and leaves it standing. Report it as a `✓ stop marker — <path>` line under `Disarmed`. ⚠️ `--subject`, never the deprecated `--topic` alias, and always the explicit `--branch`: a branch-less call defaults to `goal` and marks the wrong record.

   ⚠️ **When the probe prints `subject  (none recorded)` and the tier is manager, fall back to the subject *this session* is managing — never skip the marker.** The probe resolves a subject only from `~/.claude/state/worker-manager/<session-id>.json`, and that registration is written by the manager command's own recording step; when it is missing while the loop is genuinely armed, following the old *"skip and say so; there is no slug to mark"* line left those arms to go stale and be reported as a lapsed manager. Measured 2026-09-30: the probe printed `(none recorded)` while `~/.claude/state/sweep-gate/<slug>.arms` and `.cadence` were both on disk, written by the two `--arm` calls. **The session knows what it armed** — its own subject is in its context, not an inference from a file that may never have been written — so that is the source. Run the marker with it, and name which source it came from:

   `✓ stop marker — <path>  (subject from this session, not the probe)`

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
     Restart: /supervisor:goal-loop "<subject>"
   ```

   Print only the forms that occurred. A `●` line whose read came back absent takes its own form from `<error_handling>` instead of a fabricated value — the tick file and the gate loop each have one, and a dropped line reads as "checked and clean" when the truth may be "never looked".
</process>

<constraints>
- **Write at most one marker, and the right one for the tier.** A **worker's** bare stop writes the task page's `stopped: true` + `stop_date` and **no** state file (`<process>` step 2b, worker limb). A **manager's** writes `<branch>--<base>.stopped` + `<branch>--<base>.reset` and no page (`<process>` step 2b, manager limb). The by-name form writes the subject marker (`<by_name>` step 4). `stop` is a disarm, not a bookkeeping act; each marker exists only so a deliberate stop is not reported as a lapse — and, for the task tier, so the row is neither reopened nor resumed.
- **The by-name form refuses rather than gates.** There is no operator prompt and no confirmation turn: a session that does not own the subject prints `not-owned:` and stops. A stand-down is cheap to re-run and expensive to misattribute.
- End the turn with the session's normal closer. This report is not one.
</constraints>

<error_handling>
- **A driver that refuses to disarm** — print the failure verbatim together with the driver still standing, and say the loop is **not** fully stood down. Reporting success over a live driver is worse than reporting the failure.
- **No gate loop found**: print `⚠️ no gate loop found` in place of the gate-loop line, and say plainly that it may never have been armed. A fact to report, not to repair.
- **Tick file or gate ledger absent**: print `· tick file — absent` / `· gate ledger — absent` in place of that line, and say which of the two reasons it is — no subject recorded, so no path resolves, or the loop has not written one yet. Neither is a fault in this command.
- **No session id resolves**: the header prints `session (unknown)` verbatim, and the ledger line names the path it looked for. Say the id could not be resolved rather than leaving the slot empty — an empty slot reads as a value that was not printed, not as one that does not exist.
- **No subject recorded** — **manager limb only.** Print the session id, then **apply step 2b's fallback** — the subject this session manages — and run the marker with it. ⚠️ **Never read this clause as licence to skip `<branch>--<base>.stopped`**: the loop may have been armed before the registration was written, which is exactly the case the fallback exists for, and skipping the marker leaves the arms to go stale and be reported as a lapsed manager. The branch and vault render as `—` with it, since all three come from the same record. ⚠️ **A worker never reaches this clause** — its limb writes a page field and has no subject to fall back to; on `no-task` it reports that verdict instead.
</error_handling>
