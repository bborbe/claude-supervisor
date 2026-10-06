#!/usr/bin/env python3
"""Tests for scripts/attention-ask.py.

Covers the three decisions that make a posted question answerable:

  * the producer gate -- a post with no producer id is refused rather than
    sent. An item's `producer_id` is the only thing that can poll it back, so
    an item with none is a question asked into a void: it can never be read,
    and nothing downstream would report it as lost.
  * the option rules -- at most one recommendation, and `--recommend` must name
    one of the `--option` labels. The store enforces both too; checking here is
    what lets the failure name the fix instead of quoting a store payload.
  * the omitted-vs-empty body rule -- `context`, `options` and `expires_at` are
    omitted when empty, never sent as "". The schema reads an absent value as
    optional/pre-change and a present "" as a value, so sending blank would
    store a field holding nothing rather than no field at all.

  * the poll shapes -- OPEN while unanswered; the stored `answer` rendered as
    `kind: value`; and a `skip` rendered as the bare word, since `skip ` with
    nothing after it reads as a truncated option answer.

  * the poll id-shape guard -- a short id (a hand-truncated store id, or the
    `open-items` ledger's own display id) is refused before the store is asked,
    because both used to print `FAILED: no such item` and read as the card
    having gone away. A well-formed id the store does not hold is still absence.

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
from datetime import datetime, timedelta, timezone
from unittest import mock

_HERE = os.path.dirname(os.path.abspath(__file__))
_SCRIPT = os.path.join(os.path.dirname(_HERE), "attention-ask.py")

_spec = importlib.util.spec_from_file_location("attention_ask", _SCRIPT)
ask = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ask)

# A store item id: 32 lowercase hex characters, the shape the store mints
# (`attention-controller`, `pkg/item-id-generator.go`). `poll` refuses any other
# shape before it reaches the store, so every id a fixture hands to `cmd_poll`
# has to be one of these — an 8-char stand-in would exercise the refusal instead
# of the branch under test.
STORE_ITEM_ID = "0123456789abcdef0123456789abcdef"


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


def post_args(**overrides):
    defaults = dict(
        dedup_key="q1",
        payload="Which surface?",
        context="",
        option=[],
        recommend="",
        producer_id="session-a",
        producer_kind="session",
        liveness_ref="",
        interrupt_class="pick",
        expires_at="",
        closer="",
    )
    defaults.update(overrides)
    return mock.Mock(**defaults)


def batch_args(**overrides):
    defaults = dict(
        dedup_key="batch-1",
        task=["Alpha", "Beta"],
        context="",
        producer_id="session-a",
        producer_kind="session",
        liveness_ref="",
        interrupt_class="pick",
        expires_at="",
    )
    defaults.update(overrides)
    return mock.Mock(**defaults)


class BuildBatchPayloadTest(unittest.TestCase):
    def test_numbers_every_row(self):
        payload = ask.build_batch_payload(["Alpha", "Beta Two"])
        self.assertIn("1. Alpha", payload)
        self.assertIn("2. Beta Two", payload)
        self.assertIn("2 task(s)", payload)

    def test_prints_names_verbatim_because_the_name_is_the_approval_key(self):
        long_name = (
            "Tighten the Sentry-triage Criteria at the Source That Generates Each Weekly Jira Task"
        )
        self.assertIn(long_name, ask.build_batch_payload([long_name]))

    def test_refuses_an_empty_batch(self):
        with self.assertRaises(ValueError):
            ask.build_batch_payload([])

    def test_refuses_a_blank_name_and_names_its_position(self):
        with self.assertRaises(ValueError) as ctx:
            ask.build_batch_payload(["Alpha", "   "])
        self.assertIn("entry 2", str(ctx.exception))


class PostBatchTest(unittest.TestCase):
    def test_posts_exactly_one_item_for_the_whole_batch(self):
        """The arm's whole point: N rows, ONE card."""
        captured = {}
        calls = []

        def fake_urlopen(req, timeout=None):
            calls.append(req.full_url)
            captured["body"] = json.loads(req.data.decode())
            return FakeResponse({"item_id": "batch1"})

        out = io.StringIO()
        with mock.patch.object(ask.urllib.request, "urlopen", fake_urlopen):
            rc = ask.cmd_post_batch(batch_args(task=["Alpha", "Beta", "Gamma"]), out=out)

        self.assertEqual(rc, 0)
        self.assertEqual(len(calls), 1)
        body = captured["body"]
        self.assertEqual(body["answer_mechanism"], "message")
        self.assertIn("1. Alpha", body["payload"])
        self.assertIn("3. Gamma", body["payload"])
        self.assertIn("ITEM_ID: batch1", out.getvalue())

    def test_offers_no_options_so_the_answer_is_the_operators_own_words(self):
        captured = {}

        def fake_urlopen(req, timeout=None):
            captured["body"] = json.loads(req.data.decode())
            return FakeResponse({"item_id": "batch1"})

        with mock.patch.object(ask.urllib.request, "urlopen", fake_urlopen):
            ask.cmd_post_batch(batch_args(), out=io.StringIO())

        self.assertNotIn("options", captured["body"])

    def test_refuses_without_a_producer_id(self):
        out = io.StringIO()
        with mock.patch.dict(os.environ, {}, clear=True):
            rc = ask.cmd_post_batch(batch_args(producer_id=""), out=out)
        self.assertEqual(rc, 2)
        self.assertIn("REFUSED", out.getvalue())

    def test_refuses_a_blank_row_without_reaching_the_store(self):
        out = io.StringIO()

        def fake_urlopen(req, timeout=None):
            raise AssertionError("a refused batch must not reach the store")

        with mock.patch.object(ask.urllib.request, "urlopen", fake_urlopen):
            rc = ask.cmd_post_batch(batch_args(task=["Alpha", ""]), out=out)

        self.assertEqual(rc, 2)
        self.assertIn("entry 2", out.getvalue())


