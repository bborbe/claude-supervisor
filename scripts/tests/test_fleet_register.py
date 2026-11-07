#!/usr/bin/env python3
"""Tests for scripts/fleet-register.py.

The load-bearing property: it writes the ONE file `scripts/answer-path-check.py` gates on,
with the subject and branch that check expects. A cadence-only fix shipped once and did not
work — the probe refuses at the registration gate before it asks about liveness — so the
payload shape is pinned here rather than left to the command body.

Run: python3 -m unittest discover -s scripts/tests -v
"""

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_SCRIPTS = os.path.dirname(_HERE)
_SCRIPT = os.path.join(_SCRIPTS, "fleet-register.py")
_PROBE = os.path.join(_SCRIPTS, "answer-path-check.py")


def load():
    spec = importlib.util.spec_from_file_location("fleet_register", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def load_probe(manager_dir):
    """The REAL consumer, pointed at the same registration dir."""
    os.environ["WORKER_MANAGER_STATE_DIR"] = manager_dir
    os.environ.pop("SUPERVISOR_WORKER_MANAGER_DIR", None)
    spec = importlib.util.spec_from_file_location("answer_path_check", _PROBE)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class FleetRegister(unittest.TestCase):
    # Every env var this class touches, snapshotted once and restored after each test — the
    # probe loader sets two and the arm tests set a third, and leaving them mutated makes the
    # suite order-dependent for any later module that resolves a path at import.
    ENV_KEYS = (
        "WORKER_MANAGER_STATE_DIR",
        "SUPERVISOR_WORKER_MANAGER_DIR",
        "MANAGER_LIVENESS_STATE_DIR",
        "CLAUDE_CODE_SESSION_ID",
    )

    def setUp(self):
        self._saved_env = {key: os.environ.get(key) for key in self.ENV_KEYS}
        self.mod = load()
        self.dir = tempfile.mkdtemp()

    def tearDown(self):
        for key, value in self._saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def test_writes_the_registration_answer_path_check_reads(self):
        path = self.mod.write_registration("sid-1", "private-personal", directory=self.dir)
        self.assertEqual(path, os.path.join(self.dir, "sid-1.json"))
        with open(path, encoding="utf-8") as handle:
            payload = json.load(handle)
        self.assertEqual(payload["subject"], "Fleet Manager")
        self.assertEqual(payload["branch"], "fleet")
        self.assertEqual(payload["vault"], "private-personal")
        self.assertTrue(payload["resolved_at"])

    def test_rewriting_is_idempotent(self):
        first = self.mod.write_registration("sid-1", "v", directory=self.dir)
        second = self.mod.write_registration("sid-1", "v", directory=self.dir)
        self.assertEqual(first, second)
        self.assertEqual(len(os.listdir(self.dir)), 1)

    def test_a_session_id_that_is_not_a_bare_filename_is_refused(self):
        for bad in ("../escape", "a/b", "..", ".", "", "/etc/passwd", "a\\b"):
            with self.subTest(session_id=bad):
                with self.assertRaises(ValueError):
                    self.mod.write_registration(bad, "v", directory=self.dir)
        self.assertEqual(os.listdir(self.dir), [], "a refused id must write nothing")

    def test_the_subject_and_branch_are_the_ones_the_probe_expects(self):
        # `answer-path-check.py` passes these straight to `manager-liveness.py --state`, whose
        # BRANCHES must contain the value — a rename on either side breaks the pass direction
        # silently, which is exactly how the cadence-only fix failed.
        self.assertEqual(self.mod.SUBJECT, "Fleet Manager")
        self.assertEqual(self.mod.BRANCH, "fleet")

    def test_the_written_file_satisfies_the_real_probe(self):
        # ⚠️ THE SEAM THIS PR EXISTS TO PROVE, and the one the review asked for. Pinning both
        # sides to the same literals proves nothing: a payload-shape change here would leave
        # both suites green while the probe refused every fleet spawn — the failure this PR
        # repairs. So: run the real writer, then the real consumer.
        self.mod.write_registration("sid-1", "v", directory=self.dir)
        probe = load_probe(self.dir)
        state = tempfile.mkdtemp()
        os.environ["MANAGER_LIVENESS_STATE_DIR"] = state
        try:
            subprocess.run(
                [sys.executable, probe.LIVENESS, "--arm", "--subject", "Fleet Manager",
                 "--branch", "fleet", "--interval", "900"],
                check=True, capture_output=True,
            )
            ok, subject, branch, detail = probe.check("sid-1")
            self.assertTrue(ok, detail)
            self.assertEqual((subject, branch), ("Fleet Manager", "fleet"))
        finally:
            os.environ.pop("MANAGER_LIVENESS_STATE_DIR", None)

    def test_the_cli_writes_the_registration_and_exits_zero(self):
        env = dict(os.environ, CLAUDE_CODE_SESSION_ID="sid-cli", WORKER_MANAGER_STATE_DIR=self.dir)
        env.pop("SUPERVISOR_WORKER_MANAGER_DIR", None)
        done = subprocess.run(
            [sys.executable, _SCRIPT, "--vault", "v"], capture_output=True, text=True, env=env
        )
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertIn("registered", done.stdout)
        self.assertTrue(os.path.exists(os.path.join(self.dir, "sid-cli.json")))

    def test_the_cli_refuses_without_a_session_id(self):
        env = dict(os.environ, WORKER_MANAGER_STATE_DIR=self.dir)
        env.pop("CLAUDE_CODE_SESSION_ID", None)
        env.pop("SUPERVISOR_WORKER_MANAGER_DIR", None)
        done = subprocess.run([sys.executable, _SCRIPT], capture_output=True, text=True, env=env)
        self.assertEqual(done.returncode, 1)
        self.assertTrue(done.stdout.startswith("REFUSED\t"))
        self.assertEqual(os.listdir(self.dir), [], "a refused run must write nothing")


if __name__ == "__main__":
    unittest.main()
