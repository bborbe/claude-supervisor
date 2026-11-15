#!/usr/bin/env python3
"""A resume branch must select its path, never assume the one the reader expects.

Two call sites, one rule. `docs/fleet-surface.md` § Spawn a worker owns the liveness × mode
selector and the `role` → `mode` mapping's consequences for a resume; `docs/role-mode-matrix.md`
owns the mapping itself. Each file below must consume them rather than restate or override them.

**`commands/open.md`.** Its resume branch used to route **every** resumed row to the wezterm
path. Under a fleet default of `spawn.mode: headless` that opened a tab for every headless row —
26 of them in one `--flagged` batch on 2026-11-06 — which re-creates the direct worker→operator
question gate the headless path exists to delete.

**`agents/fleet-keepalive-actor.md`.** Its restart branch had the mirror-image defect: it chose
the path by `mode:` alone, so a topic or goal page carrying **no** `mode:` took the headless
resume and the manager came back with no tab, no pane and no jump link — invisible to the
operator while the Vault UI still rendered its card as Live. Measured 2026-11-14 on
`23 Topics/Development Pipeline Throughput.md` (session `6f14c290`): a tab for four spawns, then
a headless process with no `kind=spawn` record. The open path resolves `role` → `mode` at Step
0.6; the leg had no equivalent step, so `role:` was never consulted.

⚠️ **The leg's rule lives in the docs, not in the agent body.** The agent file is re-injected on
every dispatch and sits at the 30 KB body budget (`scripts/check-budget.py`), so the rule has its
single home in `docs/fleet-surface.md` and the agent carries only the pointer and the one
sentence a future editor would delete — *a `manager` row never takes path A*. Asserting the full
rule against the agent body would force a restatement of a rule that already has a home, which is
the defect the repo's single-home doctrine exists to prevent.

**What this check does not prove.** It is a prose-shape check over markdown, because a command or
agent file *is* prose — there is no function to call and no return value to thread. It cannot
prove a resumed row actually lands on the right path; that is the behavioural half, measured on a
real round. It proves neither site can silently go back to a role-blind path choice.

Anchored on phrases, never on line numbers — the paragraphs are long and reflow in review.
"""

import re
import sys
from pathlib import Path
from typing import Optional

REPO = Path(__file__).resolve().parent.parent
OPEN = "commands/open.md"
SURFACE = "docs/fleet-surface.md"
LEG = "agents/fleet-keepalive-actor.md"

#: The sentence that made the resume branch unconditional. Its presence IS the defect.
BANNED = (
    "always uses this path, never",
    "always the resume branch",
)

#: Anchor on the selector paragraph this check guards.
SELECTOR = "The resume branch selects its path"

#: Every arm of the liveness × mode selector. A missing arm is a selector that silently
#: routes that case to whichever path the reader assumes — the defect this replaces.
ARMS = ("interactive", "headless", "ABSENT", "LIVE", "UNKNOWN", "AMBIGUOUS")

#: The rule's single home. The call site must point at it rather than restate the table.
HOME = "fleet-surface.md"

#: The headless arm's own obligation: a resume must never guess a working tree.
CWD = "cwd"

#: The anchor of the manager-resume block in `docs/fleet-surface.md` § Spawn a worker.
LEG_SELECTOR = "A manager row never resumes headless"

#: Every classification the block must name. `role: manager` and `role: agent` are the two
#: roles; an explicit `mode: headless` is the case that refuses rather than routes.
LEG_ARMS = ("`role: manager`", "`role: agent`", "`mode: headless`")

#: The refusal literal an explicit-headless manager page must produce. A selector that routes
#: this case to either path is the silent override the operator ruling rejects.
REFUSAL = "role=manager, mode=headless — refused"

#: Path A must be named as the one a manager never takes — in the docs block AND in the agent
#: body, where it is the one sentence a future editor deletes when they simplify the branch
#: back to `mode:` alone.
PATH_A_BAN = "never takes path A"

#: The pointer the agent body must keep, so a reader of that file can reach the rule at all.
MODE_ANCHOR = "Spawn a worker item 6"


def paragraph(text: str, anchor: str) -> Optional[str]:
    """The paragraph starting at `anchor`, or None. Bounded so an arm named elsewhere cannot
    satisfy the assertion."""
    at = text.find(anchor)
    if at < 0:
        return None
    end = text.find("\n\n", at)
    return text[at:] if end < 0 else text[at:end]


