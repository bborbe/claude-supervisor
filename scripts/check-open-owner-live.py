#!/usr/bin/env python3
"""A held row is never silent, and the same-run manager counts — the two facts SC3/SC4 name.

`scripts/flagged-rows.py` answers `held-owner-live` for a task that would otherwise CREATE a
worker whose owning subject already has a live manager. That verdict is a *branch*, and a
branch is invisible: nothing in the resolver renders, so every consumer obligation lives in
`commands/open.md` prose. Four regressions are silent:

  (a) **The render disappears.** A rewrite of § Step 0.5 drops the `⛔ HOLD — owner …` line
      and the row is held with no reason printed. The batch's open count shrinks and nothing
      says why — the exact failure SC4 exists to prevent, and the one an operator cannot
      distinguish from a broken selector.
  (b) **The render stops naming the owner.** `⛔ HOLD` alone is indistinguishable from the
      pre-existing `blocked=True` hold, so the reader cannot tell "its subject has a manager"
      from "its prerequisites are unfinished".
  (c) **The same-run manager stops counting.** A manager spawned moments ago is not in the
      registry yet, so `owner_live` reads `false` for it; the spawn ledger is the only thing
      that catches it. Drop that leg and a round opens a goal manager and a worker on its
      task in the same pass — the collision this whole change exists to prevent (SC3).
  (d) **The footer stops separating the two hold kinds.** `held K (blocked)` alone folds an
      owner-live hold into the blocked count, so a shrinking open count again reads as a
      selector bug rather than a decision.

⚠️ **What this check does not prove.** It is a prose-and-source shape check, because a
command file is prose. The hold's behaviour is tested in
`scripts/tests/test_flagged_rows.py` (`HeldOwnerLive`, `RowGate`); each guard below is
proven to fire on its own one-line mutation in
`scripts/tests/test_check_open_owner_live.py`.
"""
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
COMMAND = ROOT / "commands" / "open.md"

#: Section boundaries. A renamed heading fails loudly instead of scanning an empty string and
#: reporting a clean gate — the failure mode that makes a prose check worthless.
GATE_START = "**Gate each task row on its owning subject's manager"
GATE_END = "## Step 0.6 — Classify and fill the missing `role` and `mode`"

#: (a) The render, matched on the words the operator reads — never on backtick placement,
#: which the prose does not hold still.
RENDER = re.compile(r"⛔ HOLD — owner")
#: (b) The render must name the owning subject, or it is indistinguishable from `blocked=True`.
RENDER_NAMES_OWNER = re.compile(r"⛔ HOLD — owner[^\n]{0,40}<kind>[^\n]{0,20}\"<name>\"")
#: (c) The spawn-ledger leg: a manager opened earlier in the same run counts.
SAME_RUN = re.compile(r"opened-recently\.py[^`\n]*--subject")
#: (d) The footer separates an owner-live hold from a blocked one.
FOOTER = "held K (blocked · owner live)"
#: The gate must say the hold reads the OWNER's session, never the row's — otherwise a rewrite
#: reuses `LIVE HERE` / `LIVE ELSEWHERE` and the two verdicts become one label.
ROW_OWN_LABELS = ("LIVE HERE", "LIVE ELSEWHERE")
#: Producer and consumer must agree on the branch name, or the render keys on nothing.
BRANCH = "held-owner-live"
PRODUCER = ("scripts/flagged-rows.py", 'return "held-owner-live", refused')
SCHEMA = ("scripts/tests/schema/flagged-rows.schema.json", '"held-owner-live"')


def gate(text):
    begin = text.find(GATE_START)
    if begin == -1:
        raise SystemExit("check-open-owner-live: missing gate heading %r" % GATE_START)
    finish = text.find(GATE_END, begin)
    if finish == -1:
        raise SystemExit("check-open-owner-live: missing gate end heading %r" % GATE_END)
    return text[begin:finish]


def main():
    text = COMMAND.read_text(encoding="utf-8")
    body = gate(text)
    failures = []

    # (a) The render exists.
    if not RENDER.search(body):
        failures.append("§ Step 0.5 no longer renders `⛔ HOLD — owner …` — a held row is "
                        "silent, and a shrinking open count is indistinguishable from a "
                        "broken selector (SC4)")

    # (b) The render names the owning subject.
    elif not RENDER_NAMES_OWNER.search(body):
        failures.append("the `⛔ HOLD` render no longer names the owning subject's kind and "
                        "name — it reads exactly like a `blocked=True` hold, so the reader "
                        "cannot tell the two reasons apart (SC4)")

    # (c) A manager opened earlier in the same run counts.
    if not SAME_RUN.search(body):
        failures.append("§ Step 0.5 no longer consults the spawn ledger "
                        "(`opened-recently.py --subject …`) — a manager spawned moments ago "
                        "is not in the registry yet, so the same round opens a goal manager "
                        "and a worker on its task (SC3)")

    # (d) The footer separates the two hold kinds.
    if FOOTER not in text:
        failures.append("the batch footer no longer carries %r — an owner-live hold folds "
                        "into the blocked count, and the two decisions become one number"
                        % FOOTER)

    # The verdict belongs to the owner, not to the row.
    for label in ROW_OWN_LABELS:
        if label not in body:
            failures.append("§ Step 0.5 no longer names `%s` as the row-own verdict — a "
                            "rewrite may reuse it for the owner-live hold, making two "
                            "different facts one label (SC4)" % label)

    # Producer and consumer agree on the branch name.
    source = (ROOT / PRODUCER[0]).read_text(encoding="utf-8")
    if PRODUCER[1] not in source:
        failures.append("%s no longer returns %r — the render keys on a branch nothing "
                        "emits" % (PRODUCER[0], BRANCH))
    schema = (ROOT / SCHEMA[0]).read_text(encoding="utf-8")
    if SCHEMA[1] not in schema:
        failures.append("%s no longer carries %r in the branch enum — the emitted row would "
                        "not validate" % (SCHEMA[0], BRANCH))

    if failures:
        print("check-open-owner-live: FAILED")
        for failure in failures:
            print("  - %s" % failure)
        return 1

    print("  a task whose owning subject has a live manager is held with its reason "
          "rendered; the same-run manager counts; the two hold kinds stay separate")
    return 0


if __name__ == "__main__":
    sys.exit(main())
