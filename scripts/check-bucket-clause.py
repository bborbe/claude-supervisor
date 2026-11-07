#!/usr/bin/env python3
"""The per-bucket shape clause must be byte-identical in every command that carries it.

The clause telling a sweep reader what `--write-buckets` will accept lives in
`commands/steps/goal-sweep.md` and `commands/goal-status.md` — one contract, carried
at each call site because each command is read on its own and a pointer alone is what
`docs/subject-resolution.md` § Recording already records as the liability. But a contract
with several call sites drifts unless something checks it: this clause was amended by hand
at every site on 2026-10-03 (the vocabulary half) and again on 2026-10-04 (the shape half),
and both times the copies were compared by eye.

Same shape as `check-recording-step.py` and `check-spawn-mode.py`, for the same reason.

Anchored on the clause's own opening words, never on a line number: the two declarations
this clause sits beside both moved by one line inside a single day (measured 2026-10-03),
and a line-anchored read would have reported drift that was really an offset.

**What this proves, and what it does not.** It proves the copies cannot silently diverge —
a lockstep edit that reaches one of them fails here. It cannot prove the clause is *true*
of `bucket_shape_error()`; that is the writer's own tests' job. The two are deliberately
separate: this is the drift guard, not the semantics.

Indentation is compared stripped, so the files may indent the paragraph to their own
nesting depth; everything else must match exactly.
"""

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

START = "Both doors into the record share one shape check"
COMMANDS = (
    # ⚠️ `commands/goal-loop.md` was replaced by `commands/steps/goal-sweep.md` on 2026-10-11,
    # when the loop was restructured into a router with per-round step files. The clause
    # describes what `--write-buckets` and `--save` accept, and both calls are made by the
    # sweep step — so the clause moved with the calls. The check follows the rule, not the
    # file it used to sit in.
    "commands/steps/goal-sweep.md",
    # ⚠️ `commands/goal-drive.md` left this set on 2026-10-11, when it became the loop's
    # per-round router (`commands/goal-loop.md` is the arm). A router carries no
    # executable rule — the clause's only in-tree call site is the sweep step above — so
    # leaving it there would be the second home this guard exists to prevent. The clause
    # still has two carriers, and a third appearing anywhere is caught by the same
    # byte-identity comparison below.
    "commands/goal-status.md",
)


def clause(path: Path) -> tuple[str | None, str | None]:
    """-> (stripped clause line, error). The single line carrying it, or why it is not single."""
    if not path.exists():
        return None, f"{path.relative_to(REPO)} does not exist"
    hits = [
        ln.strip()
        for ln in path.read_text(encoding="utf-8").splitlines()
        if ln.strip().startswith(START)
    ]
    if len(hits) != 1:
        return None, f"{path.relative_to(REPO)} carries the clause {len(hits)} times, want 1"
    return hits[0], None


def main() -> int:
    found: dict[str, str] = {}
    for rel in COMMANDS:
        line, err = clause(REPO / rel)
        if err or line is None:
            # `clause()` returns either (None, err) or (line, None), so the second half is
            # unreachable — but an explicit branch keeps the narrowing honest under `-O`,
            # where an `assert` used for control flow is stripped. Sibling guards
            # (`check-recording-step.py`, `check-spawn-mode.py`) use the same shape.
            print(f"bucket-clause FAILED: {err or 'clause() returned no line'}", file=sys.stderr)
            return 1
        found[rel] = line

    (ref_name, ref_line), *rest = found.items()
    for name, line in rest:
        if line != ref_line:
            print(
                f"bucket-clause FAILED: {name} and {ref_name} disagree — the clause is "
                f"carried at more than one call site and must be byte-identical in all of them",
                file=sys.stderr,
            )
            return 1

    print(f"bucket-clause ok: {len(COMMANDS)} command(s) carry the clause, byte-identical")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
