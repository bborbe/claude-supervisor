#!/usr/bin/env python3
"""The goal-row rule must stay single-valued, and its retracted readings must not come back.

`agents/manager-sweep-reader.md` states the goal-row rule in four places — input 8's
contract, step 7's frame paragraph, the optional-split paragraph and a `<success_criteria>`
bullet — because each is read on its own and a pointer alone is what this repo already
records as the liability (`scripts/check-bucket-clause.py`'s own docstring, same reason).
A rule with four call sites drifts unless something checks it, and this one did: within a
single review round the `<success_criteria>` bullet kept an escape clause the frame
paragraph had already retracted, and the input-8 contract's new "render no goal rows"
clause silently deleted the root row of every goal-branch frame. Both were found by a
reviewer reading prose, not by anything mechanical.

Same shape as `check-bucket-clause.py`, `check-recording-step.py` and
`check-necessity-templates.py`, for the same reason.

Anchored on the clauses' own opening words, never on line numbers: this file's paragraphs
moved twice inside one day (measured 2026-10-09), and a line-anchored read would have
reported drift that was really an offset.

⚠️ **The carrier count is matched by pattern, not by an exact string, and that is load-bearing.**
The four carriers do not spell the clause identically — the input-8 contract lowercases the
subject (*"a declared member goal renders as its own row…"*) and the optional-split paragraph
bolds the zero (*"…even with **0** tracked tasks"*). An exact-string count therefore sees only
**two** of the four and passes a tree that has deleted the clause from either of the others,
which is precisely the drift this guard exists to catch. A reviewer found that gap in this
guard's first version.

**What this proves, and what it does not.** It proves all four carriers still state the rule,
that the goal branch's exemption from the absent-input rule is still stated, and that the
retracted readings have not reappeared anywhere in the rule-carrying text. It cannot prove the
four carriers say the *same thing* — that is a reading, not a grep, and the round that produced
this guard is the evidence that no grep would have caught it. This is the drift guard, not the
semantics.

⚠️ **The retraction scan is deliberately scoped to `.md` under `agents/`, `commands/` and
`docs/`, and widening it to the repo root would fail on `CHANGELOG.md` itself** — that file
quotes `derived from its tasks' placement` as the reading this change retracts, so the entry
documenting the retraction is the one file guaranteed to trip the scan. Stating the boundary
here rather than leaving it to be discovered: a future widening needs an allowlist for the
changelog, and the reason it has none today is that the changelog is history while the three
scanned directories are the rule.
"""

import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

# The one sentence every carrier of the rule is built around, as a tolerant pattern. See the
# docstring: an exact string sees two of the four carriers, which is the defect this pattern
# replaces.
CLAUSE_RE = re.compile(
    r"renders as its own row even with \*{0,2}0\*{0,2} tracked tasks",
    re.IGNORECASE,
)
CARRIER = "agents/manager-sweep-reader.md"
CARRIER_MIN = 4

# Readings that were deliberately withdrawn. Each one had a defensible reading on the
# other side, which is exactly why it may not return as prose anywhere.
#
# ⚠️ The placement anchor is the LONGEST still-unambiguous fragment, not the shortest one
# that reads naturally. `"derived from its tasks"` alone is broader than the reading it
# retracts: the rule now legitimately says a goal row's **section** follows its tasks'
# placement, so a future carrier phrasing that as "derived from" would trip a precommit
# failure that reads as a rule regression. The full fragment stays narrow because the rule's
# own vocabulary for the section is *follows*, and the retracted sentence is the only place
# the derivation was ever attached to the row's EXISTENCE.
RETRACTED = (
    "derived from its tasks' placement",
    "you never place a goal",  # the imperative the existence rule contradicts
    "you place tasks, never goals",  # the same imperative, success-criteria form
)

# The goal branch always has input 8 absent, so the absent-input rule must name it as
# exempt. Deleting this sentence reintroduces the frame whose root row disappears.
GOAL_BRANCH_EXEMPTION = "The GOAL branch is exempt by construction"

SCAN_DIRS = ("agents", "commands", "docs")
SCAN_SUFFIXES = (".md",)


def markdown_files() -> list[Path]:
    out: list[Path] = []
    for name in SCAN_DIRS:
        root = REPO / name
        if not root.is_dir():
            continue
        for path in sorted(root.rglob("*")):
            if path.is_file() and path.suffix in SCAN_SUFFIXES:
                out.append(path)
    return out


def main() -> int:
    carrier = REPO / CARRIER
    if not carrier.exists():
        print(f"goal-row-clause FAILED: {CARRIER} does not exist", file=sys.stderr)
        return 1

    text = carrier.read_text(encoding="utf-8")
    hits = len(CLAUSE_RE.findall(text))
    if hits < CARRIER_MIN:
        print(
            f"goal-row-clause FAILED: {CARRIER} states the clause {hits} time(s), want "
            f"{CARRIER_MIN} — the four carriers are input 8's contract, step 7's frame "
            f"paragraph, the optional-split paragraph and the success_criteria bullet",
            file=sys.stderr,
        )
        return 1
    if GOAL_BRANCH_EXEMPTION not in text:
        print(
            f"goal-row-clause FAILED: {CARRIER} dropped the goal-branch exemption "
            f"({GOAL_BRANCH_EXEMPTION!r}) — a goal-branch sweep always has input 8 absent, "
            f"so the absent-input rule must name that branch as exempt or it deletes the "
            f"root row of every goal-branch frame",
            file=sys.stderr,
        )
        return 1

    scanned = markdown_files()
    offenders: list[str] = []
    for path in scanned:
        body = path.read_text(encoding="utf-8")
        for phrase in RETRACTED:
            if phrase in body:
                offenders.append(f"{path.relative_to(REPO)} carries the retracted reading {phrase!r}")
    if offenders:
        for line in offenders:
            print(f"goal-row-clause FAILED: {line}", file=sys.stderr)
        return 1

    print(
        f"goal-row-clause ok: the clause is stated {hits}x in {CARRIER}, the goal-branch "
        f"exemption is stated, and none of the {len(RETRACTED)} retracted readings appears across "
        f"{len(scanned)} file(s) in {', '.join(SCAN_DIRS)}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
