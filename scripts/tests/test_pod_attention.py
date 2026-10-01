#!/usr/bin/env python3
"""Tests for scripts/pod-attention.py.

Covers the decisions that make a pod's operator channel safe, each of which has a
failure mode that reads as success if it regresses:

  * the auth seam -- a non-local store with no token is REFUSED, because an
    unauthenticated remote store lets anything that can reach the port read every
    card and release every gate. The refusal is what keeps an unauthenticated
    localhost default from being read later as the shipped shape.
  * the gate verdict -- three conditions, all required. The load-bearing one is
    `resolved_by`: without it a board click could release a gate, which is the
    whole reason the provenance rule exists.
  * fail-open, never fail-allow -- an error, a malformed response or the deadline
    must print NO_DECISION and exit non-zero WITHOUT a verdict. A default of allow
    would release a gate nobody approved.
  * the producer gate -- an item with no producer can never be polled back, so it
    is refused rather than posted into a void.

Run: python3 -m unittest discover -s scripts/tests -v
"""

import importlib.util
import io
import json
import os
import sys
import unittest
from unittest import mock

_HERE = os.path.dirname(os.path.abspath(__file__))
_SCRIPT = os.path.join(os.path.dirname(_HERE), "pod-attention.py")

_spec = importlib.util.spec_from_file_location("pod_attention", _SCRIPT)
pod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pod)


class FakeResponse:
    """Minimal context-manager stand-in for urlopen's return value."""

    def __init__(self, payload):
        self._body = json.dumps(payload).encode()

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def gate_args(**overrides):
    defaults = dict(
        dedup_key="gate-1",
        payload="Run the production deploy?",
        producer_id="pod-a",
        timeout=10.0,
        interval=1.0,
    )
    defaults.update(overrides)
    return mock.Mock(**defaults)


def ask_args(**overrides):
    defaults = dict(
        dedup_key="q-1",
        payload="Which surface?",
        option=["board", "pane"],
        recommend="board",
        producer_id="pod-a",
        interrupt_class="pick",
    )
    defaults.update(overrides)
    return mock.Mock(**defaults)


def item(**overrides):
    """A store item, defaulted to an unanswered permission card."""
    base = {
        "item_id": "i1",
        "state": "open",
        "answer_mechanism": "permission",
        "producer_id": "pod-a",
    }
    base.update(overrides)
    return base


class FakeClock:
    """Monotonic clock that advances by the fake sleep's argument."""

    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class AuthSeamTest(unittest.TestCase):
    def test_loopback_hosts_are_local(self):
        for store in ("http://localhost:18080", "http://127.0.0.1:18080", "http://[::1]:18080"):
            self.assertTrue(pod.is_local(store), store)

    def test_a_remote_host_is_not_local(self):
        self.assertFalse(pod.is_local("http://attention.nuke:18080"))

    def test_refuses_a_remote_store_with_no_token(self):
        """The seam's whole point: absent and safe must coincide."""
        reason = pod.auth_refusal("http://attention.nuke:18080", "")
        self.assertIsNotNone(reason)
        self.assertIn("POD_ATTENTION_TOKEN", reason)

    def test_allows_a_remote_store_once_a_token_is_injected(self):
        self.assertIsNone(pod.auth_refusal("http://attention.nuke:18080", "s3cret"))

    def test_allows_localhost_with_no_token_because_that_is_the_intended_local_case(self):
        self.assertIsNone(pod.auth_refusal("http://localhost:18080", ""))

    def test_headers_carry_the_token_only_when_one_is_injected(self):
        with mock.patch.object(pod, "TOKEN", "s3cret"):
            self.assertEqual(pod.headers()["Authorization"], "Bearer s3cret")
        with mock.patch.object(pod, "TOKEN", ""):
            self.assertNotIn("Authorization", pod.headers())


class BuildOptionsTest(unittest.TestCase):
    def test_marks_exactly_the_recommended_label(self):
        options = pod.build_options(["a", "b"], "b")
        self.assertEqual(options, [{"label": "a", "recommended": False}, {"label": "b", "recommended": True}])

    def test_refuses_a_recommendation_that_is_not_an_option(self):
        with self.assertRaises(ValueError):
            pod.build_options(["a"], "z")

    def test_refuses_a_blank_label(self):
        with self.assertRaises(ValueError):
            pod.build_options(["  "], "")


class VerdictTest(unittest.TestCase):
    """The three conditions, each of which must be able to fail alone."""

    def test_open_item_settles_nothing(self):
        ok, decision, _ = pod.verdict(item(state="open"))
        self.assertFalse(ok)
        self.assertIsNone(decision)

    def test_answered_without_a_decision_settles_nothing(self):
        ok, _, reason = pod.verdict(item(state="answered", resolved_by="arm-a"))
        self.assertFalse(ok)
        self.assertIn("no decision", reason)

    def test_an_unknown_decision_is_refused(self):
        ok, _, reason = pod.verdict(
            item(state="answered", decision="maybe", resolved_by="arm-a")
        )
        self.assertFalse(ok)
        self.assertIn("maybe", reason)

    def test_an_answer_with_no_resolved_by_cannot_release_a_gate(self):
        """The provenance rule: a board click sets no resolved_by, so it cannot settle."""
        ok, _, reason = pod.verdict(item(state="answered", decision="allow"))
        self.assertFalse(ok)
        self.assertIn("resolved_by", reason)

    def test_a_blank_resolved_by_is_refused_as_absent(self):
        ok, _, _ = pod.verdict(item(state="answered", decision="allow", resolved_by=""))
        self.assertFalse(ok)

    def test_all_three_conditions_met_yields_the_decision(self):
        ok, decision, reason = pod.verdict(
            item(state="answered", decision="deny", resolved_by="arm-a")
        )
        self.assertTrue(ok)
        self.assertEqual(decision, "deny")
        self.assertIn("arm-a", reason)


