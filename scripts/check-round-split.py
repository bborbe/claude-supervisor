#!/usr/bin/env python3
"""The fleet round's row split is stated in four places and they must agree.

A round is run by two sessions: the manager, and the one `supervisor:fleet-round`
delegate it dispatches. Which router row belongs to which is written down four times —
the router's own list (`commands/fleet-drive.md`), the dispatch instruction
(`commands/steps/round-dispatch.md`), and the delegate's agent file, twice: the rows it
runs (§ 1) and the rows it leaves alone (§ 2).

**Why this needs a check rather than care.** The split was moved on 2026-11-19 and the
first cut edited three of the four places; the review found the fourth still telling the
delegate that a moved row was the manager's, which re-owned it in prose the move depended
on. A row can be assigned twice or not at all while every file stays individually
well-formed, every size stays within budget, and every other check passes — so nothing
else in CI can notice. Measured twice on this repo: the 2026-11-17 attempts moved rows in
one place, and the 2026-11-19 move missed `agents/fleet-round.md`'s role block.

**What this proves, and what it does not.** It proves the four statements name the same
sets. It does **not** prove the split is right — a row assigned to the wrong session in
all four places passes here. The limit is stated rather than implied.

A row is a token: `0`, `5b`, `16b`. The router's list and the dispatch instruction are
read for their two comma-separated runs; the agent's tables are read from their `#`
column, between the `## 1.` and `## 3.` headings.
"""

import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
ROUTER = REPO / "commands" / "fleet-drive.md"
DISPATCH = REPO / "commands" / "steps" / "round-dispatch.md"
AGENT = REPO / "agents" / "fleet-round.md"

ROW = re.compile(r"\d+b?")


def rows_in(text: str) -> set[str]:
    """Every row token in a comma-separated run of them."""
    return {m.group(0) for m in ROW.finditer(text) if m.group(0)}


def base(row: str) -> str:
    """`5b` → `5`. A sub-row rides its parent: the router's `2–10` covers 5b and 5d."""
    return row.rstrip("bd")


def expand(run: str) -> set[str]:
    """`0, 2–10, 12` → {0, 2..10, 12}. An en dash is a range; a comma is a break."""
    out: set[str] = set()
    for part in run.split(","):
        part = part.strip()
        if not part:
            continue
        if "–" in part:
            lo, hi = (p.strip() for p in part.split("–", 1))
            if lo.isdigit() and hi.isdigit():
                out.update(str(n) for n in range(int(lo), int(hi) + 1))
                continue
        out.update(rows_in(part))
    return {base(r) for r in out}


def router_rows() -> set[str]:
    """Every row number in the router's own `| # | Step | When | File |` table."""
    text = ROUTER.read_text(encoding="utf-8")
    if "| # | Step | When | File |" not in text:
        raise SystemExit("FAIL: commands/fleet-drive.md carries no router table")
    return {base(m.group(1)) for m in re.finditer(r"^\| (\d+b?d?) \|", text, re.M)}


def router_split() -> tuple[set[str], set[str]]:
    text = ROUTER.read_text(encoding="utf-8")
    line = next(
        (l for l in text.splitlines() if "run in `supervisor:fleet-round`" in l), None
    )
    if line is None:
        raise SystemExit("FAIL: commands/fleet-drive.md carries no row-split line")
    delegate = re.search(r"rows ([^;]+) run in `supervisor:fleet-round`", line)
    manager = re.search(r"; ([^;]+) stay here", line)
    if not (delegate and manager):
        raise SystemExit("FAIL: the row-split line does not parse")
    return expand(delegate.group(1)), expand(manager.group(1))


def dispatch_split() -> tuple[set[str], set[str]]:
    text = DISPATCH.read_text(encoding="utf-8")
    delegate = re.search(r"runs rows ([^—]+?) —", text)
    manager = re.search(r"You keep rows ([^:]+?):", text)
    if not (delegate and manager):
        raise SystemExit("FAIL: commands/steps/round-dispatch.md does not parse")
    return expand(delegate.group(1)), expand(manager.group(1))


def agent_split() -> tuple[set[str], set[str]]:
    text = AGENT.read_text(encoding="utf-8")
    try:
        head = text.index("## 1. The rows you run")
        mid = text.index("## 2. The rows you leave alone")
        tail = text.index("## 3. Read your own")
    except ValueError as exc:  # pragma: no cover - structural
        raise SystemExit(f"FAIL: agents/fleet-round.md headings not found: {exc}")
    cell = re.compile(r"^\| (\d+b?d?) \|", re.M)
    return (
        {base(m.group(1)) for m in cell.finditer(text[head:mid])},
        {base(m.group(1)) for m in cell.finditer(text[mid:tail])},
    )


def main() -> int:
    router = router_split()
    dispatch = dispatch_split()
    agent = agent_split()

    problems: list[str] = []
    for name, (delegate, manager) in (
        ("router", router),
        ("dispatch", dispatch),
        ("agent", agent),
    ):
        both = delegate & manager
        if both:
            problems.append(f"{name}: assigned to BOTH sessions: {sorted(both)}")
    if router[0] != dispatch[0]:
        problems.append(
            f"router and dispatch disagree on the delegate's rows: "
            f"only-router={sorted(router[0] - dispatch[0])} "
            f"only-dispatch={sorted(dispatch[0] - router[0])}"
        )
    if router[0] != agent[0]:
        problems.append(
            f"router and the agent's § 1 disagree: "
            f"only-router={sorted(router[0] - agent[0])} "
            f"only-agent={sorted(agent[0] - router[0])}"
        )
    if router[1] != dispatch[1]:
        problems.append(
            f"router and dispatch disagree on the manager's rows: "
            f"only-router={sorted(router[1] - dispatch[1])} "
            f"only-dispatch={sorted(dispatch[1] - router[1])}"
        )
    if router[1] != agent[1]:
        problems.append(
            f"router and the agent's § 2 disagree: "
            f"only-router={sorted(router[1] - agent[1])} "
            f"only-agent={sorted(agent[1] - router[1])}"
        )

    # Pairwise agreement is not coverage: a row dropped from ALL four places passes
    # pairwise. The router table is the population, so the union must cover it.
    uncovered = router_rows() - (router[0] | router[1])
    if uncovered:
        problems.append(
            f"row(s) in the router table assigned to NEITHER session: {sorted(uncovered)}"
        )

    if problems:
        for problem in problems:
            print(f"FAIL: {problem}", file=sys.stderr)
        return 1

    print(
        f"round-split ok: {len(router[0])} delegate row(s) and {len(router[1])} "
        f"manager row(s) agree across the router, the dispatch instruction and both "
        f"agent tables"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
