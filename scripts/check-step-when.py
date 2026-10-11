#!/usr/bin/env python3
"""A step file's `When:` must be byte-identical to its router row's `When` cell.

A router command carries a table of `#` / Step / When / File, and each named step file
restates that row's trigger on its own `When:` line. **The router row is the normative
trigger; the step file's `When:` is a restatement that must agree with it, never a second
independent statement.**

That distinction is not stylistic. A step file whose trigger is narrower than the
behaviour its body describes is silently gated out — the step simply stops running, and
nothing else in CI notices, because every file is individually well-formed and every
size is within budget. Three defects of exactly this class were found in one sibling
branch on 2026-10-11: a reading that stopped being printed every tick, a cleared-gate
prune that stopped running, and an initial arm that got gated out. Each was a `When`
that disagreed with the body it introduced.

The guard works only because the two statements are **byte-identical by construction**:
the step file's `When:` value is the router cell copied verbatim, markdown and all. Had
it been left as prose, a checker would have to normalise emphasis markers and
fuzzy-match, which is the kind of check that passes when it should fail.

**What this proves, and what it does not.** It proves the text agrees. It does **not**
prove the behaviour is right: a `When` copied faithfully into both places, and wrong in
both, passes here. A green run reads as more than it is, so the limit is stated rather
than implied.

The router command is discovered, not hard-coded: every `commands/*.md` carrying a
`| # | Step | When | File |` header is checked, so one script covers every router in the
repo. `File` cells are matched on basename, so a `${CLAUDE_PLUGIN_ROOT}/…` absolute path
works, and a cell naming more than one step file (separated by `·`) is split rather than
silently truncated to its first.
"""

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
COMMANDS = REPO / "commands"
STEPS = COMMANDS / "steps"
HEADER = ("#", "Step", "When", "File")


def cells(line: str) -> list[str]:
    return [c.strip() for c in line.strip().strip("|").split("|")]


def is_separator(line: str) -> bool:
    return set(line.strip().strip("|").replace(" ", "")) <= set("-:|")


def router_tables(path: Path):
    """-> [(row_number, when_cell, [step file paths])] for one command file."""
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
                    n, _step, when, files = c[0], c[1], c[2], c[3]
                    named = []
                    for part in files.split("·"):
                        base = part.strip().strip("`").strip()
                        if not base or base.startswith("*"):
                            continue  # an inline row names no step file
                        named.append(STEPS / Path(base).name)
                    if named:
                        rows.append((n, when, named))
                j += 1
            i = j
        else:
            i += 1
    return rows


def when_line(path: Path) -> str | None:
    if not path.exists():
        return None
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("When:"):
            return line[len("When:"):].strip()
    return None


def main() -> int:
    routers = sorted(p for p in COMMANDS.glob("*.md"))
    checked = 0
    failures: list[str] = []

    for router in routers:
        for n, when, files in router_tables(router):
            for step in files:
                rel = step.relative_to(REPO)
                if not step.exists():
                    failures.append(f"{rel}: named by {router.name} row {n} but does not exist")
                    continue
                got = when_line(step)
                if got is None:
                    failures.append(f"{rel}: carries no `When:` line (row {n} of {router.name})")
                    continue
                if got != when:
                    failures.append(
                        f"{rel}: `When:` disagrees with {router.name} row {n}.\n"
                        f"      router says: {when}\n"
                        f"      step file says: {got}\n"
                        f"      The router row is normative — the step file restates it and must match."
                    )
                checked += 1

    if failures:
        print(f"step-when FAILED: {len(failures)} problem(s)", file=sys.stderr)
        for f in failures:
            print(f"  - {f}", file=sys.stderr)
        return 1

    print(f"step-when ok: {checked} step file(s) restate their router row's When exactly")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
