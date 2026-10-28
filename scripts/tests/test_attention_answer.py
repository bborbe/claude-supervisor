#!/usr/bin/env python3
"""Tests for scripts/attention-answer.py.

Covers the two decisions that make an answer safe to route:

  * target resolution -- producer_id to a SendMessage name. A registry miss
    must read as undeliverable (the asker exited), and a name shared by two
    sessions must be refused: a bare name would reach the wrong one.
  * the answer gate -- only the arm that wins the store's compare-and-set may
    print a TARGET. A 409 must never be followed by a route, and an `ack` item
    must be refused before the store is written at all.
  * the 409 itself -- the status covers more than one failure, so the body's
    `error.code` names which: ITEM_CLOSED (the card left the queue, so no arm
    can answer it) must read differently from ALREADY_ANSWERED (another arm's
    verdict landed first). A code the arm cannot read falls back to the generic
    line, because a body it cannot parse is not evidence of either.
    ⚠️ **ITEM_CLOSED names no cause** — the store does not record why a card
    left, so the line states that rather than picking one; only
    ALREADY_ANSWERED claims a specific fact, and that fact is the verdict's
    existence, which the code does establish.
  * the permission branch -- a permission-class item needs an explicit
    --decision. Without one it is refused rather than defaulted, because
    neither an implicit allow nor an implicit deny is safe; with one it prints
    DELIVERY and never TARGET, because a session cannot release another
    session's parked gate.
  * the answer body -- `resolved_by` must name the arm's own session, and must be
    omitted rather than sent blank: downstream a "" is a *set* value, so it would
    count as a manager resolution and manufacture the false positive the field
    exists to close.
  * the attempt record -- written by the arm that ATTEMPTS delivery, after the
    send. Its outcome is refused rather than defaulted, because neither value is
    safe to assume: a `delivered` default would record a success nobody observed
    and a `failed` default a failure nobody observed, and both would be
    indistinguishable downstream from a real measurement.

Run: python3 -m unittest discover -s scripts/tests -v
"""

import http.client
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


