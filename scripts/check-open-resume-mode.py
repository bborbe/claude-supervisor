#!/usr/bin/env python3
"""The resume branch in `commands/open.md` must select its path, never assume the tab one.

`/supervisor:open`'s resume branch used to route **every** resumed row to the wezterm
path. Under a fleet default of `spawn.mode: headless` that opened a tab for every headless
row — 26 of them in one `--flagged` batch on 2026-11-06 — which re-creates the direct
worker→operator question gate the headless path exists to delete.

The rule is not new and this check does not restate it: the liveness × mode selector has
its single home in `docs/fleet-surface.md` § Spawn a worker, whose resume-path table names
all four arms. This check asserts the **call site** consumes that selector instead of
overriding it, and that it keeps the one obligation the headless arm adds — an explicit
`cwd`.

**What this check does not prove.** It is a prose-shape check over markdown, because a
command file *is* prose — there is no function to call and no return value to thread. It
cannot prove a resumed row actually lands headless; that is the behavioural half, measured
on a real `--flagged` batch (SC4 of the task that added this guard). It proves the site
cannot silently go back to the unconditional tab resume.

Anchored on phrases, never on line numbers — the paragraph is long and reflows in review.
"""

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
TARGET = "commands/open.md"

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


def check(path: Path) -> list[str]:
    """Return a list of failure strings. Empty means pass."""
    text = path.read_text(encoding="utf-8")
    failures = []

    for phrase in BANNED:
        if phrase in text:
            failures.append(
                f"{TARGET} still carries {phrase!r} — the resume branch is "
                "unconditional again; the liveness × mode selector is gone"
            )

    at = text.find(SELECTOR)
    if at < 0:
        failures.append(
            f"{TARGET} carries no {SELECTOR!r} paragraph — the resume branch states no "
            "path selector at all"
        )
        return failures

    if text.count(SELECTOR) != 1:
        failures.append(
            f"{TARGET} carries {SELECTOR!r} {text.count(SELECTOR)} times — the selector "
            "must have exactly one statement"
        )

    # Bound the read to this paragraph: `find` returns the first occurrence, so an arm
    # named anywhere else in the file would otherwise satisfy the assertion.
    end = text.find("\n\n", at)
    paragraph = text[at:] if end < 0 else text[at:end]

    missing = [arm for arm in ARMS if arm not in paragraph]
    if missing:
        failures.append(
            f"{TARGET}'s selector paragraph names no {', '.join(missing)} — every arm of "
            "the liveness × mode table must be present"
        )

    if HOME not in paragraph:
        failures.append(
            f"{TARGET}'s selector paragraph does not point at {HOME} — the rule has a "
            "single home and the call site must cite it rather than restate it"
        )

    if CWD not in paragraph:
        failures.append(
            f"{TARGET}'s selector paragraph does not require {CWD!r} on the headless arm "
            "— a resume without one must refuse rather than guess a tree"
        )

    return failures


def main() -> int:
    path = REPO / TARGET
    if not path.exists():
        print(f"open-resume-mode FAILED: {TARGET} does not exist", file=sys.stderr)
        return 1

    failures = check(path)
    if failures:
        print("open-resume-mode FAILED:", file=sys.stderr)
        for failure in failures:
            print(f"  - {failure}", file=sys.stderr)
        return 1

    print("open-resume-mode ok: the resume branch selects its path by mode and liveness")
    return 0


if __name__ == "__main__":
    sys.exit(main())
