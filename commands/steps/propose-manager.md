---
# Read by the router, never invoked — without this key Claude Code
# registers this file as /<plugin>:steps:<name>.
user-invocable: false
---

# Propose a manager

When: `propose-managers.py` printed a card — run: `python3 $P/propose-managers.py --post`

Rationale: runbook § Propose a manager for an unmanaged goal — maintained.

- **A goal holding approved work and no live manager is a lapse the fleet surfaces itself.** The round's own classification is blind to it: the sweep classifies **live sessions**, so a goal whose work was approved and never opened has no session to sweep from, and the unmanaged-topic candidate covers the other half — a topic whose workers are live and whose manager is not. Neither reaches this case. Measured 2026-10-12: five approved planning tasks on goal *Read CDB Data Through Weldall (MVP)* (seibert-brogrammers) sat unmanaged until the operator noticed and pushed the Fleet Manager by hand.
- **Run the script — it owns the predicate, the ranking, the manager-match and the slot gate.** `python3 $P/propose-managers.py --post` posts at most one card per free manager slot. ⚠️ **Not one of those rules is restated here, and none may be re-derived at this call site** — a threshold, a count or a ranking written a second time is the second counter this repo's `check-*` guards exist to refuse. Read the script's header for what qualifies a goal and why the gate reads the cap it reads. `--json` prints the whole computation and never posts, for a diagnosis.
- **The card is an ASK, never an act.** Two options under a per-goal dedup key, both produced by the script. ⚠️ **This step never spawns.** Nothing opens until the operator answers, and the answer is read back with `attention-ask.py poll <item-id>` — the id the script prints on its `posted:` line.
- ⚠️ **Exit codes are the branch, and `2` is never read as `3`.** `0` posted one or more cards · `3` nothing to do (no qualifying goal, or no free slot — the script prints which) · **`2` REFUSED**. ⚠️ **Every `2` path prints its own reason to stderr and there is no single shape to expect** — an unreadable cap or board, a tasks dir that does not exist, a `--json --post` combination, a shared dedup key, or a post that returned no item id. **Surface the line the script printed**; never summarize it as "no cards". An unreadable input and an empty fleet must not render the same, and only the second is a fact about the fleet.
- ⚠️ **On `start manager`, the manager is spawned by the operator's naming, never by this step's own initiative.** A `role: manager` session is still not something a manager opens on its own — `commands/steps/manager-spawn.md` carries that ban and this card is its one exception, because the answer *is* the operator's naming. The spawn recipe (`-n "<subject> Manager"`, the `orange` colour, the `unset` prefix) has one home in `commands/open.md` § Step 2C; the answer handling — which subject, which vault, and the `/supervisor:manager-loop "<goal>"` that arms it — has one home in `65 Runbooks/Fleet Manager Session.md` § Propose a manager for an unmanaged goal. Read them there; this step restates neither.
- ⚠️ **On `not now`, nothing is spawned and no state is written here** — the card's own record is the answer. Do not re-post the same goal this round.