class BuildOptionsTest(unittest.TestCase):
    def test_marks_the_named_option_recommended(self):
        options = ask.build_options(["the board", "the tab"], "the board")
        self.assertEqual(
            options,
            [
                {"label": "the board", "recommended": True},
                {"label": "the tab", "recommended": False},
            ],
        )

    def test_no_recommendation_marks_none(self):
        options = ask.build_options(["a", "b"], "")
        self.assertEqual([o["recommended"] for o in options], [False, False])

    def test_recommend_outside_the_labels_is_refused(self):
        with self.assertRaises(ValueError) as ctx:
            ask.build_options(["a", "b"], "c")
        self.assertIn("not one of the --option labels", str(ctx.exception))

    def test_empty_label_is_refused(self):
        with self.assertRaises(ValueError):
            ask.build_options(["a", "  "], "")


class ProducerKindTest(unittest.TestCase):
    """The store validates `producer_kind` against a closed enum and rejects an
    unknown value with a `400` naming the field, so an invented kind used to cost
    a round trip and read as a store fault rather than a bad argument. `choices=`
    refuses it client-side instead, which is correct only while this tuple still
    carries the four the store accepts.

    ⚠️ **Nothing here checks that against the store.** The enum's source is
    `attention-controller` `pkg/producer-kind.go`, a sibling repo this suite does
    not read, so the second case pins the tuple to the four values this repo
    *records* — the comment block at `scripts/pod-attention.py:104` and its mirror
    in `test_pod_attention.py`. A local edit that widens or narrows the tuple
    fails there; a change on the store's side passes silently, and the e2e is what
    catches that.
    """

    def test_an_invented_kind_is_refused_before_the_store_is_asked(self):
        for argv in (
            ["post", "--dedup-key", "k", "--payload", "p"],
            ["post-batch", "--dedup-key", "k", "--task", "t"],
        ):
            with self.subTest(cmd=argv[0]):
                err = io.StringIO()
                with mock.patch("sys.stderr", err):
                    with self.assertRaises(SystemExit) as ctx:
                        ask.main(argv + ["--producer-kind", "worker"])
                self.assertEqual(ctx.exception.code, 2)
                # Asserting only the exit code would not discriminate: a missing
                # required argument also exits 2. The refusal must name the
                # offending value, which is the whole point of the client-side
                # check over the store's round-trip rejection.
                self.assertIn("invalid choice", err.getvalue())
                self.assertIn("worker", err.getvalue())

    def test_the_accepted_kinds_are_pinned_to_the_recorded_four(self):
        # The recorded four, not a read of the store's Go source — see the class
        # docstring. A divergence on the store's side is the e2e's to catch.
        self.assertEqual(
            ask.PRODUCER_KINDS, ("session", "agent", "cron", "dark-factory")
        )


