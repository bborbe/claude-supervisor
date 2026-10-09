"""check-goal-row-clause.py: the goal-row rule must stay single-valued, and its retracted
readings must not come back.

The defect this guards against, measured in one review round: the rule is carried four times
in `agents/manager-sweep-reader.md`, and within a single round the `<success_criteria>` bullet
kept an escape clause the frame paragraph had already retracted, while the input-8 contract's
new "render no goal rows" clause silently deleted the root row of every goal-branch frame.
Both were found by a reviewer reading prose, not by anything mechanical.

The load-bearing cases are the three retraction tests and
`test_goal_branch_exemption_is_required`. A guard that only asserted the clause's presence
would have passed on the round-2 tree, where the clause was present *and* contradicted — the
exemption check is the one that pins the CRITICAL fix, and the retraction scan is the one that
keeps the withdrawn placement-derivation from returning as prose.

`test_retracted_reading_at_the_repo_root_is_out_of_scope` pins the boundary the module
docstring states rather than leaving it to be rediscovered: `CHANGELOG.md` quotes the retracted
reading as history, so a scan widened to the repo root fails on the entry documenting the
retraction.
"""
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest

SCRIPT = pathlib.Path(__file__).resolve().parent.parent / "check-goal-row-clause.py"

CARRIER = "agents/manager-sweep-reader.md"
CLAUSE = "A member goal renders as its own row even with 0 tracked tasks"
EXEMPTION = "The GOAL branch is exempt by construction"
RETRACTED = "derived from its tasks"

SCANNED = ("agents/manager-sweep-reader.md", "commands/manager-loop.md", "docs/fleet-surface.md")


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        (pathlib.Path(self.dir) / "scripts").mkdir()
        shutil.copy(SCRIPT, pathlib.Path(self.dir) / "scripts" / "check-goal-row-clause.py")
        self.write(CARRIER, f"---\nname: x\n---\n\n{CLAUSE}\n\n{EXEMPTION}\n")
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
        self.assertIn("3 file(s)", result.stdout)

    def test_missing_clause_fails(self):
        self.write(CARRIER, f"---\nname: x\n---\n\n{EXEMPTION}\n")
        result = self.run_check()
        self.assertEqual(result.returncode, 1)
        self.assertIn("no longer carries the clause", result.stderr)

    def test_goal_branch_exemption_is_required(self):
        """The CRITICAL fix. A tree carrying the clause but not the exemption is the round-2
        state: the clause present *and* contradicted by the absent-input rule."""
        self.write(CARRIER, f"---\nname: x\n---\n\n{CLAUSE}\n")
        result = self.run_check()
        self.assertEqual(result.returncode, 1)
        self.assertIn("dropped the goal-branch exemption", result.stderr)
        self.assertIn("root row", result.stderr)

    def test_missing_carrier_fails(self):
        (pathlib.Path(self.dir) / CARRIER).unlink()
        result = self.run_check()
        self.assertEqual(result.returncode, 1)
        self.assertIn("does not exist", result.stderr)

    def test_retracted_reading_in_the_carrier_fails(self):
        self.write(CARRIER, f"---\nname: x\n---\n\n{CLAUSE}\n\n{EXEMPTION}\n\na goal row is {RETRACTED}' placement\n")
        result = self.run_check()
        self.assertEqual(result.returncode, 1)
        self.assertIn(RETRACTED, result.stderr)
        self.assertIn(CARRIER, result.stderr)

    def test_retracted_reading_in_commands_fails(self):
        self.write("commands/manager-loop.md", f"---\ntitle: x\n---\n\nit is {RETRACTED}\n")
        result = self.run_check()
        self.assertEqual(result.returncode, 1)
        self.assertIn("commands/manager-loop.md", result.stderr)

    def test_retracted_reading_in_docs_fails(self):
        self.write("docs/fleet-surface.md", f"---\ntitle: x\n---\n\nit is {RETRACTED}\n")
        result = self.run_check()
        self.assertEqual(result.returncode, 1)
        self.assertIn("docs/fleet-surface.md", result.stderr)

    def test_retracted_reading_at_the_repo_root_is_out_of_scope(self):
        """The stated boundary. `CHANGELOG.md` quotes the retracted reading as history, so the
        scan stops at the three rule directories and the changelog is not read."""
        self.write("CHANGELOG.md", f"# Changelog\n\n- fix: it retracts `{RETRACTED}' placement`\n")
        result = self.run_check()
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
