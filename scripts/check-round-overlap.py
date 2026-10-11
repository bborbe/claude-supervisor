#!/usr/bin/env python3
"""The round-overlap rule has exactly one home, and it is a step file read every tick.

`commands/steps/manager-readings.md` states what a tick does when the previous round is
still in flight, which round owns each of the round's three writes, and the fate of a
late leg's `Audit` block. Three properties make that home load-bearing, and each of them
fails silently on its own:

1. **Present at all.** A deleted rule satisfies the other two vacuously — zero copies is
   still "exactly one home" under a naive count — so the marker must be *found*, not
   merely left uncounted.
2. **Single home.** A second copy is the counter a `grep` cannot tell from the real one,
   which is the repo's own stated law (`docs/fleet-surface.md` § Spawn a worker, *"a
   restated copy is a second counter"*).
3. **Every-tick reach.** A per-round rule placed in the arm is read **once** and never
   again; one placed in a `When`-gated step file is skipped on every tick whose `When`
   does not hold. Either way the rule stops governing the rounds it exists for, and no
   other check notices — every file is individually well-formed and every size is within
   budget. This is the same class `check-step-when.py` guards from the other side: that
   one asserts a step file's `When` agrees with its router row, this one asserts the
   rule's own home is a file an every-tick `When` actually reaches.

**What this proves, and what it does not.** It proves the rule is present, single-homed,
and reachable on every tick. It does **not** prove the ownership answers are right: a rule
that is present, single-homed and every-tick, and wrong in all three, passes here. A green
run reads as more than it is, so the limit is stated rather than implied.

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


def when_line(path: Path) -> str | None:
    """The step file's own trigger, or None when it carries no `When:` line."""
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("When:"):
            return line[len("When:"):].strip()
    return None


def carriers() -> list[Path]:
    """Every markdown file under commands/ carrying the marker, arm and step files alike."""
    return sorted(
        p for p in COMMANDS.rglob("*.md") if MARKER in p.read_text(encoding="utf-8")
    )


def main() -> int:
    found = carriers()
    failures: list[str] = []

    if not found:
        failures.append(
            f"no file under {COMMANDS.relative_to(REPO)}/ carries the rule marker "
            f"{MARKER!r}.\n"
            f"      The rule is missing, not merely misplaced — and a missing rule "
            f"passes a naive single-home count."
        )
    elif len(found) > 1:
        names = ", ".join(str(p.relative_to(REPO)) for p in found)
        failures.append(
            f"{len(found)} files carry the rule marker {MARKER!r}: {names}.\n"
            f"      One home only — a second copy is the counter a grep cannot tell "
            f"from the real one."
        )
    else:
        home = found[0]
        rel = home.relative_to(REPO)
        if home.parent != STEPS:
            failures.append(
                f"{rel}: the rule's home must be a step file under "
                f"{STEPS.relative_to(REPO)}/.\n"
                f"      The arm is invoked once and never re-read, so a per-round rule "
                f"there stops governing after the first tick."
            )
        else:
            when = when_line(home)
            if when is None:
                failures.append(f"{rel}: carries no `When:` line")
            elif not when.startswith(EVERY_TICK):
                failures.append(
                    f"{rel}: `When:` is {when!r}, which is not an every-tick trigger.\n"
                    f"      A gated step file is skipped on every tick whose When does "
                    f"not hold, so the rule would not be read on the rounds it governs."
                )

    if failures:
        print(f"round-overlap FAILED: {len(failures)} problem(s)", file=sys.stderr)
        for f in failures:
            print(f"  - {f}", file=sys.stderr)
        return 1

    home = found[0].relative_to(REPO)
    print(f"round-overlap ok: {MARKER!r} has one home, {home}, reached every tick")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
