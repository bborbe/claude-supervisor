#!/usr/bin/env python3
"""No router table may route a manager-proposal step.

The fleet round does not propose managers for goals that never had one. That is a decision,
not a default: the router row which ran `propose-managers.py --post` was removed on
2026-10-29, and nothing pinned the removal. `check-step-when.py` validates router rows →
step files only, so a step file with no row is never checked at all, and no test asserted the
table lacked the row — the behaviour could be re-added with every guard green.

**What this proves, and what it does not.** It proves no router row in `commands/*.md` routes
the proposal: neither by naming `commands/steps/propose-manager.md` in its `File` cell, nor by
running `propose-managers.py --post` from its `Step` or `When` cells. It does **not** prove the
script and the step file are unreachable — a step file may invoke them from its own body, and
that is deliberately out of scope. The rule is about the router, because the router is what a
round executes; a guard on step-file bodies would have to decide which invocations are
legitimate, which is the fuzzy check this repo avoids.

**Two shapes, because the step-file one alone is not enough.** `check-step-when.py` skips any
row whose `File` cell names no step file (`if named:` in its `router_tables`). An inline row —
`File` cell `—`, `When` cell carrying `python3 $P/propose-managers.py --post` — is therefore
invisible to the only existing router guard, and is exactly the shape a re-add would take if
its author avoided the step file.

**Rows, never the whole file.** Prose in a router command may legitimately discuss the
proposal; a whole-file substring match would red on a documentation edit and be switched off
within a day. Only cells inside a `| # | Step | When | File |` table are read.
"""

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
COMMANDS = REPO / "commands"
STEPS = COMMANDS / "steps"
HEADER = ("#", "Step", "When", "File")

BANNED_STEP = "propose-manager.md"
BANNED_SCRIPT = "propose-managers.py"
BANNED_FLAG = "--post"


def cells(line: str) -> list[str]:
    return [c.strip() for c in line.strip().strip("|").split("|")]


def is_separator(line: str) -> bool:
    return set(line.strip().strip("|").replace(" ", "")) <= set("-:|")


def router_rows(path: Path):
    """-> [(row_number, step_cell, when_cell, file_cell)] for one command file."""
    if not path.exists():
        return []
    rows = []
    lines = path.read_text(encoding="utf-8").splitlines()
    i = 0
    while i < len(lines):
        if cells(lines[i]) == list(HEADER):
            j = i + 1
            if j < len(lines) and is_separator(lines[j]):
                j += 1
            while j < len(lines) and lines[j].strip().startswith("|"):
                c = cells(lines[j])
                if len(c) >= 4:
                    rows.append((c[0], c[1], c[2], c[3]))
                j += 1
            i = j
        else:
            i += 1
    return rows


def names_banned_step(file_cell: str) -> bool:
    """A `File` cell naming the step file, alone or as one of several `·`-separated parts."""
    for part in file_cell.split("·"):
        base = part.strip().strip("`").strip()
        if base and Path(base).name == BANNED_STEP:
            return True
    return False


def runs_banned_script(step_cell: str, when_cell: str) -> bool:
    """An inline row that runs the proposal itself, naming no step file."""
    joined = f"{step_cell} {when_cell}"
    return BANNED_SCRIPT in joined and BANNED_FLAG in joined


def main() -> int:
    checked = 0
    failures: list[str] = []

    for router in sorted(COMMANDS.glob("*.md")):
        rel = router.relative_to(REPO)
        for n, step, when, files in router_rows(router):
            checked += 1
            if names_banned_step(files):
                failures.append(
                    f"{rel}: row {n} ({step}) routes {STEPS.name}/{BANNED_STEP}.\n"
                    f"      The fleet round must not propose managers. Remove the row."
                )
            elif runs_banned_script(step, when):
                failures.append(
                    f"{rel}: row {n} ({step}) runs `{BANNED_SCRIPT} {BANNED_FLAG}` inline.\n"
                    f"      The fleet round must not propose managers. Remove the row."
                )

    if failures:
        print(f"manager-proposal-row FAILED: {len(failures)} problem(s)", file=sys.stderr)
        for f in failures:
            print(f"  - {f}", file=sys.stderr)
        return 1

    print(f"manager-proposal-row ok: {checked} router row(s) route no manager proposal")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
