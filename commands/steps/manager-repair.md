---
# Read by the router, never invoked — without this key Claude Code
# registers this file as /<plugin>:steps:<name>.
user-invocable: false
---

# Repair

When: the `Audit` block carried `repairable` rows

- **dispatch the repair half for the round's `repairable` rows — clause (1)'s other delegated act.** Take the `repairable` subset of the `Audit` block you just persisted and run `/supervisor:ready "<task>"` once per selected row — **in score-ascending order, capped, and bounded by the fleet's remaining capacity**. ⚠️ **The rule, its ordering and both bounds have one home: `${CLAUDE_PLUGIN_ROOT}/docs/fleet-surface.md` § Spawn a worker, *The repair dispatch*.** Read it there and never restate a number here: a restated copy is the second counter a `grep` cannot tell from a real one. ⚠️ **Each call can open, and therefore spawn** — `commands/ready.md` step 4 hands to `/supervisor:open`. Read `live` and `target` with the instruments the cap check above already uses and dispatch `min(cap, target − live)`; **at or above the target, dispatch zero and print `repairs held: at target (<live>/<target>)`.** ⚠️ **Re-read `live` here, never at the cap check** — the hand-over bullet above runs first and each of its rows can spawn, so a count taken earlier over-admits by that many. ⚠️ **This bound is a courtesy, not the enforcement:** `spawn_agent` refuses at the limit itself (`concurrentLimitError()`, `server/supervisor.mjs:741`, on both the spawn and cluster paths), so a stale `live` costs a refused open the next sweep picks up — never an overrun. Never describe this bound as what keeps the fleet inside its limit. ⚠️ **Rows beyond the window print `left for the next round (cap <n>)`** — a silent cap is indistinguishable from a set that was fully processed, which is the defect this bullet closes. ⚠️ **Never dispatch a `phase: todo` row:** `/supervisor:ready` refuses one at its Gate 1, and that refusal is the operator's approval boundary rather than a defect to route around. ⚠️ **The leg stays write-free — never widen its tools to close this.** Clause (1)'s split is deliberate: the leg decides, the caller acts.
