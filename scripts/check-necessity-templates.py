#!/usr/bin/env python3
"""The widened necessity contract must stay pinned in the two agents that carry it.

`agents/manager-sweep-reader.md` § step 8 defines the three necessity verdicts and their row
shapes; `agents/manager-verify.md` § step 1 consumes them. The 2026-10-07 widening — each
verdict now names *which* of three sources a task serves (goal sentence / `SC<n>` / `DoD<n>`)
— moved the `needed:` row shape, and deliberately left the `product:` row and the summary
line byte-identical so that a before/after comparison of this read stays valid.

That "deliberately unchanged" half is a claim, and nothing asserted it. This guard pins both
halves: the widened `needed:` shape must appear at every site that carries it, and the
`product:` row plus the summary line must still read exactly as they did before the widening.
Without it the two claims ship unverified, which is what a bot review observed on
bborbe/claude-supervisor#208.

Anchored on the template strings themselves, never on line numbers — the section moved by
several lines inside the widening itself, and a line-anchored read reports drift that is
really an offset.

Same shape as `check-bucket-clause.py` and `check-recording-step.py`, for the same reason.

**What this proves, and what it does not.** It proves the row shapes cannot silently drift
and that the two "unchanged" claims are still true of the file. It cannot prove the reader
*applies* them to a real goal; that is the e2e run's job. The two are deliberately separate:
this is the drift guard, not the semantics.
"""

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

READER = "agents/manager-sweep-reader.md"
VERIFY = "agents/manager-verify.md"

# Widened 2026-10-07: every `needed:` row site must carry the three-source shape.
NEEDED_ROW = 'needed: <task> — serves <goal sentence|SC<n>|DoD<n>>: "<served line>" ← "<task line>"'
NEEDED_MIN = 3  # the three-verdict block, then the tick-mode and snapshot-mode frames

# Deliberately UNCHANGED by the widening — the byte-identity claim, pinned.
PRODUCT_ROW = "product: <task> — output of <goal> SC<n>"
SUMMARY_LINE = (
    "Necessity: <M> needed · <K> not needed · <P> product — inverted set <M+K+P> of "
    "<N> tracked — over <topic page> (<member goals>)"
)

# The consumer's own rule: a serving item per task, never a bare count.
VERIFY_RULE = "at least one serving row when any task serves"


def read(rel: str) -> str:
    path = REPO / rel
    if not path.exists():
        print(f"necessity-templates FAILED: {rel} does not exist", file=sys.stderr)
        raise SystemExit(1)
    return path.read_text(encoding="utf-8")


def main() -> int:
    reader = read(READER)
    verify = read(VERIFY)

    sites = reader.count(NEEDED_ROW)
    if sites < NEEDED_MIN:
        print(
            f"necessity-templates FAILED: {READER} carries the widened `needed:` row "
            f"{sites} time(s), want >= {NEEDED_MIN} — a row site was missed or the shape "
            f"drifted",
            file=sys.stderr,
        )
        return 1

    if PRODUCT_ROW not in reader:
        print(
            f"necessity-templates FAILED: {READER} no longer carries the pinned `product:` "
            f"row {PRODUCT_ROW!r} — it is deliberately unchanged by the widening, and the "
            f"before/after comparison depends on it",
            file=sys.stderr,
        )
        return 1

    if SUMMARY_LINE not in reader:
        print(
            f"necessity-templates FAILED: {READER} no longer carries the pinned summary line "
            f"{SUMMARY_LINE!r} — the three-token shape is the comparison surface",
            file=sys.stderr,
        )
        return 1

    if VERIFY_RULE not in verify:
        print(
            f"necessity-templates FAILED: {VERIFY} no longer requires a serving row "
            f"({VERIFY_RULE!r}) — without it a count passes as a finding",
            file=sys.stderr,
        )
        return 1

    print(
        f"necessity-templates ok: `needed:` row at {sites} site(s); `product:` row and "
        f"summary line pinned unchanged; manager-verify requires a serving row"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
