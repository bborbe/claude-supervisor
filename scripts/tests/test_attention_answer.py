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

    def _run(self, item, post=None, decision="", delivery=None):
        # A permission answer now reads the store's delivery report before it
        # claims anything; the default here is the delivered case, so the gate
        # tests below keep asserting the gate rather than the wait.
        if delivery is None:
            delivery = {"item_id": "i1", "status": "delivered", "carrier": "supervisor:attention-poll"}
        out = io.StringIO()
        with mock.patch.object(aa, "fetch_item", return_value=item), \
             mock.patch.object(aa, "post_answer", side_effect=post) as posted, \
             mock.patch.object(aa, "fetch_delivery", return_value=delivery), \
             mock.patch.object(aa, "_sleep", lambda _s: None):
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


class PermissionDeliveryTruthTest(unittest.TestCase):
    """A permission answer claims only the delivery the store observed.

    The CLI only POSTs the verdict; the spawning server's poller settles the
    park and records the attempt. The pre-fix arm printed `ANSWERED` and an
    unconditional `DELIVERY: supervisor poll` line and exited 0 whatever
    happened next — measured 2026-11-05, a manager read that as answered while
    the worker stayed parked (`wrapper holds no question for session …`).
    """

    ITEM = {"item_id": "i1", "answer_mechanism": "permission", "producer_id": "p1"}
    POSTED = {"answered_at": "t", "answered_by": "arm"}

    def _run(self, reports, wait=35.0, poll=1.0):
        """Answer `allow` while the store's delivery report walks `reports`.

        Each entry is a report dict, or an exception the read raises. The last
        entry repeats, and a fake clock advances one `poll` per read, so the
        bounded wait is exercised without sleeping.
        """
        seq = list(reports)
        reads = []
        timeouts = []
        clock = [0.0]

        def fetch(item_id, timeout=None):
            reads.append(item_id)
            timeouts.append(timeout)
            entry = seq.pop(0) if len(seq) > 1 else seq[0]
            if isinstance(entry, Exception):
                raise entry
            return entry

        def sleep(seconds):
            clock[0] += seconds

        out = io.StringIO()
        with mock.patch.object(aa, "fetch_item", return_value=self.ITEM), \
             mock.patch.object(aa, "post_answer", return_value=self.POSTED), \
             mock.patch.object(aa, "fetch_delivery", side_effect=fetch), \
             mock.patch.object(aa, "_sleep", sleep), \
             mock.patch.object(aa, "_clock", lambda: clock[0]), \
             mock.patch.object(aa, "DELIVERY_WAIT_SECONDS", wait), \
             mock.patch.object(aa, "DELIVERY_POLL_SECONDS", poll):
            rc = aa.cmd_answer("i1", "arm", [], out=out, decision="allow")
        self.timeouts = timeouts
        return rc, out.getvalue(), reads

    def test_delivered_reports_the_observed_delivery(self):
        rc, out, _ = self._run([{"status": "delivered", "carrier": "supervisor:attention-poll"}])
        self.assertEqual(rc, 0)
        self.assertIn("ANSWERED: i1", out)
        self.assertIn("DELIVERY: delivered by supervisor:attention-poll", out)
        self.assertNotIn("NOT_DELIVERED", out)
        # The pre-fix line stated a route and checked nothing; it must be gone.
        self.assertNotIn("no session to send to", out)

    def test_delivery_landing_after_a_few_polls_is_waited_for(self):
        rc, out, reads = self._run([
            {"status": "never_attempted"},
            {"status": "never_attempted"},
            {"status": "delivered", "carrier": "supervisor:attention-poll"},
        ])
        self.assertEqual(rc, 0)
        self.assertIn("ANSWERED: i1", out)
        self.assertEqual(len(reads), 3)

    def test_failed_attempt_is_not_answered(self):
        # The bdc2f1ed shape: a poller owned the item and the wrapper held no
        # question, so it recorded `failed`.
        rc, out, _ = self._run([{"status": "failed", "carrier": "supervisor:attention-poll"}])
        self.assertEqual(rc, aa.EXIT_NOT_DELIVERED)
        self.assertNotIn("ANSWERED:", out)
        self.assertIn("RECORDED: i1", out)
        self.assertIn("NOT_DELIVERED: failed by supervisor:attention-poll", out)

    def test_held_attempt_is_not_answered(self):
        rc, out, _ = self._run([{"status": "held", "carrier": "supervisor:attention-poll"}])
        self.assertEqual(rc, aa.EXIT_NOT_DELIVERED)
        self.assertNotIn("ANSWERED:", out)
        self.assertIn("NOT_DELIVERED: held by supervisor:attention-poll", out)

    def test_no_attempt_within_the_bound_is_a_failure_not_a_success(self):
        # ⚠️ The third state must never read as success: a wait that times out
        # and exits 0 is the pre-fix bug on a different clock.
        rc, out, reads = self._run([{"status": "never_attempted"}], wait=35.0, poll=1.0)
        self.assertEqual(rc, aa.EXIT_NOT_DELIVERED)
        self.assertNotIn("ANSWERED:", out)
        self.assertIn("RECORDED: i1", out)
        self.assertIn("UNCONFIRMED: never_attempted", out)
        self.assertGreaterEqual(len(reads), 35)

    def test_unreadable_report_is_unconfirmed(self):
        # Same guard as restart-precheck.py: a report without `status` decides nothing.
        rc, out, _ = self._run([{"item_id": "i1"}], wait=3.0)
        self.assertEqual(rc, aa.EXIT_NOT_DELIVERED)
        self.assertNotIn("ANSWERED:", out)
        self.assertIn("UNCONFIRMED: delivery report unreadable", out)

    def test_an_unreadable_read_then_a_delivered_one_still_succeeds(self):
        # The recovery path the wait most depends on: a failed read resets
        # status/reason but must keep polling rather than give up.
        rc, out, reads = self._run([
            urllib.error.URLError("refused"),
            {"status": "delivered", "carrier": "supervisor:attention-poll"},
        ])
        self.assertEqual(rc, 0)
        self.assertIn("ANSWERED: i1", out)
        self.assertIn("DELIVERY: delivered by supervisor:attention-poll", out)
        self.assertEqual(len(reads), 2)

    def test_every_read_is_clamped_to_the_budget_that_is_left(self):
        # The printed bound must be the enforced one: a read allowed its full
        # STORE_TIMEOUT could overshoot DELIVERY_WAIT_SECONDS.
        self._run([{"status": "never_attempted"}], wait=5.0, poll=1.0)
        self.assertTrue(self.timeouts)
        self.assertLessEqual(max(self.timeouts), 5.0)
        self.assertLess(self.timeouts[-1], 2.0)

    def test_unreachable_report_is_unconfirmed(self):
        rc, out, _ = self._run([urllib.error.URLError("refused")], wait=3.0)
        self.assertEqual(rc, aa.EXIT_NOT_DELIVERED)
        self.assertNotIn("ANSWERED:", out)
        self.assertIn("UNCONFIRMED: delivery report unreadable", out)
        self.assertIn("refused", out)

    def test_delivered_and_undelivered_outputs_are_distinguishable(self):
        _, delivered, _ = self._run([{"status": "delivered", "carrier": "c"}])
        _, failed, _ = self._run([{"status": "failed", "carrier": "c"}])
        delivered_lines = {line.split(":", 1)[0] for line in delivered.splitlines()}
        failed_lines = {line.split(":", 1)[0] for line in failed.splitlines()}
        self.assertIn("ANSWERED", delivered_lines - failed_lines)
        self.assertIn("NOT_DELIVERED", failed_lines - delivered_lines)

    def test_default_wait_outlasts_the_idle_poll_interval(self):
        # server/attention-poll.mjs reads at idleIntervalMs = 30000 when nothing
        # is open; an answer landing then is picked up up to 30 s later, so a
        # shorter bound would call a slow-but-working delivery unconfirmed.
        self.assertGreater(aa.DELIVERY_WAIT_SECONDS, 30.0)

    def test_wait_is_overridable_from_the_environment(self):
        self.assertEqual(aa.delivery_wait_seconds({"ATTENTION_DELIVERY_WAIT": "5"}), 5.0)
        self.assertEqual(aa.delivery_wait_seconds({}), aa.DEFAULT_DELIVERY_WAIT_SECONDS)

    def test_non_finite_wait_falls_back_instead_of_hanging(self):
        # ⚠️ A `nan` deadline makes `_clock() >= deadline` permanently False, so
        # the loop would never exit; `inf` blocks for ever. Neither may parse.
        for raw in ("nan", "inf", "-inf", "Infinity"):
            with self.subTest(raw=raw):
                self.assertEqual(
                    aa.delivery_wait_seconds({"ATTENTION_DELIVERY_WAIT": raw}),
                    aa.DEFAULT_DELIVERY_WAIT_SECONDS,
                )

    def test_non_positive_wait_falls_back(self):
        for raw in ("0", "-5"):
            with self.subTest(raw=raw):
                self.assertEqual(
                    aa.delivery_wait_seconds({"ATTENTION_DELIVERY_WAIT": raw}),
                    aa.DEFAULT_DELIVERY_WAIT_SECONDS,
                )

    def test_oversized_wait_is_clamped(self):
        self.assertEqual(
            aa.delivery_wait_seconds({"ATTENTION_DELIVERY_WAIT": "999999"}),
            aa.MAX_DELIVERY_WAIT_SECONDS,
        )

    def test_a_nan_wait_cannot_make_the_command_hang(self):
        # The end-to-end shape of the MAJOR: with a nan bound the pre-fix loop
        # never returned. The clamp is what makes this terminate.
        seq = [{"status": "never_attempted"}]
        reads, clock = [], [0.0]
        out = io.StringIO()

        def fetch(item_id, timeout=None):
            reads.append(item_id)
            return seq[0]

        with mock.patch.object(aa, "fetch_item", return_value={"item_id": "i1", "answer_mechanism": "permission"}), \
             mock.patch.object(aa, "post_answer", return_value={"answered_at": "t", "answered_by": "a"}), \
             mock.patch.object(aa, "fetch_delivery", side_effect=fetch), \
             mock.patch.object(aa, "_sleep", lambda s: clock.__setitem__(0, clock[0] + s)), \
             mock.patch.object(aa, "_clock", lambda: clock[0]), \
             mock.patch.object(aa, "DELIVERY_WAIT_SECONDS",
                               aa.delivery_wait_seconds({"ATTENTION_DELIVERY_WAIT": "nan"})), \
             mock.patch.object(aa, "DELIVERY_POLL_SECONDS", 1.0):
            rc = aa.cmd_answer("i1", "arm", [], out=out, decision="allow")
        self.assertEqual(rc, aa.EXIT_NOT_DELIVERED)
        self.assertIn("UNCONFIRMED:", out.getvalue())


