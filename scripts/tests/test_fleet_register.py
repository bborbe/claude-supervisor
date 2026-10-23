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
import tempfile
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_SCRIPT = os.path.join(os.path.dirname(_HERE), "fleet-register.py")


def load():
    spec = importlib.util.spec_from_file_location("fleet_register", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class FleetRegister(unittest.TestCase):
    def setUp(self):
        self.mod = load()
        self.dir = tempfile.mkdtemp()

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


if __name__ == "__main__":
    unittest.main()
