#!/usr/bin/env python3
"""Read one subject page's closure evidence — its two closure sections, counted apart.

    python3 scripts/goal-closure-read.py --kind goal  "24 Goals/<Goal>.md"
    python3 scripts/goal-closure-read.py --kind topic "23 Topics/<Topic>.md"

⚠️ **`--kind` is not cosmetic: the two tiers close on DIFFERENT evidence.** A goal and
a task close on `# Success Criteria` + `# Definition of Done`; a **topic closes on
`# Success Criteria` + `# Completion Gate`** (`docs/session-progress.md:37`,
`docs/sweep-output.md:95` — a topic is the only level carrying both sets). Measuring a
topic against a Definition of Done it does not have reads UNMEASURABLE forever, and
measuring it against an SC it does have while ignoring the Gate closes it on the wrong
evidence. The kind is stated, never guessed from the page.

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


def normalise(text):
    """Fold CRLF/CR to LF.

    Universal-newlines mode already does this for a file read with `open(..., encoding=...)`,
    but the reader is also called on strings a caller built, and a CRLF page that reaches the
    delimiter unmatched loses the `status:` line entirely — which silently disables the
    TERMINAL guard and re-closes a finished page on every tick. `docs/subject-resolution.md:137`
    records the same trap for the shell path; this is the Python half of it.
    """
    return text.replace("\r\n", "\n").replace("\r", "\n")


def strip_fences(text):
    """Remove fenced code blocks, or return None when a fence is never closed.

    A checkbox inside a fence is an EXAMPLE, not a box, and it fails open in the dangerous
    direction: a section with no real box but a fenced `- [x]` reads (1, 0) instead of (0, 0),
    so it stops being UNMEASURABLE and can contribute to a CLOSE.

    Stripping happens over the WHOLE document, before the section is located — otherwise a
    line beginning `# ` inside a fence (a shell comment in a verification example) truncates
    the section, and every box after it goes uncounted. That direction loses an OPEN box and
    reports CLOSE.

    ⚠️ An UNTERMINATED fence returns None, which the caller reads as unmeasurable. Leaving its
    content in place is the failure this function exists to prevent, so the refusal is the fix.

    ⚠️ Known limit: a 4-space-indented code block is not stripped. Counting its boxes is the
    fail-safe direction in general (an example `- [ ]` blocks a close), but an indented
    `- [x]` in an otherwise empty section still reads as measurable. Stripping it properly
    needs list context this reader does not carry.
    """
    out, in_fence, marker = [], False, ""
    for line in normalise(text).splitlines(keepends=True):
        stripped = line.lstrip()
        if in_fence:
            if stripped.startswith(marker):
                in_fence = False
            continue
        if stripped.startswith("```") or stripped.startswith("~~~"):
            in_fence, marker = True, stripped[:3]
            continue
        out.append(line)
    return None if in_fence else "".join(out)


def read_status(text):
    """The page's frontmatter `status:`, or None when the field is absent."""
    m = _FRONTMATTER.match(normalise(text))
    if not m:
        return None
    for line in m.group(1).splitlines():
        if line.startswith("status:"):
            return line.split(":", 1)[1].strip().strip("'\"") or None
    return None


_LIST_ITEM = re.compile(r"^[ \t]*(?:[-*+]|\d+\.)[ \t]+\S", re.M)


def read_section(text, name, strict=False):
    """(done, open) for one `# <name>` section, or None when the section is absent.

    Returns (0, 0) for a section that is present but carries no box — the caller
    must treat that as unmeasurable, not as complete.

    `strict=True` additionally refuses a section that carries any list item which is
    not a checkbox. That is the `# Completion Gate` rule: a gate mixing `- [ ]` items
    with an ordered list is **out of compliance**, not a second supported shape
    (`docs/sweep-output.md:96`), and counting the checkboxes alone would silently drop
    the numbered items from the denominator.
    """
    return section_of(strip_fences(text), name, strict)


def section_of(body, name, strict=False):
    """`read_section` over an already fence-stripped body, so a caller can strip once."""
    if body is None:
        return None
    m = re.search(rf"^# {re.escape(name)}\s*$(.*?)(?=^# |\Z)", body, re.S | re.M)
    if not m:
        return None
    content = m.group(1)
    done = open_ = 0
    for marker in _BOX.findall(content):
        if marker in _DONE:
            done += 1
        else:
            open_ += 1
    if strict and len(_LIST_ITEM.findall(content)) != done + open_:
        return None
    return done, open_


# A topic is the only level carrying two closure sets, and its second one is the Gate,
# not a Definition of Done (`docs/session-progress.md:37`, `docs/sweep-output.md:95`).
SECTIONS = {
    "goal": (("Success Criteria", False), ("Definition of Done", False)),
    "task": (("Success Criteria", False), ("Definition of Done", False)),
    "topic": (("Success Criteria", False), ("Completion Gate", True)),
}


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
    args, kind = [], "goal"
    rest = list(argv[1:])
    while rest:
        arg = rest.pop(0)
        if arg == "--kind":
            if not rest:
                print("--kind needs a value: goal, task or topic", file=sys.stderr)
                return 2
            kind = rest.pop(0)
        elif arg.startswith("--kind="):
            kind = arg.split("=", 1)[1]
        else:
            args.append(arg)
    if len(args) != 1 or kind not in SECTIONS:
        print(
            f"usage: {os.path.basename(argv[0])} [--kind goal|task|topic] <page>",
            file=sys.stderr,
        )
        return 2
    path = args[0]
    try:
        with open(path, encoding="utf-8") as handle:
            text = normalise(handle.read())
    except OSError as exc:
        print(f"cannot read {path}: {exc}", file=sys.stderr)
        return 2

    status = read_status(text)
    body = strip_fences(text)
    fenced_ok = body is not None
    sections = {name: section_of(body, name, strict) for name, strict in SECTIONS[kind]}
    verdict = verdict_of(status, sections)

    print(f"status: {status or 'ABSENT'}")
    if not fenced_ok:
        print("fences: UNTERMINATED — the section bodies cannot be measured")
    for name, tally in sections.items():
        if tally is None:
            print(f"{name}: MISSING" if fenced_ok else f"{name}: UNREADABLE")
        elif tally[0] + tally[1] == 0:
            print(f"{name}: 0 boxes — unmeasurable")
        else:
            print(f"{name}: {tally[0]} done, {tally[1]} open")
    print(f"VERDICT: {verdict}")
    return 0 if verdict == "CLOSE" else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
