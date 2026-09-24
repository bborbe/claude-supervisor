#!/usr/bin/env python3
"""Tests for scripts/attention-answer.py.

Covers the two decisions that make an answer safe to route:

  * target resolution -- producer_id to a SendMessage name. A registry miss
    must read as undeliverable (the asker exited), and a name shared by two
    sessions must be refused: a bare name would reach the wrong one.
  * the answer gate -- only the arm that wins the store's compare-and-set may
    print a TARGET. A 409 must never be followed by a route, and a non-message
    item must be refused before the store is written at all.
  * the answer body -- `resolved_by` must name the arm's own session, and must be
    omitted rather than sent blank: downstream a "" is a *set* value, so it would
    count as a manager resolution and manufacture the false positive the field
    exists to close.

Run: python3 -m unittest discover -s scripts/tests -v
"""

import importlib.util
import io
import json
import os
import shutil
import tempfile
import unittest
import urllib.error
from unittest import mock

_HERE = os.path.dirname(os.path.abspath(__file__))
_SCRIPT = os.path.join(os.path.dirname(_HERE), "attention-answer.py")

_spec = importlib.util.spec_from_file_location("attention_answer", _SCRIPT)
aa = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(aa)


def _session(sid, name):
    return {"sessionId": sid, "name": name}


class ResolveTargetTest(unittest.TestCase):
    def test_unique_name_resolves(self):
        reg = [_session("a", "Worker A"), _session("b", "Worker B")]
        self.assertEqual(aa.resolve_target("a", reg), ("Worker A", None))

    def test_missing_session_is_undeliverable(self):
        self.assertEqual(aa.resolve_target("gone", [_session("a", "X")]), (None, "session exited"))

    def test_shared_name_is_refused(self):
        reg = [_session("a", "Attention Routing"), _session("b", "Attention Routing")]
        name, reason = aa.resolve_target("a", reg)
        self.assertIsNone(name)
        self.assertIn("ambiguous", reason)

    def test_unnamed_session_is_undeliverable(self):
        self.assertEqual(aa.resolve_target("a", [_session("a", "")]), (None, "session has no name"))


class LoadRegistryTest(unittest.TestCase):
    def test_skips_unreadable_files(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d)
        with open(os.path.join(d, "1.json"), "w") as fh:
            json.dump(_session("a", "A"), fh)
        with open(os.path.join(d, "2.json"), "w") as fh:
            fh.write("{not json")
        self.assertEqual(aa.load_registry(d), [_session("a", "A")])


class AnswerGateTest(unittest.TestCase):
    REG = [_session("p1", "Asker")]

    def _run(self, item, post=None):
        out = io.StringIO()
        with mock.patch.object(aa, "fetch_item", return_value=item), \
             mock.patch.object(aa, "post_answer", side_effect=post) as posted:
            rc = aa.cmd_answer("i1", "arm", self.REG, out=out)
        return rc, out.getvalue(), posted

    def test_winner_prints_target(self):
        item = {"answer_mechanism": "message", "producer_id": "p1"}
        rc, out, _ = self._run(item, post=lambda *_: {"answered_at": "t", "answered_by": "arm"})
        self.assertEqual(rc, 0)
        self.assertIn("ANSWERED:", out)
        self.assertIn("TARGET: Asker", out)

    def test_lost_race_never_routes(self):
        item = {"answer_mechanism": "message", "producer_id": "p1"}
        err = urllib.error.HTTPError("u", 409, "conflict", {}, None)
        rc, out, _ = self._run(item, post=err)
        self.assertEqual(rc, 1)
        self.assertIn("LOST:", out)
        self.assertNotIn("TARGET", out)

    def test_permission_item_refused_before_write(self):
        rc, out, posted = self._run({"answer_mechanism": "permission", "producer_id": "p1"})
        self.assertEqual(rc, 2)
        self.assertIn("REFUSED:", out)
        posted.assert_not_called()

    def test_arm_session_reaches_the_body(self):
        item = {"answer_mechanism": "message", "producer_id": "p1"}
        with mock.patch.object(aa, "resolved_session_id", return_value="sess-9"):
            rc, _, posted = self._run(item, post=lambda *_: {"answered_at": "t", "answered_by": "arm"})
        self.assertEqual(rc, 0)
        self.assertEqual(posted.call_args.args[2], "sess-9")

    def test_unknown_session_is_reported_not_silent(self):
        item = {"answer_mechanism": "message", "producer_id": "p1"}
        with mock.patch.object(aa, "resolved_session_id", return_value=""):
            rc, out, _ = self._run(item, post=lambda *_: {"answered_at": "t", "answered_by": "arm"})
        self.assertEqual(rc, 0)
        self.assertIn("RESOLVED_BY: unknown", out)


class ResolvedSessionIdTest(unittest.TestCase):
    def test_reads_the_session_env(self):
        self.assertEqual(aa.resolved_session_id({"CLAUDE_CODE_SESSION_ID": "s1"}), "s1")

    def test_missing_env_is_blank(self):
        self.assertEqual(aa.resolved_session_id({}), "")


class PostAnswerBodyTest(unittest.TestCase):
    """The body the store actually receives -- asserted, not assumed."""

    def _body(self, *args):
        captured = {}

        class _Resp:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def read(self):
                return b"{}"

        def _urlopen(req, timeout=None):
            captured["body"] = json.loads(req.data.decode())
            return _Resp()

        with mock.patch.object(aa.urllib.request, "urlopen", _urlopen):
            aa.post_answer(*args)
        return captured["body"]

    def test_resolved_by_rides_beside_answered_by(self):
        self.assertEqual(
            self._body("i1", "arm", "sess-9"),
            {"answered_by": "arm", "resolved_by": "sess-9"},
        )

    def test_blank_resolved_by_is_omitted_not_sent_empty(self):
        body = self._body("i1", "arm", "")
        self.assertEqual(body, {"answered_by": "arm"})
        self.assertNotIn("resolved_by", body)


if __name__ == "__main__":
    unittest.main()
