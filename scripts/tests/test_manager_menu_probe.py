#!/usr/bin/env python3
"""SC2's probe, wired into the suite.

⚠️ **Why this is a test wrapper and not a `check-*` Makefile target.** Every `check-*`
target in this repo's `precommit` is a static assertion over the tree; this one runs
subprocesses, builds temp fixtures and takes seconds. `make test` is the target that
already runs the dynamic suites, `precommit` depends on `test`, and `unittest discover`
picks a `test_*.py` up with no wiring at all — so the probe's falsification runs in CI by
virtue of existing, which is the property that keeps it from being deleted.

⚠️ What is asserted here is the PROBE, not the guard. The guard has its own suite
(`test_manager_menu_guard.py`); what this file establishes is that `manager-menu-probe.py`
is capable of going red — a probe that has never been red is not known to be capable of
red, and a probe that always reported green would satisfy its own criterion trivially.
"""

from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
PROBE = HERE.parent / "manager-menu-probe.py"
FIXTURE = HERE / "manager_menu_probe_fixture.py"


def run(script: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(script), *args],
        capture_output=True, text=True, timeout=600,
    )


class TestProbeIsRedCapable(unittest.TestCase):

    def test_the_probe_passes_on_its_own_fixture(self):
        """The control the falsification fixture also opens with, asserted here too: a
        probe that failed on an unmutated fixture would satisfy every red case below for
        the wrong reason."""
        proc = run(PROBE)
        self.assertEqual(proc.returncode, 0,
                         f"probe did not pass its own fixture:\n{proc.stdout}\n{proc.stderr}")
        self.assertIn("PASSED", proc.stdout)

    def test_every_mutation_of_the_fixture_makes_the_probe_go_red(self):
        proc = run(FIXTURE)
        self.assertEqual(proc.returncode, 0,
                         f"the probe is not known to be red-capable:\n"
                         f"{proc.stdout}\n{proc.stderr}")
        self.assertIn("PASSED", proc.stdout)
        # ⚠️ A fixture that ran its control and no red case would pass the exit code
        # alone; the count is what says the mutations actually executed.
        self.assertIn("control + 3 red cases", proc.stdout)

    def test_the_probe_refuses_a_root_that_is_not_a_built_fixture(self):
        """A probe that silently rebuilt the tree it was pointed at would overwrite every
        mutation and report a green that means nothing."""
        proc = run(PROBE, "--fixture-root", str(HERE))
        self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
        self.assertIn("REFUSED", proc.stdout)


if __name__ == "__main__":
    unittest.main()
