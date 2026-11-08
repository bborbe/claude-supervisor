---
# Read by the router, never invoked — without this key Claude Code
# registers this file as /<plugin>:steps:<name>.
user-invocable: false
---

# Open leg

When: every tick — after Readings

⚠️ **This leg acts, and a read-only execution is not a run of it.** It opens each selected row; a
run that only prints the block has measured nothing — `opened=0` because the leg was told to print
it is a failure, not a quiet round. It is one of the round's acting legs, so the round's delegate
rule carves it out — a sub-agent may run it only with write tools (`commands/fleet-drive.md`, the
delegate rule's carve-out).

1. **Dispatch the leg — this step no longer carries its procedure.** Dispatch:

   `Task(subagent_type: "supervisor:fleet-open-actor", prompt: <the vault + the absolute tasks dir + this round's digest path + round timestamp>)`

   The plugin prefix is required — a bare `fleet-open-actor` resolves to a personal
   `~/.claude/agents/` copy. The agent (Sonnet) runs `open-queue.py --json` once, writes the
   rendered block to this round's payload, runs every `selected[].action` verbatim through
   `/supervisor:open`, and hands back the verdicts. It owns the one-invocation rule, the `--vault`
   rule, the never-compose rule, the block's three shapes and the "workers only, never a manager"
   boundary — read them in `agents/fleet-open-actor.md`; this step does not restate them.

2. **What this step does with the hand-back.**

   - **`status=ok`** → **print the block at `render=` verbatim.** That file's contents are the
     `open leg:` block `commands/steps/output-shape.md` item 3 renders — the head with its
     `workers` reading, the selected rows in rank order, and the `skipped:` tally by reason. Then
     print `opened=<n>` of `selected=<n>` and every `refused:` line **verbatim**.
     ⚠️ **Open nothing a refusal names**, and never reorder or retry a row.
   - **`status=unknown`** → print `open leg: unknown` and **open nothing this tick.** The agent has
     already opened nothing; do not retry the leg. ⚠️ **`unknown` is a third answer, never `0`** —
     *"no free slots"* and *"I could not count"* must not render the same, and a leg that opened
     nothing because it could not read is not a leg that found nothing.
   - **`status=not-run`** → print `open leg: not run — <reason>` with `<reason>` taken from the
     hand-back's `reason=`, and open nothing. ⚠️ **The reason comes from `reason=`, never from the
     agent's prose** — the hand-back is the only channel that crosses the delegation boundary, and a
     `not-run` with no `reason=` leaves the dash empty. A leg whose payload write failed is not a
     round that found nothing to open.
   - **`truncated=` greater than zero** → `n` refusals ride `payload=` only; read that file, print
     the remainder verbatim, and reconcile it against `refused=` as
     `docs/handback-contract.md` § The shape — the truncation exception — states.
   - ⚠️ **This leg carries no `LEGS` digest key** — `fleet-legs.py`'s three keys are the keep-alive's
     two and the flagged-open's one, and item 3 renders this leg's block from the payload. Never
     write a `fleet-legs.py` line for it, and never invent a `--not-run` record for a key that does
     not exist.

   ⚠️ **When the delegation returns no usable hand-back** — it errored, came back empty, or
   resolved to something that returned no hand-back — run the leg by hand from
   `agents/fleet-open-actor.md` (this step no longer carries its procedure) and carry on. Trigger on
   the missing hand-back, never on a matched error string. ⚠️ **A hand-back carrying a block but no
   `opened` count is the read-only failure this leg exists to remove** — treat it as no usable
   hand-back and run the leg. ⚠️ **Re-invoke only when nothing was opened** — the leg is not
   idempotent across a lost hand-back: the first invocation may have spawned rows whose
   `claude_session_id` registration had not landed when the hand-back was lost, and a second run
   would spawn a second worker onto those tasks. One read decides it: the roster
   (`python3 $P/fleet-sessions.py`). A session whose `ATTRIBUTION` names a row the block selected
   was opened — report `opened=unknown` and open nothing more. Only with no roster row naming a
   selected task may the leg run again.

## What the leg does not decide

⚠️ **The leg ranks; `/supervisor:open` decides admissibility.** Every gate that command owns —
Gate 1 (approval), Gate 2 (the readiness ladder, `docs/fleet-surface.md` § Spawn a worker item 2),
Gate 3 (the cap and the operator-named marker) — runs unchanged for every row this leg hands it.
The leg never pre-clears one, and a row that clears the leg can still be refused at the spawn.

⚠️ **Never open a row the leg skipped.** The skipped list carries a reason per row and those
reasons are the whole point of printing it: `waiting approval` is the operator's inbox
(`commands/open.md` § Step 1.5 Gate 1), `blocked` and `deferred` are the row's own conditions, and
`capacity` means the rank admitted more than the fleet has room for. A round that opens past the
printed count has replaced the rank with its own judgement.

⚠️ **This leg opens workers only. It never spawns, arms or resumes a topic manager or a goal
manager.** Operator ruling 2026-10-30, verbatim: *"we dont want to auto open topic-manager,
goal-manager ... but flag are fine"*. A goal with ready rows and no live manager gets its
**workers** opened; the goal's manager state is unchanged by this leg and is the negative control
the round reports (`manager-liveness.py --state --subject "<goal>"`, before and after).

## Where the rules live

This step restates none of them. The rank, the guards and the capacity instruments are
`scripts/open-queue.py`'s header; the spawn gates are `commands/open.md`; the tier boundary is
`docs/session-tiers.md`; the cap is `docs/fleet-surface.md` § Spawn a worker item 5; the block's
three shapes and every act count are `agents/fleet-open-actor.md`. A change to any of them belongs
in that file, not here.
