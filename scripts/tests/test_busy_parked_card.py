#!/usr/bin/env python3
"""Tests for the parked-`busy` card: `busy_parked_session_ids()` + the tick's card pass.

The shape under test is the one `candidate_session_ids()` can never return — a registry
record that is `entrypoint: sdk-cli` and **not** `status: idle`, whose wrapper record reads
`state: idle`, `pending_turns: 0` and a `question` parked past `IDLE_FLOOR`. The reaper
skips it at its own filter, so nothing refuses it and nothing closes it; the operator's
2026-11-13 ruling is that it gets an attention card instead of a widened guard.

What the tests pin, and the lazy build each one defeats:

  1. **Two parked sessions in one tick -> exactly two cards.** A build that posts once and
     then goes permanently silent passes "one card, then zero" and re-creates the exact
     invisibility this change exists to end.
  2. **A second tick over the same park posts nothing new**, but a **re-park** with a new
     `requested_at` does — so dedup is per session *and* per park, not per session id.
  3. **The payload names its own session and its integer park age.** The count alone passes
     a build that posts one fixed body twice.
  4. **Every negative shape**: `generating`, `pending_turns != 0`, no `question`,
     within the bound, `question: false`, `entrypoint != sdk-cli`, registry `idle`, an
     unageable `requested_at`, and a session carrying its own open card.
  5. **Two positives that defeat a `== "busy"` build**: `status: "starting"` and a record
     with no `status` at all must both card.
  6. **The wrapper join**: a ledger mapping the registry id to a different
     `wrapper_session_id` must card — a build reading the wrapper by `sessionId` matches
     nothing for every wrapper-backed worker and passes everything else.
  7. **`--dry-run` posts nothing and writes no marker**, so an operator's close-nothing pass
     cannot mint live cards.

Run: python3 -m unittest discover -s scripts/tests -v
"""

import http.server
import importlib.util
import json
import os
import shutil
import tempfile
import threading
import time
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_SCRIPTS = os.path.dirname(_HERE)


def _load(name, filename):
    spec = importlib.util.spec_from_file_location(name, os.path.join(_SCRIPTS, filename))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


reap = _load("reap_idle_headless", "reap-idle-headless.py")
tick_mod = _load("idle_reaper_tick", "idle-reaper-tick.py")

SID_A = "aaaaaaaa-1111-4111-8111-111111111111"
SID_B = "bbbbbbbb-2222-4222-8222-222222222222"
SID_C = "cccccccc-3333-4333-8333-333333333333"

#: A fixed clock, so "past the bound" and "within the bound" are arithmetic, not wall time.
NOW = 1_800_000_000.0
OLD = "2026-11-13T09:00:00+00:00"  # far past NOW - 900 by construction below
QUESTION_OLD = {"requested_at": OLD}


def _stamp(seconds_ago):
    """An ISO `requested_at` `seconds_ago` before NOW."""
    import datetime

    moment = datetime.datetime.fromtimestamp(NOW - seconds_ago, datetime.timezone.utc)
    return moment.isoformat()


