#!/usr/bin/env python3
"""Read one subject page's closure evidence — the two checkbox sections, counted apart.

    python3 scripts/goal-closure-read.py "24 Goals/<Goal>.md"

Prints one line per section plus a verdict, and exits on the verdict:

    status: in_progress
    Success Criteria: 3 done, 0 open
    Definition of Done: 2 done, 0 open
    VERDICT: CLOSE

    CLOSE         every section present, every section carrying at least one box,
                  and zero open boxes across both          -> exit 0
    OPEN          at least one box is open                -> exit 1
    UNMEASURABLE  a section is absent, or carries no recognised box -> exit 1
    TERMINAL      the page already reads completed/aborted -> exit 1

`VERDICT: TERMINAL` is the guard that stops a closed page being re-closed on every
later tick: the close verb writes `status: completed` and leaves every box at `[x]`,
so a page that closed last tick satisfies the same predicate this tick. Nothing else
in the tree stops a goal loop on completion.

⚠️ **An empty or unrecognised section is UNMEASURABLE, never complete.** The
predicate is "every box reads `[x]`", and a section with no box at all satisfies it
vacuously — which is how a page with an empty `# Definition of Done` used to reach
the close verb. The old whole-page `grep -c` had the same defect from the other side:
it counted the `# Tasks` boxes too, so it could not report the SC/DoD split at all.

⚠️ **Any marker that is not `x`/`X`/space/`/` counts as OPEN**, not as done. A
`- [-]` or `- [?]` box is a marker this reader does not understand, and the safe
direction is to refuse the close rather than to guess. The close verb re-checks its
own gate, so a refusal here costs one round; a false CLOSE costs a wrongly-closed page.
"""
import os
import re
import sys

TERMINAL_STATUSES = {"completed", "aborted"}

# `-`, `*` and `+` are all valid markdown bullets, and `[X]` is the same tick as
# `[x]` to every renderer — the earlier `-`-only, lowercase-only regex read both as
# "no boxes", which the verdict then treated as complete.
_BOX = re.compile(r"^[ \t]*[-*+][ \t]*\[(.)\]", re.M)
_DONE = {"x", "X"}
_FRONTMATTER = re.compile(r"\A---\n(.*?)\n---\n", re.S)
# A checkbox inside a fenced block is an EXAMPLE, not a box. It fails open in the
# dangerous direction: a section with no real box but a fenced `- [x]` reads (1, 0)
# instead of (0, 0), so it stops being UNMEASURABLE and can contribute to a CLOSE.
_FENCE = re.compile(r"^[ \t]*(```|~~~).*?^[ \t]*\1[^\n]*$", re.S | re.M)


def read_status(text):
    """The page's frontmatter `status:`, or None when the field is absent."""
    m = _FRONTMATTER.match(text)
    if not m:
        return None
    for line in m.group(1).splitlines():
        if line.startswith("status:"):
            return line.split(":", 1)[1].strip().strip("'\"") or None
    return None


def read_section(text, name):
    """(done, open) for one `# <name>` section, or None when the section is absent.

    Returns (0, 0) for a section that is present but carries no box — the caller
    must treat that as unmeasurable, not as complete.
    """
    m = re.search(rf"^# {re.escape(name)}\s*$(.*?)(?=^# |\Z)", text, re.S | re.M)
    if not m:
        return None
    done = open_ = 0
    for marker in _BOX.findall(_FENCE.sub("", m.group(1))):
        if marker in _DONE:
            done += 1
        else:
            open_ += 1
    return done, open_


def verdict_of(status, sections):
    if (status or "").strip().lower() in TERMINAL_STATUSES:
        return "TERMINAL"
    for _, tally in sections.items():
        if tally is None:
            return "UNMEASURABLE"
        if tally[0] + tally[1] == 0:
            return "UNMEASURABLE"
    if any(tally[1] > 0 for _, tally in sections.items()):
        return "OPEN"
    return "CLOSE"


def main(argv):
    if len(argv) != 2:
        print(f"usage: {os.path.basename(argv[0])} <page>", file=sys.stderr)
        return 2
    try:
        with open(argv[1], encoding="utf-8") as handle:
            text = handle.read()
    except OSError as exc:
        print(f"cannot read {argv[1]}: {exc}", file=sys.stderr)
        return 2

    status = read_status(text)
    sections = {name: read_section(text, name) for name in ("Success Criteria", "Definition of Done")}
    verdict = verdict_of(status, sections)

    print(f"status: {status or 'ABSENT'}")
    for name, tally in sections.items():
        if tally is None:
            print(f"{name}: MISSING")
        elif tally[0] + tally[1] == 0:
            print(f"{name}: 0 boxes — unmeasurable")
        else:
            print(f"{name}: {tally[0]} done, {tally[1]} open")
    print(f"VERDICT: {verdict}")
    return 0 if verdict == "CLOSE" else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
