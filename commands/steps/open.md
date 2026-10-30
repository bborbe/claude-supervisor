---
# Read by the router, never invoked — without this key Claude Code
# registers this file as /<plugin>:steps:<name>.
user-invocable: false
---

# Open leg

When: every tick — after Readings

## The leg

    python3 $P/open-queue.py --tasks-dir "<vault>/<tasks-dir>"

The script owns the population, the guards, the rank and the cap; **this step owns only the opening**. Print its block verbatim into the round's output, then open the rows it lists, in the order it lists them.

**Opening one row** — `/supervisor:open "<name>" --task --vault "<vault>"`, once per row, in the printed order. ⚠️ **`--vault` is load-bearing.** Task titles are not namespaced across vaults (`commands/open.md:321`), and without it `/supervisor:open` resolves the vault from the session cwd — so a row read from `<vault>` can open a same-named task in a different vault, or be refused as not-found. The population and the action must name one vault. Nothing else: no batch helper, no in-line `spawn_agent` (`docs/fleet-surface.md` § Spawn a worker item 6 holds `spawn_agent` for `resume=` alone), no reordering.

## The block

    open leg: selected 3 (workers 27/30)
      1. Fix the Fleet View — operator-flagged
      2. Reap a Manager Whose Subject Is Terminal — priority 1
      3. Tighten the Reflect Scan — priority 2
      skipped: 130 — 3 blocked · 116 capacity · 10 deferred · 1 waiting approval

The skipped reasons are **counted, not named**, and print in alphabetical order — that is what the script emits, so print it as it comes.

Two rounds print a different line and nothing else:

    open leg: no free slots — selected 0 (workers 30/30)
    open leg: target unlimited — selected 0 (workers unlimited)

⚠️ **`target unlimited` is the `spawn.maxConcurrent: 0` config, and the leg opens nothing there on purpose.** `0` removes the server's cap, and the cap is the only bound this leg has to rank against — admitting every eligible row because the bound was removed would be the bulk opening the 2026-10-01 ruling forbids. The line names a config state, not a quiet round: a fleet with no target set is the operator's to fix, and it is the round's to report.

## What the leg does not decide

⚠️ **The leg ranks; `/supervisor:open` decides admissibility.** Every gate that command owns — Gate 1 (approval), Gate 2 (the readiness ladder, `docs/fleet-surface.md` § Spawn a worker item 2), Gate 3 (the cap and the operator-named marker) — runs unchanged for every row this leg hands it. The leg never pre-clears one, and a row that clears the leg can still be refused at the spawn. Report that refusal and **continue to the next row**; a refusal is not a reason to reorder the list or to retry the same row.

⚠️ **Never open a row the leg skipped.** The skipped list carries a reason per row and those reasons are the whole point of printing it: `waiting approval` is the operator's inbox (`commands/open.md` § Step 1.5 Gate 1), `blocked` and `deferred` are the row's own conditions, and `capacity` means the rank admitted more than the fleet has room for. A round that opens past the printed count has replaced the rank with its own judgement.

⚠️ **`unknown` is a third answer, never `0`.** An unreadable queue or capacity store prints `open leg: unknown` and exits non-zero. Print it as it comes back and **open nothing this tick** — *"no free slots"* and *"I could not count"* must not render the same, and a leg that opens nothing because it could not read is not a leg that found nothing to open.

## The boundary — no manager, ever

⚠️ **This leg opens workers only. It never spawns, arms or resumes a topic manager or a goal manager.** Operator ruling 2026-10-30, verbatim: *"we dont want to auto open topic-manager, goal-manager ... but flag are fine"*. A goal with ready rows and no live manager gets its **workers** opened; the goal's manager state is unchanged by this leg and is the negative control the round reports (`manager-liveness.py --state --subject "<goal>"`, before and after).

⚠️ **The operator's flag is a mark, not an approval, and it is the operator's alone.** `flag: true` **with `flag_set_by: operator`** ranks a row first — the operator saying "this one". A bare `flag: true`, or one an agent set, is **not** an approval and does not rank here; the script refuses it as an operator flag.

⚠️ **This predicate is deliberately narrower than `commands/open.md` § Step 0.5's.** That selector's `OPENING_ACTORS` is `('operator', 'legacy')` — it also opens the 270 pre-field rows dispositioned `legacy` on 2026-09-30, because for those the disposition *is* the operator's answer. The leg accepts `operator` only, so a `legacy` row opens through the selector but never ranks first here. The two are not the same question: the selector decides **whether a row may open at all**, the leg decides **which approved row goes first**, and widening the leg to `legacy` would let a dispositioned row jump the queue ahead of the operator's current mark.

## Where the rules live

This step restates none of them. The rank, the guards and the capacity instruments are `scripts/open-queue.py`'s header; the spawn gates are `commands/open.md`; the tier boundary is `docs/session-tiers.md`; the cap is `docs/fleet-surface.md` § Spawn a worker item 5. A change to any of them belongs in that file, not here.
