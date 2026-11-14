#!/usr/bin/env python3
"""Tests for restart-worker.resolve_owned_target — the owning-pid rule.

The rule exists because several registry entries for one session id is not
automatically a refusal: a shell wrapper and the child it launched are one
logical process, and refusing there blinds every close verb to the ordinary
shape of a supervised session. These tests pin the ordering AND the refusal —
a rule that resolves everything is as wrong as one that refuses everything.

The branch logic is exercised with injected readings, because a dead pid and an
unrelated live pid are states a test cannot reliably produce. `is_ancestor`
itself is exercised against a real parent/child pair in its own test, so the
mocking never stands in for the process-tree read.

Run: python3 -m unittest discover -s scripts/tests -v
"""

import importlib.util
import os
import signal
import subprocess
import time
import unittest
from unittest import mock

_HERE = os.path.dirname(os.path.abspath(__file__))
_SCRIPTS = os.path.dirname(_HERE)
_spec = importlib.util.spec_from_file_location(
    "restart_worker", os.path.join(_SCRIPTS, "restart-worker.py")
)
restart_worker = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(restart_worker)

SID = "dddddddd-4444-4444-8444-444444444444"


def entry(pid, sid=SID):
    """A `(path, record)` registry pair, shaped as the verbs receive them."""
    return (f"/sessions/{pid}.json", {"sessionId": sid, "pid": pid})


class ResolveOwnedTargetTest(unittest.TestCase):
    def test_no_entry_claims_the_id(self):
        self.assertEqual(
            restart_worker.resolve_owned_target([entry(1, sid="another")], SID),
            (None, None, None),
        )

    def test_one_entry_wins_without_a_note(self):
        _path, rec, note = restart_worker.resolve_owned_target([entry(4242)], SID)
        self.assertEqual(rec["pid"], 4242)
        self.assertIsNone(note)

    def test_the_only_live_pid_wins_and_is_named(self):
        with mock.patch.object(
            restart_worker, "pid_alive", side_effect=lambda p: p == 4242
        ):
            _path, rec, note = restart_worker.resolve_owned_target(
                [entry(1111), entry(4242)], SID
            )
        self.assertEqual(rec["pid"], 4242)
        self.assertIn("4242", note)

    def test_a_descendant_wins_over_its_ancestor_and_is_named(self):
        with mock.patch.object(restart_worker, "pid_alive", return_value=True), (
            mock.patch.object(
                restart_worker,
                "is_ancestor",
                side_effect=lambda a, d: (a, d) == (1111, 4242),
            )
        ):
            _path, rec, note = restart_worker.resolve_owned_target(
                [entry(1111), entry(4242)], SID
            )
        self.assertEqual(rec["pid"], 4242)
        self.assertIn("4242", note)

    def test_two_unrelated_live_pids_still_refuse(self):
        with mock.patch.object(restart_worker, "pid_alive", return_value=True), (
            mock.patch.object(restart_worker, "is_ancestor", return_value=False)
        ):
            path, _rec, note = restart_worker.resolve_owned_target(
                [entry(1111), entry(4242)], SID
            )
        self.assertEqual(path, "ambiguous")
        self.assertIsNone(note)

    def test_no_live_pid_refuses(self):
        with mock.patch.object(restart_worker, "pid_alive", return_value=False):
            path, _rec, _note = restart_worker.resolve_owned_target(
                [entry(1111), entry(4242)], SID
            )
        self.assertEqual(path, "ambiguous")

    def test_a_live_pid_beside_a_live_chain_still_refuses(self):
        # 1111 -> 4242 is a chain and 9999 is unrelated, so the id has two owners.
        with mock.patch.object(restart_worker, "pid_alive", return_value=True), (
            mock.patch.object(
                restart_worker,
                "is_ancestor",
                side_effect=lambda a, d: (a, d) == (1111, 4242),
            )
        ):
            path, _rec, _note = restart_worker.resolve_owned_target(
                [entry(1111), entry(4242), entry(9999)], SID
            )
        self.assertEqual(path, "ambiguous")


class IsAncestorTest(unittest.TestCase):
    def test_reads_a_real_parent_child_pair(self):
        # Its own session, so cleanup can kill the group: killing the shell alone
        # leaves the `sleep` orphaned for the rest of its run.
        shell = subprocess.Popen(
            ["sh", "-c", "sleep 30 & wait"], start_new_session=True
        )
        try:
            child = None
            for _ in range(50):
                found = subprocess.run(
                    ["pgrep", "-P", str(shell.pid)], capture_output=True, text=True
                ).stdout.split()
                if found:
                    child = int(found[0])
                    break
                time.sleep(0.05)
            self.assertIsNotNone(child, "no child appeared under the shell")
            self.assertTrue(restart_worker.is_ancestor(shell.pid, child))
            self.assertFalse(restart_worker.is_ancestor(child, shell.pid))
        finally:
            os.killpg(shell.pid, signal.SIGKILL)
            shell.wait()


if __name__ == "__main__":
    unittest.main()
