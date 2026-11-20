"""check-round-split.py: the four statements of the round's row split must agree.

The split is written in the router's list, the dispatch instruction, and the delegate
agent's two tables. The first cut of the 2026-11-19 row move edited three of the four and
left `agents/fleet-round.md`'s role block re-owning a moved row to the manager — which
re-owned it in the very prose the move depended on, while every file stayed well-formed.
The second cut parsed `round-dispatch.md`'s manager list and never compared it, which left
the same defect class open in the fourth statement.

The load-bearing cases:
  - the real tree passes;
  - a row removed from the agent's § 1 fails, naming it;
  - a row added to the agent's § 2 fails, naming it;
  - a row added to the dispatch instruction's manager list fails — the comparison the
    second cut forgot;
  - a shrunk router range fails, naming the rows it dropped;
  - a missing split line is a parse failure, never a silent pass;
  - `expand`/`base` resolve an en-dash range and a sub-row as documented.
"""
import importlib.util
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest

SCRIPTS = pathlib.Path(__file__).resolve().parent.parent
CHECK = SCRIPTS / "check-round-split.py"

_spec = importlib.util.spec_from_file_location("check_round_split", CHECK)
check_round_split = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(check_round_split)


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

    def router(self) -> pathlib.Path:
        return self.root / "commands" / "fleet-drive.md"

    def dispatch(self) -> pathlib.Path:
        return self.root / "commands" / "steps" / "round-dispatch.md"

    def rewrite(self, path: pathlib.Path, old: str, new: str) -> None:
        text = path.read_text(encoding="utf-8")
        self.assertEqual(text.count(old), 1, f"anchor not unique in {path.name}: {old!r}")
        path.write_text(text.replace(old, new), encoding="utf-8")

    def test_the_real_tree_agrees(self):
        done = run(self.root)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("round-split ok", done.stdout)

    def test_a_row_missing_from_the_run_table_fails(self):
        self.rewrite(
            self.agent(),
            "| 20 | Render the output shape — write `<round>.render`, never print, never ask | `steps/output-shape.md` |\n",
            "",
        )
        done = run(self.root)
        self.assertEqual(done.returncode, 1)
        self.assertIn("20", done.stderr)
        self.assertIn("§ 1", done.stderr)

    def test_a_row_left_in_the_leave_alone_table_fails(self):
        self.rewrite(
            self.agent(),
            "| 19 | Round tail |",
            "| 8 | Act on the open-items ledger | stale |\n| 19 | Round tail |",
        )
        done = run(self.root)
        self.assertEqual(done.returncode, 1)
        self.assertIn("BOTH", done.stderr)

    def test_a_row_added_to_the_dispatch_manager_list_fails(self):
        # The comparison the second cut forgot: dispatch's manager set was parsed and dropped.
        self.rewrite(self.dispatch(), "You keep rows 1, 11, 13, 14, 17 and 19:", "You keep rows 1, 8, 11, 13, 14, 17 and 19:")
        done = run(self.root)
        self.assertEqual(done.returncode, 1)
        self.assertIn("manager's rows", done.stderr)
        self.assertIn("only-dispatch", done.stderr)

    def test_a_shrunk_router_range_fails(self):
        self.rewrite(self.router(), "rows 0, 2–3, 5–10, 12", "rows 0, 2–3, 5–6, 12")
        done = run(self.root)
        self.assertEqual(done.returncode, 1)
        self.assertIn("7", done.stderr)

    def test_a_row_assigned_to_neither_session_fails(self):
        # Pairwise agreement is not coverage: drop a row from the router's list AND both
        # agent tables and every pairwise comparison still holds.
        self.rewrite(self.router(), "rows 0, 2–3, 5–10, 12, 15, 16b, 18, 20", "rows 0, 2–3, 5–10, 12, 15, 16b, 18")
        self.rewrite(self.agent(), "| 20 | Render the output shape — write `<round>.render`, never print, never ask | `steps/output-shape.md` |\n", "")
        self.rewrite(self.dispatch(), "runs rows 0, 2–3, 5–10, 12, 15, 16b, 18 and 20", "runs rows 0, 2–3, 5–10, 12, 15, 16b and 18")
        done = run(self.root)
        self.assertEqual(done.returncode, 1)
        self.assertIn("NEITHER", done.stderr)

    def test_a_missing_router_table_is_a_parse_failure(self):
        # The coverage half reads the router's own table; its absence must fail loudly.
        text = self.router().read_text(encoding="utf-8")
        self.assertEqual(text.count("| # | Step | When | File |"), 1)
        self.router().write_text(
            text.replace("| # | Step | When | File |", "| row | Step | When | File |"),
            encoding="utf-8",
        )
        done = run(self.root)
        self.assertEqual(done.returncode, 1)
        self.assertIn("no router table", done.stderr)

    def test_a_missing_split_line_is_a_parse_failure(self):
        text = self.router().read_text(encoding="utf-8")
        line = next(l for l in text.splitlines() if "run in `supervisor:fleet-round`" in l)
        self.router().write_text(text.replace(line, "no split here"), encoding="utf-8")
        done = run(self.root)
        self.assertEqual(done.returncode, 1)
        self.assertIn("no row-split line", done.stderr)


class TestRowArithmetic(unittest.TestCase):
    """`expand`/`base` as documented: a range expands, a sub-row rides its parent."""

    def test_an_en_dash_range_expands(self):
        self.assertEqual(
            check_round_split.expand("0, 2–10, 12"), {str(n) for n in [0, *range(2, 11), 12]}
        )

    def test_a_sub_row_rides_its_parent(self):
        self.assertEqual(check_round_split.base("5d"), "5")
        self.assertEqual(check_round_split.base("5b"), "5")
        self.assertEqual(check_round_split.base("16b"), "16")
        self.assertIn("5", check_round_split.expand("2–10"))


if __name__ == "__main__":
    unittest.main()
