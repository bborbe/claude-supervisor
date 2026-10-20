#!/usr/bin/env python3
"""The round-overlap rule has exactly one home, and the tick router dispatches it every tick.

`commands/steps/round-readings.md` states what a tick does when the previous round is
still in flight, which round owns each of the round's three writes, and the fate of a
late leg's `Audit` block. Three properties make that home load-bearing, and each of them
fails silently on its own:

1. **Present at all.** A deleted rule satisfies the other two vacuously — zero copies is
   still "exactly one home" under a naive count — so the marker must be *found*, not
   merely left uncounted.
2. **One copy, not one file.** The repo's own law is that *"a restated copy is a second
   counter"* (`docs/fleet-surface.md` § Spawn a worker). Counting files is not enough: the
   marker pasted twice inside the same file is two homes wearing one filename.
3. **Dispatched every tick — and by the router that actually fires every tick.** This is
   the one that reads backwards, twice.
   - A step file's own `When:` line is a *restatement*; the normative trigger is the router
     row that names it (`check-step-when.py` keeps the two in agreement). Asserting on the
     step file alone proves only that the file describes itself — **delete the row naming
     it from every router and the assertion still passes while nothing reads the rule at
     all** (measured 2026-10-11 by mutation).
   - ⚠️ **And "some router names it" is still too weak, because the loop has two routers
     and only one of them re-runs.** `commands/goal-loop.md` is the *arm* — invoked once
     to start the loop and never re-invoked, which its own tick file states — and it
     carries the same step table as the *tick* router `commands/manager-loop-tick.md`,
     which is what a wakeup names. A row in either satisfies an "any router" test, so
     deleting the row from the **tick** router alone leaves the guard green while the rule
     stops being read on every round it governs. Measured 2026-10-11 by mutation, on the
     review of #256 — the guard printed *"named by a router row that fires every tick"*
     while the only row that fires was gone.
   The reach is therefore pinned to a **round router** — a `*-tick.md` file, or one of the
   paths listed in `ROUND_ROUTERS` — so the arm's row cannot stand in for the row that
   fires.
   - ⚠️ **The filename was only ever a proxy for the property.** On 2026-10-11 the manager
     loop's round router stopped being a `*-tick.md` file: `commands/goal-drive.md`
     became the one full round and the tick was retired, and the arm's duplicate table was
     removed in the same change. The property this check needs is unchanged — *a router the
     wakeup re-invokes every round, which the arm is not* — so the round routers are named
     explicitly here rather than inferred from a suffix that no longer tracks the fact.

**What this proves, and what it does not.** It proves the rule is present, single-copy,
and named by a **round-router** row whose trigger is every-tick. It does **not** prove the
ownership answers are right — a rule that is present, single-copy and tick-dispatched, and
wrong in all three, passes here. A green run reads as more than it is, so the limit is
stated rather than implied.

The marker is matched as a plain substring rather than as a regex or a heading, so it
survives a reflow of the surrounding prose. It is deliberately a phrase from the rule's
own first sentence: rewording that sentence is a deliberate act, and the guard failing is
the reminder that its home assertion moves with it.
"""

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
COMMANDS = REPO / "commands"
STEPS = COMMANDS / "steps"
MARKER = "is ABSORBED, never run"
EVERY_TICK = "every tick"
TICK_ROUTER = "-tick.md"
# ⚠️ The manager loop's round router is no longer named `*-tick.md`: since 2026-10-11
# `commands/goal-drive.md` is the one full round and the separate tick is retired, so the
# suffix no longer tracks the property this check needs. That property — "a router the wakeup
# re-invokes every round, which the arm is not" — is unchanged, so the round routers are
# listed explicitly beside the `*-tick.md` glob the fleet loop still satisfies.
ROUND_ROUTERS = ("commands/goal-drive.md",)
HEADER = ("#", "Step", "When", "File")


def cells(line: str) -> list[str]:
    return [c.strip() for c in line.strip().strip("|").split("|")]


def is_separator(line: str) -> bool:
    return set(line.strip().strip("|").replace(" ", "")) <= set("-:|")


