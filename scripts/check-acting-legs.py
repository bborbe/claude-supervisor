#!/usr/bin/env python3
"""The round's delegate rule carves out its two acting legs, and each leg says so at its own home.

`commands/fleet-drive.md`'s delegate rule tells every round to hand anything that is not the
sweep to a sub-agent. Rows 5b (keep alive) and 5c (flagged open) *are* "anything not the
sweep", and they are also the only two rows whose purpose is to **act** — restart or resume a
session, open a page the operator flagged. With no carve-out the natural reading of the rule
swallows exactly those two legs, and a delegated sub-agent runs read-only, so the round prints
a count it was told to print instead of doing the thing the row exists for.

Measured 2026-11-01 13:27 CET, session `ac24db61`: the round delegated 5c with the instruction
*"the literal line `unactioned: <found − opened> operator-flagged` **with opened=0**"*, never
invoked `/supervisor:open --flagged`, and a sub-agent that scanned 1532 of 3580 worker rows
resumed none of the 8 it read `resume`. A round that classified 50 sessions restarted nothing
and opened nothing, while printing three `unactioned:` lines that read like a leg that ran and
found work to leave alone.

Three properties, each of which fails silently on its own:

1. **The carve-out exists, once.** A deleted carve-out satisfies a naive count vacuously — zero
   copies is still "exactly one home" — so it must be *found*; and a second copy is the counter
   a grep cannot tell from the real one.
2. **It lives in the router's delegate rule**, not in a step file. The rule it excepts is
   there, and a carve-out filed away from its rule is read by nobody. It must name both rows,
   the write tools a delegated run needs, and the read-only case that is not a run.
3. **Each leg states its own acting property at its own home.** The router is read on every
   tick; a step file is read when the round reaches that row — the moment the leg runs. A leg
   whose own file never says it acts can be run read-only by a sub-agent that did exactly what
   the file said, and nothing in the tree notices.

**What this proves, and what it does not.** It proves the three texts are present, single-copy,
mutually pointed, and that the router still dispatches both legs. It does **not** prove a round
acted: a tree whose prose is right and whose round is still read-only passes here. The live
evidence is the round's own output; this guard only stops the rule being deleted or softened
without a test noticing.
"""

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
COMMANDS = REPO / "commands"
STEPS = COMMANDS / "steps"

#: Phrase from the carve-out's own first sentence. A plain substring, so a reflow of the
#: surrounding prose leaves it intact; rewording that sentence is a deliberate act, and the
#: failure is the reminder that this home assertion moves with it.
CARVEOUT = "5b and 5c are acting legs"
CARVEOUT_HOME = COMMANDS / "fleet-drive.md"
CARVEOUT_NEEDS = ("write tools", "read-only")

#: Phrase each leg states about itself. Deliberately the same text in both files: it is one
#: property, and two spellings of it would be two rules.
STEP_MARKER = "a read-only execution is not a run of it"
POINTER = "commands/fleet-drive.md"
LEGS = {
    "5b": STEPS / "keepalive.md",
    "5c": STEPS / "flagged-open.md",
}
HEADER = ("#", "Step", "When", "File")


def cells(line: str) -> list[str]:
    return [c.strip() for c in line.strip().strip("|").split("|")]


def is_separator(line: str) -> bool:
    return set(line.strip().strip("|").replace(" ", "")) <= set("-:|")


def router_rows(path: Path) -> dict[str, list[str]]:
    """-> {row number: [step file basenames]} for one command file's router table."""
    rows: dict[str, list[str]] = {}
    if not path.exists():
        return rows
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
                    named = []
                    for part in c[3].split("·"):
                        base = part.strip().strip("`").strip()
                        if base and not base.startswith("*"):
                            named.append(Path(base).name)
                    rows[c[0]] = named
                j += 1
            i = j
        else:
            i += 1
    return rows


def occurrences(marker: str) -> list[tuple[Path, int]]:
    """-> [(file, count)] for every file under commands/ carrying the marker."""
    found = []
    for p in sorted(COMMANDS.rglob("*.md")):
        n = p.read_text(encoding="utf-8").count(marker)
        if n:
            found.append((p, n))
    return found


def paragraph_with(path: Path, marker: str) -> str:
    """The blank-line-delimited block containing `marker`, or '' when absent."""
    for block in path.read_text(encoding="utf-8").split("\n\n"):
        if marker in block:
            return block
    return ""


def main() -> int:
    failures: list[str] = []

    # 1 + 2 — the carve-out exists exactly once, and it is the router's delegate rule.
    found = occurrences(CARVEOUT)
    total = sum(n for _, n in found)
    if total == 0:
        failures.append(
            f"no file under {COMMANDS.relative_to(REPO)}/ carries the carve-out {CARVEOUT!r}.\n"
            f"      Without it the delegate rule reads as 'anything not the sweep', which\n"
            f"      swallows rows 5b and 5c — the two legs whose whole purpose is to act."
        )
    elif total > 1:
        where = ", ".join(f"{p.relative_to(REPO)} ×{n}" for p, n in found)
        failures.append(
            f"the carve-out {CARVEOUT!r} appears {total} times: {where}.\n"
            f"      One copy only — the two step files point at it rather than restating it."
        )
    else:
        home, _ = found[0]
        if home != CARVEOUT_HOME:
            failures.append(
                f"{home.relative_to(REPO)}: the carve-out's home must be "
                f"{CARVEOUT_HOME.relative_to(REPO)}, beside the delegate rule it excepts.\n"
                f"      A carve-out filed away from its rule is read by nobody."
            )
        else:
            block = paragraph_with(home, CARVEOUT)
            for need in CARVEOUT_NEEDS:
                if need not in block:
                    failures.append(
                        f"{home.relative_to(REPO)}: the carve-out does not state {need!r}.\n"
                        f"      It must name the write tools a delegated run needs and say that\n"
                        f"      a read-only run is not a run of the leg — the measured failure."
                    )

    # 3 — each leg states its own acting property, at its own home, and points at the carve-out.
    for row, leg in LEGS.items():
        rel = leg.relative_to(REPO)
        if not leg.exists():
            failures.append(f"{rel}: named as row {row}'s acting leg but the file does not exist")
            continue
        text = leg.read_text(encoding="utf-8")
        n = text.count(STEP_MARKER)
        if n == 0:
            failures.append(
                f"{rel}: carries no acting statement {STEP_MARKER!r}.\n"
                f"      Row {row} exists to act. A file that never says so can be delegated\n"
                f"      read-only — the round then prints a count it was told to, not one it measured."
            )
        elif n > 1:
            failures.append(f"{rel}: the acting statement appears {n} times — once is the rule")
        if POINTER not in text:
            failures.append(
                f"{rel}: does not point at {POINTER} for the delegation carve-out.\n"
                f"      The exception has one home; the leg names it rather than restating it."
            )

    # 4 — the router still dispatches both legs, so the carve-out governs something.
    rows = router_rows(CARVEOUT_HOME)
    for row, leg in LEGS.items():
        if leg.name not in rows.get(row, []):
            failures.append(
                f"{CARVEOUT_HOME.relative_to(REPO)}: row {row} does not name {leg.name}.\n"
                f"      A carve-out for a row the router no longer dispatches governs nothing."
            )

    if failures:
        print(f"acting-legs FAILED: {len(failures)} problem(s)", file=sys.stderr)
        for f in failures:
            print(f"  - {f}", file=sys.stderr)
        return 1

    print(
        f"acting-legs ok: {CARVEOUT!r} appears once, in "
        f"{CARVEOUT_HOME.relative_to(REPO)}, and both acting legs state it at their own home"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