class _Store(http.server.BaseHTTPRequestHandler):
    """A stub ledger + wrapper store + attention store; the test sets the payloads."""

    ledger = {}
    wrapper = {}
    items = []

    def do_GET(self):
        path = self.path
        if path.startswith("/api/1.0/session-ledger/"):
            sid = path.rsplit("/", 1)[-1]
            body = json.dumps(_Store.ledger.get(sid, {})).encode()
        elif path.startswith("/api/1.0/sessions/"):
            handle = path.rsplit("/", 1)[-1]
            body = json.dumps(_Store.wrapper.get(handle, {})).encode()
        elif path == "/api/1.0/attention":
            body = json.dumps(_Store.items).encode()
        else:
            self.send_response(404)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="parked-card-")
        self.sessions = os.path.join(self.tmp, "sessions")
        os.makedirs(self.sessions)
        self.marker = os.path.join(self.tmp, "carded.json")
        _Store.ledger = {}
        _Store.wrapper = {}
        _Store.items = []
        self.httpd = http.server.HTTPServer(("127.0.0.1", 0), _Store)
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        # The module reads its store addresses at import time, so point the real constants
        # at this stub — otherwise `card_parked_sessions` (which takes no URLs, exactly as
        # production calls it) would talk to the live localhost stores.
        self._saved = (reap.WRAPPER_URL, reap.LEDGER_URL, reap.ATTENTION_URL)
        reap.WRAPPER_URL = reap.LEDGER_URL = reap.ATTENTION_URL = self.base

    def tearDown(self):
        reap.WRAPPER_URL, reap.LEDGER_URL, reap.ATTENTION_URL = self._saved
        self.httpd.shutdown()
        self.httpd.server_close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    # --- fixtures ---------------------------------------------------------------------

    def _session(self, sid, status="busy", entrypoint="sdk-cli", **over):
        rec = {"sessionId": sid, "entrypoint": entrypoint}
        if status is not None:
            rec["status"] = status
        rec.update(over)
        with open(os.path.join(self.sessions, f"{sid}.json"), "w", encoding="utf-8") as h:
            json.dump(rec, h)

    def _wrapper(self, sid, handle=None, state="idle", pending_turns=0, question="default"):
        if handle is not None:
            _Store.ledger[sid] = {"wrapper_session_id": handle}
        _Store.wrapper[handle or sid] = {
            "id": handle or sid,
            "state": state,
            "pending_turns": pending_turns,
            "question": QUESTION_OLD if question == "default" else question,
        }

    def _parked(self, **over):
        return reap.busy_parked_session_ids(
            self.sessions,
            wrapper_url=self.base,
            ledger_url=self.base,
            attention_url=self.base,
            now=NOW,
            **over,
        )

    def _ids(self, **over):
        return [row[0] for row in self._parked(**over)]

    def _cards(self, dry_run=False, post=None, now=None):
        calls = []

        def fake(session_id, requested_at, park_age):
            calls.append((session_id, requested_at, park_age))
            return True, f"item-{session_id[:4]}"

        cards, ok = tick_mod.card_parked_sessions(
            reap,
            self.sessions,
            dry_run=dry_run,
            now=NOW if now is None else now,
            marker_path=self.marker,
            post=post or fake,
        )
        return cards, ok, calls


