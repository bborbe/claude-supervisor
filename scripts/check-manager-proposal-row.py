#!/usr/bin/env python3
"""No router table may route a manager-proposal step.

The fleet round does not propose managers for goals that never had one. That is a decision,
not a default: the router row which ran `propose-managers.py --post` was removed on
2026-10-29, and nothing pinned the removal. `check-step-when.py` validates router rows →
step files only, so a step file with no row is never checked at all, and no test asserted the
table lacked the row — the behaviour could be re-added with every guard green.

**What this proves, and what it does not.** It proves no router row in `commands/*.md` routes
the proposal: neither by naming `commands/steps/propose-manager.md` in its `File` cell, nor by
running `propose-managers.py --post` from any of its cells. It does **not** prove the script
and the step file are unreachable — a step file may invoke them from its own body, and that is
deliberately out of scope. The rule is about the router, because the router is what a round
executes; a guard on step-file bodies would have to decide which invocations are legitimate,
which is the fuzzy check this repo avoids.

**Two shapes, because the step-file one alone is not enough.** `check-step-when.py` skips any
row whose `File` cell is `*inline …*` (its `router_tables` drops a part starting with `*`), so
an inline row — `File` cell `*inline*`, `When` cell carrying `python3 $P/propose-managers.py
--post` — is invisible to the only existing router guard, and is exactly the shape a re-add
would take if its author avoided the step file.

**Rows, never the whole file.** Prose in a router command may legitimately discuss the
proposal; a whole-file substring match would red on a documentation edit and be switched off
within a day. Only cells inside a `| # | Step | When | File |` table are read.

**A vacuous pass is a failure.** The header is matched by containing the four known labels, so
an extra column or a renamed neighbour does not hide the table. Recognising **zero** router
rows, or finding no router command at all, fails rather than printing `ok` — a guard that
silently checks nothing is worse than no guard, because it reads as coverage.
"""

import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
COMMANDS = REPO / "commands"
STEPS = COMMANDS / "steps"
HEADER = ("#", "Step", "When", "File")

BANNED_STEP = "propose-manager.md"
BANNED_SCRIPT = "propose-managers.py"
# Token-bounded: `--postpone` and `--post-x` are not the posting flag. `--post=value` is.
BANNED_FLAG = re.compile(r"--post(?![-\w])")


def cells(line: str) -> list[str]:
    return [c.strip() for c in line.strip().strip("|").split("|")]


def is_separator(line: str) -> bool:
    return set(line.strip().strip("|").replace(" ", "")) <= set("-:|")


def header_index(line: str) -> dict[str, int] | None:
    """-> {label: column} when the line carries all four router labels, else None.

    Containment rather than equality: a table with a trailing `Notes` column, or with its
    cells reordered, is still a router table and must still be read.
    """
    c = cells(line)
    if not all(label in c for label in HEADER):
        return None
    return {label: c.index(label) for label in HEADER}


def router_rows(path: Path):
    """-> [(row_number, step_cell, when_cell, file_cell)] for one command file.

    Raises OSError/UnicodeDecodeError to the caller, which reports it rather than letting a
    traceback stand in for the script's own diagnostic.
    """
    rows = []
    lines = path.read_text(encoding="utf-8").splitlines()
    i = 0
    while i < len(lines):
        idx = header_index(lines[i])
        if idx is None:
            i += 1
            continue
        width = max(idx.values()) + 1
        j = i + 1
        if j < len(lines) and is_separator(lines[j]):
            j += 1
        while j < len(lines) and lines[j].strip().startswith("|"):
            c = cells(lines[j])
            if len(c) >= width:
                rows.append((c[idx["#"]], c[idx["Step"]], c[idx["When"]], c[idx["File"]]))
            j += 1
        i = j
    return rows


def backticked(cell: str) -> str:
    """The first backticked token if the cell has one, else the cell itself.

    A `File` cell may carry a path plus trailing prose (` (see note)`) or a suffix; stripping
    only the cell's outer edges would leave a basename that matches nothing.
    """
    m = re.search(r"`([^`]+)`", cell)
    return m.group(1) if m else cell


def names_banned_step(file_cell: str) -> bool:
    """A `File` cell naming the step file, alone or as one of several `·`-separated parts."""
    for part in file_cell.split("·"):
        token = backticked(part).strip().split()[0] if backticked(part).strip() else ""
        if token and Path(token).name == BANNED_STEP:
            return True
    return False


def runs_banned_script(*text_cells: str) -> bool:
    """An inline row that runs the proposal itself, naming no step file."""
    joined = " ".join(text_cells)
    return BANNED_SCRIPT in joined and BANNED_FLAG.search(joined) is not None


def main() -> int:
    failures: list[str] = []
    checked = 0

    routers = sorted(COMMANDS.glob("*.md"))
    if not routers:
        print(
            f"manager-proposal-row FAILED: no router command found under {COMMANDS.relative_to(REPO)}.\n"
            f"      A green run over nothing is not coverage.",
            file=sys.stderr,
        )
        return 1

    for router in routers:
        rel = router.relative_to(REPO)
        try:
            rows = router_rows(router)
        except (OSError, UnicodeDecodeError) as exc:
            failures.append(f"{rel}: unreadable — {exc}")
            continue
        for n, step, when, files in rows:
            checked += 1
            if names_banned_step(files):
                failures.append(
                    f"{rel}: row {n} ({step}) routes {STEPS.name}/{BANNED_STEP}.\n"
                    f"      The fleet round must not propose managers. Remove the row."
                )
            elif runs_banned_script(step, when, files):
                failures.append(
                    f"{rel}: row {n} ({step}) runs `{BANNED_SCRIPT} --post` inline.\n"
                    f"      The fleet round must not propose managers. Remove the row."
                )

    if checked == 0:
        failures.append(
            f"recognised 0 router rows across {len(routers)} command file(s).\n"
            f"      The parser matched no `| # | Step | When | File |` table — a green run here would be vacuous."
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