def _conflict(code=None):
    """A 409 the way the store sends one, with `code` in the body.

    `code=None` builds the body-less shape (an empty body, `fp=None`), which is
    kept reachable on purpose: a body this arm cannot read must fall back to the
    generic line rather than to one of the named ones.
    """
    body = {} if code is None else {"error": {"code": code}}
    return urllib.error.HTTPError(
        "u", 409, "conflict", {}, io.BytesIO(json.dumps(body).encode())
    )


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

    def _run(self, item, post=None, decision=""):
        out = io.StringIO()
        with mock.patch.object(aa, "fetch_item", return_value=item), \
             mock.patch.object(aa, "post_answer", side_effect=post) as posted:
            rc = aa.cmd_answer("i1", "arm", self.REG, out=out, decision=decision)
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

    def test_permission_without_a_decision_is_refused_before_write(self):
        rc, out, posted = self._run(
            {"item_id": "i1", "answer_mechanism": "permission", "producer_id": "p1"}
        )
        self.assertEqual(rc, 2)
        self.assertIn("REFUSED:", out)
        self.assertIn("--decision", out)
        posted.assert_not_called()

    def test_permission_with_an_unknown_decision_is_refused_before_write(self):
        rc, out, posted = self._run(
            {"item_id": "i1", "answer_mechanism": "permission", "producer_id": "p1"},
            decision="maybe",
        )
        self.assertEqual(rc, 2)
        self.assertIn("REFUSED:", out)
        posted.assert_not_called()

    def test_permission_with_a_decision_prints_delivery_never_target(self):
        item = {"item_id": "i1", "answer_mechanism": "permission", "producer_id": "p1"}
        rc, out, _ = self._run(
            item, post=lambda *_: {"answered_at": "t", "answered_by": "arm"}, decision="allow"
        )
        self.assertEqual(rc, 0)
        self.assertIn("ANSWERED:", out)
        self.assertIn("DECISION: allow", out)
        self.assertIn("DELIVERY:", out)
        # The load-bearing assertion of this whole branch: a TARGET line here
        # would make the wrapping command SendMessage, which is exactly the
        # Claude-session relay this path exists to remove.
        self.assertNotIn("TARGET", out)

    def test_permission_deny_reaches_the_body(self):
        item = {"item_id": "i1", "answer_mechanism": "permission", "producer_id": "p1"}
        rc, _, posted = self._run(
            item, post=lambda *_: {"answered_at": "t", "answered_by": "arm"}, decision="deny"
        )
        self.assertEqual(rc, 0)
        self.assertEqual(posted.call_args.args[3], "deny")

    def test_lost_permission_race_never_delivers(self):
        item = {"item_id": "i1", "answer_mechanism": "permission", "producer_id": "p1"}
        err = urllib.error.HTTPError("u", 409, "conflict", {}, None)
        rc, out, _ = self._run(item, post=err, decision="allow")
        self.assertEqual(rc, 1)
        self.assertIn("LOST:", out)
        self.assertNotIn("DELIVERY", out)

    def test_item_closed_does_not_pick_a_cause(self):
        item = {"item_id": "i1", "answer_mechanism": "message", "producer_id": "p1"}
        rc, out, _ = self._run(item, post=_conflict("ITEM_CLOSED"))
        self.assertEqual(rc, 1)
        self.assertIn("ITEM_CLOSED:", out)
        self.assertNotIn("LOST:", out)
        # ⚠️ The whole point of the split is that a reader who sees only this
        # line learns no verdict was recorded — NOT which cause produced it. The
        # line used to offer "the card was cleared, or its producer exited" as
        # if the record had settled which; it cannot. An empty-body close comes
        # from the dead-session sweep, the compact-crossing clear, a forwarded
        # SessionEnd and `close_session_approval`, and the last is not a
        # producer exit at all. Naming one member of that set would be a claim
        # the record does not support, so the line must name the ambiguity.
        self.assertIn("no verdict was recorded", out)
        self.assertIn("the store does not record why", out)
        self.assertNotIn("producer exited;", out)
        self.assertNotIn("TARGET", out)

    def test_already_answered_names_the_lost_race(self):
        item = {"item_id": "i1", "answer_mechanism": "message", "producer_id": "p1"}
        rc, out, _ = self._run(item, post=_conflict("ALREADY_ANSWERED"))
        self.assertEqual(rc, 1)
        self.assertIn("ALREADY_ANSWERED:", out)
        self.assertNotIn("LOST:", out)
        self.assertIn("verdict from another arm", out)
        self.assertNotIn("TARGET", out)

    def test_body_less_409_falls_back_to_the_generic_line(self):
        item = {"item_id": "i1", "answer_mechanism": "message", "producer_id": "p1"}
        rc, out, _ = self._run(item, post=_conflict())
        self.assertEqual(rc, 1)
        self.assertIn("LOST:", out)
        self.assertNotIn("ITEM_CLOSED:", out)
        # ⚠️ This branch fires on a code the arm could not read at all, so it
        # carries strictly LESS evidence than ITEM_CLOSED carries — and it used
        # to assert "already answered or no longer open", two specific causes
        # drawn from zero information. It must now say it cannot tell which.
        self.assertIn("cannot read", out)
        self.assertIn("cannot say whether", out)
        self.assertNotIn("already answered or no longer open", out)
        self.assertNotIn("ALREADY_ANSWERED:", out)

    def test_unknown_code_falls_back_to_the_generic_line(self):
        item = {"item_id": "i1", "answer_mechanism": "message", "producer_id": "p1"}
        rc, out, _ = self._run(item, post=_conflict("VALIDATION_ERROR"))
        self.assertEqual(rc, 1)
        self.assertIn("LOST:", out)
        self.assertNotIn("ITEM_CLOSED:", out)
        self.assertNotIn("ALREADY_ANSWERED:", out)

    def test_non_object_body_falls_back_to_the_generic_line(self):
        # `{"error": ...}` is the store's shape, but nothing guarantees a proxy or
        # a future store keeps it, so a list or a string body must not raise.
        item = {"item_id": "i1", "answer_mechanism": "message", "producer_id": "p1"}
        err = urllib.error.HTTPError(
            "u", 409, "conflict", {}, io.BytesIO(b'["not", "an", "object"]')
        )
        rc, out, _ = self._run(item, post=err)
        self.assertEqual(rc, 1)
        self.assertIn("LOST:", out)

    def test_truncated_body_falls_back_instead_of_raising(self):
        # A 409 whose body is cut short against its Content-Length is what a proxy
        # or a mid-body reset produces. http.client.IncompleteRead is neither a
        # ValueError nor an OSError, so without it in the handler's tuple the arm
        # raises where the caller expects a label -- a path the pre-fix arm could
        # not reach at all, because it never read the 409 body.
        item = {"item_id": "i1", "answer_mechanism": "message", "producer_id": "p1"}

        class _Truncated(io.BytesIO):
            def read(self, *args, **kwargs):
                raise http.client.IncompleteRead(b'{"error":')

        err = urllib.error.HTTPError("u", 409, "conflict", {}, _Truncated())
        rc, out, _ = self._run(item, post=err)
        self.assertEqual(rc, 1)
        self.assertIn("LOST:", out)

    def test_item_closed_on_a_permission_card_never_delivers(self):
        item = {"item_id": "i1", "answer_mechanism": "permission", "producer_id": "p1"}
        rc, out, _ = self._run(item, post=_conflict("ITEM_CLOSED"), decision="allow")
        self.assertEqual(rc, 1)
        self.assertIn("ITEM_CLOSED:", out)
        self.assertIn("do not deliver", out)
        self.assertNotIn("DELIVERY", out)

    def test_already_answered_on_a_permission_card_never_delivers(self):
        item = {"item_id": "i1", "answer_mechanism": "permission", "producer_id": "p1"}
        rc, out, _ = self._run(item, post=_conflict("ALREADY_ANSWERED"), decision="allow")
        self.assertEqual(rc, 1)
        self.assertIn("ALREADY_ANSWERED:", out)
        self.assertIn("do not deliver", out)
        self.assertNotIn("DELIVERY", out)

    def test_ack_item_is_still_refused(self):
        rc, out, posted = self._run(
            {"item_id": "i1", "answer_mechanism": "ack", "producer_id": "p1"}
        )
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

    def test_decision_rides_beside_answered_by(self):
        self.assertEqual(
            self._body("i1", "arm", "sess-9", "allow"),
            {"answered_by": "arm", "resolved_by": "sess-9", "decision": "allow"},
        )

    def test_blank_decision_is_omitted_not_sent_empty(self):
        # The same rule as resolved_by, and it matters more: the schema adds no
        # write-time rejection for an omitted decision, so a "" here would store
        # a *present* field holding no verdict rather than an absent one.
        body = self._body("i1", "arm", "sess-9", "")
        self.assertEqual(body, {"answered_by": "arm", "resolved_by": "sess-9"})
        self.assertNotIn("decision", body)