class TestShape(_Base):
    """The positive shape, and every negative that bounds it."""

    def test_a_parked_busy_session_is_found_with_its_age(self):
        self._session(SID_A)
        self._wrapper(SID_A)
        found = self._parked()
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0][0], SID_A)
        self.assertGreater(found[0][1], reap.IDLE_FLOOR)
        self.assertEqual(found[0][2], OLD, "the dedup clock is the park timestamp")

    def test_a_starting_session_cards(self):
        """Defeats a `== "busy"` build: the guard refuses any non-idle status."""
        self._session(SID_A, status="starting")
        self._wrapper(SID_A)
        self.assertEqual(self._ids(), [SID_A])

    def test_a_record_with_no_status_cards(self):
        """The guard renders it `'(none)'` and refuses `busy-target` all the same."""
        self._session(SID_A, status=None)
        self._wrapper(SID_A)
        self.assertEqual(self._ids(), [SID_A])

    def test_a_registry_idle_session_never_cards(self):
        """It is an ordinary candidate; carding it would double-report."""
        self._session(SID_A, status="idle")
        self._wrapper(SID_A)
        self.assertEqual(self._ids(), [])

    def test_a_tab_session_never_cards(self):
        self._session(SID_A, entrypoint="cli")
        self._wrapper(SID_A)
        self.assertEqual(self._ids(), [])

    def test_a_generating_wrapper_never_cards(self):
        self._session(SID_A)
        self._wrapper(SID_A, state="generating")
        self.assertEqual(self._ids(), [])

    def test_pending_turns_never_cards(self):
        self._session(SID_A)
        self._wrapper(SID_A, pending_turns=2)
        self.assertEqual(self._ids(), [])

    def test_no_question_never_cards(self):
        self._session(SID_A)
        self._wrapper(SID_A, question=None)
        self.assertEqual(self._ids(), [])

    def test_question_false_never_cards(self):
        """The named discriminator: `19a80954` read `question: false` at 316 s.

        A boolean is not a dict, so `question_age_seconds` reports "unageable" and the
        session is dropped — which is what defeats a build that cards every `busy-target`
        refusal.
        """
        self._session(SID_A)
        self._wrapper(SID_A, question=False)
        self.assertEqual(self._ids(), [])

    def test_an_empty_question_never_cards(self):
        self._session(SID_A)
        self._wrapper(SID_A, question={})
        self.assertEqual(self._ids(), [])

    def test_a_park_within_the_bound_never_cards(self):
        self._session(SID_A)
        self._wrapper(SID_A, question={"requested_at": _stamp(60)})
        self.assertEqual(self._ids(), [])

    def test_an_unageable_park_never_cards(self):
        self._session(SID_A)
        self._wrapper(SID_A, question={"requested_at": "not-a-date"})
        self.assertEqual(self._ids(), [])

    def test_a_session_carrying_its_own_open_card_never_cards(self):
        """Already visible to a human, so a second card would be a duplicate."""
        self._session(SID_A)
        self._wrapper(SID_A)
        _Store.items = [
            {"item_id": "x" * 32, "producer_id": SID_A, "state": "open"},
        ]
        self.assertEqual(self._ids(), [])

    def test_an_unreadable_store_fails_closed(self):
        """`None` is "could not read", never "no cards" — a blind post could duplicate."""
        self._session(SID_A)
        self._wrapper(SID_A)
        self.assertEqual(
            reap.busy_parked_session_ids(
                self.sessions,
                wrapper_url=self.base,
                ledger_url=self.base,
                attention_url="http://127.0.0.1:1",  # nothing listens here
                now=NOW,
            ),
            [],
        )

    def test_a_distinct_wrapper_handle_still_cards(self):
        """⚠️ The join. A build reading the wrapper by `sessionId` matches nothing here."""
        self._session(SID_A)
        self._wrapper(SID_A, handle="wrapper-handle-1")
        self.assertEqual(self._ids(), [SID_A])

    def test_the_same_fixture_queried_by_the_registry_id_finds_nothing(self):
        """The control for the join test: the wrapper record exists ONLY under the handle.

        This is the trap a build reading the wrapper by `sessionId` falls into — the
        lookup succeeds, returns `{}`, and every wrapper-backed worker is silently skipped.
        """
        self._session(SID_A)
        self._wrapper(SID_A, handle="wrapper-handle-1")
        self.assertEqual(
            reap.fetch_json(f"{self.base}/api/1.0/sessions/{SID_A}"),
            {},
            "the registry id must not resolve to a wrapper record here",
        )
        self.assertEqual(
            reap.wrapper_record(SID_A, self.base, self.base).get("state"),
            "idle",
            "while the handle-based read does resolve",
        )


