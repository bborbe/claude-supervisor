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

**What this proves, and what it does not.** It proves the canonical clause is still
carried, that the retracted readings have not reappeared anywhere in the rule-carrying
text, and that the goal branch's exemption from the absent-input rule is still stated.
It cannot prove the four carriers say the *same thing* — that is a reading, not a grep,
and the round that produced this guard is the evidence that no grep would have caught it.
This is the drift guard, not the semantics.
"""

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

# The one sentence every carrier of the rule is built around.
CLAUSE = "A member goal renders as its own row even with 0 tracked tasks"
CLAUSE_CARRIER = "agents/manager-sweep-reader.md"

# Readings that were deliberately withdrawn. Each one had a defensible reading on the
# other side, which is exactly why it may not return as prose anywhere.
RETRACTED = (
    "derived from its tasks",  # the placement-derivation that dropped zero-task rows
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
    carrier = REPO / CLAUSE_CARRIER
    if not carrier.exists():
        print(f"goal-row-clause FAILED: {CLAUSE_CARRIER} does not exist", file=sys.stderr)
        return 1

    text = carrier.read_text(encoding="utf-8")
    hits = text.count(CLAUSE)
    if hits < 1:
        print(
            f"goal-row-clause FAILED: {CLAUSE_CARRIER} no longer carries the clause "
            f"{CLAUSE!r}",
            file=sys.stderr,
        )
        return 1
    if GOAL_BRANCH_EXEMPTION not in text:
        print(
            f"goal-row-clause FAILED: {CLAUSE_CARRIER} dropped the goal-branch exemption "
            f"({GOAL_BRANCH_EXEMPTION!r}) — a goal-branch sweep always has input 8 absent, "
            f"so the absent-input rule must name that branch as exempt or it deletes the "
            f"root row of every goal-branch frame",
            file=sys.stderr,
        )
        return 1

    offenders: list[str] = []
    for path in markdown_files():
        body = path.read_text(encoding="utf-8")
        for phrase in RETRACTED:
            if phrase in body:
                offenders.append(f"{path.relative_to(REPO)} carries the retracted reading {phrase!r}")
    if offenders:
        for line in offenders:
            print(f"goal-row-clause FAILED: {line}", file=sys.stderr)
        return 1

    print(
        f"goal-row-clause ok: the clause is carried {hits}x in {CLAUSE_CARRIER}, the goal-branch "
        f"exemption is stated, and none of the {len(RETRACTED)} retracted readings appears across "
        f"{len(markdown_files())} file(s) in {', '.join(SCAN_DIRS)}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
