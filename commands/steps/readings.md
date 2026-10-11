## When

Either reading returned `unknown` or a non-zero exit.

## The saturation reading — the ratio, not tok/s

Every tick prints the fleet working ratio beside the marker line:

    saturation: 3/6 (busy + shell over live sessions)

Read it from `python3 $P/fleet-board.py --json` → `saturation`, which carries `numerator`, `denominator` and `ratio`. The numerator counts the registry **status** (`busy` + `shell`), not the board's `running` bucket — the buckets apply a precedence, so a `busy` session holding an open gate is bucketed `needs-input` and would be dropped from a bucket-based count. `ratio` is `None` for an empty fleet; print it as absent, never as `0%`.
## The approved-not-started reading

Every tick prints this line with the marker line:

    approved, not started: 3 · oldest 7h35m (Build claude-interactive)

Read it from `python3 $P/approved-not-started.py --tasks-dir "<vault>/<tasks-dir>"`. A row counts when its task-file frontmatter carries `approved_at` with a non-terminal `status` and no **live** `claude_session_id`. Rows waiting longer than one tick (**30 min, a script constant** — never a per-call flag, because two rounds disagreeing about the same row is the defect this line exists to remove) are listed beneath it with their owning manager.

⚠️ **`unknown` is a third answer, not `0`.** If the session registry cannot be read the script prints `approved, not started: unknown` and exits non-zero — *"no approved rows are waiting"* and *"I could not ask"* must not render the same (`docs/pane-reads.md` § The rule). Print it as it comes back; never round `unknown` down to a number.
