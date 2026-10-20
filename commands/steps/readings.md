---
# Read by the router, never invoked — without this key Claude Code
# registers this file as /<plugin>:steps:<name>.
user-invocable: false
---

# Readings

When: every tick — print both readings with the marker line, run: `python3 $P/fleet-board.py --json` · `python3 $P/approved-not-started.py --tasks-dir "<vault>/<tasks-dir>"`

## The saturation reading — the ratio, not tok/s

Every tick prints the fleet working ratio beside the marker line:

    saturation: 3/6 (busy + shell over live sessions)

Read it from `python3 $P/fleet-board.py --json` → `saturation`, which carries `numerator`, `denominator` and `ratio`. The numerator counts the registry **status** (`busy` + `shell`), not the board's `running` bucket — the buckets apply a precedence, so a `busy` session holding an open gate is bucketed `needs-input` and would be dropped from a bucket-based count. `ratio` is `None` for an empty fleet; print it as absent, never as `0%`.

**The ratio is the signal, not tok/s.** Measured 2026-09-20 (Fleet Manager session `b700c650`): output throughput swung 629 → 905 tok/s in 45 minutes while the ratio moved only 44% → 42%. Throughput tracks whichever model happens to be mid-response at the moment of the read; the ratio does not. Print the tok/s figure beside it for context only — `~/.claude/scripts/claude-metrics.sh` reads `out_tok/s` — and never act on it as the number.

**Open Questions 1 and 2 — ANSWERED 2026-10-01, and neither answer is a ratio.** Both asked whether a *ratio threshold* should trigger a spawn. The operator's design of 2026-10-01 replaces the ratio as the capacity signal outright: the trigger is the fleet-wide **worker target** (`spawn.maxConcurrent`, whose single home is `${CLAUDE_PLUGIN_ROOT}/docs/fleet-surface.md` § Spawn a worker item 5 — **read the number there and never restate it here**), read as live workers against that number and printed as `workers: <live>/<target>` (Output shape item 1).

- **Open Question 1 is superseded rather than answered with a threshold.** No ratio value triggers a spawn, and none is to be inferred from this line. The ratio stays a *diagnostic* — it is what tells you the fleet is saturated, or human-queued — while the target is what decides whether a manager proposes work.
- **Open Question 2 is answered: capacity does drive action — but through the target, not through this ratio.** A manager tick below the target with no ready row proposes the next most important rows to approve (`commands/goal-loop.md` step 4). **This line itself stays read-only**, exactly as the measurement argued.

## The approved-not-started reading

Every tick prints this line with the marker line:

    approved, not started: 3 · oldest 7h35m (Build claude-interactive)

Read it from `python3 $P/approved-not-started.py --tasks-dir "<vault>/<tasks-dir>"`. A row counts when its task-file frontmatter carries `approved_at` with a non-terminal `status` and no **live** `claude_session_id`. Rows waiting longer than one tick (**30 min, a script constant** — never a per-call flag, because two rounds disagreeing about the same row is the defect this line exists to remove) are listed beneath it with their owning manager.

⚠️ **`unknown` is a third answer, not `0`.** If the session registry cannot be read the script prints `approved, not started: unknown` and exits non-zero — *"no approved rows are waiting"* and *"I could not ask"* must not render the same (`docs/pane-reads.md` § The rule). Print it as it comes back; never round `unknown` down to a number.

⚠️ **The line reports; it never opens.** These rows are the drive leg's and the owning manager's to act on, under the standing mandate and its approval boundary — this reading adds visibility, not authority.
