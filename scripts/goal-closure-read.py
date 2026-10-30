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
                  and zero open boxes across both — or an absent `# Definition of
                  Done` on a GRANDFATHERED goal              -> exit 0
    OPEN          at least one box is open                -> exit 1
    UNMEASURABLE  a section is absent, or carries no recognised box -> exit 1
    TERMINAL      the page already reads completed/aborted -> exit 1

⚠️ **A grandfathered goal is the one exception to "a section is absent".** The vault
requires a `# Definition of Done` only on goals created on or after `DOD_REQUIRED_AS_OF`
(`2026-06-26`); older goals — and ones with no `created` — are grandfathered, so an
absent DoD is not a gap there. The exception covers ABSENCE ONLY (an empty DoD section
stays UNMEASURABLE) and GOALS ONLY (a task keeps the strict rule, and a topic closes on
its `# Completion Gate`, which has no cutoff).

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

# The date the vault began requiring a `# Definition of Done` on goals. A goal created
# before it — or carrying no `created` at all — is GRANDFATHERED: an absent DoD is a
# known, accepted gap, not a violation. Lexicographic comparison is exact here because
# the vault writes ISO-8601 (`2026-10-19`, `2026-09-20T00:00:00Z`); no parsing, no
# timezone, and a `created` on the cutoff date itself reads as post-cutoff.
#
# ⚠️ This is the SAME constant the vault's `agents/goal-auditor.md` § 12 carries. Copies
# of one date are the trade, not an oversight: a plugin script cannot import from a vault
# agent. `commands/steps/goal-closure.md` and the CHANGELOG restate it too — change them
# together. The closure rule shipped in v0.217.0 without this cutoff and
# so made every grandfathered goal permanently unclosable — closable by every other
# reader in the vault, unclosable by the one that actually closes it.
#
# Revisit once the calendar has moved well past this date (> 6 months): either drop
# grandfathering entirely, retrofitting the vault, or move the cutoff forward.
DOD_REQUIRED_AS_OF = "2026-06-26"

# `-`, `*` and `+` are all valid markdown bullets, and `[X]` is the same tick as
# `[x]` to every renderer — the earlier `-`-only, lowercase-only regex read both as
# "no boxes", which the verdict then treated as complete.
_BOX = re.compile(r"^[ \t]*[-*+][ \t]*\[(.)\]", re.M)
_DONE = {"x", "X"}
_FRONTMATTER = re.compile(r"\A---\n(.*?)\n---\n", re.S)
# The only `created` shape the cutoff compares: a zero-padded ISO date with a real month
# and day, optionally followed by a time. Anchored and range-checked — an unanchored prefix
# match would let `2026-01-15garbage` through, and `2026-00-00` sorts below the cutoff and
# would exempt a page whose date is malformed. Anything else is refused, not compared.
_ISO_DATE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])($|[T ])")
# Any DoD heading, at any level and with any suffix — deliberately looser than the exact
# `# Definition of Done` that `section_of` reads. See `has_dod_heading`.
_DOD_HEADING = re.compile(r"^#+[ \t]*Definition of Done", re.M)


def normalise(text):
    """Fold CRLF/CR to LF.

    Universal-newlines mode already does this for a file read with `open(..., encoding=...)`,
    but the reader is also called on strings a caller built, and a CRLF page that reaches the
    delimiter unmatched loses the `status:` line entirely — which silently disables the
    TERMINAL guard and re-closes a finished page on every tick. `docs/subject-resolution.md:137`
    records the same trap for the shell path; this is the Python half of it.
    """
    return text.lstrip("﻿").replace("\r\n", "\n").replace("\r", "\n")


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


def _field(text, name):
    """One frontmatter scalar, unquoted, or None when the field is absent."""
    m = _FRONTMATTER.match(normalise(text))
    if not m:
        return None
    for line in m.group(1).splitlines():
        if line.startswith(f"{name}:"):
            return line.split(":", 1)[1].strip().strip("'\"") or None
    return None


def read_status(text):
    """The page's frontmatter `status:`, or None when the field is absent."""
    return _field(text, "status")


def read_created(text):
    """The page's frontmatter `created:` date, or None when the field is absent.

    Absent is a value here, not a parse failure: the grandfathering rule trusts the
    document's own metadata and never infers a date from the filesystem, so a goal with
    no `created` is grandfathered exactly like one dated before the cutoff.
    """
    return _field(text, "created")


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


