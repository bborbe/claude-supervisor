#!/usr/bin/env python3
"""Tests for scripts/permission-answer-poll.py.

Each case pins a property measured on a live gate on 2026-09-27:

  * verdict -- only an answered card carrying allow/deny AND resolved_by counts.
  * resolver -- matches only THIS gate's permission card. A stale card left open
    by an earlier, already-answered gate of the same session must be refused;
    answering it would release the current gate under another's subject.
  * gate race -- attention-log.py runs in parallel, so a gate not yet in the
    feed is waited for, never read as closed.
  * fail-open -- a deadline, an error or the kill switch emits nothing on stdout.

Run: python3 -m unittest discover -s scripts/tests -v
"""

import importlib.util
import io
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
PATH = os.path.join(HERE, "..", "permission-answer-poll.py")


def load():
    spec = importlib.util.spec_from_file_location("permission_answer_poll", PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def iso(ts):
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def card(item_id, payload="Write: /tmp/x", mech="permission", state="open", created=None, producer="S"):
    return {"item_id": item_id, "producer_id": producer, "answer_mechanism": mech,
            "state": state, "payload": payload, "created_at": iso(created or time.time())}


class VerdictTest(unittest.TestCase):
    def setUp(self):
        self.m = load()

    def test_only_an_arm_answer_counts(self):
        cases = [
            ({"state": "open", "decision": "allow", "resolved_by": "s"}, False),
            ({"state": "answered", "decision": None, "resolved_by": "s"}, False),
            ({"state": "answered", "decision": "maybe", "resolved_by": "s"}, False),
            ({"state": "answered", "decision": "allow"}, False),
            ({"state": "answered", "decision": "deny", "resolved_by": ""}, False),
            ({"state": "answered", "decision": "allow", "resolved_by": "s"}, True),
            ({"state": "answered", "decision": "deny", "resolved_by": "s"}, True),
            (None, False),
        ]
        for item, want in cases:
            with self.subTest(item=item):
                self.assertEqual(self.m.verdict(item)[0], want)

    def test_emit_is_the_documented_shape(self):
        out = io.StringIO()
        with redirect_stdout(out):
            self.m.emit("allow")
        self.assertEqual(json.loads(out.getvalue()), {
            "hookSpecificOutput": {"hookEventName": "PermissionRequest", "decision": {"behavior": "allow"}}})


class ResolverTest(unittest.TestCase):
    def setUp(self):
        self.m = load()

    def resolve(self, items, expect=None, not_before=None):
        self.m.get_json = lambda url: (True, items)
        return self.m.resolve_item_for_session("store", "S", expect, not_before)

    def test_message_card_is_never_a_permission_answer(self):
        self.assertFalse(self.resolve([card("m1", mech="message")])[0])

    def test_single_permission_card_matches(self):
        self.assertEqual(self.resolve([card("p1"), card("m1", mech="message")]), (True, "p1"))

    def test_two_permission_cards_are_ambiguous(self):
        self.assertFalse(self.resolve([card("p1"), card("p2")])[0])

    def test_stale_card_for_another_gate_is_refused(self):
        # Measured: an in-pane approval left its card `open` for 9+ minutes.
        stale = card("old", payload="Write: /tmp/z.txt")
        self.assertFalse(self.resolve([stale], expect="Write: /tmp/w.txt")[0])

    def test_card_older_than_the_hook_is_refused(self):
        now = time.time()
        old = card("old", payload="Write: /tmp/w.txt", created=now - 600)
        self.assertFalse(self.resolve([old], expect="Write: /tmp/w.txt", not_before=now)[0])
        # Control: the same card passes with an older floor, so the filter is not refuse-all.
        self.assertEqual(self.resolve([old], expect="Write: /tmp/w.txt", not_before=now - 3600), (True, "old"))

    def test_other_producers_are_ignored(self):
        self.assertFalse(self.resolve([card("p1", producer="OTHER")])[0])


class RunTest(unittest.TestCase):
    """Drive main() through its hook entry point (JSON on stdin)."""

    def setUp(self):
        self.m = load()
        self.dir = tempfile.mkdtemp()
        self.env = mock.patch.dict(os.environ, {"ATTENTION_STATE_DIR": self.dir}, clear=False)
        self.env.start()
        os.environ.pop("SUPERVISOR_PERMISSION_POLL", None)

    def tearDown(self):
        self.env.stop()
        shutil.rmtree(self.dir, ignore_errors=True)

    def feed(self, kind):
        with open(os.path.join(self.dir, "S.open.json"), "w") as fh:
            json.dump({"kind": kind}, fh)

    def run_main(self, argv, stdin="", store=None, sleep=None):
        self.m.get_json = store or (lambda url: (True, []))
        if sleep:
            self.m.time.sleep = sleep
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(sys, "argv", ["poll"] + argv), \
             mock.patch.object(sys, "stdin", io.StringIO(stdin)), \
             redirect_stdout(out), redirect_stderr(err):
            rc = self.m.main()
        return rc, out.getvalue(), err.getvalue()

    def test_kill_switch_exits_silently(self):
        os.environ["SUPERVISOR_PERMISSION_POLL"] = "off"
        rc, out, _ = self.run_main([], stdin='{"session_id":"S"}')
        self.assertEqual((rc, out), (0, ""))

    def test_headless_worker_is_left_to_its_own_park(self):
        # Blocking here would delay the server's canUseTool park by the whole deadline.
        self.feed("permission")
        with mock.patch.dict(os.environ, {"SUPERVISOR_WORKER_MODE": "headless"}):
            t0 = time.monotonic()
            rc, out, _ = self.run_main(["--session-id", "S", "--timeout", "30"])
        self.assertEqual((rc, out), (0, ""))
        self.assertLess(time.monotonic() - t0, 1.0)

    def test_interactive_worker_still_polls(self):
        self.feed("permission")
        with mock.patch.dict(os.environ, {"SUPERVISOR_WORKER_MODE": "interactive"}):
            rc, out, err = self.run_main(["--session-id", "S", "--timeout", "0.2", "--interval", "0.05"])
        self.assertEqual((rc, out), (0, ""))
        self.assertIn("deadline", err)

    def test_deadline_is_fail_open(self):
        self.feed("permission")
        rc, out, err = self.run_main(["--session-id", "S", "--timeout", "0.3", "--interval", "0.05"])
        self.assertEqual((rc, out), (0, ""))
        self.assertIn("deadline", err)

    def test_answered_card_is_delivered(self):
        self.feed("permission")
        hook = json.dumps({"session_id": "S", "tool_name": "Write", "tool_input": {"file_path": "/tmp/w.txt"}})
        live = card("p1", payload="Write: /tmp/w.txt", created=time.time() + 1)

        def store(url):
            if url.endswith("/attention"):
                return True, [live]
            return True, {"state": "answered", "decision": "allow", "resolved_by": "manager"}

        rc, out, _ = self.run_main(["--timeout", "2", "--interval", "0.05"], stdin=hook, store=store)
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(out)["hookSpecificOutput"]["decision"], {"behavior": "allow"})

    def test_gate_not_yet_in_feed_is_waited_for(self):
        # attention-log.py writes the feed item in parallel; appearing late is normal.
        path = os.path.join(self.dir, "S.open.json")
        calls = {"n": 0}

        def sleep(_):
            calls["n"] += 1
            if calls["n"] == 3:
                self.feed("permission")
            if calls["n"] == 6:
                os.remove(path)

        rc, out, err = self.run_main(["--session-id", "S", "--timeout", "30", "--interval", "0"], sleep=sleep)
        self.assertEqual((rc, out), (0, ""))
        self.assertIn("gate closed", err)

    def test_closed_card_ends_the_poll(self):
        self.feed("permission")
        rc, out, err = self.run_main(["--item-id", "p1", "--timeout", "30", "--interval", "0"],
                                     store=lambda url: (True, {"state": "closed"}))
        self.assertEqual((rc, out), (0, ""))
        self.assertIn("'closed'", err)

    def test_store_error_is_never_a_decision(self):
        self.feed("permission")
        rc, out, _ = self.run_main(["--item-id", "p1", "--timeout", "0.2", "--interval", "0.05"],
                                   store=lambda url: (False, "boom"))
        self.assertEqual((rc, out), (0, ""))


if __name__ == "__main__":
    unittest.main()
