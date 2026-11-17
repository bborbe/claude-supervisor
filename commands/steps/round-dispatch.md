---
# Read by the router, never invoked — without this key Claude Code
# registers this file as /<plugin>:steps:<name>.
user-invocable: false
---

# Round dispatch

When: every tick — after the re-arm (row 1), before the read half

⚠️ **The round's machine half runs in ONE delegate.** Dispatch `supervisor:fleet-round`
**once**, passing the round id, this session's id, the vault, the absolute tasks dir and this
round's digest path. The delegate reads the step files, runs rows 0, 2–6, 9, 12, 15, 16, 16b,
18 and 19 — dispatching the existing sweep, keep-alive, readings and open legs — persists the
round, and returns the ≤ 1 KB verdict plus the payload path. You keep rows 7, 8, 10, 11, 13,
14, 17 and 20: the provenance acts, the operator questions, the watchers, the voice and the
render. The split, and why the manager no longer reads the step files, is `agents/fleet-round.md`.

⚠️ **Arming stays here and is unchanged.** A delegate's lifetime ends with its turn, so an arm
written inside it would expire with it.

⚠️ **Arming without dispatching loses the round.** A wakeup that re-arms and returns has lost
it: the predecessor `fbad5082` did exactly that for two hours from 10:13 on 2026-11-17,
writing "out of context" in each closer while the keeper logged `lapse=none`. Every wakeup
ends with a dispatched round or a named reason it could not run.

⚠️ **A compaction never leaves the loop armed-but-not-driven.** After a compaction the first
turn re-arms (row 1) **and** dispatches the round; the re-arm alone is not a round.