def has_dod_heading(body):
    """True when the fence-stripped body carries a DoD heading at ANY level or suffix.

    `section_of` matches only an exact H1 `# Definition of Done`, so an `## Definition of
    Done`, or a `# Definition of Done (optional)`, reads as absent. That refusal is right
    for an ordinary page — the reader does not guess at shapes it does not support — but
    it must not become a CLOSE for a grandfathered goal: the document plainly HAS a DoD
    section holding boxes, this reader merely cannot read it, and closing on a section it
    failed to parse is a false CLOSE, the one direction this module refuses everywhere.

    Absence is therefore decided by a loose search, and exemption needs BOTH halves: no
    DoD heading anywhere, AND the strict section reader finding none.
    """
    return body is not None and _DOD_HEADING.search(body) is not None


def dod_exempt(kind, created):
    """True when an ABSENT `# Definition of Done` must not block this page's closure.

    The vault's grandfathering decision: a goal created before `DOD_REQUIRED_AS_OF`, or
    carrying no `created`, has no DoD and is not in violation — so refusing to close it
    contradicts a decision the vault already made. Every other reader honours the cutoff;
    a closure rule that does not is the one rule that can never agree with them.

    ⚠️ **Absence only, never an empty section.** A page whose DoD heading exists but
    holds no box is UNMEASURABLE at every cutoff. Grandfathering accepts that the section
    was never written, not that it was written and left blank.

    ⚠️ **Goals only.** The decision names goals (`agents/goal-auditor.md` § 12). A task's
    DoD requirement was never part of it, and a topic closes on `# Completion Gate`,
    which has no grandfathering at all. Widening this to every kind would close tasks on
    evidence the vault never agreed to accept.

    ⚠️ **Zero-padded ISO-8601 only, and anything else fails CLOSED.** The comparison is
    lexicographic, which is exact for `YYYY-MM-DD` and for a timestamp on that prefix —
    but `15.01.2026` and a bare `2026` both sort BELOW the cutoff and would exempt a page
    that is not grandfathered. A false CLOSE is the expensive direction here, so an
    unrecognised shape is refused rather than compared.
    """
    if kind != "goal":
        return False
    if created is None:
        return True
    return bool(_ISO_DATE.match(created)) and created < DOD_REQUIRED_AS_OF


def verdict_of(status, sections, dod_may_be_absent=False):
    if (status or "").strip().lower() in TERMINAL_STATUSES:
        return "TERMINAL"
    for name, tally in sections.items():
        if tally is None:
            if dod_may_be_absent and name == "Definition of Done":
                continue
            return "UNMEASURABLE"
        if tally[0] + tally[1] == 0:
            return "UNMEASURABLE"
    if any(tally[1] > 0 for tally in sections.values() if tally is not None):
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
    created = read_created(text)
    body = strip_fences(text)
    fenced_ok = body is not None
    sections = {name: section_of(body, name, strict) for name, strict in SECTIONS[kind]}
    # Two guards the cutoff must never skip, both failing CLOSED:
    #  - the frontmatter has to have PARSED. `read_created` returns None for an absent
    #    field, and `_field` returns None for an unreadable page too — so without this,
    #    a page whose frontmatter does not parse (a BOM, a missing trailing newline)
    #    would read as "no created" and be grandfathered, turning a refusal into a CLOSE.
    #  - the page must carry no DoD heading at all, not merely none this reader could
    #    parse (see `has_dod_heading`).
    frontmatter_ok = _FRONTMATTER.match(normalise(text)) is not None
    exempt = frontmatter_ok and not has_dod_heading(body) and dod_exempt(kind, created)
    verdict = verdict_of(status, sections, exempt)

    print(f"status: {status or 'ABSENT'}")
    if not fenced_ok:
        print("fences: UNTERMINATED — the section bodies cannot be measured")
    for name, tally in sections.items():
        if tally is None:
            if fenced_ok and exempt and name == "Definition of Done":
                print(f"{name}: ABSENT — grandfathered (created {created or 'absent'} < {DOD_REQUIRED_AS_OF})")
            else:
                print(f"{name}: MISSING" if fenced_ok else f"{name}: UNREADABLE")
        elif tally[0] + tally[1] == 0:
            print(f"{name}: 0 boxes — unmeasurable")
        else:
            print(f"{name}: {tally[0]} done, {tally[1]} open")
    print(f"VERDICT: {verdict}")
    return 0 if verdict == "CLOSE" else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
