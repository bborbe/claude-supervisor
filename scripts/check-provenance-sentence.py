#!/usr/bin/env python3
"""The drive-leg provenance paragraph must agree in both commands that carry it, and
neither may name the sweep-gate snapshot as the leg's `recorded_at` source.

`commands/manager-loop.md` step 4 and `commands/manager-drive.md` step 4 each carry the
same paragraph telling a caller which file the drive leg's `recorded_at` comes from. One
contract, carried twice because each command is read on its own — the liability
`docs/subject-resolution.md` § Recording already records for the recording step, and the
same shape `check-bucket-clause.py` and `check-recording-step.py` guard for their own
clauses.

⚠️ **This paragraph drifted, and the drift was found by eye.** The fix that moved the
provenance from the snapshot to the store touched **four** dispatch sites while the row
that commissioned it named **three** — `commands/manager-drive.md` step 4 carried the
identical sentence and would have been left naming the snapshot while the loop named the
store. A rename reaching one carrier and not the other is indistinguishable from a
correct one at the point of use: a manager reading only the loop, or only the drive
command, gets the other's answer. That is the defect this guard exists to make
unrepeatable.

Two assertions, both required:

  1. **The paragraph agrees.** Anchored on its opening words, never a line number, it is
     present exactly once in each carrier and byte-identical between them — indentation
     stripped, because the two files nest at different depths and the paragraph is
     carried at each one's own.
  2. **Neither carrier names the snapshot as the leg's provenance.** This is the half (1)
     cannot cover: both carriers could agree on the *wrong* sentence, and a lockstep edit
     is exactly what a rename produces.

**What this proves, and what it does not.** It proves the two carriers cannot silently
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
# span is bounded rather than line-wide because the two carriers legitimately differ
# after it ("this sweep's own" vs "this run's own classification") — a divergence that
# pre-dates the guard and is outside the shared contract.
START = "**Both halves come from the store, which is their single producer**"
END = "(single home: `docs/fleet-surface.md` § Session end)."

# The two ways a carrier states the old producer. Narrow on purpose — see the docstring.
OLD_PRODUCER = re.compile(
    r"`recorded_at` from the snapshot|snapshot provenance:"
)

COMMANDS = (
    "commands/manager-loop.md",
    "commands/manager-drive.md",
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
    for rel in COMMANDS:
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
                f"paragraph is carried twice and must be byte-identical in both",
                file=sys.stderr,
            )
            return 1

    for rel in COMMANDS:
        for lineno, line in enumerate((REPO / rel).read_text(encoding="utf-8").splitlines(), 1):
            if OLD_PRODUCER.search(line):
                print(
                    f"provenance-sentence FAILED: {rel}:{lineno} still names the snapshot "
                    f"as the drive leg's provenance — the leg's `recorded_at` comes from "
                    f"the manager-predispatch store",
                    file=sys.stderr,
                )
                return 1

    print(
        f"provenance-sentence ok: {len(COMMANDS)} command(s) carry the paragraph, "
        f"byte-identical; neither names the snapshot as the leg's provenance"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
