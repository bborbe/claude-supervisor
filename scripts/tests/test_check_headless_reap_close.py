"""check-headless-reap-close.py: a gate that only ever exits 0 grades itself.

Every clause is an ABSENCE or a COUNT, and an absence that is never shown firing is
indistinguishable from a pattern that matches nothing. So each clause is exercised
against a tree crafted to violate it, and the assertion is that the check exits NON-zero
and names the right clause.

The staged tree is a real directory layout, not a monkeypatched REPO: the check derives
its root from its own `__file__`, so copying it into `<tmp>/scripts/` is what makes the
resolution path under test the real one — the same shape `test_check_drive_ledger_writes`
uses.
"""
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest

SCRIPT = pathlib.Path(__file__).resolve().parent.parent / "check-headless-reap-close.py"

#: A conforming set: the drive leg names the script twice and the entrypoint, the goal leg
#: names both, the command carries the rule without the retired literal, the reap contract
#: carries the carve-out.
GOOD_DRIVE = """\
1b. Run `python3 $P/close-worker.py "<sid>" --dry-run`, then `python3 $P/close-worker.py "<sid>"`.
    A `would close pid N (entrypoint sdk-cli, no pane)` row is headless.
"""

GOOD_GOAL = """\
- Read the tier: `python3 scripts/close-worker.py "<sid>" --dry-run`.
  A `(entrypoint sdk-cli, no pane)` row is closed, not reaped.
"""

GOOD_CMD = """\
- **Close via `close-worker.py` only.** A finished headless row is closed by the drive leg.
"""

GOOD_REAP = """\
The one carve-out is a finished headless row: the drive leg closes it through
`scripts/close-worker.py`.
"""


class Base(unittest.TestCase):
    def setUp(self):
        self.root = pathlib.Path(tempfile.mkdtemp(prefix="check-hrc-"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        (self.root / "scripts").mkdir()
        shutil.copyfile(SCRIPT, self.root / "scripts" / SCRIPT.name)

    def stage(self, drive=GOOD_DRIVE, goal=GOOD_GOAL, cmd=GOOD_CMD, reap=GOOD_REAP, callee=True):
        for rel, body in (
            ("agents/fleet-drive.md", drive),
            ("agents/goal-drive.md", goal),
            ("commands/fleet-drive.md", cmd),
            ("commands/steps/reap.md", reap),
        ):
            path = self.root / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(body, encoding="utf-8")
        if callee:
            (self.root / "scripts" / "close-worker.py").write_text(
                "# stub for the gate's existence check\n", encoding="utf-8"
            )

    def run_check(self):
        return subprocess.run(
            [sys.executable, str(self.root / "scripts" / SCRIPT.name)],
            capture_output=True,
            text=True,
        )

    def assert_fails_naming(self, token):
        result = self.run_check()
        self.assertNotEqual(result.returncode, 0, "the gate passed a violating tree")
        self.assertIn(token, result.stderr, "the gate failed but did not name the clause")


class TestConforming(Base):
    def test_a_clean_tree_passes(self):
        self.stage()
        result = self.run_check()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("headless-reap-close ok", result.stdout)


class TestCalleeMissing(Base):
    def test_a_named_script_that_is_not_there_fails(self):
        self.stage(callee=False)
        self.assert_fails_naming("does not exist")


class TestDriveLeg(Base):
    def test_one_mention_is_the_prefix_state(self):
        # The real pre-fix text with a single name dropped in: a comment, not a close.
        self.stage(drive='Run `close-worker.py "<sid>"` eventually.\n')
        self.assert_fails_naming("fewer than the 2")

    def test_no_sdk_cli_means_no_tier_read(self):
        self.stage(
            drive='`close-worker.py "<sid>" --dry-run`, then `close-worker.py "<sid>"`.\n'
        )
        self.assert_fails_naming("sdk-cli")


class TestGoalLeg(Base):
    def test_the_goal_leg_must_name_the_script(self):
        self.stage(goal="Reap the finished with SendMessage. A `sdk-cli` row is special.\n")
        self.assert_fails_naming("goal-drive.md never names")

    def test_the_goal_leg_must_name_sdk_cli(self):
        self.stage(goal="Run `scripts/close-worker.py` for a finished row.\n")
        self.assert_fails_naming("goal-drive.md never names `sdk-cli`")


class TestCommandRule(Base):
    def test_the_retired_rule_fails_even_with_the_script_named(self):
        self.stage(
            cmd="- **Close via `/supervisor:close` only.** `close-worker.py` is mentioned.\n"
        )
        self.assert_fails_naming("retired rule")

    def test_the_command_must_name_the_script(self):
        self.stage(cmd="- **Close via `/supervisor:close` only.**\n")
        self.assert_fails_naming("fleet-drive.md never names")


class TestReapContract(Base):
    def test_the_reap_contract_must_name_the_script(self):
        self.stage(reap="The reap is evidence plus the session's own close.\n")
        self.assert_fails_naming("reap.md never names")


if __name__ == "__main__":
    unittest.main()
