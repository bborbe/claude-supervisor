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

import contextlib
import importlib.util
import io
import json
import os
import shutil
import sys
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


class _Harness(unittest.TestCase):
    """A module instance wired to temp config and ledger paths.

    Both env overrides exist for this: without them main()/commit() cannot run
    without touching the operator's real ledger, which is why these paths went
    untested until the local review called it out.
    """

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.state = os.path.join(self.dir, "ledger.json")
        self.config = os.path.join(self.dir, "config.json")
        with open(self.config, "w") as handle:
            json.dump(
                {
                    "notify": {
                        "env": "dev",
                        "endpoints": {
                            "dev": {
                                "baseUrl": "http://127.0.0.1:1",
                                "teamvaultKey": "unused-in-tests",
                            }
                        },
                    }
                },
                handle,
            )
        self._env = {
            key: os.environ.get(key)
            for key in ("SUPERVISOR_GATE_STATE", "SUPERVISOR_CONFIG")
        }
        os.environ["SUPERVISOR_GATE_STATE"] = self.state
        os.environ["SUPERVISOR_CONFIG"] = self.config
        self.mod = _reload(self._testMethodName)

    def tearDown(self):
        for key, value in self._env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        shutil.rmtree(self.dir, ignore_errors=True)

    def read(self):
        with open(self.state) as handle:
            return json.load(handle)

    def feed(self, raw):
        before = sys.stdin
        sys.stdin = io.StringIO(raw)
        try:
            # main() prints its round summary; capturing it keeps the suite's own
            # output readable instead of interleaving the script's.
            with contextlib.redirect_stdout(io.StringIO()):
                return self.mod.main()
        finally:
            sys.stdin = before


class Ledger(_Harness):
    """commit() and load_ledger(), which the pure-function tests above cannot reach.

    Pruning is load-bearing -- it is what makes the delivery bound per-gate rather
    than per-lifetime, and both manager commands depend on it when they instruct a
    `{"gates": []}` sweep -- so it gets real coverage rather than a promise.
    """

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
        with open(self.state, "w") as handle:
            handle.write("{not json")
        self.assertEqual(self.mod.load_ledger(), {})

    def test_commit_leaves_no_temp_file_behind(self):
        self.mod.commit({}, {}, {})
        self.assertEqual([f for f in os.listdir(self.dir) if f.endswith(".tmp")], [])

    def test_a_bare_filename_state_path_is_accepted(self):
        """SUPERVISOR_GATE_STATE=gate.json has no directory part to create."""
        self.mod.STATE_PATH = os.path.join(self.dir, "bare.json").replace(
            self.dir + "/", ""
        )
        cwd = os.getcwd()
        os.chdir(self.dir)
        try:
            self.mod.commit({}, {}, {})
        finally:
            os.chdir(cwd)
        self.assertTrue(os.path.exists(os.path.join(self.dir, "bare.json")))


class ReadGates(_Harness):
    """read_gates(), whose whole job is turning a hand-typed one-liner into an
    actionable message. Untested, it tracebacked on the two likeliest typos.
    """

    def parse(self, raw):
        before = sys.stdin
        sys.stdin = io.StringIO(raw)
        try:
            return self.mod.read_gates()
        finally:
            sys.stdin = before

    def message(self, raw):
        with self.assertRaises(SystemExit) as caught:
            self.parse(raw)
        return str(caught.exception)

    def test_a_well_formed_call_parses(self):
        self.assertEqual(
            self.parse('{"gates": [{"owner": "pane-1", "text": "approve: x"}]}'),
            [{"owner": "pane-1", "text": "approve: x"}],
        )

    def test_an_empty_list_is_a_clean_sweep(self):
        self.assertEqual(self.parse('{"gates": []}'), [])

    def test_a_bare_list_is_reported_not_tracebacked(self):
        self.assertIn("must be an object", self.message("[]"))

    def test_a_missing_gates_key_is_reported_not_read_as_empty(self):
        self.assertIn("no `gates` key", self.message("{}"))

    def test_a_non_object_gate_is_reported(self):
        self.assertIn("each gate must be an object", self.message('{"gates": ["x"]}'))

    def test_a_missing_field_is_reported(self):
        self.assertIn("missing `text`", self.message('{"gates": [{"owner": "p"}]}'))

    def test_invalid_json_is_reported(self):
        self.assertIn("not valid JSON", self.message("{not json"))


class FailureHandling(_Harness):
    """The paths the local review said were covered only by the commit message:
    a failed publish must not be recorded as delivered, and a mid-loop failure must
    not discard the deliveries that already succeeded.
    """

    def fail_on(self, nth):
        """Stub publish() so the nth call raises, after the earlier ones succeed."""
        calls = []

        def stub(base_url, teamvault_key, message, notification_type):
            calls.append(message)
            if len(calls) == nth:
                raise SystemExit("notify-gate: publish failed: synthetic")
            return "ok"

        self.mod.publish = stub
        return calls

    def test_a_failed_publish_is_not_recorded_as_delivered(self):
        self.fail_on(1)
        with self.assertRaises(SystemExit):
            self.feed('{"gates": [{"owner": "pane-1", "text": "approve: x"}]}')
        self.assertEqual(self.mod.load_ledger(), {})

    def test_a_later_failure_keeps_the_earlier_delivery(self):
        self.fail_on(2)
        with self.assertRaises(SystemExit):
            self.feed(
                '{"gates": [{"owner": "pane-1", "text": "approve: a"},'
                ' {"owner": "pane-2", "text": "approve: b"}]}'
            )
        ledger = self.mod.load_ledger()
        self.assertEqual(len(ledger), 1, "the successful publish must survive")
        self.assertEqual(list(ledger.values())[0]["deliveries"], 1)

    def test_a_clean_sweep_prunes_without_publishing(self):
        self.fail_on(1)
        self.feed('{"gates": []}')
        self.assertEqual(self.mod.load_ledger(), {})


if __name__ == "__main__":
    unittest.main()
