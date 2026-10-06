"""Tests for scripts/answered-watch.py.

The two properties worth pinning, because each is a way the arm could look like
it works while being wrong:

  - **The negative probe.** An item posted by a *different* session must produce
    no line. An implementation that emits for any answered item passes every
    positive test and fails the task's whole point.
  - **No wake for a pre-existing answer.** A watcher that fires for answers that
    predate it turns every manager restart into a burst of stale wakes.
"""
import importlib.util
import io
import os
import unittest
from datetime import datetime, timezone
from unittest import mock

SCRIPTS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ME = "sess-me"
OTHER = "sess-other"


def load(name, filename):
    path = os.path.join(SCRIPTS, filename)
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


watch = load("answered_watch", "answered-watch.py")


class FakeAttribution:
    """Stands in for answered-attribution.py.

    Only ids in `attributed` are operator answers; everything else — a clear, a
    scripted client, an item with no answer at all — is not. Mirrors the real
    module's one predicate, without restating its rules here.
    """

    def __init__(self, attributed):
        self.attributed = set(attributed)

    def operator_answered(self, item):
        return item.get("item_id") in self.attributed


def item(item_id, producer=ME, answered_at="2026-10-01T12:05:00+00:00", state="answered"):
    return {
        "item_id": item_id,
        "producer_id": producer,
        "state": state,
        "answered_at": answered_at,
    }


class HelpersTest(unittest.TestCase):
    def test_is_mine_matches_only_the_posting_session(self):
        self.assertTrue(watch.is_mine(item("a"), ME))
        self.assertFalse(watch.is_mine(item("a", producer=OTHER), ME))

    def test_is_mine_rejects_an_empty_session(self):
        # An empty session id would otherwise match every item whose producer
        # declared none, which is how a watcher becomes a board reader.
        self.assertFalse(watch.is_mine({"item_id": "a", "producer_id": ""}, ""))
        self.assertFalse(watch.is_mine({"item_id": "a"}, ""))

    def test_parse_ts_accepts_rfc3339_and_z(self):
        self.assertIsNotNone(watch.parse_ts("2026-10-01T12:05:00+00:00"))
        self.assertIsNotNone(watch.parse_ts("2026-10-01T12:05:00Z"))

    def test_parse_ts_returns_none_on_junk(self):
        for value in (None, "", "not-a-date", 17):
            self.assertIsNone(watch.parse_ts(value))

    def test_items_of_tolerates_both_response_shapes(self):
        self.assertEqual(watch.items_of([{"item_id": "a"}]), [{"item_id": "a"}])
        self.assertEqual(watch.items_of({"items": [{"item_id": "a"}]}), [{"item_id": "a"}])
        self.assertEqual(watch.items_of({"nope": 1}), [])
        self.assertEqual(watch.items_of("junk"), [])

    def test_fetch_history_passes_an_explicit_limit(self):
        # ⚠️ The store's history read is PAGED and the default page is 1000 items,
        # so a bare read truncates silently. `reconcile()` re-baselines from this
        # on every (re)connect, so the truncation drops an answer that arrived
        # while the stream was down — a missed wake, the failure this arm exists
        # to prevent. Asserted on the URL the reader actually builds, so dropping
        # the query string again fails here rather than in production.
        # ⚠️ This URL pin is the deliberate proxy for the behaviour: the catch-up
        # test below stubs `fetch_history`, so nothing here exercises an answer
        # sitting beyond the old 1000-item page. The URL is what removes the
        # truncation, so pinning it is what keeps that mechanism honest.
        seen = []

        class Response:
            def read(self):
                return b"[]"

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        def urlopen(url, timeout=None):
            seen.append(url)
            return Response()

        with mock.patch("urllib.request.urlopen", urlopen):
            watch.fetch_history("http://store")

        self.assertEqual(
            seen,
            [f"http://store/api/1.0/attention/history?limit={watch.HISTORY_LIMIT}"],
        )

    def test_history_limit_matches_the_reference_reader(self):
        # ⚠️ Load the sibling module rather than hardcoding its number. A test
        # that only asserts `== 50000` passes unchanged when the *reference*
        # reader's default moves — which is exactly the drift this pins.
        lookup = load("attention_card_lookup", "attention-card-lookup.py")
        self.assertEqual(watch.HISTORY_LIMIT, lookup.HISTORY_LIMIT)

    def test_history_limit_default_clears_the_store(self):
        # The literal is asserted separately from the cross-reader equality
        # above, so a coordinated change to both files still has to be
        # deliberate. 50000 clears the store's measured 25,683 items.
        self.assertEqual(watch.HISTORY_LIMIT, 50000)

    def test_fetch_history_warns_when_the_page_comes_back_full(self):
        # A full page is the only signal the read has a ceiling at all. Without
        # the warning, crossing it re-introduces the silent truncation the
        # limit itself exists to remove — so the detector is pinned here, not
        # left to the comment.

        class Response:
            def read(self):
                return b'[{"item_id": "a"}, {"item_id": "b"}, {"item_id": "c"}]'

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        stderr = io.StringIO()
        with mock.patch("urllib.request.urlopen", lambda url, timeout=None: Response()):
            with mock.patch("sys.stderr", stderr):
                items = watch.fetch_history("http://store", limit=3)

        self.assertEqual(len(items), 3)
        self.assertIn("may be truncated", stderr.getvalue())

    def test_fetch_history_is_silent_on_a_short_page(self):
        # The negative probe: a warning that fires on every read is noise the
        # operator learns to ignore, which is the same as no warning.

        class Response:
            def read(self):
                return b'[{"item_id": "a"}]'

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        stderr = io.StringIO()
        with mock.patch("urllib.request.urlopen", lambda url, timeout=None: Response()):
            with mock.patch("sys.stderr", stderr):
                watch.fetch_history("http://store", limit=3)

        self.assertEqual(stderr.getvalue(), "")


