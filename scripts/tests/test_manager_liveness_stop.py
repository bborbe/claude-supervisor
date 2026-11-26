#!/usr/bin/env python3
"""Tests for the `manager-liveness.py --stop` guard.

A manager must not stand down while it still has live spawned workers: their permission
prompts park with it, and only it can release them. The guard reuses close-worker's join
(ledger + registry ∪ heartbeat) and refuses on `live-workers` or `liveness-unknown`; a
dead worker, or a caller with no session id, does not block.

Run: python3 -m unittest discover -s scripts/tests -v
"""

import importlib.util
import os
import unittest
from unittest import mock

_HERE = os.path.dirname(os.path.abspath(__file__))
_SCRIPTS = os.path.dirname(_HERE)
_spec = importlib.util.spec_from_file_location(
    "manager_liveness", os.path.join(_SCRIPTS, "manager-liveness.py")
)
ml = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ml)


class TestStopGuard(unittest.TestCase):
    def _stop(self, children, caller="mgr"):
        # An empty-string env value is falsy, so the guard is skipped when `caller` is None.
        env = {
            "CLAUDE_CODE_SESSION_ID": caller if caller else "",
            "CLAUDE_SESSION_ID": "",
        }
        with mock.patch.dict(os.environ, env, clear=False):
            with mock.patch.object(
                ml._close_worker, "read_live_spawned_workers", return_value=children
            ), mock.patch.object(ml, "write_atomic") as wa, mock.patch.object(
                ml, "migrate_legacy"
            ):
                rc = ml.stop("Some Subject", "goal")
        return rc, wa

    def test_a_live_worker_refuses_the_stand_down(self):
        rc, wa = self._stop(["w1"])
        self.assertEqual(rc, 1)
        wa.assert_not_called()

    def test_an_unreadable_ledger_refuses(self):
        rc, wa = self._stop(None)
        self.assertEqual(rc, 1)
        wa.assert_not_called()

    def test_a_dead_worker_still_stands_down(self):
        rc, wa = self._stop([])
        self.assertEqual(rc, 0)
        wa.assert_called()

    def test_no_caller_session_skips_the_guard(self):
        rc, wa = self._stop([], caller=None)
        self.assertEqual(rc, 0)
        wa.assert_called()


if __name__ == "__main__":
    unittest.main()
