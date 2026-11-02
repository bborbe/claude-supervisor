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

    if readiness_at < 0:
        print(
            f"open-approve-ordering FAILED: {TARGET} does not carry the readiness "
            f"dispatch paragraph ({READINESS!r}). Without it the approve pass has "
            f"nothing to gate on and every selected row is admitted.",
            file=sys.stderr,
        )
        return 1

    if approve_at < 0:
        print(
            f"open-approve-ordering FAILED: {TARGET} does not carry the approve pass "
            f"paragraph ({APPROVE!r}). A silent drop of the ordering is exactly the "
            f"regression this guard exists to catch, so a missing paragraph fails "
            f"rather than passing.",
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

    scope_at = text.find(SCOPE)
    if scope_at < 0 or scope_at < approve_at:
        print(
            f"open-approve-ordering FAILED: the reorder is not scoped to the batch. "
            f"{TARGET} must carry {SCOPE!r} in the approve pass paragraph — on the "
            f"single-task path the operator's approval write must precede the command, "
            f"so an unscoped reorder refuses every single-row open.",
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