class PostTest(unittest.TestCase):
    def test_refuses_without_a_producer_id(self):
        out = io.StringIO()
        with mock.patch.dict(os.environ, {}, clear=True):
            rc = ask.cmd_post(post_args(producer_id=""), out=out)
        self.assertEqual(rc, 2)
        self.assertIn("REFUSED", out.getvalue())
        self.assertIn("poll it back", out.getvalue())

    def test_posts_the_declaration_and_prints_the_item_id(self):
        captured = {}

        def fake_urlopen(req, timeout=None):
            captured["url"] = req.full_url
            captured["body"] = json.loads(req.data.decode())
            return FakeResponse({"item_id": STORE_ITEM_ID})

        out = io.StringIO()
        with mock.patch.object(ask.urllib.request, "urlopen", fake_urlopen):
            rc = ask.cmd_post(
                post_args(option=["the board", "the tab"], recommend="the board"),
                out=out,
            )

        self.assertEqual(rc, 0)
        self.assertTrue(captured["url"].endswith("/api/1.0/attention"))
        body = captured["body"]
        self.assertEqual(body["producer_id"], "session-a")
        # ⚠️ `owner:`, not `session:` — and this assertion is load-bearing rather
        # than cosmetic. A `session:` ref ties the card to the life of the
        # session that posted it, and the store prunes an open asked item whose
        # liveness subject is gone, removing its history row with it — so a
        # manager's card was deleted the moment the manager's session ended,
        # after a `201` and with nothing reported to the producer. Measured
        # 2026-10-06: a fleet restart killed the poster and the operator's
        # approval question vanished silently.
        self.assertEqual(body["liveness_ref"], "owner:session-a")
        self.assertEqual(body["answer_mechanism"], "message")
        self.assertEqual(body["options"][0], {"label": "the board", "recommended": True})
        self.assertIn(f"ITEM_ID: {STORE_ITEM_ID}", out.getvalue())

    def test_expires_at_defaults_to_a_bound_so_an_owner_card_cannot_live_forever(self):
        """The other half of the `owner:` default, and it has to ship with it.

        ⚠️ An `owner:` item is never pruned by liveness — `OwnerLivenessModel`
        returns true unconditionally, because a human owner's absence cannot be
        read from the session registry. So the producer's life stopped being the
        bound at exactly the moment the card stopped dying with it, and this
        field is what replaced it. Without a default the board would only grow.
        """
        captured = {}

        def fake_urlopen(req, timeout=None):
            captured["body"] = json.loads(req.data.decode())
            return FakeResponse({"item_id": STORE_ITEM_ID})

        before = datetime.now(timezone.utc)
        with mock.patch.object(ask.urllib.request, "urlopen", fake_urlopen):
            ask.cmd_post(post_args(), out=io.StringIO())
        after = datetime.now(timezone.utc)

        sent = datetime.fromisoformat(captured["body"]["expires_at"])
        ttl = timedelta(hours=ask.DEFAULT_ASK_TTL_HOURS)
        self.assertGreaterEqual(sent, before + ttl)
        self.assertLessEqual(sent, after + ttl)

    def test_an_explicit_expires_at_wins_over_the_default(self):
        """The default must not swallow a caller who knows its own bound."""
        captured = {}

        def fake_urlopen(req, timeout=None):
            captured["body"] = json.loads(req.data.decode())
            return FakeResponse({"item_id": STORE_ITEM_ID})

        with mock.patch.object(ask.urllib.request, "urlopen", fake_urlopen):
            ask.cmd_post(
                post_args(expires_at="2030-01-01T00:00:00+00:00"),
                out=io.StringIO(),
            )

        self.assertEqual(captured["body"]["expires_at"], "2030-01-01T00:00:00+00:00")

    def test_empty_optionals_are_omitted_not_sent_blank(self):
        captured = {}

        def fake_urlopen(req, timeout=None):
            captured["body"] = json.loads(req.data.decode())
            return FakeResponse({"item_id": STORE_ITEM_ID})

        with mock.patch.object(ask.urllib.request, "urlopen", fake_urlopen):
            ask.cmd_post(post_args(), out=io.StringIO())

        body = captured["body"]
        # ⚠️ `expires_at` is deliberately NOT in this list any more. Omitting it
        # left every card unbounded, which was harmless only while the store
        # pruned a dead producer's ask on the next read. The `owner:` liveness
        # default stops that prune, so the bound moved from "the producer's
        # life" to this field and the poster now always declares one.
        for key in ("context", "options"):
            self.assertNotIn(key, body, f"{key} must be omitted when empty, not sent as ''")
        self.assertIn("expires_at", body)

    def test_present_optionals_are_sent(self):
        captured = {}

        def fake_urlopen(req, timeout=None):
            captured["body"] = json.loads(req.data.decode())
            return FakeResponse({"item_id": STORE_ITEM_ID})

        with mock.patch.object(ask.urllib.request, "urlopen", fake_urlopen):
            ask.cmd_post(
                post_args(context="why", option=["a"], expires_at="2026-10-01T00:00:00Z"),
                out=io.StringIO(),
            )

        body = captured["body"]
        self.assertEqual(body["context"], "why")
        self.assertEqual(body["expires_at"], "2026-10-01T00:00:00Z")
        self.assertEqual(len(body["options"]), 1)

    def test_store_rejection_is_reported_with_its_detail(self):
        def fake_urlopen(req, timeout=None):
            raise urllib.error.HTTPError(
                req.full_url, 400, "Bad Request", None, io.BytesIO(b'{"error":"options not allowed"}')
            )

        out = io.StringIO()
        with mock.patch.object(ask.urllib.request, "urlopen", fake_urlopen):
            rc = ask.cmd_post(post_args(), out=out)

        self.assertEqual(rc, 1)
        self.assertIn("400", out.getvalue())
        self.assertIn("options not allowed", out.getvalue())


