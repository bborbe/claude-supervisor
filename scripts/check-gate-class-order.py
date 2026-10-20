#!/usr/bin/env python3
"""The gate class test must precede the post instruction in `steps/manager-gates.md`.

`commands/steps/goal-gates.md` carries the waiting-on-human branch. A manager reading it
must classify the gate **before** it acts on it, because the class picks the channel:
a class-D/E gate cannot be released by a board button, a selection modal cannot be
deferred, and a class-C gate that cannot be carded at all — today a closer-line
`pick` — has nothing to defer to. Those hand over a jump link instead of a card.

Measured 2026-10-11: with the post instruction first and its own exception later in
the same bullet, a manager reading top-down **carded a closer-line `pick`** and the
operator was asked twice. The fix (commit 54cbcf1, released v0.128.4) was purely an
ordering change: lead with the class test.

This row moved that bullet into `commands/steps/goal-gates.md` verbatim. **Nothing else
asserts the order** — before this guard, any later edit could undo it with every
other check green, which is exactly how it broke the first time. So this is a
relocation guard, not a semantics guard: it proves the two phrases are in the right
order, never that the branch behaves correctly.

Anchored on the two phrases, never on a line number. The bullet is a single very
long line and the row that owns it reflowed it twice in one day, so a line-anchored
read would report drift that was really an offset.
"""

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
TARGET = "commands/steps/goal-gates.md"
CLASS_TEST = "classify the gate before you act on it"
POST_CALL = "attention-ask.py post"


def main() -> int:
    path = REPO / TARGET
    if not path.exists():
        print(f"gate-class-order FAILED: {TARGET} does not exist", file=sys.stderr)
        return 1

    text = path.read_text(encoding="utf-8")
    class_at = text.find(CLASS_TEST)
    post_at = text.find(POST_CALL)

    if class_at < 0:
        print(
            f"gate-class-order FAILED: {TARGET} does not carry the class test "
            f"({CLASS_TEST!r}). Without it the post instruction below has no "
            f"preceding classification and a top-down reader cards an un-cardable gate.",
            file=sys.stderr,
        )
        return 1

    if post_at < 0:
        # Not a failure. A later edit may legitimately drop the post call from this
        # branch, and then there is nothing to order against. Say so plainly rather
        # than passing silently — a silent pass is indistinguishable from a check
        # that never ran.
        print(
            f"gate-class-order ok: class test at char {class_at}; no post call in "
            f"{TARGET} to order against"
        )
        return 0

    if class_at > post_at:
        print(
            f"gate-class-order FAILED: in {TARGET} the post call sits at char {post_at} "
            f"and the class test at char {class_at} — the class test must come FIRST. "
            f"With the post first, a manager reading top-down cards a gate that cannot "
            f"be carded; that defect was measured 2026-10-11 and cost the operator a "
            f"double ask.",
            file=sys.stderr,
        )
        return 1

    print(
        f"gate-class-order ok: class test at char {class_at} precedes the post call "
        f"at char {post_at}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
