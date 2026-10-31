#!/usr/bin/env python3
"""`/supervisor:stop` must mark the stand-down under the branch the loop ARMED.

The defect this pins (measured 2026-10-31): step 2b ran `manager-liveness.py --stop --topic
"<subject>"` with no `--branch`. For a subject with no page — the Fleet Manager — the script
defaulted the branch to `goal` and wrote `goal--fleet-manager.stopped`, while the fleet arms
under `fleet--fleet-manager`. Nothing read the marker, so every fleet stop read as a lapse and
the fleet keeper would restart a fleet the operator had stood down.

Two guards: the command text must pass `--branch` on its stop call, and the shape it
prescribes must round-trip through the real script — the marker it writes is the one the
keeper's read returns as STOPPED.
"""

import os
import pathlib
import re
import subprocess
import sys
import tempfile
import unittest

_REPO = pathlib.Path(__file__).resolve().parent.parent.parent
_STOP_MD = _REPO / "commands" / "stop.md"
_LIVENESS = _REPO / "scripts" / "manager-liveness.py"

STOP_CALL = re.compile(r"manager-liveness\.py --stop\b[^\n`]*")


class StopCommandPassesTheBranchTest(unittest.TestCase):
    def test_every_stop_call_in_the_command_passes_a_branch(self):
        calls = STOP_CALL.findall(_STOP_MD.read_text(encoding="utf-8"))
        self.assertTrue(calls, "commands/stop.md carries no manager-liveness --stop call at all")
        for call in calls:
            self.assertIn("--branch", call, f"stop call leaves the branch to default: {call}")

    def test_the_old_branchless_form_is_gone(self):
        """Positive control for the regex above: the defective shape it must reject."""
        self.assertNotIn("--stop --topic", _STOP_MD.read_text(encoding="utf-8"))


class StopRoundTripTest(unittest.TestCase):
    """The prescribed shape, run through the REAL script against a temp state dir."""

    def run_liveness(self, env, *args):
        return subprocess.run([sys.executable, str(_LIVENESS), *args],
                              capture_output=True, text=True, timeout=30, env=env)

    def test_a_fleet_stop_is_read_back_as_stopped_under_the_armed_slug(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = dict(os.environ, MANAGER_LIVENESS_STATE_DIR=tmp)
            self.run_liveness(env, "--arm", "--subject", "Fleet Manager", "--branch", "fleet",
                              "--interval", "900")
            self.run_liveness(env, "--stop", "--subject", "Fleet Manager", "--branch", "fleet")
            state = self.run_liveness(env, "--state", "--subject", "Fleet Manager",
                                      "--branch", "fleet").stdout.split("\t")[0]
            self.assertEqual("STOPPED", state)
            self.assertTrue((pathlib.Path(tmp) / "fleet--fleet-manager.stopped").exists())

    def test_the_branchless_form_misses_the_fleet(self):
        """Documents the defect: the old call defaults to `goal` and the fleet never sees it."""
        with tempfile.TemporaryDirectory() as tmp:
            env = dict(os.environ, MANAGER_LIVENESS_STATE_DIR=tmp)
            self.run_liveness(env, "--stop", "--topic", "Fleet Manager")
            state = self.run_liveness(env, "--state", "--subject", "Fleet Manager",
                                      "--branch", "fleet").stdout.split("\t")[0]
            self.assertNotEqual("STOPPED", state)


if __name__ == "__main__":
    unittest.main()
