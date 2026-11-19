"""check-round-split.py: the four statements of the round's row split must agree.

The split is written in the router's list, the dispatch instruction, and the delegate
agent's two tables. The first cut of the 2026-11-19 row move edited three of the four and
left `agents/fleet-round.md`'s role block re-owning a moved row to the manager — which
re-owned it in the very prose the move depended on, while every file stayed well-formed.

The load-bearing cases:
  - the real tree passes;
  - a row removed from the agent's § 1 fails, naming it;
  - a row added to the agent's § 2 fails, naming it;
  - a sub-row (`5d`) is covered by its parent's range, so the router's `2–10` shorthand
    does not read as a missing row.
"""
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest

SCRIPTS = pathlib.Path(__file__).resolve().parent.parent
CHECK = SCRIPTS / "check-round-split.py"


def run(root: pathlib.Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(root / "scripts" / "check-round-split.py")],
        capture_output=True,
        text=True,
    )


class TestRoundSplit(unittest.TestCase):
    def setUp(self):
        self.root = pathlib.Path(tempfile.mkdtemp(prefix="round-split-"))
        self.addCleanup(shutil.rmtree, self.root, True)
        for name in ("scripts", "commands", "agents"):
            shutil.copytree(SCRIPTS.parent / name, self.root / name)

    def agent(self) -> pathlib.Path:
        return self.root / "agents" / "fleet-round.md"

    def test_the_real_tree_agrees(self):
        done = run(self.root)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("round-split ok", done.stdout)

    def test_a_row_missing_from_the_run_table_fails(self):
        p = self.agent()
        text = p.read_text(encoding="utf-8")
        row = "| 20 | Render the output shape — write `<round>.render`, never print, never ask | `steps/output-shape.md` |\n"
        self.assertEqual(text.count(row), 1)
        p.write_text(text.replace(row, ""), encoding="utf-8")
        done = run(self.root)
        self.assertEqual(done.returncode, 1)
        self.assertIn("20", done.stderr)
        self.assertIn("§ 1", done.stderr)

    def test_a_row_left_in_the_leave_alone_table_fails(self):
        p = self.agent()
        text = p.read_text(encoding="utf-8")
        anchor = "| 19 | Round tail |"
        self.assertEqual(text.count(anchor), 1)
        p.write_text(
            text.replace(anchor, "| 8 | Act on the open-items ledger | stale |\n" + anchor),
            encoding="utf-8",
        )
        done = run(self.root)
        self.assertEqual(done.returncode, 1)
        self.assertIn("BOTH", done.stderr)

    def test_a_sub_row_is_covered_by_its_parent_range(self):
        # 5d is a delegate row and the router's `2–10` does not name it; that must pass.
        done = run(self.root)
        self.assertEqual(done.returncode, 0, done.stderr)


if __name__ == "__main__":
    unittest.main()