class EmitFormatTest(unittest.TestCase):
    def test_line_is_exactly_the_token_and_the_id(self):
        out = io.StringIO()
        watch.emit("be10d13e", out)
        self.assertEqual(out.getvalue(), "ANSWERED be10d13e\n")


class ReconcileTest(unittest.TestCase):
    def setUp(self):
        self.out = io.StringIO()
        self.err = io.StringIO()
        self.started = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)

    def run_reconcile(self, items, attributed=None, seen=None, session=ME):
        if attributed is None:
            attributed = {i["item_id"] for i in items}
        with mock.patch.object(watch, "fetch_history", return_value=items):
            return watch.reconcile(
                "http://store",
                session,
                set() if seen is None else seen,
                self.started,
                FakeAttribution(attributed),
                self.out,
                self.err,
            )

    def test_emits_for_own_item_answered_after_start(self):
        seen = self.run_reconcile([item("abc")])
        self.assertEqual(self.out.getvalue(), "ANSWERED abc\n")
        self.assertIn("abc", seen)

    def test_adopts_a_pre_start_answer_without_emitting(self):
        seen = self.run_reconcile([item("old", answered_at="2026-10-01T11:00:00+00:00")])
        self.assertEqual(self.out.getvalue(), "")
        self.assertIn("old", seen)

    def test_does_not_emit_for_another_sessions_item(self):
        # The negative probe: this is the property the task's SC1(b) asserts.
        seen = self.run_reconcile([item("theirs", producer=OTHER)])
        self.assertEqual(self.out.getvalue(), "")
        self.assertNotIn("theirs", seen)

    def test_does_not_emit_when_the_answer_is_not_attributed(self):
        seen = self.run_reconcile([item("abc")], attributed=set())
        self.assertEqual(self.out.getvalue(), "")
        self.assertNotIn("abc", seen)

    def test_does_not_re_emit_across_calls(self):
        items = [item("abc")]
        seen = self.run_reconcile(items)
        seen = self.run_reconcile(items, seen=seen)
        self.assertEqual(self.out.getvalue(), "ANSWERED abc\n")

    def test_catch_up_emits_an_answer_missed_while_disconnected(self):
        seen = self.run_reconcile([])
        seen = self.run_reconcile([item("late")], seen=seen)
        self.assertEqual(self.out.getvalue(), "ANSWERED late\n")
        self.assertIn("late", seen)

    def test_a_read_failure_warns_and_does_not_raise(self):
        """The reconcile twin of `HandleItemTest`'s mirror.

        An unreadable history must warn and leave `seen` unchanged — never
        propagate, because `main()` catches only `KeyboardInterrupt` and the
        process dying IS the missed wake the arm exists to prevent.

        ⚠️ Patch `fetch_history` itself. An unreachable store is **not** a
        discriminating probe: `watch()` reaches `reconcile()` only after
        `stream_item_ids` yields `CONNECTED`, which it does only once
        `urlopen(stream_url)` has succeeded — and `stream_item_ids` already
        swallows `URLError`/`OSError` and reconnects. A dead store therefore
        never arrives here, and both the pre-fix and post-fix revisions survive
        it, so that probe would report a false pass.
        """
        import urllib.error

        # The name is deliberately identical to `HandleItemTest`'s: these are
        # twins, one per guarded read path, and the symmetry is the point.
        for exc in (urllib.error.URLError("boom"), OSError("boom")):
            with self.subTest(exc=type(exc).__name__):
                self.out = io.StringIO()
                self.err = io.StringIO()
                seen = {"already"}
                with mock.patch.object(watch, "fetch_history", side_effect=exc):
                    result = watch.reconcile(
                        "http://store",
                        ME,
                        seen,
                        self.started,
                        FakeAttribution(set()),
                        self.out,
                        self.err,
                    )
                self.assertEqual(self.out.getvalue(), "")
                self.assertIn("could not read history", self.err.getvalue())
                self.assertIs(result, seen)
                self.assertEqual(result, {"already"})

        # The recovery the guard exists for, and the half a warn-only assertion
        # would miss: a failed pass must not poison the next one.
        # `stream_item_ids` yields `CONNECTED` on every reconnect, so `watch()`
        # re-enters `reconcile` — assert that second pass still emits.
        self.err = io.StringIO()
        recovered = self.run_reconcile([item("late")], seen={"already"})
        self.assertEqual(self.out.getvalue(), "ANSWERED late\n")
        self.assertIn("late", recovered)


