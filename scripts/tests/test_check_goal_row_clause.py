"""check-goal-row-clause.py: the goal-row rule must stay single-valued, and its retracted
readings must not come back.

The defect this guards against, measured in one review round: the rule is carried four times
in `agents/manager-sweep-reader.md`, and within a single round the `<success_criteria>` bullet
kept an escape clause the frame paragraph had already retracted, while the input-8 contract's
new "render no goal rows" clause silently deleted the root row of every goal-branch frame.
Both were found by a reviewer reading prose, not by anything mechanical.

The load-bearing cases are `test_a_single_missing_carrier_fails` and
`test_goal_branch_exemption_is_required`. A guard that only asked "is the clause present at
least once" would have passed on the round-2 tree, where the clause was present *and*
contradicted — and it would also have passed a tree that had deleted the clause from two of
its four carriers, which is the gap a reviewer found in this guard's first version.

`test_every_retracted_phrase_fails_the_gate` runs a `subTest` over all three withdrawn
phrasings rather than exercising one across three directories: the first version of this file
tested only the placement derivation, so a typo in either of the other two anchors would have
gone unnoticed while the suite stayed green.

`test_retracted_reading_at_the_repo_root_is_out_of_scope` pins the boundary the module
docstring states rather than leaving it to be rediscovered: `CHANGELOG.md` quotes the retracted
reading as history, so a scan widened to the repo root fails on the entry documenting the
retraction.
"""
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

SCRIPT = pathlib.Path(__file__).resolve().parent.parent / "check-goal-row-clause.py"

CARRIER = "agents/manager-sweep-reader.md"
EXEMPTION = "The GOAL branch is exempt by construction"

#: The four carriers as the real file spells them, verbatim in shape — the input-8 contract
#: lowercases the subject, the optional-split paragraph bolds the zero. A fixture that used one
#: exact string four times would not exercise the pattern the guard matches on.
FOUR_CARRIERS = (
    "step 7's row-existence rule (a declared member goal renders as its own row even with 0 tracked tasks) and step 8's necessity read",
    "**A member goal renders as its own row even with 0 tracked tasks.** The row is a property of the topic's `## Goals` declaration",
    "a declared member goal renders as its own row even with **0** tracked tasks, and a goal owning no task simply has no rows",
    "**A member goal renders as its own row even with 0 tracked tasks.** The row set is read from the topic's `## Goals` declaration",
)

RETRACTED_PHRASES = (
    "derived from its tasks' placement",
    "you never place a goal",
    "you place tasks, never goals",
)


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        (pathlib.Path(self.dir) / "scripts").mkdir()
        shutil.copy(SCRIPT, pathlib.Path(self.dir) / "scripts" / "check-goal-row-clause.py")
        self.write(CARRIER, "---\nname: x\n---\n\n" + "\n\n".join(FOUR_CARRIERS) + f"\n\n{EXEMPTION}\n")
        self.write("commands/manager-loop.md", "---\ntitle: x\n---\n\nnothing here\n")
        self.write("docs/fleet-surface.md", "---\ntitle: x\n---\n\nnothing here\n")

    def write(self, relpath, text):
        path = pathlib.Path(self.dir) / relpath
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)

    def run_check(self):
        return subprocess.run(
            [sys.executable, "scripts/check-goal-row-clause.py"],
            cwd=self.dir,
            capture_output=True,
            text=True,
        )


class TestGoalRowClauseGuard(Base):
    def test_clean_tree_passes(self):
        result = self.run_check()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("goal-row-clause ok", result.stdout)
        self.assertIn("stated 4x", result.stdout)
        # Matched as a pattern, not a literal: the assertion is on the *shape* of the message,
        # so adding a fourth scanned fixture does not break a passing test for no reason.
        self.assertRegex(result.stdout, r"\d+ file\(s\) in agents, commands, docs")

    def test_a_single_missing_carrier_fails(self):
        """The gap a reviewer found in this guard's first version: an exact-string count saw
        only two of the four carriers, so deleting the clause from either of the others
        passed. Dropping one of the four must now fail."""
        self.write(CARRIER, "---\nname: x\n---\n\n" + "\n\n".join(FOUR_CARRIERS[:3]) + f"\n\n{EXEMPTION}\n")
        result = self.run_check()
        self.assertEqual(result.returncode, 1)
        self.assertIn("states the clause 3 time(s), want 4", result.stderr)

    def test_missing_carrier_fails(self):
        (pathlib.Path(self.dir) / CARRIER).unlink()
        result = self.run_check()
        self.assertEqual(result.returncode, 1)
        self.assertIn("does not exist", result.stderr)

    def test_goal_branch_exemption_is_required(self):
        """The CRITICAL fix. A tree carrying all four clauses but not the exemption is the
        round-2 state: the clause present *and* contradicted by the absent-input rule."""
        self.write(CARRIER, "---\nname: x\n---\n\n" + "\n\n".join(FOUR_CARRIERS) + "\n")
        result = self.run_check()
        self.assertEqual(result.returncode, 1)
        self.assertIn("dropped the goal-branch exemption", result.stderr)
        self.assertIn("root row", result.stderr)

    def test_retracted_reading_in_the_carrier_fails(self):
        self.write(
            CARRIER,
            "---\nname: x\n---\n\n"
            + "\n\n".join(FOUR_CARRIERS)
            + f"\n\n{EXEMPTION}\n\na goal row is structural, {RETRACTED_PHRASES[0]}\n",
        )
        result = self.run_check()
        self.assertEqual(result.returncode, 1)
        self.assertIn(RETRACTED_PHRASES[0], result.stderr)
        self.assertIn(CARRIER, result.stderr)

    def test_every_retracted_phrase_fails_the_gate(self):
        """All three anchors, not just the placement one — the first version of this file
        exercised a single phrase across three directories and left two anchors unguarded."""
        for phrase in RETRACTED_PHRASES:
            with self.subTest(phrase=phrase):
                self.write("commands/manager-loop.md", f"---\ntitle: x\n---\n\nit says: {phrase}\n")
                result = self.run_check()
                self.assertEqual(result.returncode, 1)
                self.assertIn(phrase, result.stderr)
                self.assertIn("commands/manager-loop.md", result.stderr)

    def test_retracted_reading_in_docs_fails(self):
        self.write("docs/fleet-surface.md", f"---\ntitle: x\n---\n\nit is {RETRACTED_PHRASES[0]}\n")
        result = self.run_check()
        self.assertEqual(result.returncode, 1)
        self.assertIn("docs/fleet-surface.md", result.stderr)

    def test_retracted_reading_at_the_repo_root_is_out_of_scope(self):
        """The stated boundary. `CHANGELOG.md` quotes the retracted reading as history, so the
        scan stops at the three rule directories and the changelog is not read."""
        self.write("CHANGELOG.md", f"# Changelog\n\n- fix: it retracts `{RETRACTED_PHRASES[0]}`\n")
        result = self.run_check()
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