def router_rows() -> list[tuple[Path, str, list[str]]]:
    """-> [(router, when_cell, [step file basenames])] over every router under commands/.

    Discovered by header, never hard-coded, so a second router is covered without
    registration — the same shape `check-step-when.py` uses. `File` cells are matched on
    basename, so a `${CLAUDE_PLUGIN_ROOT}/…` absolute path works, and a cell naming more
    than one step file (separated by `·`) is split rather than truncated.
    """
    rows: list[tuple[Path, str, list[str]]] = []
    for path in sorted(COMMANDS.glob("*.md")):
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
                            if not base or base.startswith("*"):
                                continue  # an inline row names no step file
                            named.append(Path(base).name)
                        if named:
                            rows.append((path, c[2], named))
                    j += 1
                i = j
            else:
                i += 1
    return rows


def marker_occurrences() -> list[tuple[Path, int]]:
    """-> [(file, count)] for every file carrying the marker, files with none omitted."""
    found = []
    for p in sorted(COMMANDS.rglob("*.md")):
        n = p.read_text(encoding="utf-8").count(MARKER)
        if n:
            found.append((p, n))
    return found


def main() -> int:
    failures: list[str] = []
    found = marker_occurrences()
    total = sum(n for _, n in found)

    if total == 0:
        failures.append(
            f"no file under {COMMANDS.relative_to(REPO)}/ carries the rule marker "
            f"{MARKER!r}.\n"
            f"      The rule is missing, not merely misplaced — and a missing rule "
            f"passes a naive single-home count."
        )
    elif total > 1:
        where = ", ".join(f"{p.relative_to(REPO)} ×{n}" for p, n in found)
        failures.append(
            f"the rule marker {MARKER!r} appears {total} times: {where}.\n"
            f"      One copy only — a second copy is the counter a grep cannot tell "
            f"from the real one, and one file carrying it twice is still two homes."
        )
    else:
        home, _ = found[0]
        rel = home.relative_to(REPO)
        if home.parent != STEPS:
            failures.append(
                f"{rel}: the rule's home must be a step file under "
                f"{STEPS.relative_to(REPO)}/.\n"
                f"      The arm is invoked once and never re-read, so a per-round rule "
                f"there stops governing after the first tick."
            )
        else:
            base = home.name
            rows = router_rows()
            named_by = [(r, w) for r, w, named in rows if base in named]
            round_rows = [
                (r, w) for r, w in named_by
                if r.name.endswith(TICK_ROUTER)
                or str(r.relative_to(REPO)) in ROUND_ROUTERS
            ]
            reaching = [(r, w) for r, w in round_rows if w.startswith(EVERY_TICK)]

            if not named_by:
                failures.append(
                    f"{rel}: no router row names this file.\n"
                    f"      The file's own `When:` describes itself; the router is what "
                    f"actually reads it. A step file nothing dispatches is dead text."
                )
            elif not round_rows:
                shown = "; ".join(f"{r.name}: {w!r}" for r, w in named_by)
                failures.append(
                    f"{rel}: only the arm names it — {shown}.\n"
                    f"      The arm (`commands/goal-loop.md`) is invoked once to start "
                    f"the loop and is never re-invoked, so a row there does NOT read the "
                    f"rule on the rounds it governs. A round router — a `*{TICK_ROUTER}` "
                    f"file, or one of {', '.join(ROUND_ROUTERS)} — must name this file."
                )
            elif not reaching:
                shown = "; ".join(f"{r.name}: {w!r}" for r, w in round_rows)
                failures.append(
                    f"{rel}: the tick router names it, but no row's `When` is every-tick "
                    f"— {shown}.\n"
                    f"      A gated row is skipped on every tick whose `When` does not "
                    f"hold, so the rule would not be read on the rounds it governs."
                )

    if failures:
        print(f"round-overlap FAILED: {len(failures)} problem(s)", file=sys.stderr)
        for f in failures:
            print(f"  - {f}", file=sys.stderr)
        return 1

    home = found[0][0].relative_to(REPO)
    print(
        f"round-overlap ok: {MARKER!r} appears once, in {home}, "
        f"named by a round-router row that fires every tick"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