OPERATOR_CLIENT = {
    "user_agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/153.0.0.0 Safari/537.36",
    "remote_addr": "127.0.0.1:63415",
    "automation": False,
}


def answered_lines(text):
    """Only the lines that ARE an answer.

    ⚠️ A bare `assertNotIn("ANSWERED:", text)` is not this check:
    `NOT_OPERATOR_ANSWERED:` contains `ANSWERED:`, so it would fail on exactly
    the output it is meant to accept.
    """
    return [line for line in text.splitlines() if line.startswith("ANSWERED:")]


class PollTest(unittest.TestCase):
    def poll(self, item):
        with mock.patch.object(
            ask.urllib.request, "urlopen", lambda req, timeout=None: FakeResponse(item)
        ):
            out = io.StringIO()
            rc = ask.cmd_poll(STORE_ITEM_ID, out=out)
        return rc, out.getvalue()

    def test_open_item_reads_as_open(self):
        rc, text = self.poll({"item_id": STORE_ITEM_ID, "state": "open"})
        self.assertEqual(rc, 0)
        self.assertIn("OPEN", text)

    def test_option_answer_is_rendered_with_its_value(self):
        rc, text = self.poll(
            {
                "item_id": STORE_ITEM_ID,
                "state": "answered",
                "answer": {"kind": "option", "value": "the board"},
                "answered_by": "attention-board",
                # An operator answer carries the store-read client record. It is
                # in the fixture because the poll now gates on it: without one
                # the same payload reads NOT_OPERATOR_ANSWERED, which is the
                # fail-closed rule and not a rendering change.
                "answered_client": {
                    "user_agent": "Mozilla/5.0 (Macintosh) Chrome/153.0.0.0",
                    "remote_addr": "127.0.0.1:63415",
                    "automation": False,
                },
            }
        )
        self.assertEqual(rc, 0)
        self.assertIn("ANSWERED: option: the board", text)
        self.assertIn("ANSWERED_BY: attention-board", text)

    def test_skip_is_rendered_without_a_trailing_value(self):
        _, text = self.poll(
            {
                "item_id": STORE_ITEM_ID,
                "state": "answered",
                "answer": {"kind": "skip"},
                "answered_client": {"user_agent": "curl/8.7.1", "remote_addr": "127.0.0.1:1"},
            }
        )
        self.assertIn("ANSWERED: skip", text)
        # A trailing colon-space would read as a truncated option answer.
        self.assertNotIn("skip:", text)

    def test_answer_with_no_client_is_not_operator_answered(self):
        # ⚠️ The fail-closed case, and the one this change exists for: an answer
        # recorded before `answered_client` existed, or posted by a client the
        # store could not describe. It must NOT satisfy a caller's gate.
        rc, text = self.poll(
            {
                "item_id": STORE_ITEM_ID,
                "state": "answered",
                "answer": {"kind": "option", "value": "the board"},
                "answered_by": "attention-board",
                "answered_at": "2026-09-26T20:19:18Z",
            }
        )
        self.assertEqual(rc, 0)
        self.assertEqual(answered_lines(text), [])
        self.assertIn("NOT_OPERATOR_ANSWERED: option: the board", text)

    def test_automation_true_is_not_operator_answered(self):
        # The positive control for a scripted click: the page reports
        # navigator.webdriver, the store records it, and the gate stays shut.
        _, text = self.poll(
            {
                "item_id": STORE_ITEM_ID,
                "state": "answered",
                "answer": {"kind": "option", "value": "the board"},
                "answered_by": "attention-board",
                "answered_client": {
                    "user_agent": "Mozilla/5.0 (Macintosh) Chrome/153.0.0.0",
                    "remote_addr": "127.0.0.1:55022",
                    "automation": True,
                },
            }
        )
        self.assertEqual(answered_lines(text), [])
        self.assertIn("NOT_OPERATOR_ANSWERED:", text)
        self.assertIn("automation: true", text)

    def test_ack_close_with_no_client_is_not_operator_answered(self):
        # The close path, which writes `answered_by` and never `answered_at`.
        # A consumer reading `closed` as resolved would release the gate here.
        _, text = self.poll(
            {
                "item_id": STORE_ITEM_ID,
                "state": "closed",
                "answered_by": "attention-board",
            }
        )
        self.assertIn("NOT_OPERATOR_ANSWERED:", text)
        self.assertIn("item is closed", text)

    def test_multi_question_answer_is_rendered_from_answers(self):
        # ⚠️ `answer` and `answers` are mutually exclusive by construction: an
        # item carrying `questions` is answered through `answers`, one entry per
        # tab. A renderer that read only `answer` would call this item
        # contentless, which is the state a manager polls to escape.
        _, text = self.poll(
            {
                "item_id": STORE_ITEM_ID,
                "state": "answered",
                "answered_at": "2026-09-26T21:01:27Z",
                "answered_by": "attention-board",
                "answered_client": OPERATOR_CLIENT,
                "questions": [{"tab": "Only", "payload": "Pick one?"}],
                "answers": [{"question": "Only", "kind": "option", "value": "A"}],
            }
        )
        self.assertIn("ANSWERED: Only: option: A", text)

    def test_multiple_pick_answer_renders_its_values(self):
        _, text = self.poll(
            {
                "item_id": STORE_ITEM_ID,
                "state": "answered",
                "answered_at": "2026-09-26T21:01:27Z",
                "answered_client": OPERATOR_CLIENT,
                "answers": [
                    {"question": "Chores", "kind": "option",
                     "values": ["Broken wikilinks", "Huge pages"]},
                ],
            }
        )
        self.assertIn("ANSWERED: Chores: option: Broken wikilinks, Huge pages", text)

    def test_answered_permission_item_is_not_reported_as_open(self):
        # A permission item carries a `decision` and never an `answer`, and the
        # schema keeps the two apart on purpose — so `describe_answer` reads
        # neither for it. It must still not read OPEN: the item DID move, and
        # `OPEN` would invite a caller to keep polling a settled gate.
        _, text = self.poll(
            {
                "item_id": STORE_ITEM_ID,
                "state": "answered",
                "answered_at": "2026-09-26T21:01:27Z",
                "answered_by": "attention-board",
                "answered_client": OPERATOR_CLIENT,
                "decision": "allow",
            }
        )
        self.assertIn("ANSWERED:", text)
        self.assertNotIn("\nOPEN", text)
        self.assertIn("item is answered", text)

    def test_attributed_item_with_no_content_names_its_state_not_closed(self):
        # `closed` is a different transition. Saying it here would misdescribe
        # what happened to the item.
        _, text = self.poll(
            {
                "item_id": STORE_ITEM_ID,
                "state": "answered",
                "answered_at": "2026-09-26T21:01:27Z",
                "answered_client": OPERATOR_CLIENT,
            }
        )
        self.assertIn("item is answered", text)
        self.assertNotIn("item is closed", text)

    def test_reaped_close_with_no_actor_reads_as_open(self):
        # The dominant closed shape in the live store is a reap: no
        # answered_at, no answered_by, no client. It is not an act by anyone,
        # so it must not be reported as an answer in either direction.
        rc, text = self.poll({"item_id": STORE_ITEM_ID, "state": "closed"})
        self.assertEqual(rc, 0)
        self.assertIn("OPEN", text)
        self.assertNotIn("NOT_OPERATOR_ANSWERED", text)

    def test_answer_absent_reads_as_open_even_when_state_says_answered(self):
        # A permission-class item is answered with a `decision` and carries no
        # `answer`, so the poll must not report content that is not there.
        rc, text = self.poll(
            {"item_id": STORE_ITEM_ID, "state": "answered", "decision": "allow"}
        )
        self.assertEqual(rc, 0)
        self.assertIn("OPEN", text)

    def poll_error(self, code, body=b""):
        # fetch_item passes a URL string to urlopen, not a Request, so the fake
        # takes a str — reading `.full_url` here would be an AttributeError, not
        # a raised HTTPError.
        def fake(url, timeout=None):
            raise urllib.error.HTTPError(url, code, "err", None, io.BytesIO(body))

        with mock.patch.object(ask.urllib.request, "urlopen", fake):
            out = io.StringIO()
            rc = ask.cmd_poll(STORE_ITEM_ID, out=out)
        return rc, out.getvalue()

    def test_missing_item_names_the_item(self):
        rc, text = self.poll_error(404)
        self.assertEqual(rc, 1)
        self.assertIn(f"no such item {STORE_ITEM_ID}", text)

    def test_store_error_names_the_item_and_carries_the_detail(self):
        # A poll runs unattended on a loop tick, so a bare "store returned 500"
        # naming no item is not something a manager can act on.
        rc, text = self.poll_error(500, b'{"error":"boom"}')
        self.assertEqual(rc, 1)
        self.assertIn(STORE_ITEM_ID, text)
        self.assertIn("boom", text)

    # --- the id-shape guard -------------------------------------------------
    #
    # The control has to be two-sided. Before the guard, a short id and an
    # unknown full id both printed `FAILED: no such item <id>` and differed only
    # by the echoed id, so any assertion that the output merely *changed* passed
    # on the unfixed code. What discriminates is the pair below: a short id is
    # refused without the store being asked, and a well-formed id the store does
    # not hold still reads as absence (the 404 tests above, which now carry a
    # real 32-hex id).

    def test_a_short_id_is_refused_before_the_store_is_asked(self):
        # The store is mocked to blow up if it is reached at all: a refusal that
        # still issued the request would be a message change, not a guard.
        # `70eb5521` is the tick-127 id from the incident, verbatim.
        def explode(url, timeout=None):
            raise AssertionError(f"poll reached the store with {url!r}")

        with mock.patch.object(ask.urllib.request, "urlopen", explode):
            out = io.StringIO()
            rc = ask.cmd_poll("70eb5521", out=out)
        text = out.getvalue()
        self.assertEqual(rc, 2)
        self.assertIn("REFUSED", text)
        # The false negative the guard exists to remove: this must not be
        # readable as the item having gone away.
        self.assertNotIn("no such item", text)
        self.assertIn("NEITHER MEANS", text)

    def test_the_ledger_display_id_is_refused_too(self):
        # `open-items.py` mints its own 8-char id (`uuid.uuid4().hex[:8]`) and
        # `cmd_list` renders it. It is NOT a truncation of a store id -- it is
        # an unrelated uuid -- and it is equally unpollable, so the refusal has
        # to cover it without claiming it was truncated.
        def explode(url, timeout=None):
            raise AssertionError(f"poll reached the store with {url!r}")

        with mock.patch.object(ask.urllib.request, "urlopen", explode):
            out = io.StringIO()
            rc = ask.cmd_poll("8e1854a7", out=out)
        self.assertEqual(rc, 2)
        self.assertIn("REFUSED", out.getvalue())

    def test_a_well_formed_id_still_reaches_the_store(self):
        # The positive control: the guard refuses a shape, not everything. Every
        # other test in this class leans on it, since they all poll STORE_ITEM_ID.
        rc, text = self.poll({"item_id": STORE_ITEM_ID, "state": "open"})
        self.assertEqual(rc, 0)
        self.assertIn("OPEN", text)


