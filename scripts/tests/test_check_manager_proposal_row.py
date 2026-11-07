"""check-manager-proposal-row.py: no router table may route a manager-proposal step.

The load-bearing case is `test_prose_mention_outside_the_table_passes`. The check reads the
table's rows and never the file body, because a router command may legitimately discuss the
proposal in prose — a whole-file substring match would red on a documentation edit, and a
guard that reds on a clean tree is switched off rather than fixed.

`test_inline_row_fails` is the second: `check-step-when.py` skips any row whose `File` cell
starts with `*` (its `router_tables` drops that part), so the repo's `*inline …*` row is the
shape a re-add would take and the one no other guard can see. A check that only read `File`
cells for a filename would pass it.

`test_zero_recognised_rows_fails` and `test_missing_commands_dir_fails` are the third: a guard
that silently matches no table prints `ok` and reads as coverage, which is worse than none.
"""
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest

SCRIPT = pathlib.Path(__file__).resolve().parent.parent / "check-manager-proposal-row.py"

ROUTER = "commands/fleet-drive.md"
HEADER = "| # | Step | When | File |\n|---|---|---|---|\n"
CLEAN_ROW = "| 0 | Rules | every tick | `${CLAUDE_PLUGIN_ROOT}/commands/steps/rules.md` |\n"


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        (pathlib.Path(self.dir) / "scripts").mkdir()
        shutil.copy(SCRIPT, pathlib.Path(self.dir) / "scripts" / "check-manager-proposal-row.py")
        self.write(ROUTER, "# Router\n\n" + HEADER + CLEAN_ROW)

    def write(self, relpath, text):
        path = pathlib.Path(self.dir) / relpath
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)

    def run_check(self):
        return subprocess.run(
            [sys.executable, "scripts/check-manager-proposal-row.py"],
            cwd=self.dir,
            capture_output=True,
            text=True,
        )


class TestManagerProposalRowGuard(Base):
    def test_absent_row_passes(self):
        result = self.run_check()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("route no manager proposal", result.stdout)

    def test_file_cell_row_fails(self):
        self.write(ROUTER, "# Router\n\n" + HEADER + CLEAN_ROW +
                   "| 19 | Propose a manager | every tick | `${CLAUDE_PLUGIN_ROOT}/commands/steps/propose-manager.md` |\n")
        result = self.run_check()
        self.assertEqual(result.returncode, 1)
        self.assertIn("row 19", result.stderr)
        self.assertIn(ROUTER, result.stderr)

    def test_inline_row_fails(self):
        """The repo's inline convention: a `*inline …*` File cell, no step file named."""
        self.write(ROUTER, "# Router\n\n" + HEADER + CLEAN_ROW +
                   "| 19 | Propose a manager | run: `python3 $P/propose-managers.py --post` | *inline* |\n")
        result = self.run_check()
        self.assertEqual(result.returncode, 1)
        self.assertIn("row 19", result.stderr)
        self.assertIn("inline", result.stderr)

    def test_inline_row_in_file_cell_fails(self):
        """The invocation may sit in any cell; scanning only Step+When would miss this."""
        self.write(ROUTER, "# Router\n\n" + HEADER + CLEAN_ROW +
                   "| 19 | Propose | every tick | `python3 $P/propose-managers.py --post` |\n")
        result = self.run_check()
        self.assertEqual(result.returncode, 1)
        self.assertIn("row 19", result.stderr)

    def test_prose_mention_outside_the_table_passes(self):
        self.write(ROUTER, "# Router\n\n"
                   "The round no longer runs `propose-managers.py --post`; that row was removed.\n\n"
                   + HEADER + CLEAN_ROW)
        result = self.run_check()
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_other_step_file_passes(self):
        self.write(ROUTER, "# Router\n\n" + HEADER +
                   "| 1 | Re-arm | every tick | `${CLAUDE_PLUGIN_ROOT}/commands/steps/re-arm.md` |\n")
        result = self.run_check()
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_script_without_the_post_flag_passes(self):
        """Only the posting invocation is banned; a row may still name the script for a read."""
        self.write(ROUTER, "# Router\n\n" + HEADER + CLEAN_ROW +
                   "| 9 | Verify | a cause is named | — (`propose-managers.py` read-only) |\n")
        result = self.run_check()
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_post_prefixed_flag_is_not_the_posting_flag(self):
        self.write(ROUTER, "# Router\n\n" + HEADER + CLEAN_ROW +
                   "| 9 | Verify | `propose-managers.py --postpone` | *inline* |\n")
        result = self.run_check()
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_extra_column_header_still_reads_the_table(self):
        """Containment, not equality: a trailing Notes column must not hide the table."""
        self.write(ROUTER, "# Router\n\n| # | Step | When | File | Notes |\n|---|---|---|---|---|\n"
                   "| 19 | Propose a manager | every tick | `commands/steps/propose-manager.md` | x |\n")
        result = self.run_check()
        self.assertEqual(result.returncode, 1)
        self.assertIn("row 19", result.stderr)

    def test_multi_part_file_cell_is_split(self):
        self.write(ROUTER, "# Router\n\n" + HEADER + CLEAN_ROW +
                   "| 19 | Propose | every tick | `${CLAUDE_PLUGIN_ROOT}/commands/steps/reap.md` · `commands/steps/propose-manager.md` |\n")
        result = self.run_check()
        self.assertEqual(result.returncode, 1)
        self.assertIn("row 19", result.stderr)

    def test_file_cell_with_trailing_prose_still_matches(self):
        self.write(ROUTER, "# Router\n\n" + HEADER + CLEAN_ROW +
                   "| 19 | Propose | every tick | `commands/steps/propose-manager.md` (see note) |\n")
        result = self.run_check()
        self.assertEqual(result.returncode, 1)
        self.assertIn("row 19", result.stderr)

    def test_zero_recognised_rows_fails(self):
        """A green run over no table is not coverage."""
        self.write(ROUTER, "# Router\n\nNo table here.\n")
        result = self.run_check()
        self.assertEqual(result.returncode, 1)
        self.assertIn("recognised 0 router rows", result.stderr)

    def test_missing_commands_dir_fails(self):
        shutil.rmtree(pathlib.Path(self.dir) / "commands")
        result = self.run_check()
        self.assertEqual(result.returncode, 1)
        self.assertIn("no router command found", result.stderr)


class TestAgainstTheRealTree(unittest.TestCase):
    def test_shipped_tree_passes(self):
        """The tree this guard ships in must pass it — the only other check is precommit."""
        result = subprocess.run(
            [sys.executable, str(SCRIPT)],
            cwd=SCRIPT.parent.parent,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("route no manager proposal", result.stdout)


if __name__ == "__main__":
    unittest.main()
