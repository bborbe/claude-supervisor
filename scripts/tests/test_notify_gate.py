#!/usr/bin/env python3
"""Tests for scripts/notify-gate.py.

Covers the cadence decision, which is the whole reason the script exists: without
it the manager loop would put one open gate on the phone roughly 96 times a day.

Three cases carry the weight, each against a specific way the bound could be wrong:

  * a gate that stays open must re-raise at 1h and 4h and then STOP. A bound that
    re-raises but never stops is the noise this exists to prevent; a bound that
    stops too early is a gate the operator never hears about again.
  * a gate whose TEXT changes must count as a new identity. Otherwise a worker
    rephrasing its question is silently swallowed -- the question changed and the
    operator should hear it.
  * whitespace alone must NOT change the identity. A re-rendered line is the same
    gate, and treating it as new would defeat the cap entirely.

The constants are asserted directly as well: the cadence is a decided contract
(2026-09-19), so a later edit that widens it should have to change a test.

Run: python3 -m unittest discover -s scripts/tests -v
"""

import importlib.util
import json
import os
import shutil
import tempfile
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_SCRIPT = os.path.join(os.path.dirname(_HERE), "notify-gate.py")

_spec = importlib.util.spec_from_file_location("notify_gate", _SCRIPT)
notify_gate = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(notify_gate)

RAISED_AT = "2026-09-19T10:00:00Z"


def at(value):
    return notify_gate.parse_time(value)


def entry(deliveries):
    return {"deliveries": deliveries, "firstRaisedAt": RAISED_AT}


class Cadence(unittest.TestCase):
    def test_first_sight_delivers(self):
        self.assertTrue(notify_gate.decide(None, at(RAISED_AT)))

    def test_next_sweep_is_suppressed(self):
        self.assertFalse(notify_gate.decide(entry(1), at("2026-09-19T10:30:00Z")))

    def test_re_raises_at_one_hour(self):
        self.assertTrue(notify_gate.decide(entry(1), at("2026-09-19T11:00:00Z")))

    def test_second_re_raise_waits_until_four_hours(self):
        self.assertFalse(notify_gate.decide(entry(2), at("2026-09-19T13:00:00Z")))

    def test_second_re_raise_fires_at_four_hours(self):
        self.assertTrue(notify_gate.decide(entry(2), at("2026-09-19T14:00:00Z")))

    def test_cap_stops_a_gate_that_stays_open_all_day(self):
        self.assertFalse(notify_gate.decide(entry(3), at("2026-09-20T10:00:00Z")))

    def test_cadence_is_the_decided_contract(self):
        self.assertEqual(notify_gate.RE_RAISE_AFTER_SECONDS, (3600, 14400))
        self.assertEqual(notify_gate.MAX_DELIVERIES, 3)
        self.assertEqual(notify_gate.DEFAULT_TYPE, "pending-approval")


class Identity(unittest.TestCase):
    def test_whitespace_alone_is_the_same_gate(self):
        self.assertEqual(
            notify_gate.gate_key("pane-243", "approve:  make apply"),
            notify_gate.gate_key("pane-243", "approve: make apply\n"),
        )

    def test_a_changed_question_is_a_new_gate(self):
        self.assertNotEqual(
            notify_gate.gate_key("pane-243", "approve: make apply"),
            notify_gate.gate_key("pane-243", "approve: make upgrade"),
        )

    def test_the_same_gate_on_another_pane_is_a_new_gate(self):
        self.assertNotEqual(
            notify_gate.gate_key("pane-243", "approve: make apply"),
            notify_gate.gate_key("pane-251", "approve: make apply"),
        )


class Message(unittest.TestCase):
    def test_names_the_gate_and_its_owner(self):
        text = notify_gate.render_message(
            {"owner": "pane-243", "text": "approve: make apply"}
        )
        self.assertIn("pane-243", text)
        self.assertIn("approve: make apply", text)

    def test_never_presents_itself_as_answerable(self):
        text = notify_gate.render_message(
            {"owner": "pane-243", "text": "approve: make apply"}
        )
        self.assertIn("not here", text)
        self.assertNotIn("reply", text.lower())


class CurlConfig(unittest.TestCase):
    def test_escapes_quote_and_backslash(self):
        self.assertEqual(
            notify_gate.curl_config_user('a"b', "c\\d"),
            'user = "a\\"b:c\\\\d"\n',
        )

    def test_plain_credentials_pass_through(self):
        self.assertEqual(
            notify_gate.curl_config_user("user", "secret"),
            'user = "user:secret"\n',
        )


def _reload(suffix):
    """A fresh module instance, so STATE_PATH re-reads the current env."""
    spec = importlib.util.spec_from_file_location(f"notify_gate_{suffix}", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Ledger(unittest.TestCase):
    """commit() and load_ledger(), which the pure-function tests above cannot reach.

    Pruning is load-bearing -- it is what makes the delivery bound per-gate rather
    than per-lifetime, and both manager commands depend on it when they instruct a
    `{"gates": []}` sweep -- so it gets real coverage rather than a promise.
    """

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "gate-notifications.json")
        self._before = os.environ.get("SUPERVISOR_GATE_STATE")
        os.environ["SUPERVISOR_GATE_STATE"] = self.path
        self.mod = _reload(self._testMethodName)

    def tearDown(self):
        if self._before is None:
            os.environ.pop("SUPERVISOR_GATE_STATE", None)
        else:
            os.environ["SUPERVISOR_GATE_STATE"] = self._before
        shutil.rmtree(self.dir, ignore_errors=True)

    def read(self):
        with open(self.path) as handle:
            return json.load(handle)

    def test_commit_prunes_a_gate_that_cleared(self):
        key = self.mod.gate_key("pane-243", "approve: make apply")
        self.mod.commit({key: {"deliveries": 2, "firstRaisedAt": RAISED_AT}}, {}, {})
        self.assertEqual(self.read(), {})

    def test_commit_keeps_a_gate_that_is_still_open(self):
        key = self.mod.gate_key("pane-243", "approve: make apply")
        self.mod.commit(
            {key: {"deliveries": 2, "firstRaisedAt": RAISED_AT}},
            {key: {"owner": "pane-243", "text": "approve: make apply"}},
            {},
        )
        self.assertEqual(self.read()[key]["deliveries"], 2)

    def test_a_cleared_gate_raises_again_at_full_cadence(self):
        key = self.mod.gate_key("pane-243", "approve: make apply")
        self.mod.commit({key: {"deliveries": 3, "firstRaisedAt": RAISED_AT}}, {}, {})
        self.assertIsNone(self.mod.load_ledger().get(key))
        self.assertTrue(self.mod.decide(self.mod.load_ledger().get(key), at(RAISED_AT)))

    def test_an_unreadable_ledger_reads_as_empty(self):
        with open(self.path, "w") as handle:
            handle.write("{not json")
        self.assertEqual(self.mod.load_ledger(), {})

    def test_commit_leaves_no_temp_file_behind(self):
        self.mod.commit({}, {}, {})
        self.assertEqual([f for f in os.listdir(self.dir) if f.endswith(".tmp")], [])


if __name__ == "__main__":
    unittest.main()
