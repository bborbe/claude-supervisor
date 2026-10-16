"""check-drive-audit-budget.py: the guard must actually fail when the clause loses a part.

A guard's whole value is in whether it can fail, so every assertion it makes has a case
here that removes exactly the thing it asserts and requires a non-zero exit. The two that
matter most are the ones a plausible future edit would break:

  * **`test_bound_value_restated_fails`** — the single-home rule is the one a well-meaning
    author breaks by "helpfully" writing the number into the prose. Nothing else in the
    tree notices, and the copy then drifts from the constant it mirrors.
  * **`test_prohibition_removed_fails`** — clause (1) forbids a per-tick row cap and the
    bound is legal only because it is a wall clock instead. A reader who resolves that
    apparent tension by deleting the prohibition has removed the distinction the whole fix
    rests on, and every other assertion here stays green.

`test_shipped_clause_passes` runs the guard against this repository's real
`agents/manager-drive.md` rather than a fixture, so a future edit that satisfies the
fixture while breaking the shipped clause is caught.
"""
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest

SCRIPT = pathlib.Path(__file__).resolve().parent.parent / "check-drive-audit-budget.py"
REPO = SCRIPT.parent.parent
HOME = "agents/manager-drive.md"
BOUND = "scripts/drive-audit-budget.py"

#: A minimal clause (1) carrying every token the guard asserts: the script named twice (the
#: call and the single-home reference), all three branch tokens, no restated bound value,
#: and the row-cap prohibition that makes a wall clock legal where a row cap is not.
#: The three branch lines, keyed by the token each carries. Held as whole lines so a test
#: can DELETE one: a substring edit such as `TERMINAL:` -> `XXTERMINAL:` leaves the token
#: present and the guard rightly stays green, which is a broken test, not a broken guard.
BRANCH_LINES = {
    "CONTINUE": "- **`CONTINUE …` (exit 0)** → dispatch the next row.",
    "TERMINAL:": "- **`TERMINAL: …` (exit 3)** → stop dispatching, and report the line verbatim.",
    "REFUSED": "- **`REFUSED` (exit 2)** → the read failed: take neither branch.",
}

CLAUSE = (
    "**The cost model — a cold tick pays once.** ⚠️ **Do not invent a budget, a batch size "
    "or a sampling rule:** there is no per-tick cap in this clause, and a leg that "
    "introduces one silently narrows the set the caller asked it to cover.\n\n"
    "⚠️ **A wall-clock bound is the one bound this clause does NOT forbid.** Before every "
    "dispatch, run\n\n"
    "```bash\n"
    'python3 ${CLAUDE_PLUGIN_ROOT}/scripts/drive-audit-budget.py --started-at "<stamp>" '
    "--audited <n> --total <m>\n"
    "```\n\n"
    + "\n".join(BRANCH_LINES.values())
    + "\n\nIts value has one home — the `DEFAULT_BUDGET_SECONDS` constant in `%s`.\n" % BOUND
)


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        scripts = pathlib.Path(self.dir) / "scripts"
        scripts.mkdir()
        shutil.copy(SCRIPT, scripts / "check-drive-audit-budget.py")
        # The real bound script, copied — never a stub. Assertion (a) is about the callee
        # existing, so a stub would let a guard pass against a tree where the leg's own
        # command is missing.
        shutil.copy(REPO / "scripts" / "drive-audit-budget.py", scripts / "drive-audit-budget.py")
        self.write_clause(CLAUSE)

    def write_clause(self, clause):
        path = pathlib.Path(self.dir) / HOME
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(clause)

    def run_check(self, cwd=None):
        return subprocess.run(
            [sys.executable, "scripts/check-drive-audit-budget.py"],
            cwd=cwd or self.dir,
            capture_output=True,
            text=True,
        )


class TestDriveAuditBudgetGuard(Base):
    def test_shipped_clause_passes(self):
        """The real repo, not a fixture: this is the case that catches a broken shipped clause."""
        result = self.run_check(cwd=str(REPO))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("drive-audit-budget ok", result.stdout)

    def test_valid_clause_passes(self):
        result = self.run_check()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("drive-audit-budget ok", result.stdout)

    def test_missing_agent_file_fails(self):
        (pathlib.Path(self.dir) / HOME).unlink()
        result = self.run_check()
        self.assertEqual(result.returncode, 1)
        self.assertIn("does not exist", result.stderr)

    def test_missing_script_fails(self):
        """(a) The callee, not the mention — a name is not a working call."""
        (pathlib.Path(self.dir) / BOUND).unlink()
        result = self.run_check()
        self.assertEqual(result.returncode, 1)
        self.assertIn("does not exist", result.stderr)

    def test_a_single_mention_fails(self):
        """(b) One mention is a call with no single-home reference, or a mention with no call."""
        self.write_clause(CLAUSE.replace("in `%s`" % BOUND, "in the script"))
        result = self.run_check()
        self.assertEqual(result.returncode, 1)
        self.assertIn("fewer than", result.stderr)

    def test_each_branch_token_is_required(self):
        """(c) A clause that keeps only the happy path has dropped the stop.

        ⚠️ **The whole LINE is deleted, never the token's characters.** A substring edit
        leaves the token present -- `TERMINAL:` survives inside `XXTERMINAL:` -- so the
        guard correctly stays green and the test would be reporting its own mistake.
        """
        for token, line in BRANCH_LINES.items():
            with self.subTest(token=token):
                self.write_clause(CLAUSE.replace(line + "\n", ""))
                result = self.run_check()
                self.assertEqual(result.returncode, 1, "removing %s stayed green" % token)
                self.assertIn(token, result.stderr)

    def test_bound_value_restated_fails(self):
        """(d) The number has one home; a second copy is one a `grep` cannot tell from a real one."""
        self.write_clause(CLAUSE.replace("the `DEFAULT_BUDGET_SECONDS` constant", "900 seconds"))
        result = self.run_check()
        self.assertEqual(result.returncode, 1)
        self.assertIn("one home", result.stderr)

    def test_prohibition_removed_fails(self):
        """(e) The bound is legal only BECAUSE it is a wall clock and not a row cap."""
        self.write_clause(
            CLAUSE.replace("there is no per-tick cap in this clause", "a budget is fine")
        )
        result = self.run_check()
        self.assertEqual(result.returncode, 1)
        self.assertIn("prohibition", result.stderr)

    def test_the_constant_name_alone_is_not_enough(self):
        """Naming the constant without naming its home leaves the reader nowhere to look."""
        self.write_clause(
            CLAUSE.replace("the `DEFAULT_BUDGET_SECONDS` constant in `%s`" % BOUND,
                           "the `DEFAULT_BUDGET_SECONDS` constant")
        )
        result = self.run_check()
        self.assertEqual(result.returncode, 1)
        self.assertIn("fewer than", result.stderr)


if __name__ == "__main__":
    unittest.main()
