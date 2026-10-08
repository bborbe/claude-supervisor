#!/usr/bin/env python3
"""The in-flight drop line must keep naming the limb that answered.

Limb 2 of the drive leg's in-flight check reads a marker written by a hook outside this
repo (`~/.claude/hooks/attention-log.py`, operator-local). When that writer was removed
on 2026-09-21 the limb answered nothing for seventeen days and **no output changed**:
the absent branch reads absence as "between turns", so every drop still read
`in flight: <a transcript age>` — indistinguishable from a working check. The only
repo-side detector for a recurrence is the rule that an `in flight:` drop names the
limb, which turns a dead limb into a visible absence of `limb 2` rows.

That detector is prose, and prose drifts. This guard is what fails if it does: it pins
the rule clause and the worked exemplar the rule governs, so the two cannot disagree and
the naming cannot be dropped without a red check. Measured on the review that asked for
it: the exemplar and its own rule had already diverged once (`4 min ago` against
`4 min`), which is exactly the drift a reader cannot see by eye.

Same shape as `check-bucket-clause.py` and `check-recording-step.py`.

**What this proves, and what it does not.** It proves the rule and its exemplar still
carry the limb-naming form *in this repo*. It cannot prove the hook writes the marker —
that file is not vendored here and would not be installed in CI, so a guard asserting its
existence would fail unconditionally on every checkout. The two halves are deliberately
separate: this is the in-repo drift guard, the live report is the runtime detector.

Anchored on the clause's own words, never on a line number: this file's neighbours cite
`agents/manager-drive.md` by line range, those ranges have already drifted once, and the
change that added this guard shifted one of them by a line — so a line-anchored read
would report an offset as a defect.
"""

import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DRIVE = REPO / "agents/manager-drive.md"

# The rule's own opening words. Present exactly once, in the drop-line bullet.
RULE_ANCHOR = "An `in flight:` drop names the LIMB that answered"
# The worked exemplar's distinguishing prefix. Present exactly once, in the report block.
#
# ⚠️ **The anchor must NOT contain a limb token, and that is the whole reason it reads
# `<task> — in flight: ` rather than the exemplar's full text.** A first version anchored
# on `in flight: limb 2 (tool.json state=open`, which already contains `limb 2 (` — so the
# LIMB assertion below matched by construction, its failure branch was unreachable, and the
# guard reported success on a weaker condition than the one it named. That is the same
# defect class this whole guard exists to catch, reproduced inside the guard; the unit test
# beside it is what keeps the branch reachable.
EXEMPLAR_ANCHOR = "<task> — in flight: "
# A limb label. The rule names all three; the exemplar must carry one.
LIMB = re.compile(r"limb [123] \(")


def fail(msg: str) -> None:
    print(f"FAIL {msg}", file=sys.stderr)
    sys.exit(1)


def main() -> None:
    if not DRIVE.exists():
        fail(f"{DRIVE.relative_to(REPO)} does not exist")
    text = DRIVE.read_text(encoding="utf-8").splitlines()
    rel = DRIVE.relative_to(REPO)

    rule = [ln for ln in text if RULE_ANCHOR in ln]
    if len(rule) != 1:
        fail(
            f"{rel}: expected exactly 1 line carrying the drop-line rule "
            f"({RULE_ANCHOR!r}), found {len(rule)}"
        )
    if not LIMB.search(rule[0]):
        fail(
            f"{rel}: the drop-line rule no longer names a limb label "
            f"(`limb 1 (`, `limb 2 (`, `limb 3 (`). That naming is this repo's only "
            f"detector for the marker's writer dying again, and nothing else checks it"
        )

    exemplar = [ln for ln in text if EXEMPLAR_ANCHOR in ln]
    if len(exemplar) != 1:
        fail(
            f"{rel}: expected exactly 1 worked exemplar carrying "
            f"{EXEMPLAR_ANCHOR!r}, found {len(exemplar)}"
        )
    if not LIMB.search(exemplar[0]):
        fail(
            f"{rel}: the worked exemplar no longer names a limb, so it no longer "
            f"exemplifies the rule it sits under"
        )

    print("  in-flight drop line names its limb (rule + worked exemplar)")


if __name__ == "__main__":
    main()