class AttemptBodyTest(unittest.TestCase):
    """The attempt record names the carrier and what it observed."""

    def _post(self, item_id, carrier, outcome):
        captured = {}

        class _Resp:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def read(self):
                return b"{}"

        def _urlopen(req, timeout=None):
            captured["url"] = req.full_url
            captured["method"] = req.get_method()
            captured["body"] = json.loads(req.data.decode())
            return _Resp()

        with mock.patch.object(aa.urllib.request, "urlopen", _urlopen):
            aa.post_attempt(item_id, carrier, outcome)
        return captured

    def test_body_names_the_carrier_and_the_outcome(self):
        captured = self._post("i1", "supervisor:attention-next", "delivered")
        self.assertEqual(
            captured["body"],
            {"carrier": "supervisor:attention-next", "outcome": "delivered"},
        )

    def test_posts_to_the_attempt_endpoint(self):
        captured = self._post("i1", "arm", "failed")
        self.assertTrue(captured["url"].endswith("/api/1.0/attention/i1/attempt"))
        self.assertEqual(captured["method"], "POST")

    def test_a_failure_is_recorded_as_failed_not_dropped(self):
        # The trail's whole point: "a carrier looked and could not deliver" must
        # be recorded, or it is indistinguishable from "no carrier ever looked".
        captured = self._post("i1", "arm", "failed")
        self.assertEqual(captured["body"]["outcome"], "failed")


class AttemptOutcomeGateTest(unittest.TestCase):
    """cmd_attempt refuses an unobserved outcome rather than defaulting it."""

    def test_refuses_an_empty_outcome_without_posting(self):
        with mock.patch.object(aa, "post_attempt") as posted:
            out = io.StringIO()
            code = aa.cmd_attempt("i1", "arm", "", out=out)
        self.assertEqual(code, 1)
        posted.assert_not_called()
        self.assertIn("REFUSED", out.getvalue())

    def test_refuses_an_unknown_outcome_without_posting(self):
        with mock.patch.object(aa, "post_attempt") as posted:
            out = io.StringIO()
            code = aa.cmd_attempt("i1", "arm", "maybe", out=out)
        self.assertEqual(code, 1)
        posted.assert_not_called()

    def test_records_a_delivered_outcome(self):
        with mock.patch.object(aa, "post_attempt") as posted:
            out = io.StringIO()
            code = aa.cmd_attempt("i1", "arm", "delivered", out=out)
        self.assertEqual(code, 0)
        posted.assert_called_once_with("i1", "arm", "delivered")
        self.assertIn("ATTEMPT: i1 delivered by arm", out.getvalue())


if __name__ == "__main__":
    unittest.main()
