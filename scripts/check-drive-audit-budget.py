#!/usr/bin/env python3
"""The manager drive leg's audit bound has one home in code, and the clause keeps it there.

`agents/manager-drive.md` clause (1) dispatches one `vault-cli:task-auditor` subagent per
row and nothing bounded the loop, so a cold tick ran to completion however long that took
-- measured 2026-10-16 at about 34 min on a round with zero actionable rows
(`.tracked.txt` 11:05:04 -> `.verdicts.json` 11:39:01). The bound now lives in
`scripts/drive-audit-budget.py`, and this is the gate that keeps it there.

Same shape, and the same reason, as `check-drive-ledger-writes.py` and
`check-content-key-formula.py`: a contract an agent executes by hand needs a mechanical
check, not a keep-in-sync sentence.

Five assertions, all required:

  (a) **`scripts/drive-audit-budget.py` exists on disk.** The mention count below cannot
      tell a name from a working call, and a leg naming a script that is not there dies at
      runtime with `No such file` while every other assertion reads green. Measured on the
      sibling gate 2026-10-12: `check-drive-ledger-writes.py`'s (a) green, (d) red.
  (b) `agents/manager-drive.md` **names the script** at least twice -- the call, and the
      single-home reference to its constant.
  (c) The clause carries **all three branch tokens** -- `CONTINUE`, `TERMINAL:` and
      `REFUSED`. A clause naming only the happy path has dropped the stop it exists for.
  (d) **The bound's VALUE is not restated in the agent file.** The number has one home --
      the script's constant -- for the reason the clause's own cost-model paragraph gives
      about restated counters: a second copy is one a `grep` cannot tell from a real one.
      Asserted as "no bare bound value in the file" rather than by matching prose, so the
      check does not have to be reworded when the surrounding sentence is.
  (e) **Clause (1)'s row-cap prohibition is still present.** The bound is defensible ONLY
      because it is a wall clock and not a row cap. A later reader who resolves the
      apparent tension by deleting the prohibition has removed the rule that makes the
      bound legal, and nothing else in the tree would notice.

**What this check does not prove.** It reads what the files *say*. A clause that names the
script and then dispatches anyway passes here and fails at runtime -- that half is
behavioural, and `scripts/tests/test_drive_audit_budget.py` is where the bound's own
verdicts are exercised.
"""
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

#: The one file allowed to name the script and to carry the clause.
DRIVE = "agents/manager-drive.md"

#: The script's name, as the clause must call it.
SCRIPT = "drive-audit-budget.py"
MIN_SCRIPT_MENTIONS = 2  # the call, and the constant's single-home reference

#: Every branch of the script's contract that the clause must keep naming.
BRANCH_TOKENS = ("CONTINUE", "TERMINAL:", "REFUSED")

#: The clause (1) sentence that makes a wall clock legal where a row cap is not.
PROHIBITION = "there is no per-tick cap in this clause"

#: The bound's value, which must live in the script alone. Kept in step with the
#: script's `DEFAULT_BUDGET_SECONDS` by its own test, not by a second copy here.
BOUND_VALUE = "900"


def main():
    failures = []

    drive = REPO / DRIVE
    if not drive.exists():
        print("check-drive-audit-budget: FAIL", file=sys.stderr)
        print("  - %s does not exist -- the drive leg has no body to check" % DRIVE, file=sys.stderr)
        return 1

    body = drive.read_text(encoding="utf-8")

    # (a) the callee exists. Checked BEFORE the mention count, because a name is exactly
    # what the mention count cannot tell from a working call.
    script = REPO / "scripts" / SCRIPT
    if not script.exists():
        failures.append(
            "%s names `%s` but scripts/%s does not exist -- the leg would run a command "
            "that is not there, and the mention count below would still be green"
            % (DRIVE, SCRIPT, SCRIPT)
        )

    # (b) the script is actually wired in.
    mentions = body.count(SCRIPT)
    if mentions < MIN_SCRIPT_MENTIONS:
        failures.append(
            "%s names `%s` %d time(s), fewer than the %d a call and a single-home "
            "reference need -- the bound is being enforced some other way"
            % (DRIVE, SCRIPT, mentions, MIN_SCRIPT_MENTIONS)
        )

    # (c) all three branches survive in the prose.
    for token in BRANCH_TOKENS:
        if token not in body:
            failures.append(
                "%s no longer names the `%s` branch of the bound -- a clause that keeps "
                "only the happy path has dropped the stop it exists for" % (DRIVE, token)
            )

    # (d) the value is not restated. The constant's NAME may appear; its number may not.
    if BOUND_VALUE in body:
        failures.append(
            "the bound's value `%s` appears in %s -- it has one home, the "
            "`DEFAULT_BUDGET_SECONDS` constant in scripts/%s, and a second copy is one a "
            "`grep` cannot tell from a real one" % (BOUND_VALUE, DRIVE, SCRIPT)
        )

    # (e) the prohibition that makes the bound legal is still stated.
    if PROHIBITION not in body:
        failures.append(
            "%s no longer carries clause (1)'s row-cap prohibition (%r) -- the bound is "
            "legal only BECAUSE it is a wall clock and not a row cap, so removing the "
            "prohibition removes the distinction it rests on" % (DRIVE, PROHIBITION)
        )

    if failures:
        print("check-drive-audit-budget: FAIL", file=sys.stderr)
        for failure in failures:
            print("  - %s" % failure, file=sys.stderr)
        return 1

    print(
        "drive-audit-budget ok: scripts/%s exists, %s names it %d time(s), all %d branch "
        "tokens survive, the bound's value is stated nowhere in the prose, and clause "
        "(1)'s row-cap prohibition is intact"
        % (SCRIPT, DRIVE, mentions, len(BRANCH_TOKENS))
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