class TestCards(_Base):
    """SC1/SC2: one card per session per park, and never a close."""

    def test_two_parked_sessions_in_one_tick_produce_two_cards(self):
        self._session(SID_A)
        self._session(SID_B)
        self._wrapper(SID_A)
        self._wrapper(SID_B)
        cards, ok, calls = self._cards()
        self.assertTrue(ok)
        self.assertEqual([c[0] for c in cards], [SID_A, SID_B])
        self.assertEqual([c[1] for c in cards], ["carded:item-aaaa", "carded:item-bbbb"])
        self.assertEqual([c[0] for c in calls], [SID_A, SID_B])

    def test_the_second_tick_posts_nothing_new(self):
        self._session(SID_A)
        self._session(SID_B)
        self._wrapper(SID_A)
        self._wrapper(SID_B)
        self._cards()
        cards, ok, calls = self._cards()
        self.assertTrue(ok)
        self.assertEqual(calls, [], "a second tick over the same park must post nothing")
        self.assertEqual([c[1] for c in cards], ["deduped", "deduped"])

    def test_a_repark_posts_a_second_card(self):
        """Dedup is per session AND per park — a re-park is a new card, not a silent one."""
        self._session(SID_A)
        self._wrapper(SID_A)
        self._cards()
        _Store.wrapper[SID_A]["question"] = {"requested_at": _stamp(1200)}
        cards, ok, calls = self._cards()
        self.assertTrue(ok)
        self.assertEqual([c[0] for c in calls], [SID_A])
        self.assertTrue(cards[0][1].startswith("carded:"))

    def test_a_card_that_expired_is_reposted(self):
        """The 24 h TTL: a park that outlives its card resurfaces rather than vanishing."""
        self._session(SID_A)
        self._wrapper(SID_A)
        self._cards()
        cards, ok, calls = self._cards(now=NOW + tick_mod.CARD_TTL_SECONDS + 1)
        self.assertTrue(ok)
        self.assertEqual([c[0] for c in calls], [SID_A])

    def test_a_cleared_card_does_not_return_every_tick(self):
        """The operator clearing the card must not resurrect it 900 s later."""
        self._session(SID_A)
        self._wrapper(SID_A)
        self._cards()
        _Store.items = []  # the operator cleared it; the store no longer lists it
        cards, ok, calls = self._cards()
        self.assertEqual(calls, [])
        self.assertEqual([c[1] for c in cards], ["deduped"])

    def test_a_dry_run_posts_nothing_and_writes_no_marker(self):
        self._session(SID_A)
        self._wrapper(SID_A)
        cards, ok, calls = self._cards(dry_run=True)
        self.assertTrue(ok)
        self.assertEqual(calls, [])
        self.assertEqual([c[1] for c in cards], ["would-card"])
        self.assertFalse(os.path.exists(self.marker), "a dry run must not write the marker")

    def test_a_failed_post_is_a_fault_and_is_not_marked(self):
        self._session(SID_A)
        self._wrapper(SID_A)
        cards, ok, _ = self._cards(post=lambda *a: (False, "transport"))
        self.assertFalse(ok)
        self.assertEqual(cards[0][1], "error:transport")
        self.assertFalse(os.path.exists(self.marker), "a failed post must not be deduped")

    def test_the_card_pass_never_signals_or_touches_the_registry(self):
        """SC2: the card is the only output. Nothing is closed, and the pid stays alive."""
        self._session(SID_A)
        self._wrapper(SID_A)
        path = os.path.join(self.sessions, f"{SID_A}.json")
        before = os.path.getmtime(path)
        self._cards()
        self.assertTrue(os.path.exists(path), "the registry entry must survive")
        self.assertEqual(os.path.getmtime(path), before, "the registry entry must be untouched")


class TestPayload(unittest.TestCase):
    """The wording is the operator's ruling made checkable."""

    def test_the_payload_says_not_reaped_and_names_busy_target(self):
        body = tick_mod.park_payload(SID_A, 1234.7)
        self.assertIn("not reaped", body)
        self.assertIn("busy-target", body)

    def test_the_payload_names_the_session_and_an_integer_park_age(self):
        body = tick_mod.park_payload(SID_A, 1234.7)
        self.assertIn(SID_A, body)
        self.assertIn("1234s", body)
        self.assertNotIn("1234.7", body)

    def test_the_payload_never_implies_a_close(self):
        body = tick_mod.park_payload(SID_A, 1234.7).lower()
        for forbidden in ("closed the session", "terminated", "killed", "sigterm"):
            self.assertNotIn(forbidden, body)
        self.assertIn("nothing was closed", body)


class TestRealEntryPoint(unittest.TestCase):
    """⚠️ Drives the real `attention-ask.py` as a subprocess, never a mock.

    A mocked poster passes even when the CLI rejects the argv — which is exactly how the
    missing `--producer-id` survived a round of planning. The assertion is on the failure
    *text*: a usage error and a transport failure are different facts, and only the second
    proves the argv was accepted. The store is pointed at a dead port so nothing is posted.
    """

    def test_the_argv_is_accepted_by_the_real_parser(self):
        dead = "http://127.0.0.1:1"
        previous = os.environ.get("ATTENTION_STORE_URL")
        os.environ["ATTENTION_STORE_URL"] = dead
        try:
            ok, detail = tick_mod.post_park_card(SID_A, OLD, 1234.0)
        finally:
            if previous is None:
                os.environ.pop("ATTENTION_STORE_URL", None)
            else:
                os.environ["ATTENTION_STORE_URL"] = previous

        self.assertFalse(ok, "a dead store must not read as a posted card")
        lowered = detail.lower()
        for usage in ("unrecognized arguments", "the following arguments are required", "invalid choice"):
            self.assertNotIn(usage, lowered, f"argv was rejected by the parser: {detail!r}")
        self.assertTrue(
            any(
                word in lowered
                for word in ("unreachable", "transport", "connection", "failed", "refused")
            ),
            f"expected a store/transport failure naming the cause, got: {detail!r}",
        )


if __name__ == "__main__":
    unittest.main()
