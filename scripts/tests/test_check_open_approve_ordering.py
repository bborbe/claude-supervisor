"""check-open-approve-ordering.py: the readiness dispatch precedes the approve pass.

`commands/open.md` Step 0.6 approves only the rows the readiness dispatch will open. An
approval is an *admission* — `approved_at` is the field `scripts/approved-not-started.py`
counts — so approving before readiness admits every row the dispatch then holds, refilling a
queue that exists to be drained.

The load-bearing case is `test_reversed_order_fails`. It feeds the ordering that shipped
before 2026-11-02 and asserts the check rejects it. A check that cannot fail on the tree it
was written against proves nothing.
"""
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest

SCRIPT = pathlib.Path(__file__).resolve().parent.parent / "check-open-approve-ordering.py"
ROOT = SCRIPT.parent.parent

READINESS = "**Readiness-gate every CREATE-bound row first, concurrently.** Dispatch it."
APPROVE = (
    "**Approve only the rows the dispatch will open — after readiness, never before.** "
    "⚠️ **`--flagged` batch only.** Approve them."
)

#: A tree the check accepts: dispatch first, approve second, scope inside the approve paragraph.
GOOD = f"## Step 0.6\n\n{READINESS}\n\n{APPROVE}\n\nA later paragraph.\n"


class CheckOpenApproveOrderingTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        (pathlib.Path(self.dir) / "scripts").mkdir()
        shutil.copy(SCRIPT, pathlib.Path(self.dir) / "scripts" / SCRIPT.name)
        self.write("commands/open.md", GOOD)

    def write(self, relpath, text):
        path = pathlib.Path(self.dir) / relpath
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)

    def run_check(self, cwd=None):
        return subprocess.run(
            [sys.executable, "scripts/check-open-approve-ordering.py"],
            cwd=cwd or self.dir,
            capture_output=True,
            text=True,
        )

    def test_wired_tree_passes(self):
        result = self.run_check()
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_shipped_tree_passes(self):
        """The real `commands/open.md` this branch ships must satisfy its own check."""
        result = self.run_check(cwd=ROOT)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_reversed_order_fails(self):
        """The pre-change ordering must be rejected — this is the negative control.

        Approve-before-readiness is what shipped until 2026-11-02. Without this case the
        check could pass on a tree that still approves every selected row, and every later
        pass would be indistinguishable from a no-op.
        """
        self.write("commands/open.md", f"## Step 0.6\n\n{APPROVE}\n\n{READINESS}\n")
        result = self.run_check()
        self.assertEqual(result.returncode, 1)
        self.assertIn("the dispatch must come FIRST", result.stderr)

    def test_scope_outside_the_approve_paragraph_fails(self):
        """A scope marker parked in a later paragraph must not satisfy the check.

        Otherwise the approve paragraph reads as applying to the single-task path too, and
        an unscoped reorder refuses every single-row open.
        """
        text = (
            f"## Step 0.6\n\n{READINESS}\n\n"
            "**Approve only the rows the dispatch will open — after readiness, never before.** "
            "Approve them.\n\n"
            "A later paragraph. ⚠️ **`--flagged` batch only.**\n"
        )
        self.write("commands/open.md", text)
        result = self.run_check()
        self.assertEqual(result.returncode, 1)
        self.assertIn("not scoped inside the approve paragraph", result.stderr)

    def test_paragraph_outside_step_0_6_fails(self):
        """Correct order but the wrong section must fail.

        Step 0.5 runs before the selector is final and Step 1.5 runs after the dispatch, so
        neither position reproduces approve-after-readiness. A guard that only compared
        file positions would stay green while the mechanism broke.
        """
        text = f"## Step 0.5\n\n{READINESS}\n\n## Step 0.6\n\n{APPROVE}\n"
        self.write("commands/open.md", text)
        result = self.run_check()
        self.assertEqual(result.returncode, 1)
        self.assertIn("outside Step 0.6", result.stderr)

    def test_duplicate_anchor_fails(self):
        """A second copy of an anchor must fail, not let first-match mask a reversal.

        `find` returns the first occurrence, so a duplicate earlier in the file would make
        the comparison read two positions that are not the paragraphs it means.
        """
        self.write("commands/open.md", GOOD + f"\n{READINESS}\n")
        result = self.run_check()
        self.assertEqual(result.returncode, 1)
        self.assertIn("occurs 2 time(s)", result.stderr)


if __name__ == "__main__":
    unittest.main()
