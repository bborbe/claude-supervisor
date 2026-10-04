#!/usr/bin/env python3
"""The per-bucket shape clause must be byte-identical in all three commands that carry it.

The clause telling a sweep reader what `--write-buckets` will accept lives in
`commands/manager-loop.md`, `commands/manager-drive.md` and `commands/manager-status.md`
— one contract, carried three times because each command is read on its own and a
pointer alone is what `docs/subject-resolution.md` § Recording already records as the
liability. But a contract with several call sites drifts unless something checks it:
this clause was amended by hand in all three on 2026-10-03 (the vocabulary half) and
again on 2026-10-04 (the shape half), and both times the copies were compared by eye.

Same shape as `check-recording-step.py` and `check-spawn-mode.py`, for the same reason.

Anchored on the clause's own opening words, never on a line number: the two declarations
this clause sits beside both moved by one line inside a single day (measured 2026-10-03),
and a line-anchored read would have reported drift that was really an offset.

**What this proves, and what it does not.** It proves the three copies cannot silently
diverge — a lockstep edit that reaches two of them fails here. It cannot prove the clause
is *true* of `bucket_shape_error()`; that is the writer's own tests' job. The two are
deliberately separate: this is the drift guard, not the semantics.

Indentation is compared stripped, so the three files may indent the paragraph to their
own nesting depth; everything else must match exactly.
"""

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

START = "Both doors into the record share one shape check"
COMMANDS = (
    "commands/manager-loop.md",
    "commands/manager-drive.md",
    "commands/manager-status.md",
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
                f"carried three times and must be byte-identical in all three",
                file=sys.stderr,
            )
            return 1

    print(f"bucket-clause ok: {len(COMMANDS)} command(s) carry the clause, byte-identical")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