class RecordPostedCloserTest(unittest.TestCase):
    """The opt-in record that lets the hook and the feed decline a closer echo.

    The write is best-effort and must never turn a successful post into a
    failure, so most of these cases assert that nothing happened rather than
    that something did.
    """

    def setUp(self):
        self._dir = tempfile.mkdtemp()
        self._orig = ask.STATE_DIR
        ask.STATE_DIR = self._dir

    def tearDown(self):
        ask.STATE_DIR = self._orig
        shutil.rmtree(self._dir, ignore_errors=True)

    def _names(self):
        return sorted(os.listdir(self._dir))

    def test_an_empty_closer_writes_nothing_and_creates_no_state_dir(self):
        shutil.rmtree(self._dir)
        ask.record_posted_closer("session-a", "", "k")
        self.assertFalse(os.path.exists(self._dir))

    def test_a_non_string_closer_writes_nothing(self):
        # `post_args` builds a Mock, so an unset `--closer` arrives as a TRUTHY
        # Mock rather than as "". The type check is the only thing standing
        # between this suite and the operator's real state dir.
        ask.record_posted_closer("session-a", mock.Mock(), "k")
        self.assertEqual(self._names(), [])

    def test_writes_one_record_named_for_the_producer(self):
        with mock.patch.object(ask, "resolved_session_id", return_value=""):
            ask.record_posted_closer("session-a", "pick — 1. x · 2. y", "k")
        self.assertEqual(self._names(), ["session-a.posted.json"])

    def test_writes_a_second_record_named_for_the_session_when_it_differs(self):
        # The hook keys the record by session_id and the feed keys it by the
        # store item's producer_id. An explicit --producer-id makes those differ,
        # so a single key would write the record where nothing looks.
        with mock.patch.object(ask, "resolved_session_id", return_value="session-b"):
            ask.record_posted_closer("session-a", "pick — 1. x · 2. y", "k")
        self.assertEqual(
            self._names(), ["session-a.posted.json", "session-b.posted.json"]
        )

    def test_records_the_closer_its_dedup_key_and_a_timestamp(self):
        with mock.patch.object(ask, "resolved_session_id", return_value=""):
            ask.record_posted_closer("session-a", "pick — 1. x · 2. y", "cap-1")
        with open(
            os.path.join(self._dir, "session-a.posted.json"), encoding="utf-8"
        ) as f:
            rec = json.load(f)
        self.assertEqual(rec["closer"], "pick — 1. x · 2. y")
        self.assertEqual(rec["dedup_key"], "cap-1")
        self.assertGreater(rec["ts"], 0)

    def test_a_failed_write_leaves_no_tmp_and_does_not_raise(self):
        with mock.patch.object(ask, "resolved_session_id", return_value=""):
            with mock.patch.object(ask.os, "replace", side_effect=OSError("boom")):
                ask.record_posted_closer("session-a", "pick — 1. x · 2. y", "k")
        self.assertEqual([n for n in self._names() if n.endswith(".tmp")], [])


if __name__ == "__main__":
    unittest.main()
