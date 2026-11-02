#!/usr/bin/env python3
"""The readiness dispatch must precede the approve pass in `commands/open.md` Step 0.6.

An approval is an **admission**: `approved_at` is the field `scripts/approved-not-started.py`
counts, so every row the batch approves and then holds enters the approved-not-started
queue — a queue that exists to be drained, and that nothing on this side removes. Running
the readiness dispatch first, and approving only the rows it will actually open, is what
stops the batch refilling that queue. Swap the two paragraphs back and the refill returns
with every other check green, because prose order is the whole mechanism.

Measured 2026-11-02: the batch approved every selected row before readiness ran, so each
run holding K rows added K to the queue. Task "Close or Gate the Intake That Refills the
Approved-Not-Started Queue" (private-personal) carries the measurements.

The behavioural probe lives in that task's SC3 and needs a manager session to run; it
therefore covers a release, not a later edit. This guard is the static half: it proves
the two paragraphs are in the right order and that the reorder stays scoped to the batch,
never that the batch behaves correctly.

Anchored on the two phrases, never on a line number — both paragraphs are long and have
been reflowed in review, so a line-anchored read reports drift that is really an offset.
"""

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
TARGET = "commands/open.md"
READINESS = "Readiness-gate every CREATE-bound row first, concurrently"
APPROVE = "Approve only the rows the dispatch will open"
SCOPE = "`--flagged` batch only."


def main() -> int:
    path = REPO / TARGET
    if not path.exists():
        print(f"open-approve-ordering FAILED: {TARGET} does not exist", file=sys.stderr)
        return 1

    text = path.read_text(encoding="utf-8")
    readiness_at = text.find(READINESS)
    approve_at = text.find(APPROVE)

    # Each anchor must occur exactly once. `find` returns the FIRST occurrence, so a
    # duplicate earlier in the file would let a genuine reversal hide behind it: the
    # guard would compare two positions that are not the paragraphs it means.
    for name, phrase in (("readiness", READINESS), ("approve", APPROVE)):
        count = text.count(phrase)
        if count != 1:
            print(
                f"open-approve-ordering FAILED: the {name} anchor occurs {count} time(s) "
                f"in {TARGET}, expected exactly 1 ({phrase!r}). With a duplicate, the "
                f"first-match comparison can pass while the real paragraphs are reversed.",
                file=sys.stderr,
            )
            return 1

    # Both paragraphs must sit INSIDE Step 0.6. Ordering them correctly but placing one in
    # a neighbouring step would leave this guard green while the mechanism breaks: Step 0.5
    # runs before the selector is final and Step 1.5 runs after the dispatch, so neither
    # position reproduces approve-after-readiness.
    step_start = text.find("## Step 0.6")
    if step_start < 0:
        print(
            f"open-approve-ordering FAILED: {TARGET} has no '## Step 0.6' heading, so the "
            f"ordering has no section to be bounded to. A renamed heading must fail loudly "
            f"rather than scan an unbounded file.",
            file=sys.stderr,
        )
        return 1
    step_end = text.find("\n## ", step_start + 1)
    if step_end < 0:
        step_end = len(text)

    for name, pos in (("readiness", readiness_at), ("approve", approve_at)):
        if not (step_start < pos < step_end):
            print(
                f"open-approve-ordering FAILED: the {name} paragraph sits at char {pos}, "
                f"outside Step 0.6 ({step_start}..{step_end}). Correct order anywhere else "
                f"in the file proves nothing — only inside Step 0.6 does the dispatch run "
                f"before the approve pass on the same batch.",
                file=sys.stderr,
            )
            return 1

    if readiness_at > approve_at:
        print(
            f"open-approve-ordering FAILED: in {TARGET} the approve pass sits at char "
            f"{approve_at} and the readiness dispatch at char {readiness_at} — the "
            f"dispatch must come FIRST. Approving before readiness admits every row "
            f"the dispatch then holds, refilling the approved-not-started queue.",
            file=sys.stderr,
        )
        return 1

    # The scope marker must sit INSIDE the approve paragraph, not merely somewhere at
    # or after it. Moving it into a later paragraph would otherwise leave the guard
    # green while the approve paragraph itself reads as applying to the single-task
    # path — the exact misreading this check exists to prevent.
    para_end = text.find("\n\n", approve_at)
    if para_end < 0:
        para_end = len(text)
    scope_at = text.find(SCOPE, approve_at)
    if scope_at < 0 or scope_at >= para_end:
        print(
            f"open-approve-ordering FAILED: the reorder is not scoped inside the "
            f"approve paragraph. {TARGET} must carry {SCOPE!r} between the approve "
            f"heading (char {approve_at}) and the end of that paragraph "
            f"(char {para_end}) — on the single-task path the operator's approval "
            f"write must precede the command, so an unscoped reorder refuses every "
            f"single-row open.",
            file=sys.stderr,
        )
        return 1

    print(
        f"open-approve-ordering ok: readiness dispatch at char {readiness_at} precedes "
        f"the approve pass at char {approve_at}, scoped to the batch at char {scope_at}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
