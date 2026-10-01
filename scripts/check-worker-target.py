#!/usr/bin/env python3
"""The fleet-wide worker target has exactly one home, and the number in that home
agrees with the number in the code.

The constant lives in two places by necessity — a prose default in
`docs/fleet-surface.md` § Spawn a worker **item 5**, and `DEFAULT_MAX_CONCURRENT` in
`server/spawn-mode.mjs`. Those two are allowed to exist; a *third* copy is not. The
repo has shipped that failure before: the cap was restated in `commands/manager-loop.md`,
`commands/manager-verify.md` and the manager runbook's Guardrail 2 until 2026-09-24, so
one cap became four counters the day one home was edited and the others were not — with
no error and no diff to catch it.

So this check asserts three things:

  (a) the home exists and still names the default — the anchor is a pointer, and a
      pointer into a section that no longer states the value points at nothing
  (b) every `commands/` and `agents/` file that names `maxConcurrent` carries the
      **pointer** to the home rather than its own copy of the number
  (c) the prose default in the home and `DEFAULT_MAX_CONCURRENT` in the code **agree**

⚠️ (c) is the half that cannot be satisfied by writing prose. Changing the code constant
without the doc, or the doc without the code, fails here — which is exactly the drift a
reader cannot see, because both surfaces look self-consistent while they disagree.

**What this check does not prove.** It is a prose-shape check over markdown plus one
constant read, because a command file *is* prose — there is no function to call. It
cannot prove the target is *consulted* at a tick; it proves the number has one home and
that the home and the code have not drifted apart. The behavioural half is the manager
loop actually posting a card below the target, which no grep can fake.
"""
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent

HOME = "docs/fleet-surface.md"
#: The pointer every file naming the constant must carry. Deliberately specific: naming
#: the file alone would pass for a file that mentions fleet-surface for any other reason.
HOME_ANCHOR = "Spawn a worker item 5"
#: The home itself is identified by its **section heading plus item**, never by the pointer
#: string above. ⚠️ That distinction is load-bearing and was measured: `docs/fleet-surface.md`
#: contains the literal `Spawn a worker item 5` at two *other* places (lines 35 and 70, each
#: citing the rule from a different topic), so a home check keyed on the pointer passes even
#: after the section it points at has been renamed or renumbered away.
HOME_HEADING = "## Spawn a worker"
HOME_ITEM = re.compile(r"^5\. \*\*Respect the fleet-wide worker target", re.M)
#: The number's home in code.
CODE = "server/spawn-mode.mjs"
CODE_CONSTANT = "DEFAULT_MAX_CONCURRENT"

#: Directories whose files may *reference* the constant but must never restate it.
REFERENCING_DIRS = ("commands", "agents")
#: The name that makes a file a referencing site at all.
CONSTANT_NAME = "maxConcurrent"


def code_default(root):
    """`DEFAULT_MAX_CONCURRENT`'s value, or None when it cannot be read.

    None is reported as a failure, never as a pass: an unreadable constant means the
    cross-check could not run, and "could not check" reading as "they agree" is the
    defect one level down.
    """
    path = root / CODE
    if not path.exists():
        return None
    match = re.search(
        rf"^export const {CODE_CONSTANT}\s*=\s*(\d+)\s*$",
        path.read_text(encoding="utf-8"),
        re.M,
    )
    return int(match.group(1)) if match else None


def check(root):
    """Return a list of failure strings for the tree at `root`. Empty means pass."""
    failures = []

    home_path = root / HOME
    if not home_path.exists():
        return [f"{HOME}: the constant's home is missing"]
    home = home_path.read_text(encoding="utf-8")

    if HOME_HEADING not in home:
        failures.append(
            f"{HOME}: no {HOME_HEADING!r} section — the constant's home is gone, so every "
            f"pointer to it now points at nothing"
        )
    elif not HOME_ITEM.search(home):
        failures.append(
            f"{HOME}: {HOME_HEADING!r} exists but carries no item 5 for the fleet-wide worker "
            f"target — the section was renumbered or retitled, and every `{HOME_ANCHOR}` "
            f"pointer into it now lands somewhere else"
        )

    default = code_default(root)
    if default is None:
        failures.append(
            f"{CODE}: could not read `{CODE_CONSTANT}` — the doc/code cross-check could "
            f"not run, which is not the same as passing"
        )
    else:
        # (c) the prose default and the code default must agree.
        if f"The default is {default}" not in home:
            failures.append(
                f"{HOME}: does not state `The default is {default}` — the home's prose and "
                f"`{CODE_CONSTANT}` in {CODE} have drifted apart, and both read as "
                f"self-consistent while they disagree"
            )

    # (b) a referencing file points at the home; it does not carry its own copy.
    for sub in REFERENCING_DIRS:
        for path in sorted((root / sub).rglob("*.md")):
            text = path.read_text(encoding="utf-8")
            if CONSTANT_NAME not in text:
                continue
            rel = str(path.relative_to(root))
            if HOME_ANCHOR not in text:
                failures.append(
                    f"{rel}: names `{CONSTANT_NAME}` but carries no pointer to the "
                    f"constant's home ({HOME_ANCHOR!r}) — read it there, never restate it"
                )

    return failures


def main():
    failures = check(ROOT)

    if failures:
        for f in failures:
            print(f"  worker-target FAIL: {f}", file=sys.stderr)
        sys.exit(1)

    default = code_default(ROOT)
    referencing = sum(
        1
        for sub in REFERENCING_DIRS
        for path in (ROOT / sub).rglob("*.md")
        if CONSTANT_NAME in path.read_text(encoding="utf-8")
    )
    print(
        f"  worker-target ok: one home ({HOME} {HOME_ANCHOR}) states the default "
        f"({default}), agrees with {CODE_CONSTANT}, and {referencing} referencing "
        f"file(s) point at it rather than restating it"
    )


if __name__ == "__main__":
    main()