def block(text: str, anchor: str) -> Optional[str]:
    """The block starting at `anchor`, or None — the paragraph plus the list under it.

    `paragraph()` stops at the first blank line, and the manager-resume block is a paragraph
    followed by the arm list it introduces — bounding on the blank line would drop every arm
    into a region the assertion never reads. This list's items are flush left, so indentation
    is no bound at all: the block ends at the next numbered item in the enclosing list.
    """
    at = text.find(anchor)
    if at < 0:
        return None
    rest = text[at:]
    nxt = re.search(r"\n\d+\. ", rest)
    return rest[: nxt.start()] if nxt else rest


def check_open(path: Path) -> list:
    """Return a list of failure strings for `commands/open.md`. Empty means pass."""
    text = path.read_text(encoding="utf-8")
    failures = []

    for phrase in BANNED:
        if phrase in text:
            failures.append(
                f"{OPEN} still carries {phrase!r} — the resume branch is "
                "unconditional again; the liveness × mode selector is gone"
            )

    body = paragraph(text, SELECTOR)
    if body is None:
        failures.append(
            f"{OPEN} carries no {SELECTOR!r} paragraph — the resume branch states no "
            "path selector at all"
        )
        return failures

    if text.count(SELECTOR) != 1:
        failures.append(
            f"{OPEN} carries {SELECTOR!r} {text.count(SELECTOR)} times — the selector "
            "must have exactly one statement"
        )

    missing = [arm for arm in ARMS if arm not in body]
    if missing:
        failures.append(
            f"{OPEN}'s selector paragraph names no {', '.join(missing)} — every arm of "
            "the liveness × mode table must be present"
        )

    if HOME not in body:
        failures.append(
            f"{OPEN}'s selector paragraph does not point at {HOME} — the rule has a "
            "single home and the call site must cite it rather than restate it"
        )

    if CWD not in body:
        failures.append(
            f"{OPEN}'s selector paragraph does not require {CWD!r} on the headless arm "
            "— a resume without one must refuse rather than guess a tree"
        )

    return failures


def check_surface(path: Path) -> list:
    """Return a list of failure strings for the manager-resume block in `docs/fleet-surface.md`."""
    text = path.read_text(encoding="utf-8")
    failures = []

    body = block(text, LEG_SELECTOR)
    if body is None:
        failures.append(
            f"{SURFACE} carries no {LEG_SELECTOR!r} block — the resume rule states no "
            "role-first branch, so a manager page with no `mode:` resumes headless"
        )
        return failures

    missing = [arm for arm in LEG_ARMS if arm not in body]
    if missing:
        failures.append(
            f"{SURFACE}'s resume block names no {', '.join(missing)} — the branch must "
            "classify the subject's `role:` and the explicit-headless case"
        )

    if PATH_A_BAN not in body:
        failures.append(
            f"{SURFACE}'s resume block does not say a `manager` row {PATH_A_BAN!r} — "
            "without it a manager restart may fall through to the headless resume"
        )

    if REFUSAL not in body:
        failures.append(
            f"{SURFACE}'s resume block does not carry {REFUSAL!r} — an explicit "
            "`mode: headless` on a manager page must refuse, not route"
        )

    return failures


def check_leg(path: Path) -> list:
    """Return a list of failure strings for `agents/fleet-keepalive-actor.md`."""
    text = path.read_text(encoding="utf-8")
    failures = []

    if PATH_A_BAN not in text:
        failures.append(
            f"{LEG} does not say a `manager` row {PATH_A_BAN!r} — its path-choice sentence "
            "is back to `mode:` alone"
        )

    if MODE_ANCHOR not in text:
        failures.append(
            f"{LEG} carries no {MODE_ANCHOR!r} pointer — a reader of this file has no route "
            "to the resume rule"
        )

    return failures


def main() -> int:
    failures = []
    for rel, check in ((OPEN, check_open), (SURFACE, check_surface), (LEG, check_leg)):
        path = REPO / rel
        if not path.exists():
            failures.append(f"{rel} does not exist")
            continue
        failures.extend(check(path))

    if failures:
        print("open-resume-mode FAILED:", file=sys.stderr)
        for failure in failures:
            print(f"  - {failure}", file=sys.stderr)
        return 1

    print(
        "open-resume-mode ok: both resume branches select their path — open.md by mode × "
        "liveness, a manager resume by role first"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