class FetchDeliveryTest(unittest.TestCase):
    def test_reads_the_attempt_endpoint(self):
        captured = {}

        class _Resp(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        def _urlopen(url, timeout=None):
            # `fetch_delivery` builds a Request so it can carry the bearer token;
            # accept either shape so this test asserts the endpoint, not the call
            # convention.
            captured["url"] = getattr(url, "full_url", url)
            return _Resp(json.dumps({"item_id": "i1", "status": "delivered"}).encode())

        with mock.patch.object(aa.urllib.request, "urlopen", _urlopen):
            report = aa.fetch_delivery("i1")
        self.assertTrue(captured["url"].endswith("/api/1.0/attention/i1/attempt"))
        self.assertEqual(report["status"], "delivered")


class BearerTokenTest(unittest.TestCase):
    """Every store call carries the token, and an option card answers by label.

    Measured 2026-11-08: `attention-answer.py` sent no `Authorization` header at
    any of its five request sites, so every answer against a token-requiring
    store returned 401 while `attention-ask.py poll` through the same wrapper
    read the card fine. The operator's own prod command failed twice on it.
    """

    class _Resp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def _capture(self):
        seen = []

        def _urlopen(req, timeout=None):
            seen.append(req)
            return self._Resp(json.dumps([]).encode())

        return seen, _urlopen

    def test_every_read_carries_the_token(self):
        seen, _urlopen = self._capture()
        with mock.patch.object(aa, "TOKEN", "secret"), \
             mock.patch.object(aa.urllib.request, "urlopen", _urlopen):
            aa.fetch_open()
            try:
                aa.fetch_item("i1")
            except Exception:
                pass
            try:
                aa.fetch_delivery("i1")
            except Exception:
                pass
        self.assertEqual(len(seen), 3)
        for req in seen:
            self.assertEqual(req.get_header("Authorization"), "Bearer secret")

    def test_no_token_sends_no_header(self):
        seen, _urlopen = self._capture()
        with mock.patch.object(aa, "TOKEN", ""), \
             mock.patch.object(aa.urllib.request, "urlopen", _urlopen):
            aa.fetch_open()
        self.assertIsNone(seen[0].get_header("Authorization"))

    def test_option_rides_beside_answered_by(self):
        captured = {}

        def _urlopen(req, timeout=None):
            captured["body"] = json.loads(req.data.decode())
            return self._Resp(json.dumps({"answered_by": "operator"}).encode())

        with mock.patch.object(aa.urllib.request, "urlopen", _urlopen):
            aa.post_answer("i1", "operator", "", option="approve")
        self.assertEqual(captured["body"]["option"], "approve")
        self.assertNotIn("decision", captured["body"])

    def test_permission_card_refuses_an_option(self):
        out = io.StringIO()
        with mock.patch.object(aa, "fetch_item",
                               lambda _id: {"answer_mechanism": "permission"}):
            rc = aa.cmd_answer("i1", "operator", {}, out=out, option="allow")
        self.assertEqual(rc, 2)
        self.assertIn("takes --decision allow|deny, not --option", out.getvalue())

    def test_unknown_option_label_is_refused(self):
        out = io.StringIO()
        item = {"answer_mechanism": "pick",
                "options": [{"label": "approve"}, {"label": "decline"}]}
        with mock.patch.object(aa, "fetch_item", lambda _id: item):
            rc = aa.cmd_answer("i1", "operator", {}, out=out, option="maybe")
        self.assertEqual(rc, 2)
        self.assertIn("has no option 'maybe'", out.getvalue())


if __name__ == "__main__":
    unittest.main()
