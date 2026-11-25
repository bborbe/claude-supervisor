#!/usr/bin/env python3
"""Tests for scripts/topicless-goal-keepalive.py — the retired topicless-goal leg.

The leg is retired: the goal-manager tier is merged into the topic tier (2026-11-24), so
there is no goal manager for a topicless goal to keep alive — the fleet round drives the
goal's rows directly through `agents/goal-drive.md`.

What remains is a refusal contract, and these tests pin it:

  * it exits non-zero and says `retired` on every invocation — a caller that still runs it
    must stop loudly, never silently select or resume anything;
  * it prints the reason, naming the merge and the replacement, so the caller's log shows a
    decision rather than a bare failure;
  * it accepts the old `--goals-dir` / `--topics-dir` flags so an older caller gets the
    retirement message instead of an argparse error that looks like a broken install;
  * it still refuses to run without `--vault` (argparse), the one contract it keeps.
"""

import importlib.util
import io
import pathlib
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout

_HERE = pathlib.Path(__file__).resolve().parent


def load():
    spec = importlib.util.spec_from_file_location(
        "topicless_goal_keepalive", str(_HERE.parent / "topicless-goal-keepalive.py")
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class RetiredLegTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.vault = pathlib.Path(self.tmp.name) / "vault"
        self.vault.mkdir(parents=True)
        self.leg = load()

    def run_leg(self, argv=()):
        out = io.StringIO()
        with redirect_stdout(out):
            rc = self.leg.main(["--vault", str(self.vault), *argv])
        return rc, out.getvalue().splitlines()

    def test_the_leg_is_retired(self):
        rc, lines = self.run_leg()
        self.assertEqual(1, rc)
        self.assertEqual("retired", lines[0])

    def test_it_names_the_merge_and_the_replacement(self):
        _, lines = self.run_leg()
        self.assertIn("merged into the topic tier", lines[1])
        self.assertIn("agents/goal-drive.md", lines[1])

    def test_it_selects_and_resumes_nothing(self):
        """The retired leg must not print a population — `selected:` would read as a run."""
        _, lines = self.run_leg()
        self.assertNotIn("selected:", "\n".join(lines))
        self.assertNotIn("actionable:", "\n".join(lines))

    def test_the_old_directory_flags_are_still_accepted(self):
        """An older caller passes the flags; it must get the retirement message, not an
        argparse error that reads like a broken install."""
        rc, lines = self.run_leg(["--goals-dir", "24 Goals", "--topics-dir", "23 Topics"])
        self.assertEqual(1, rc)
        self.assertEqual("retired", lines[0])

    def test_it_still_requires_a_vault(self):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err), \
                self.assertRaises(SystemExit) as ctx:
            self.leg.main([])
        self.assertEqual(2, ctx.exception.code)


if __name__ == "__main__":
    unittest.main()
