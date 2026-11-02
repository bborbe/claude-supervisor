"""check-manager-proposal-row.py: no router table may route a manager-proposal step.

The load-bearing case is `test_prose_mention_outside_the_table_passes`. The check reads the
table's rows and never the file body, because a router command may legitimately discuss the
proposal in prose — a whole-file substring match would red on a documentation edit, and a
guard that reds on a clean tree is switched off rather than fixed.

`test_inline_post_row_fails` is the second: `check-step-when.py` skips any row whose `File`
cell names no step file, so the inline `--post` shape is the one a re-add would take and the
one no other guard can see. A check that only read `File` cells would pass it.
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

    def test_inline_post_row_fails(self):
        """The shape `check-step-when.py` cannot see: no step file named, the script run inline."""
        self.write(ROUTER, "# Router\n\n" + HEADER + CLEAN_ROW +
                   "| 19 | Propose a manager | run: `python3 $P/propose-managers.py --post` | — |\n")
        result = self.run_check()
        self.assertEqual(result.returncode, 1)
        self.assertIn("row 19", result.stderr)
        self.assertIn("inline", result.stderr)

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


if __name__ == "__main__":
    unittest.main()
