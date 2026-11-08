---
# Read by the router, never invoked — without this key Claude Code
# registers this file as /<plugin>:steps/<name>.
user-invocable: false
---

# Flagged open

When: every tick

⚠️ **This leg acts, and a read-only execution is not a run of it.** It opens each admitted row; a
run that only reports `unactioned: <n> operator-flagged` without invoking the command has measured
nothing — `opened=0` because the leg was told to print it is a failure, not a quiet round. It is one
of the round's three acting legs, so the round's delegate rule carves it out — a sub-agent may run it
only with write tools (`commands/fleet-drive.md`, the delegate rule's carve-out).

⚠️ **Gate this leg on the round's debt — chain it, do not merely read it:**

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/manager-receipt.py" --debt --subject "Fleet Manager" --branch fleet \
  && <run this leg>
```

Exit **12** = a step is still owed: print the `debt:` lines, skip this leg, and **carry on with the round — the tail still runs**, because the tail's `--check` is what reports the round's verdict to the operator. Exit **0** = no debt, run it. ⚠️ **Exit 2 = the gate could not read or write the debt file** — it prints nothing to stdout, only a reason to stderr; skip the leg and print that reason, never read 2 as "no debt". ⚠️ **`--debt` judges the round in progress**, re-reading the receipt, so a round that already swept is not blocked by a debt it has paid. ⚠️ **The exit code is the gate, never the printed line** — a leg that reads only the text is satisfied by an empty read. ⚠️ **An operator's flag is not a licence to run ahead of the sweep** — the 13:27 round on 2026-10-31 opened three flagged rows and never swept, and nothing in its render said so. Opening the row is one round's delay; sweeping is what makes the next round's opening decision correct.

1. **Dispatch the leg — this step no longer carries its procedure.** Dispatch:

   `Task(subagent_type: "supervisor:fleet-flagged-open-actor", prompt: <this round's digest path + round timestamp>)`

   The plugin prefix is required — a bare `fleet-flagged-open-actor` resolves to a personal `~/.claude/agents/` copy. The agent (Sonnet) invokes `/supervisor:open --flagged`, opens every admitted row through it, and records the `operator-flagged` digest count itself. It owns the `flag_set_by` safety rule, the blast-radius note and the "this leg opens work, never a manager" clause; read them in `agents/fleet-flagged-open-actor.md` — this step does not restate them.

2. **What this step does with the hand-back.** Print `opened=<n>` as the round's report — the batch's own tally, and the one number that separates a leg which opened nothing from a leg that was told to print nothing. Then print every `refused:` line **verbatim** — that is the operator's own `REFUSED | <vault> | <name> | flag_set_by=<who> | not the operator — not opened, not approved` line, and a count cannot be reconciled against a list of names. ⚠️ **Open nothing a refusal names.** ⚠️ **A `truncated=<n>` greater than zero means `n` refusals ride `payload=` only** — read that file, print the remainder verbatim, and reconcile it against `refused=` as `docs/handback-contract.md` § The shape — the truncation exception — states. ⚠️ **The `operator-flagged` reading is the digest's, rendered from `commands/steps/output-shape.md` item 14, never from the hand-back.** ⚠️ **A failed write is reported, never swallowed.** `legs=UNKNOWN` means `fleet-legs.py` exited 2 and the digest could not be read or written (the sweep may not have written it yet); render `legs: UNKNOWN — <reason>` for this round, with `<reason>` taken from the hand-back's `legs-reason=`, and retry the record once the digest exists. ⚠️ **The reason comes from `legs-reason=`, never from the agent's prose** — the hand-back is the only channel that crosses the delegation boundary, and a `legs=UNKNOWN` with no `legs-reason=` leaves the dash empty. A reading that never landed must not read as a leg that never ran.

   ⚠️ **When the delegation returns no usable hand-back** — it errored, came back empty, or resolved to something that returned no hand-back — **do not re-invoke the batch and do not run it by hand.** `/supervisor:open --flagged` is not idempotent across a lost hand-back: the lost run may already have spawned a worker whose `claude_session_id` had not landed, and a second run would spawn a second worker onto that task — unrecoverable, where one round's delay is not. Trigger on the missing hand-back, never on a matched error string. ⚠️ **A hand-back carrying a count but no invocation is the read-only failure this leg exists to remove** — treat it as no usable hand-back and refuse it, never read it as a completed leg. ⚠️ **No signal this step can read is sound for that population, so the fallback refuses rather than guards.** A headless worker never binds `claude_session_id` and leaves no transcript, so the roster (`fleet-sessions.py`) cannot see a just-spawned one; the digest's `operator-flagged` line proves only that the leg recorded, never what it opened; and the spawn ledger records that a spawn happened, not that this batch made it — the same manager spawns on other rows in the same round, so a ledger row cannot be attributed to this leg. ⚠️ **Refuse in writing, at key granularity:** `python3 "${CLAUDE_PLUGIN_ROOT:-$HOME/.claude/plugins/marketplaces/claude-supervisor}/scripts/fleet-legs.py" --digest "<digest>" --leg operator-flagged --not-run "<reason>"`, with `<reason>` naming the lost hand-back, and report `operator-flagged: not run — <reason>` for the round. The operator's flags stand and the next round's batch opens them.

3. ⚠️ **A leg that does not execute records WHY.** When the debt gate skips this leg (exit 12 = a step is still owed, exit 2 = the gate could not read the debt file), write `fleet-legs.py --leg flagged-open --not-run "<reason>"` rather than leaving the sweep's seeded `not run — no reading recorded this round` standing — `--check` reads that seed as "nobody has spoken for this key". ⚠️ **Never render `unactioned: 0` for a leg that did not run** — a suppressed leg and a leg that opened nothing must never print the same, which is the whole reason the count is written to the digest instead of spoken into chat. The line's format and its not-run form have their **single home in `commands/steps/output-shape.md` item 14** — read it there and never restate it here.
