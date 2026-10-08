#!/usr/bin/env python3
"""The widened necessity contract must stay pinned in the two agents that carry it.

`agents/manager-sweep-reader.md` § step 8 defines the necessity verdicts and their row
shapes; `agents/manager-verify.md` § step 1 consumes them. The 2026-10-07 widening — each
verdict then named *which* of three sources a task serves (goal sentence / `SC<n>` / `DoD<n>`)
— moved the `needed:` row shape, and deliberately left the `product:` row and the summary
line byte-identical so that a before/after comparison of this read stays valid.

⚠️ **Narrowed to two sources on 2026-10-08:** the goal sentence was retired
(`SC<n>` / `DoD<n>` only), matching vault-cli. That edit changed the *text inside* the
`needed:` and `not needed:` rows but not the **prefixes this guard matches on**, so every
count below is unchanged and the guard passed without modification. Read that as the guard's
declared scope, not as coverage: it pins that a site was not dropped, duplicated or moved, and
says nothing about the wording within a site.

That "deliberately unchanged" half is a claim, and nothing asserted it. This guard pins both
halves: every row shape the widening introduces must appear at **exactly** its known sites,
and the `product:` row plus the summary line must still read exactly as they did before.

**Anchored on stripped whole lines, compared for equality — never a substring, never a floor,
never a line number.** Each of the three alternatives was tried here and each is wrong:

- A **line number** moves whenever the section is edited; it moved several times inside the
  widening itself.
- A **substring count** over a string that also appears in prose tolerates the deletion of a
  real site: `needed: <task> — serves …` occurs in three templates *and* once inside a
  sentence at step 8's prose, so any threshold is either satisfiable by the prose or blind to
  one deletion.
- A **floor** (`found < want`) with prefix matching is blind twice over: duplicating a site
  keeps the count at its threshold while a real one is deleted, and a row that drifts past the
  pinned prefix still counts as a site.

Equality on the stripped line closes all three, and is what the sibling `check-bucket-clause.py`
does. The cost is that adding a legitimate fourth site fails here until the count is updated —
which is the point: a shape that moves should be looked at, not absorbed.

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

SERVES = "needed: <task> — serves"
FOUNDATION = "needed: <task> — foundation for"
NOT_NEEDED = "not needed: <task> — serves none of"
UNPROVEN = "not needed: <task> — unproven:"

#: Each row shape, the number of stripped lines that must BEGIN with it, and a label. The
#: three `serves` sites are the three-verdict block, then the tick-mode and snapshot-mode
#: frames; the others appear in the block alone. Counts are exact — see the module docstring.
ROW_SHAPES: tuple[tuple[str, int, str], ...] = (
    (SERVES, 3, "the two-source `needed:` row"),
    (FOUNDATION, 1, "the foundation row"),
    (NOT_NEEDED, 1, "the plain `not needed` row"),
    (UNPROVEN, 1, "the unproven `not needed` row"),
)

#: The source this change retires. Every check above matches by *prefix*, so none of them
#: can see it: the placeholder sits **inside** a row, and a uniform re-widening at all three
#: sites leaves every count and the one-shape assertion satisfied. This is the only
#: assertion here that reads wording rather than shape, and it exists because the PR's own
#: docstring concedes the prefix scope — a conceded gap is worth one line to close.
RETIRED = "<goal sentence|SC<n>|DoD<n>>"

# Deliberately UNCHANGED by the widening — the byte-identity claim, pinned as whole lines.
PRODUCT_ROW = "product: <task> — output of <goal> SC<n>"
PRODUCT_SITES = 1
SUMMARY_LINE = (
    "Necessity: <M> needed · <K> not needed · <P> product — inverted set <M+K+P> of "
    "<N> tracked — over <topic page> (<member goals>)"
)
SUMMARY_SITES = 3  # the three-verdict block, then the tick-mode and snapshot-mode frames

# The consumer's own rule: a serving item per task, never a bare count. Carried inside
# sentences rather than beginning a line, so it is matched by containment with an exact count.
VERIFY_RULE = "at least one serving row when any task serves"
VERIFY_SITES = 2


def read(rel: str) -> str:
    path = REPO / rel
    if not path.exists():
        print(f"necessity-templates FAILED: {rel} does not exist", file=sys.stderr)
        raise SystemExit(1)
    return path.read_text(encoding="utf-8")


def lines(text: str) -> list[str]:
    return [line.strip() for line in text.splitlines()]


def beginning(rows: list[str], shape: str) -> list[str]:
    return [row for row in rows if row.startswith(shape)]


def exact(rows: list[str], want: str) -> list[str]:
    return [row for row in rows if row == want]


def main() -> int:
    reader_rows = lines(read(READER))
    verify_rows = lines(read(VERIFY))

    for shape, want, label in ROW_SHAPES:
        found = beginning(reader_rows, shape)
        if len(found) != want:
            print(
                f"necessity-templates FAILED: {READER} begins {len(found)} line(s) with "
                f"{label} ({shape!r}), want exactly {want} — a site was dropped, duplicated "
                f"or moved",
                file=sys.stderr,
            )
            return 1
        if len(set(found)) != 1:
            print(
                f"necessity-templates FAILED: {READER} carries {label} in more than one shape "
                f"— the sites are one contract and must read identically: "
                f"{sorted(set(found))!r}",
                file=sys.stderr,
            )
            return 1

    if RETIRED in "\n".join(reader_rows):
        print(
            f"necessity-templates FAILED: {READER} still carries the retired goal-sentence "
            f"source ({RETIRED!r}) — the 2026-10-08 narrowing removed it, and a prefix match "
            f"cannot see it come back",
            file=sys.stderr,
        )
        return 1

    for label, want_line, want in (
        ("`product:` row", PRODUCT_ROW, PRODUCT_SITES),
        ("summary line", SUMMARY_LINE, SUMMARY_SITES),
    ):
        found = exact(reader_rows, want_line)
        if len(found) != want:
            print(
                f"necessity-templates FAILED: {READER} carries the pinned {label} on "
                f"{len(found)} line(s), want exactly {want} — it is deliberately unchanged by "
                f"the widening, and the before/after comparison depends on it",
                file=sys.stderr,
            )
            return 1

    rule = [row for row in verify_rows if VERIFY_RULE in row]
    if len(rule) != VERIFY_SITES:
        print(
            f"necessity-templates FAILED: {VERIFY} carries the serving-row rule on "
            f"{len(rule)} line(s), want exactly {VERIFY_SITES} — without it a count passes as "
            f"a finding",
            file=sys.stderr,
        )
        return 1

    print(
        f"necessity-templates ok: {len(ROW_SHAPES)} row shape(s) pinned at their exact sites; "
        f"`product:` row and summary line unchanged; manager-verify requires a serving row"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
