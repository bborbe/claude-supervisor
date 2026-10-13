#!/usr/bin/env python3
"""The drive-leg provenance paragraph must stay stated, and no carrier may name the
sweep-gate snapshot as the leg's `recorded_at` source.

`commands/steps/manager-drive.md` carries the paragraph telling a caller which file the
drive leg's `recorded_at` comes from. It used to be carried **twice** — the loop's step 4
and `commands/manager-drive.md` step 4 each held the identical span, because each command
is read on its own, the liability `docs/subject-resolution.md` § Recording already records
for the recording step, and the same shape `check-bucket-clause.py` and
`check-recording-step.py` guard for their own clauses.

⚠️ **On 2026-10-11 the second carrier was retired, and the byte-identity half went with
it.** Both commands now reach the paragraph through the same step file: `manager-loop.md`
became the arm and `commands/manager-drive.md` became the loop's per-round router, so the
drive-leg dispatch — and this paragraph inside it — has exactly **one** verbatim carrier.
The comparison loop below is therefore a no-op today; it is kept so a second carrier,
wherever it appears, must match rather than silently diverge. The **negative** assertion
is the half that is live, and it is the half that caught the original defect.

⚠️ **This paragraph drifted, and the drift was found by eye.** The fix that moved the
provenance from the snapshot to the store touched **four** dispatch sites while the row
that commissioned it named **three** — `commands/manager-drive.md` step 4 carried the
identical sentence and would have been left naming the snapshot while the loop named the
store. A rename reaching one carrier and not the other is indistinguishable from a
correct one at the point of use: a manager reading only the loop, or only the drive
command, gets the other's answer. That is the defect this guard exists to make
unrepeatable.

Two assertions, both required:

  1. **The paragraph is stated once per carrier.** Anchored on its opening words, never a
     line number, it is present exactly once in the carrier and — where a second carrier
     exists — byte-identical between them, indentation stripped, because the carriers nest
     at different depths.
  2. **No carrier names the snapshot as the leg's provenance.** This is the half (1)
     cannot cover: every carrier could agree on the *wrong* sentence, and a lockstep edit
     is exactly what a rename produces. It runs over **every** file that states the
     contract — the paragraph carrier plus `agents/manager-drive.md`,
     `commands/manager-status.md` and `docs/fleet-surface.md` — because the contract is
     carried at several dispatch sites and the drift this guard exists to stop was found by
     eye at one of them.

**What this proves, and what it does not.** It proves the carrier(s) cannot silently
diverge and cannot jointly assert the old producer. It cannot prove the paragraph is
*true* of the code that writes the store; that is `manager-predispatch.py`'s own tests'
job. This is the drift guard, not the semantics.

The negative pattern is deliberately narrow — the two phrasings that *state* the old
producer, not every mention of the snapshot. `commands/manager-loop.md` legitimately
names the snapshot's `recorded_at` in the `--compare-tracked` reading line, and a broader
pattern would fail a correct file.
"""

import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

# The paragraph's own opening words, and the end of the span this guard compares. The
# span is bounded rather than line-wide because each carrier continues into its own
# prose after it — the loop's sentence runs on into the dispatch's remaining inputs,
# which the drive command states differently. Bounding it keeps the comparison on the
# shared contract rather than on each command's own text.
START = "**Both halves come from the store, which is their single producer**"
END = "(single home: `docs/fleet-surface.md` § Session end)."

# The two ways a carrier states the old producer. Narrow on purpose — see the docstring.
OLD_PRODUCER = re.compile(
    r"`recorded_at` from the snapshot|snapshot provenance:"
)

# The carriers that hold the paragraph *verbatim*, which is what makes a byte comparison
# meaningful. ⚠️ **One entry today**, because both commands reach the drive-leg dispatch
# through the same step file since 2026-10-11; a second verbatim carrier added anywhere
# must be listed here or it is the failure this list exists to make visible.
# `agents/manager-drive.md` states the same contract in its own wording at five sites
# (`<constraints>`, clause (0), the failure-mode bullet, the report template, the closing
# summary), so there is no shared span to compare there; `commands/manager-status.md` and
# `docs/fleet-surface.md` likewise carry it in their own words. All three are covered by
# the negative assertion below, which is the half that catches a rename.
PARAGRAPH_CARRIERS = (
    "commands/steps/manager-drive.md",
)

# Every file that states this contract; the negative assertion runs over all of them.
ALL_CARRIERS = PARAGRAPH_CARRIERS + (
    "agents/manager-drive.md",
    "commands/manager-status.md",
    "docs/fleet-surface.md",
)


def paragraph(path: Path) -> tuple[str | None, str | None]:
    """-> (the shared span, error). The one span carrying it, or why it is not single."""
    if not path.exists():
        return None, f"{path.relative_to(REPO)} does not exist"
    text = path.read_text(encoding="utf-8")
    starts = text.count(START)
    if starts != 1:
        return None, f"{path.relative_to(REPO)} carries the paragraph {starts} times, want 1"
    begin = text.index(START)
    end = text.find(END, begin)
    if end == -1:
        return None, (
            f"{path.relative_to(REPO)} carries the paragraph's opening but not its "
            f"closing {END!r}"
        )
    return " ".join(text[begin : end + len(END)].split()), None


def main() -> int:
    found: dict[str, str] = {}
    for rel in PARAGRAPH_CARRIERS:
        span, err = paragraph(REPO / rel)
        if err or span is None:
            print(f"provenance-sentence FAILED: {err or 'paragraph() returned no span'}", file=sys.stderr)
            return 1
        found[rel] = span

    (ref_name, ref_span), *rest = found.items()
    for name, span in rest:
        if span != ref_span:
            print(
                f"provenance-sentence FAILED: {name} and {ref_name} disagree — the "
                f"paragraph is carried at more than one site and must be byte-identical in all of them",
                file=sys.stderr,
            )
            return 1

    for rel in ALL_CARRIERS:
        path = REPO / rel
        if not path.exists():
            print(f"provenance-sentence FAILED: {rel} does not exist", file=sys.stderr)
            return 1
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if OLD_PRODUCER.search(line):
                print(
                    f"provenance-sentence FAILED: {rel}:{lineno} still names the snapshot "
                    f"as the drive leg's provenance — the leg's `recorded_at` comes from "
                    f"the manager-predispatch store",
                    file=sys.stderr,
                )
                return 1

    print(
        f"provenance-sentence ok: {len(PARAGRAPH_CARRIERS)} verbatim carrier(s) of the "
        f"paragraph; {len(ALL_CARRIERS)} carrier(s) checked for the old provenance"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
