#!/usr/bin/env python3
"""The widened necessity contract must stay pinned in the two agents that carry it.

`agents/manager-sweep-reader.md` § step 8 defines the three necessity verdicts and their row
shapes; `agents/manager-verify.md` § step 1 consumes them. The 2026-10-07 widening — each
verdict now names *which* of three sources a task serves (goal sentence / `SC<n>` / `DoD<n>`)
— moved the `needed:` row shape, and deliberately left the `product:` row and the summary
line byte-identical so that a before/after comparison of this read stays valid.

That "deliberately unchanged" half is a claim, and nothing asserted it. This guard pins both
halves: every row shape the widening introduces must appear at every site that carries it,
and the `product:` row plus the summary line must still read exactly as they did before.

Anchored on **line shape, never a substring count and never a line number.** Both alternatives
were tried and both are wrong here. A line number moves whenever the section is edited — it
moved several times inside the widening itself. A substring count over a string that also
appears in prose tolerates the deletion of a real site: `needed: <task> — serves …` occurs in
three templates *and* once inside a sentence at step 8's prose, so any threshold is either
satisfiable by the prose or blind to one deletion. Counting only lines that *begin* with the
shape excludes the prose and asserts each site.

Same shape as `check-bucket-clause.py` and `check-recording-step.py`.

**What this proves, and what it does not.** It proves the row shapes cannot silently drift or
be dropped, and that the two "unchanged" claims are still true of the file. It cannot prove
the reader *applies* them to a real goal; that is the e2e run's job. The two are deliberately
separate: this is the drift guard, not the semantics.
"""

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

READER = "agents/manager-sweep-reader.md"
VERIFY = "agents/manager-verify.md"

# Each is a row shape the widening introduces or preserves, with the number of template sites
# that must begin a line with it. The three `serves` sites are the three-verdict block, then
# the tick-mode and snapshot-mode frames; the others appear in the block alone.
ROW_SHAPES: tuple[tuple[str, int, str], ...] = (
    ("needed: <task> — serves", 3, "the widened three-source `needed:` row"),
    ("needed: <task> — foundation for", 1, "the foundation row"),
    ("not needed: <task> — serves none of", 1, "the plain `not needed` row"),
    ("not needed: <task> — unproven:", 1, "the unproven `not needed` row"),
)

# Deliberately UNCHANGED by the widening — the byte-identity claim, pinned exactly.
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


def sites(text: str, shape: str) -> int:
    """Lines that BEGIN with the shape. Excludes the prose mention of the same string."""
    return sum(1 for line in text.splitlines() if line.strip().startswith(shape))


def main() -> int:
    reader = read(READER)
    verify = read(VERIFY)

    for shape, want, label in ROW_SHAPES:
        found = sites(reader, shape)
        if found < want:
            print(
                f"necessity-templates FAILED: {READER} begins {found} line(s) with {label} "
                f"({shape!r}), want >= {want} — a row site was dropped or the shape drifted",
                file=sys.stderr,
            )
            return 1

    for label, needle in (("`product:` row", PRODUCT_ROW), ("summary line", SUMMARY_LINE)):
        if needle not in reader:
            print(
                f"necessity-templates FAILED: {READER} no longer carries the pinned {label} "
                f"{needle!r} — it is deliberately unchanged by the widening, and the "
                f"before/after comparison depends on it",
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
        f"necessity-templates ok: {len(ROW_SHAPES)} row shape(s) pinned per site; `product:` "
        f"row and summary line unchanged; manager-verify requires a serving row"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
