"""The budget ratchet's FAILURE paths, not just its green one.

`scripts/check-budget.py` is a tighten-or-fail ratchet over the plugin's bodies.
`scripts/budget-baseline.json` records the size each over-budget body carries in the tree
it was seeded from, and the gate fails in **both** directions — above an entry (a
regression) and below it (a stale baseline that would otherwise let the file regrow one
incident at a time) — plus on a body that is over budget and absent from the baseline.

⚠️ **A green run alone proves nothing about this script, and that is the whole reason
this file exists.** The baseline is seeded from this repository's own tree, so
`make precommit` passes by construction on any revision of it: a script that did nothing
at all would pass identically. The failure modes below are therefore the test. Each is
checked against a fixture tree rather than the shipped one, so a future edit cannot
satisfy the shipped tree while breaking the guard.

`test_shipped_tree_passes` runs against this repository's real files, so an edit that
satisfies the fixture while breaking the shipped tree is still caught.
"""
import json
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest

SCRIPT = pathlib.Path(__file__).resolve().parent.parent / "check-budget.py"
REPO = SCRIPT.parent.parent

BODY_MAX = 30 * 1024
TICK_MAX = 10 * 1024


class CheckBudgetTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = pathlib.Path(self.tmp.name)
        (self.root / "scripts").mkdir()
        (self.root / "commands").mkdir()
        (self.root / "agents").mkdir()
        shutil.copy(SCRIPT, self.root / "scripts" / SCRIPT.name)
        # Two baselined bodies: one over the body budget, one under it. The under-budget
        # one is baselined deliberately — the ratchet pins every entry it holds, not only
        # the ones currently over the line.
        self.write("commands/big.md", BODY_MAX + 1024)
        self.write("agents/small.md", 1000)
        # The suffix-less wakeup targets must exist in the fixture, or the guard's own
        # "named as a wakeup target but the file is gone" failure fires on every case.
        # One per `WAKEUP_TARGETS` entry: the manager loop's round, and the fleet loop's.
        self.write("commands/manager-drive.md", 1000)
        self.write("commands/fleet-drive.md", 1000)
        self.baseline({"commands/big.md": BODY_MAX + 1024, "agents/small.md": 1000})

    def write(self, rel, size):
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x" * size)

    def baseline(self, files):
        (self.root / "scripts" / "budget-baseline.json").write_text(
            json.dumps({"files": files}), encoding="utf-8"
        )

    def run_guard(self, *args):
        return subprocess.run(
            [sys.executable, str(self.root / "scripts" / SCRIPT.name), *args],
            capture_output=True,
            text=True,
        )

    # --- the three failure modes the criterion names -------------------------

    def test_seeded_tree_passes(self):
        result = self.run_guard()
        self.assertEqual(0, result.returncode, result.stderr)

    def test_baselined_body_above_its_max_fails(self):
        self.write("commands/big.md", BODY_MAX + 2048)
        result = self.run_guard()
        self.assertEqual(1, result.returncode)
        self.assertIn("commands/big.md", result.stderr)
        self.assertIn("exceeds", result.stderr)

    def test_baselined_body_below_its_max_fails(self):
        # A stale baseline: the file shrank, so the recorded max would let it regrow.
        self.write("commands/big.md", BODY_MAX - 5000)
        result = self.run_guard()
        self.assertEqual(1, result.returncode)
        self.assertIn("commands/big.md", result.stderr)
        self.assertIn("UNDER", result.stderr)

    def test_new_oversized_body_absent_from_the_baseline_fails(self):
        self.write("commands/unlisted.md", BODY_MAX + 1)
        result = self.run_guard()
        self.assertEqual(1, result.returncode)
        self.assertIn("commands/unlisted.md", result.stderr)
        self.assertIn("not baselined", result.stderr)

    # --- the strict tick cap, and the repair path ----------------------------

    def test_oversized_tick_fails(self):
        # The tick is the one class with a strict cap: it is what a wakeup injects.
        self.write("commands/thing-tick.md", TICK_MAX + 1)
        result = self.run_guard()
        self.assertEqual(1, result.returncode)
        self.assertIn("thing-tick.md", result.stderr)

    def test_tick_exactly_at_the_cap_passes(self):
        self.write("commands/thing-tick.md", TICK_MAX)
        result = self.run_guard()
        self.assertEqual(0, result.returncode, result.stderr)

    # --- the strict cap follows the WAKEUP TARGET, not the filename -------------------

    def test_oversized_suffixless_wakeup_target_fails(self):
        # The manager loop's wakeup names `commands/manager-drive.md`, which carries no
        # `-tick` suffix — so the glob cannot find it and the explicit list is the only
        # thing holding the cap on that side.
        self.write("commands/manager-drive.md", TICK_MAX + 1)
        result = self.run_guard()
        self.assertEqual(1, result.returncode)
        self.assertIn("commands/manager-drive.md", result.stderr)

    def test_suffixless_wakeup_target_at_the_cap_passes(self):
        self.write("commands/manager-drive.md", TICK_MAX)
        result = self.run_guard()
        self.assertEqual(0, result.returncode, result.stderr)

    def test_missing_wakeup_target_fails(self):
        # A cap pointed at a file that is gone reports green against nothing, so its
        # absence is a failure rather than a skip.
        (self.root / "commands" / "manager-drive.md").unlink()
        result = self.run_guard()
        self.assertEqual(1, result.returncode)
        self.assertIn("wakeup target", result.stderr)

    def test_tighten_lowers_only_the_under_max_entry(self):
        # The half the criterion calls out by name: --tighten must rewrite the entries
        # genuinely under their max and leave every other entry alone. A tighten that
        # rewrote the whole file would let a second branch rebasing onto this one undo
        # the ratchet.
        self.write("commands/big.md", BODY_MAX - 5000)
        result = self.run_guard("--tighten")
        self.assertEqual(0, result.returncode, result.stderr)
        files = json.loads(
            (self.root / "scripts" / "budget-baseline.json").read_text(encoding="utf-8")
        )["files"]
        self.assertEqual(BODY_MAX - 5000, files["commands/big.md"])
        self.assertEqual(1000, files["agents/small.md"])

    def test_tighten_does_not_rescue_a_growth(self):
        # Growth is deliberately NOT auto-repairable: --tighten lowers only, so a file
        # that grew stays a failure until a human raises the entry by hand. That is the
        # property that keeps a ratchet from being quietly loosened.
        self.write("commands/big.md", BODY_MAX + 2048)
        result = self.run_guard("--tighten")
        self.assertEqual(1, result.returncode)
        self.assertIn("commands/big.md", result.stderr)

    def test_missing_baseline_fails_closed(self):
        (self.root / "scripts" / "budget-baseline.json").unlink()
        result = self.run_guard()
        self.assertEqual(1, result.returncode)
        self.assertIn("does not exist", result.stderr)

    def test_baselined_file_gone_is_reported(self):
        (self.root / "commands" / "big.md").unlink()
        result = self.run_guard()
        self.assertEqual(1, result.returncode)
        self.assertIn("commands/big.md", result.stderr)

    # --- the shipped tree ----------------------------------------------------

    def test_shipped_tree_passes(self):
        result = subprocess.run(
            [sys.executable, str(SCRIPT)], capture_output=True, text=True, cwd=REPO
        )
        self.assertEqual(0, result.returncode, f"{result.stdout}\n{result.stderr}")
        self.assertIn("budget ok", result.stdout)


if __name__ == "__main__":
    unittest.main()