class RenderPollTest(unittest.TestCase):
    def test_a_permission_item_that_has_not_settled_reads_as_open(self):
        out = io.StringIO()
        pod.render_poll(item(state="open"), out)
        self.assertEqual(out.getvalue().strip(), "OPEN")

    def test_a_settled_permission_item_prints_the_verdict(self):
        out = io.StringIO()
        pod.render_poll(item(state="answered", decision="allow", resolved_by="arm-a"), out)
        self.assertTrue(out.getvalue().startswith("ANSWERED: allow"))

    def test_an_answer_with_no_resolved_by_is_not_read_as_answered(self):
        out = io.StringIO()
        pod.render_poll(item(state="answered", decision="allow"), out)
        self.assertTrue(out.getvalue().startswith("NOT_OPERATOR_ANSWERED:"))

    def test_a_message_item_with_no_answer_reads_as_open(self):
        out = io.StringIO()
        pod.render_poll(item(answer_mechanism="message"), out)
        self.assertEqual(out.getvalue().strip(), "OPEN")


class AskTest(unittest.TestCase):
    def test_posts_a_message_card_carrying_the_options(self):
        captured = {}

        def fake_urlopen(req, timeout=None):
            captured["body"] = json.loads(req.data.decode())
            return FakeResponse({"item_id": "q1"})

        out = io.StringIO()
        with mock.patch.object(pod.urllib.request, "urlopen", fake_urlopen):
            rc = pod.cmd_ask(ask_args(), out=out)

        self.assertEqual(rc, 0)
        body = captured["body"]
        self.assertEqual(body["answer_mechanism"], "message")
        self.assertEqual(body["producer_id"], "pod-a")
        self.assertEqual(body["dedup_key"], "q-1")
        self.assertTrue(any(o["recommended"] for o in body["options"]))
        self.assertIn("ITEM_ID: q1", out.getvalue())

    def test_refuses_without_a_producer_because_nobody_could_poll_it_back(self):
        out = io.StringIO()
        with mock.patch.dict(os.environ, {}, clear=True):
            rc = pod.cmd_ask(ask_args(producer_id=""), out=out)
        self.assertEqual(rc, 2)
        self.assertIn("REFUSED", out.getvalue())

    def test_refuses_a_remote_store_with_no_token_before_any_request(self):
        out = io.StringIO()
        with mock.patch.object(pod, "STORE", "http://attention.nuke:18080"), mock.patch.object(
            pod, "TOKEN", ""
        ):
            rc = pod.cmd_ask(ask_args(), out=out)
        self.assertEqual(rc, 2)
        self.assertIn("not loopback", out.getvalue())


class GateTest(unittest.TestCase):
    def _run(self, responses, args=None):
        """Drive cmd_gate against a scripted sequence of store responses."""
        posted = {}

        def fake_urlopen(req, timeout=None):
            if req.get_method() == "POST":
                posted["body"] = json.loads(req.data.decode())
                return FakeResponse({"item_id": "g1"})
            nxt = responses.pop(0) if responses else {"item_id": "g1", "state": "open"}
            if isinstance(nxt, Exception):
                raise nxt
            return FakeResponse(nxt)

        clock = FakeClock()
        out = io.StringIO()
        with mock.patch.object(pod.urllib.request, "urlopen", fake_urlopen):
            rc = pod.cmd_gate(args or gate_args(), out=out, sleep=clock.sleep, clock=clock)
        return rc, out.getvalue(), posted

    def test_posts_a_permission_class_card(self):
        """The class is the whole difference — attention-ask.py refuses to post it."""
        _, _, posted = self._run(
            [item(state="answered", decision="allow", resolved_by="arm-a")]
        )
        self.assertEqual(posted["body"]["answer_mechanism"], "permission")

    def test_returns_the_verdict_once_the_operator_answers(self):
        rc, text, _ = self._run(
            [item(state="open"), item(state="answered", decision="allow", resolved_by="arm-a")]
        )
        self.assertEqual(rc, 0)
        self.assertIn("DECISION: allow", text)

    def test_blocks_while_the_board_is_unanswered_and_times_out_with_no_decision(self):
        """The stub-stays-blocked property: an unanswered gate yields NO verdict."""
        rc, text, _ = self._run([item(state="open")] * 20)
        self.assertEqual(rc, 1)
        self.assertIn("NO_DECISION:", text)
        self.assertNotIn("DECISION: allow", text)

    def test_an_answered_card_with_no_resolved_by_never_becomes_a_decision(self):
        """The never-auto-answer guard: an unattributed answer cannot release a gate."""
        rc, text, _ = self._run([item(state="answered", decision="allow")] * 20)
        self.assertEqual(rc, 1)
        self.assertIn("NO_DECISION:", text)
        # No line may carry a verdict — `NO_DECISION:` is a refusal, not a decision.
        self.assertFalse(any(line.startswith("DECISION: ") for line in text.splitlines()))

    def test_a_store_error_fails_open_rather_than_guessing(self):
        err = pod.urllib.error.URLError("connection refused")
        rc, text, _ = self._run([err] * 20)
        self.assertEqual(rc, 1)
        self.assertIn("NO_DECISION:", text)

    def test_an_unreachable_remote_store_is_refused_before_posting(self):
        out = io.StringIO()
        with mock.patch.object(pod, "STORE", "http://attention.nuke:18080"), mock.patch.object(
            pod, "TOKEN", ""
        ):
            rc = pod.cmd_gate(gate_args(), out=out)
        self.assertEqual(rc, 2)
        self.assertIn("not loopback", out.getvalue())


if __name__ == "__main__":
    sys.exit(unittest.main())
