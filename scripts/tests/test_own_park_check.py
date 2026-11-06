#!/usr/bin/env python3
"""Fixtures for `scripts/own-park-check.py` — every refusal branch, and the one OWN branch.

Also pins the own-parks step file: its fail-closed preconditions and its explicit C–E list,
so a later edit that collapses the boundary to a catch-all fails here.

Run: python3 -m unittest discover -s scripts/tests -v
"""

import importlib.util
import io
import os
import unittest
from contextlib import redirect_stdout

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(os.path.dirname(_HERE))
_SCRIPT = os.path.join(os.path.dirname(_HERE), "own-park-check.py")

_spec = importlib.util.spec_from_file_location("own_park_check", _SCRIPT)
opc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(opc)

SELF = "7253a6aa-0000-4000-8000-000000000001"
OTHER = "3052a109-94b8-4984-96e5-d1d09721466d"
WORKER = "7619ab7a-9344-4165-81da-d604cba38918"


class Verdict(unittest.TestCase):
    def test_own_via_wire_name(self):
        self.assertEqual(
            opc.verdict(SELF, WORKER, {WORKER: {"spawner_session_id": SELF}}),
            (True, "spawned by this session"),
        )

    def test_own_via_file_name(self):
        own, _ = opc.verdict(SELF, WORKER, {WORKER: {"parent_session": SELF}})
        self.assertTrue(own)

    def test_refuse_other_spawner(self):
        own, reason = opc.verdict(SELF, WORKER, {WORKER: {"spawner_session_id": OTHER}})
        self.assertFalse(own)
        self.assertIn("not this session", reason)

    def test_refuse_unreadable_ledger(self):
        self.assertEqual(opc.verdict(SELF, WORKER, None), (False, "spawn ledger unreadable"))

    def test_refuse_missing_row(self):
        self.assertFalse(opc.verdict(SELF, WORKER, {})[0])

    def test_refuse_null_spawner(self):
        self.assertFalse(opc.verdict(SELF, WORKER, {WORKER: {"parent_session": None}})[0])

    def test_refuse_empty_self(self):
        self.assertFalse(opc.verdict("", WORKER, {WORKER: {"parent_session": ""}})[0])

    def test_refuse_empty_worker(self):
        self.assertFalse(opc.verdict(SELF, "", {"": {"parent_session": SELF}})[0])

    def test_refuse_empty_string_wire_spawner(self):
        # the endpoint serves `spawner_session_id: ""` for an unresolved spawn edge
        own, reason = opc.verdict(SELF, WORKER, {WORKER: {"spawner_session_id": ""}})
        self.assertFalse(own)
        self.assertEqual(reason, "ledger row names no spawner")


class Main(unittest.TestCase):
    def _run(self, ledger, argv):
        class Fake:
            @staticmethod
            def read_ledger(endpoint=None):
                return ledger

        orig = opc._spawn_ledger
        opc._spawn_ledger = lambda: Fake
        buf = io.StringIO()
        try:
            with redirect_stdout(buf):
                code = opc.main(argv)
        finally:
            opc._spawn_ledger = orig
        return code, buf.getvalue().splitlines()

    def test_unreadable_ledger_exits_2_and_refuses_all(self):
        code, lines = self._run(None, ["--self", SELF, "--session", WORKER])
        self.assertEqual(code, 2)
        self.assertEqual(lines, ["REFUSE %s spawn ledger unreadable" % WORKER])

    def test_no_self_exits_2(self):
        code, lines = self._run({WORKER: {"parent_session": ""}}, ["--session", WORKER])
        self.assertEqual(code, 2)
        self.assertTrue(lines[0].startswith("REFUSE"))

    def test_empty_ledger_is_read_but_refuses(self):
        code, lines = self._run({}, ["--self", SELF, "--session", WORKER])
        self.assertEqual(code, 0)
        self.assertEqual(lines, ["REFUSE %s no spawn-ledger row for this worker" % WORKER])

    def test_no_sessions_prints_nothing(self):
        code, lines = self._run({WORKER: {"parent_session": SELF}}, ["--self", SELF])
        self.assertEqual((code, lines), (0, []))

    def test_mixed_batch(self):
        ledger = {WORKER: {"spawner_session_id": SELF}, OTHER: {"spawner_session_id": WORKER}}
        code, lines = self._run(ledger, ["--self", SELF, "--session", WORKER, "--session", OTHER])
        self.assertEqual(code, 0)
        self.assertEqual(lines[0], "OWN %s" % WORKER)
        self.assertTrue(lines[1].startswith("REFUSE %s" % OTHER))


class StepFile(unittest.TestCase):
    """The step's boundary is written as an explicit list, and it fails closed."""

    def setUp(self):
        with open(os.path.join(_ROOT, "commands", "steps", "own-parks.md"), encoding="utf-8") as fh:
            self.text = fh.read()

    def test_lists_c_d_e_explicitly(self):
        for row in ("| **C** |", "| **D** |", "| **E** |"):
            self.assertIn(row, self.text)

    def test_no_catch_all_boundary(self):
        self.assertNotIn("everything else", self.text.lower())

    def test_fail_closed_preconditions(self):
        self.assertIn("`answer_permission` is unavailable", self.text)
        self.assertIn("own-park-check.py", self.text)
        self.assertIn("do nothing", self.text.lower())

    def test_router_row_present(self):
        with open(os.path.join(_ROOT, "commands", "fleet-drive.md"), encoding="utf-8") as fh:
            self.assertIn("commands/steps/own-parks.md", fh.read())

    def test_wrangler_honours_scope(self):
        with open(os.path.join(_ROOT, "agents", "goal-wrangler.md"), encoding="utf-8") as fh:
            text = fh.read()
        self.assertIn("Answer only these request_ids:", text)
        self.assertIn("Never** call `await_permission`", text)
        # scoped mode must still hold-check, keyed on the caller-supplied session id
        self.assertIn("The hold check still runs", text)
        self.assertIn("<request_id>=<session_id>", text)
        self.assertIn("<request_id>=<session_id>", self.text)

    def test_any_nonzero_exit_skips(self):
        self.assertIn("Any non-zero exit", self.text)


if __name__ == "__main__":
    unittest.main()
