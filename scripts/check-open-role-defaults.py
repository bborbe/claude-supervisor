#!/usr/bin/env python3
"""A missing `role`/`mode` is a default in `commands/open.md`, never a refusal.

Operator ruling 2026-11-06: an operator flag is the approval, so the `--flagged` batch opens
every flagged row; a missing field takes the default from `docs/role-mode-matrix.md`. Before
this, Step 0.6 printed `⛔ REFUSED — role: undecidable` for a body with no role signal while
§ 3.0 said `role:` absent means agent — two answers to one question in one file.

Static guard, three clauses:
1. the refusal literal `role: undecidable` does not occur in `commands/open.md`;
2. `commands/open.md` and `docs/fleet-surface.md` both cite the matrix by path;
3. the matrix exists and states the `agent` role default and the `agent` → `headless`
   mode mapping. ⚠️ **The mode needle reads `(forced)`, not `(default)`, since the
   2026-11-12 operator ruling** — `role: agent` is `headless` always, so the matrix's
   Rule 2 row is a forced mapping rather than a default. The clause is unchanged in
   intent: the matrix must still pin `agent` → `headless` in words a reader cannot miss.

The behavioural proof is a live `--flagged` run, which needs a manager session; this guard
only stops the text from regressing.
"""

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
OPEN = REPO / "commands/open.md"
SURFACE = REPO / "docs/fleet-surface.md"
MATRIX = REPO / "docs/role-mode-matrix.md"


def fail(msg: str) -> int:
    print(f"open-role-defaults FAILED: {msg}", file=sys.stderr)
    return 1


def main() -> int:
    for p in (OPEN, SURFACE, MATRIX):
        if not p.exists():
            return fail(f"{p.relative_to(REPO)} does not exist")
    open_text = OPEN.read_text(encoding="utf-8")
    if "role: undecidable" in open_text:
        return fail("commands/open.md still carries the `role: undecidable` refusal — a missing role defaults to agent")
    for stale in ("role: human is SKIPPED", "skipped H (human)", "is REFUSED and left unwritten"):
        if stale in open_text:
            return fail(f"commands/open.md still carries {stale!r} — human rows open DIRECT, missing fields default")
    if "docs/role-mode-matrix.md" not in open_text:
        return fail("commands/open.md does not cite docs/role-mode-matrix.md")
    if "role-mode-matrix.md" not in SURFACE.read_text(encoding="utf-8"):
        return fail("docs/fleet-surface.md does not cite role-mode-matrix.md")
    matrix = MATRIX.read_text(encoding="utf-8")
    for needle in ("**`agent` (default)**", "**`headless` (forced)**"):
        if needle not in matrix:
            return fail(f"docs/role-mode-matrix.md lost the default {needle}")
    print("open-role-defaults OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