class HandleItemTest(unittest.TestCase):
    def setUp(self):
        self.out = io.StringIO()
        self.err = io.StringIO()

    def run_handle(self, stored, item_id, seen=None, attributed=None):
        if attributed is None:
            attributed = {stored["item_id"]}
        with mock.patch.object(watch, "fetch_item", return_value=stored):
            return watch.handle_item(
                "http://store",
                ME,
                set() if seen is None else seen,
                item_id,
                FakeAttribution(attributed),
                self.out,
                self.err,
            )

    def test_emits_for_own_answered_item(self):
        seen = self.run_handle(item("abc"), "abc")
        self.assertEqual(self.out.getvalue(), "ANSWERED abc\n")
        self.assertIn("abc", seen)

    def test_skips_another_sessions_item(self):
        self.run_handle(item("theirs", producer=OTHER), "theirs")
        self.assertEqual(self.out.getvalue(), "")

    def test_skips_an_already_seen_item(self):
        self.run_handle(item("abc"), "abc", seen={"abc"})
        self.assertEqual(self.out.getvalue(), "")

    def test_a_read_failure_warns_and_does_not_raise(self):
        import urllib.error

        with mock.patch.object(
            watch, "fetch_item", side_effect=urllib.error.URLError("boom")
        ):
            watch.handle_item(
                "http://store", ME, set(), "abc", FakeAttribution({"abc"}), self.out, self.err
            )
        self.assertEqual(self.out.getvalue(), "")
        self.assertIn("could not read abc", self.err.getvalue())


if __name__ == "__main__":
    unittest.main()
