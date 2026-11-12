#!/usr/bin/env python3
"""Tests for scripts/round-readings.py and the receipt's `readings` step.

The load-bearing case is the incident: on 2026-10-21 the BRO-21389 manager opened its
round receipt and recorded **nothing** —

    {"topic": "BRO-21389 MDM via REST", "round_started_at": 1792578804.898, "fields": {}}

— so step 2 never ran, the sweep reader got no verdicts, and a worker denied `gh pr
merge` twice bucketed `progressing`. `test_receipt_flags_skipped_readings_on_the_1217_fixture`
is the red/green test for that: it fails on any build whose receipt does not require a
`readings` artifact, and it is written against the real receipt shape rather than a
convenient one.

The rest pin the three-valued discipline, which is where this class of check fails
silently: `unread` is never `clear`, `unread` is never `quiet`, and a marker that could
not be read is never a negative.

Run: python3 -m unittest discover -s scripts/tests -v
"""

import datetime
import importlib.util
import json
import os
import sys
import tempfile
import time
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.dirname(_HERE)


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


rr = load_module("round_readings", os.path.join(SCRIPTS, "round-readings.py"))


def receipt_module(receipt_dir, liveness_dir):
    os.environ["MANAGER_RECEIPT_STATE_DIR"] = receipt_dir
    os.environ["MANAGER_LIVENESS_STATE_DIR"] = liveness_dir
    return load_module("manager_receipt", os.path.join(SCRIPTS, "manager-receipt.py"))


class Tmp(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.dir = self._dir.name
        self.attention = os.path.join(self.dir, "attention")
        self.projects = os.path.join(self.dir, "projects")
        os.makedirs(self.attention, exist_ok=True)
        os.makedirs(self.projects, exist_ok=True)

    def tearDown(self):
        self._dir.cleanup()

    def write(self, name, payload):
        path = os.path.join(self.attention, name)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(payload if isinstance(payload, str) else json.dumps(payload))
        return path

    def write_transcript(self, sid, calls):
        """Write `sid`'s transcript holding `calls` — `(tool, tool_use_id, is_error, ts)`.

        Each call becomes the two records the harness actually emits: the `tool_use`
        block on an assistant message, and the `tool_result` block on the following
        user message. The name lives only on the first; the second carries just the
        id, which is the join the reader has to make.
        """
        lines = []
        for name, tool_use_id, is_error, ts in calls:
            stamp = datetime.datetime.fromtimestamp(ts, datetime.timezone.utc)
            iso = stamp.isoformat().replace("+00:00", "Z")
            lines.append(json.dumps({"type": "assistant", "timestamp": iso,
                                     "message": {"content": [
                                         {"type": "tool_use", "id": tool_use_id,
                                          "name": name}]}}))
            block = {"type": "tool_result", "tool_use_id": tool_use_id}
            if is_error:
                block["is_error"] = True
            lines.append(json.dumps({"type": "user", "timestamp": iso,
                                     "message": {"content": [block]}}))
        project = os.path.join(self.projects, "project")
        os.makedirs(project, exist_ok=True)
        path = os.path.join(project, f"{sid}.jsonl")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")
        return path


class The1217Case(unittest.TestCase):
    """The incident, asserted against the real receipt shape."""

    def test_receipt_flags_skipped_readings_on_the_1217_fixture(self):
        with tempfile.TemporaryDirectory() as receipts, \
                tempfile.TemporaryDirectory() as liveness:
            mod = receipt_module(receipts, liveness)
            now = time.time()
            # The BRO-21389 receipt verbatim: a window opened, nothing recorded.
            with open(os.path.join(receipts, "bro-21389-mdm-via-rest.json"), "w",
                      encoding="utf-8") as fh:
                json.dump({"topic": "BRO-21389 MDM via REST",
                           "round_started_at": now, "fields": {}}, fh)

            import io
            from contextlib import redirect_stdout
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = mod.check("BRO-21389 MDM via REST", now=now + 5)
            out = buf.getvalue()

            self.assertEqual(rc, 11, "a round that recorded nothing must be RED")
            self.assertIn("skipped: readings", out,
                          "the skipped step 2 must be named, not folded into the others")


class ReadingsShape(unittest.TestCase):
    def setUp(self):
        self.mod = receipt_module(tempfile.mkdtemp(), tempfile.mkdtemp())

    def doc(self, **row):
        base = {"liveness": "LIVE", "in_flight": "quiet", "feed": "clear",
                "external_wait": "none"}
        base.update(row)
        return {"recorded_at": time.time(), "feed_state": "read",
                "rows": {"A task": base}}

    def test_accepts_the_producers_shape(self):
        self.assertTrue(self.mod.readings_shape(json.dumps(self.doc())))

    def test_rejects_a_bare_empty_object(self):
        self.assertFalse(self.mod.readings_shape("{}"))

    def test_rejects_a_row_missing_a_verdict(self):
        doc = self.doc()
        del doc["rows"]["A task"]["feed"]
        self.assertFalse(self.mod.readings_shape(json.dumps(doc)))

    def test_rejects_a_value_outside_the_vocabulary(self):
        self.assertFalse(self.mod.readings_shape(json.dumps(self.doc(feed="unknown"))))


class Verify(unittest.TestCase):
    def test_missing_file_refuses(self):
        code, msg = rr.verify("S", "/nonexistent/x.readings", 900)
        self.assertEqual(code, rr.EXIT_REFUSED)
        self.assertIn("REFUSED", msg)

    def test_stale_file_refuses(self):
        with tempfile.NamedTemporaryFile("w", suffix=".readings", delete=False) as fh:
            json.dump({"recorded_at": time.time() - 4000, "feed_state": "read",
                       "rows": {}}, fh)
            path = fh.name
        try:
            code, msg = rr.verify("S", path, 900)
            self.assertEqual(code, rr.EXIT_REFUSED)
            self.assertIn("step 2 did not run", msg)
        finally:
            os.unlink(path)

    def test_readings_predating_the_round_start_refuse(self):
        """A readings file from the PREVIOUS round is not this round's readings."""
        now = time.time()
        with tempfile.TemporaryDirectory() as receipts:
            old_state = rr.STATE_DIR
            rr.STATE_DIR = receipts
            try:
                with open(os.path.join(receipts, "s.json"), "w", encoding="utf-8") as fh:
                    json.dump({"topic": "S", "round_started_at": now, "fields": {}}, fh)
                with tempfile.NamedTemporaryFile("w", suffix=".readings", delete=False) as fh:
                    json.dump({"recorded_at": now - 60, "feed_state": "read", "rows": {}}, fh)
                    path = fh.name
                try:
                    code, msg = rr.verify("S", path, 900, now=now + 1)
                    self.assertEqual(code, rr.EXIT_REFUSED)
                    self.assertIn("predate this round's start", msg)
                finally:
                    os.unlink(path)
            finally:
                rr.STATE_DIR = old_state

    def test_fresh_file_passes(self):
        with tempfile.NamedTemporaryFile("w", suffix=".readings", delete=False) as fh:
            json.dump({"recorded_at": time.time(), "feed_state": "read",
                       "rows": {}}, fh)
            path = fh.name
        try:
            code, _ = rr.verify("S", path, 900)
            self.assertEqual(code, 0)
        finally:
            os.unlink(path)


class Verdicts(Tmp):
    def test_external_wait_reads_a_non_empty_ends_slot(self):
        sid = "aaaa1111"
        self.write(f"{sid}.events.jsonl",
                   json.dumps({"event": "Stop", "ts": time.time(),
                               "detail": "🟡 WAITING\n⏰ Ends: CI goes green (watch.sh)"}) + "\n")
        self.assertEqual(rr.external_wait([sid], self.attention), "external")

    def test_external_wait_ignores_an_empty_ends_slot(self):
        sid = "bbbb2222"
        self.write(f"{sid}.events.jsonl",
                   json.dumps({"event": "Stop", "ts": time.time(),
                               "detail": "🟡 WAITING\n⏰ Ends: "}) + "\n")
        self.assertEqual(rr.external_wait([sid], self.attention), "none")

    def test_external_wait_takes_the_newest_stop_record(self):
        sid = "cccc3333"
        lines = [
            json.dumps({"event": "Stop", "ts": 1000, "detail": "⏰ Ends: an old wait"}),
            json.dumps({"event": "Stop", "ts": 2000, "detail": "🟢 ACTIVE · nothing running"}),
        ]
        self.write(f"{sid}.events.jsonl", "\n".join(lines) + "\n")
        self.assertEqual(rr.external_wait([sid], self.attention), "none",
                         "an append-only log means the NEWEST Stop wins")

    def test_external_wait_with_no_log_is_unread_not_none(self):
        self.assertEqual(rr.external_wait(["dddd4444"], self.attention), "unread")

    def test_feed_unread_is_never_clear(self):
        self.assertEqual(rr.feed_verdict(["x"], set(), "unread"), "unread")

    def test_feed_clear_only_from_a_read(self):
        self.assertEqual(rr.feed_verdict(["x"], set(), "read"), "clear")

    def test_feed_gated_when_the_session_is_in_the_feed(self):
        self.assertEqual(rr.feed_verdict(["x"], {"x"}, "read"), "gated")


class TheParkLimb(Tmp):
    """A `waiting` session is parked, not stuck — asserted on BOTH fields of one row.

    Measured 2026-10-29: a headless worker suspended on a permission gate writes
    nothing to its transcript, so transcript staleness reads identically to a finished
    turn. `in_flight()` returned `quiet` for it and `stuck_verdict()` — which took no
    `in_flight` argument at all — stamped the same row `frozen`, from the same read.
    """

    def registry(self, sid, status):
        return {sid: {"status": status}}

    def stale(self, sid):
        """A transcript older than `LIVE_WINDOW`, i.e. the frozen shape."""
        return 600.0

    def test_a_waiting_session_reads_parked(self):
        sid = "eeee5555"
        verdict, limb = rr.in_flight([sid], self.registry(sid, "waiting"),
                                     self.attention, time.time(), self.stale)
        self.assertEqual(verdict, "parked")
        self.assertIn("waiting", limb)

    def test_a_parked_row_is_not_frozen(self):
        sid = "ffff6666"
        row = {"mtime": time.time() - 3600}
        verdict, _ = rr.stuck_verdict(
            row, [sid], "LIVE", "clear", 900, time.time(),
            self.attention, self.projects, in_flight_verdict="parked")
        self.assertEqual(
            verdict, "none",
            "a parked worker's tool call is suspended, so the transcript cannot tell "
            "a park from a death — and this is the park")

    def test_an_idle_session_with_a_static_transcript_is_still_frozen(self):
        """The negative control: the park limb must not swallow the real case."""
        sid = "9999aaaa"
        row = {"mtime": time.time() - 3600}
        fl, _ = rr.in_flight([sid], self.registry(sid, "idle"),
                             self.attention, time.time(), self.stale)
        self.assertEqual(fl, "quiet")
        verdict, _ = rr.stuck_verdict(
            row, [sid], "LIVE", "clear", 900, time.time(),
            self.attention, self.projects, in_flight_verdict=fl)
        self.assertEqual(verdict, "frozen")

    def test_a_park_does_not_survive_the_gate_being_released(self):
        """Once the registry leaves `waiting`, the same row is frozen again."""
        sid = "8888bbbb"
        row = {"mtime": time.time() - 3600}
        fl, _ = rr.in_flight([sid], self.registry(sid, "busy"),
                             self.attention, time.time(), self.stale)
        self.assertNotEqual(fl, "parked")
        verdict, _ = rr.stuck_verdict(
            row, [sid], "LIVE", "clear", 900, time.time(),
            self.attention, self.projects, in_flight_verdict=fl)
        self.assertEqual(verdict, "frozen")


    def test_compute_wires_the_park_verdict_through_to_stuck(self):
        """`compute()` must pass its `in_flight` verdict into `stuck_verdict()`.

        The unit cases above call `stuck_verdict()` directly, so they would all pass
        on a build that computed `parked` and then dropped it at the call site — the
        row would read `in_flight: parked` beside `stuck: frozen`, which is exactly
        the contradiction this change exists to remove.
        """
        sid = "7777cccc"
        sessions = os.path.join(self.dir, "sessions")
        os.makedirs(sessions, exist_ok=True)
        with open(os.path.join(sessions, "12345.json"), "w", encoding="utf-8") as fh:
            json.dump({"sessionId": sid, "status": "waiting", "pid": 12345}, fh)

        predispatch = rr._load("manager-predispatch")
        orig_resolve = predispatch.resolve_tracked_by_member
        orig_live = rr.liveness
        try:
            predispatch.resolve_tracked_by_member = lambda vault, members: (
                [{"name": "A task", "mtime": 1_700_000_000 - 3600,
                  "session": sid, "sessions": [sid]}], list(members))
            rr.liveness = lambda ids, endpoint, registry_dir: "LIVE"
            path = os.path.join(self.dir, "tracked.txt")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write("A task\n")
            doc = rr.compute("A subject", path, self.dir, 900, now=1_700_000_000,
                             attention_dir=self.attention, sessions_dir=sessions,
                             feed={"panes": set(), "owners": {}, "orphan_owners": [],
                                   "state": "read", "error": None})
        finally:
            predispatch.resolve_tracked_by_member = orig_resolve
            rr.liveness = orig_live

        row = doc["rows"]["A task"]
        self.assertEqual(row["in_flight"], "parked")
        self.assertEqual(row["stuck"], "none")


# The feed's pane column is right-aligned to four, so a real row reads `[ 417]`, and a
# headless gate reads `[ hst]` with its store item id on the `answer:` continuation
# line. The pre-fix pattern `^\s*\[(\d+)\]` matched 1 of these 3 rows (`[1234]`).
FEED = (
    "  [ 417]  2m  some-task  permission · Bash\n"
    "  [ hst]  5m  headless-task  permission · Bash\n"
    "          answer: python3 attention-answer.py answer item-abc\n"
    "          owner: sid-hst\n"
    "  [1234]  1m  other-task  question\n"
)

# A feed read that ran and saw nothing unattributable.
CLEAN_FEED = {"panes": set(), "owners": {}, "orphan_owners": [], "state": "read",
              "error": None}


class FeedParse(Tmp):
    def setUp(self):
        super().setUp()
        self.filt = rr._load("gate-owner-filter")
        self._orig_checked = self.filt.headless_items_checked
        self._orig_log = self.filt.log_items

    def tearDown(self):
        self.filt.headless_items_checked = self._orig_checked
        self.filt.log_items = self._orig_log
        super().tearDown()

    def _store(self, records, error=None):
        def fake(keys, endpoint=None):
            return [r for r in records if r["pane"] in keys], error
        self.filt.headless_items_checked = fake

    def test_padded_and_headless_rows_are_matched(self):
        feed = rr.parse_feed(FEED)
        self.assertEqual(feed["panes"], {"417", "hst:item-abc", "1234"})
        self.assertEqual(feed["state"], "read")
        self.assertIsNone(feed["error"])

    def test_a_pane_less_row_carries_its_owner(self):
        """The owner line is what removes the second attention-store read."""
        self.assertEqual(rr.parse_feed(FEED)["owners"], {"hst:item-abc": "sid-hst"})

    def test_headless_row_resolves_without_touching_the_store(self):
        """A store read here is exactly the transient this fix removes."""
        def boom(keys, endpoint=None):
            raise AssertionError("the store must not be read when the row names its owner")
        self.filt.headless_items_checked = boom
        gated, state, error = rr.gated_sessions_from_panes(rr.parse_feed(FEED), self.attention)
        self.assertEqual((gated, state, error), ({"sid-hst"}, "read", None))
        self.assertEqual(rr.feed_verdict(["sid-hst"], gated, state), "gated")
        self.assertEqual(rr.feed_verdict(["sid-other"], gated, state), "clear")

    def test_a_store_failure_on_an_ownerless_feed_is_partial_not_a_family_blank(self):
        """An older feed carries no owner line, so the store is the only route —
        and a failed read is named, never rendered as a verdict about the rows."""
        self._store([], "attention store read failed: URLError: timed out")
        feed = {"panes": {"hst:item-abc"}, "owners": {}, "orphan_owners": [],
                "state": "read", "error": None}
        gated, state, error = rr.gated_sessions_from_panes(feed, self.attention)
        self.assertEqual(gated, set())
        self.assertEqual(state, "partial")
        self.assertIn("attention store read failed", error)
        self.assertEqual(rr.feed_verdict(["sid-other"], gated, state), "unread")

    def test_a_failed_event_log_read_is_unread_and_named(self):
        """A digit pane joins through the event log; an unreadable log blanks nothing
        silently — it is `unread`, and the failing probe is named."""
        def boom(_state_dir):
            raise RuntimeError("log dir unreadable")

        self.filt.log_items = boom
        feed = {"panes": {"417"}, "owners": {}, "orphan_owners": [],
                "state": "read", "error": None}
        gated, state, error = rr.gated_sessions_from_panes(feed, self.attention)
        self.assertEqual(gated, set())
        self.assertEqual(state, "unread")
        self.assertIn("event-log read failed", error)
        self.assertIn("log dir unreadable", error)

    def test_a_raising_store_read_is_named_not_crashed(self):
        def boom(keys, endpoint=None):
            raise RuntimeError("store down")
        self.filt.headless_items_checked = boom
        feed = {"panes": {"hst:item-abc"}, "owners": {}, "orphan_owners": [],
                "state": "read", "error": None}
        _, state, error = rr.gated_sessions_from_panes(feed, self.attention)
        self.assertEqual(state, "partial")
        self.assertIn("store down", error)

    def test_an_unkeyable_row_makes_the_read_partial(self):
        for text in ("  [   ?]  3m  orphan  question\n",
                     "  [ hst]  3m  headless  permission · Bash\n  [1234]  1m  x  q\n"):
            feed = rr.parse_feed(text)
            self.assertEqual(feed["state"], "partial", text)
            self.assertIsNotNone(feed["error"], text)

    def test_a_pane_less_row_with_an_owner_is_attributable_not_unkeyable(self):
        feed = rr.parse_feed("  [   ?]  3m  orphan  question\n          owner: sid-orphan\n")
        self.assertEqual(feed["state"], "read")
        self.assertEqual(feed["orphan_owners"], ["sid-orphan"])

    def test_a_store_fallback_read_is_partial_and_named(self):
        """`Needs you (0)` from the log fallback is not a gate-free fleet."""
        text = ("store unreachable — reading event log\n"
                "Needs you (0)\n\nNothing needs you.\n")
        feed = rr.parse_feed(text)
        self.assertEqual(feed["state"], "partial")
        self.assertIn("attention store was unreachable", feed["error"])

    def test_denial_marker_yields_stuck_denied(self):
        sid = "eeee5555"
        now = time.time()
        self.write(f"{sid}.denial.json",
                   {"tool": "Bash", "reason": "auto-mode classifier denied gh pr merge",
                    "ts": now})
        verdict, reason = rr.stuck_verdict({"mtime": now}, [sid], "LIVE",
                                           "clear", 900, now, self.attention, self.projects)
        self.assertEqual(verdict, "denied")
        self.assertIn("Bash", reason)

    # -- the denial carrier clears on evidence, not on age alone ----------------
    # Measured 2026-11-03: session 6040cad5's marker stayed on disk after the denied
    # `crane delete` was re-run successfully, so a healthy worker read `stuck: denied`
    # for hours. Each test below pins one half of the fix.

    def test_a_later_success_of_the_same_tool_clears_the_denial(self):
        sid = "aaaa0001"
        now = time.time()
        self.write(f"{sid}.denial.json",
                   {"tool": "Bash", "reason": "denied", "ts": now - 60})
        self.write_transcript(sid, [("Bash", "tu-1", False, now - 30)])
        verdict, _ = rr.stuck_verdict({"mtime": now}, [sid], "LIVE", "clear", 900, now,
                                      self.attention, self.projects)
        self.assertEqual(verdict, "none")

    def test_a_later_success_of_a_different_tool_does_not_clear_the_denial(self):
        """The negative control. An implementation that clears on ANY later success
        passes every other test here while un-sticking a worker that is still
        blocked on the denied tool."""
        sid = "aaaa0002"
        now = time.time()
        self.write(f"{sid}.denial.json",
                   {"tool": "Bash", "reason": "denied", "ts": now - 60})
        self.write_transcript(sid, [("Edit", "tu-2", False, now - 30)])
        verdict, _ = rr.stuck_verdict({"mtime": now}, [sid], "LIVE", "clear", 900, now,
                                      self.attention, self.projects)
        self.assertEqual(verdict, "denied")

    def test_a_success_before_the_marker_does_not_clear_the_denial(self):
        sid = "aaaa0004"
        now = time.time()
        self.write(f"{sid}.denial.json",
                   {"tool": "Bash", "reason": "denied", "ts": now - 60})
        self.write_transcript(sid, [("Bash", "tu-4", False, now - 120)])
        verdict, _ = rr.stuck_verdict({"mtime": now}, [sid], "LIVE", "clear", 900, now,
                                      self.attention, self.projects)
        self.assertEqual(verdict, "denied")

    def test_a_failed_later_retry_does_not_clear_the_denial(self):
        """`is_error` present means the retry failed, so the gate is still closed.
        Without this case the `is_error` skip branch is never exercised and a
        regression counting a failure as success would pass the whole suite."""
        sid = "aaaa0007"
        now = time.time()
        self.write(f"{sid}.denial.json",
                   {"tool": "Bash", "reason": "denied", "ts": now - 60})
        self.write_transcript(sid, [("Bash", "tu-7", True, now - 30)])
        verdict, _ = rr.stuck_verdict({"mtime": now}, [sid], "LIVE", "clear", 900, now,
                                      self.attention, self.projects)
        self.assertEqual(verdict, "denied")

    def test_a_missing_transcript_keeps_the_denial(self):
        """`unread` is never a reason to clear — the fail-safe direction."""
        sid = "aaaa0005"
        now = time.time()
        self.write(f"{sid}.denial.json",
                   {"tool": "Bash", "reason": "denied", "ts": now - 60})
        verdict, _ = rr.stuck_verdict({"mtime": now}, [sid], "LIVE", "clear", 900, now,
                                      self.attention, self.projects)
        self.assertEqual(verdict, "denied")

    def test_a_stale_marker_on_the_first_id_does_not_mask_a_live_one_later(self):
        """A row carries every id the task ran under. Stopping at the first readable
        marker would let an expired one hide a live one behind it — a false `none`
        for a worker that is still blocked."""
        now = time.time()
        self.write("aaaa0008.denial.json",
                   {"tool": "Bash", "reason": "old", "ts": now - 31 * 60})
        self.write("aaaa0009.denial.json",
                   {"tool": "Bash", "reason": "still blocked", "ts": now - 60})
        verdict, reason = rr.stuck_verdict(
            {"mtime": now}, ["aaaa0008", "aaaa0009"], "LIVE", "clear", 900, now,
            self.attention, self.projects)
        self.assertEqual(verdict, "denied")
        self.assertIn("still blocked", reason)

    def test_a_denial_older_than_the_ttl_is_ignored(self):
        sid = "aaaa0003"
        now = time.time()
        self.write(f"{sid}.denial.json",
                   {"tool": "Bash", "reason": "denied", "ts": now - 31 * 60})
        verdict, _ = rr.stuck_verdict({"mtime": now}, [sid], "LIVE", "clear", 900, now,
                                      self.attention, self.projects)
        self.assertEqual(verdict, "none")

    def test_a_fresh_aborting_transcript_yields_permission_channel(self):
        sid = "abcd0007"
        now = time.time()
        self._abort_transcript(sid, now - 120, now)
        verdict, reason = rr.stuck_verdict({"mtime": now}, [sid], "LIVE", "clear",
                                           900, now, self.attention, self.projects)
        self.assertEqual(verdict, "permission-channel")
        self.assertIn("abcd0007", reason)

    def test_a_STALE_aborting_transcript_is_not_permission_channel(self):
        """⚠️ The blocking defect from this change's first review, 2026-11-09.

        A row carries every id it ever ran under, and the window is anchored on the
        transcript's own last stamp — so an id whose session died months ago with its
        last 15 minutes full of aborts matches forever and pins a healthy LIVE row to
        `permission-channel`. The staleness bound is what stops it.
        """
        sid = "abcd0008"
        now = time.time()
        self._abort_transcript(sid, now - 3 * 60 * 60, now - 3 * 60 * 60)
        verdict, _ = rr.stuck_verdict({"mtime": now}, [sid], "LIVE", "clear",
                                      900, now, self.attention, self.projects)
        self.assertEqual(verdict, "none")

    def _abort_transcript(self, sid, first_ts, last_ts):
        """A transcript whose tool results carry the closed-channel marker."""
        lines = []
        for i in range(4):
            ts = first_ts + (last_ts - first_ts) * i / 3
            iso = datetime.datetime.fromtimestamp(
                ts, datetime.timezone.utc).isoformat().replace("+00:00", "Z")
            lines.append(json.dumps({"type": "user", "timestamp": iso, "message": {
                "content": [{"type": "tool_result", "tool_use_id": f"t{i}",
                             "is_error": True,
                             "content": "Tool permission request failed: "
                                        "AbortError: Stream closed"}]}}))
        project = os.path.join(self.projects, "project")
        os.makedirs(project, exist_ok=True)
        path = os.path.join(project, f"{sid}.jsonl")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")
        os.utime(path, (last_ts, last_ts))
        return path

    def test_a_stale_denial_still_falls_through_to_the_frozen_carrier(self):
        """Dropping the denial must not short-circuit to `none`: a live worker whose
        task file has not moved is still frozen."""
        sid = "aaaa0006"
        now = time.time()
        self.write(f"{sid}.denial.json",
                   {"tool": "Bash", "reason": "denied", "ts": now - 31 * 60})
        verdict, _ = rr.stuck_verdict({"mtime": now - 1000}, [sid], "LIVE", "clear", 900,
                                      now, self.attention, self.projects)
        self.assertEqual(verdict, "frozen")

    def test_frozen_when_live_and_the_file_is_older_than_one_interval(self):
        now = time.time()
        verdict, reason = rr.stuck_verdict({"mtime": now - 1000}, ["sid"], "LIVE",
                                           "clear", 900, now, self.attention)
        self.assertEqual(verdict, "frozen")
        self.assertIn("while live", reason)

    def test_a_gated_worker_is_not_double_reported(self):
        verdict, _ = rr.stuck_verdict({"mtime": time.time() - 1000}, ["sid"], "LIVE",
                                      "gated", 900, time.time(), self.attention)
        self.assertEqual(verdict, "none")

    def test_a_dead_owner_is_not_stuck(self):
        verdict, _ = rr.stuck_verdict({"mtime": time.time() - 10000}, ["sid"], "ABSENT",
                                      "clear", 900, time.time(), self.attention)
        self.assertEqual(verdict, "none")

    def test_in_flight_limb2_reads_an_open_marker(self):
        sid = "ffff6666"
        now = time.time()
        self.write(f"{sid}.tool.json",
                   {"state": "open", "ts": now - 60, "tool": "Bash"})
        verdict, limb = rr.in_flight([sid], {}, self.attention, now,
                                     lambda _sid: float("inf"))
        self.assertEqual(verdict, "in-flight")
        self.assertIn("limb 2", limb)

    def test_in_flight_with_no_readable_limb_is_unread(self):
        verdict, _ = rr.in_flight(["nosuchsid"], {}, self.attention, time.time(),
                                  lambda _sid: float("inf"))
        self.assertEqual(verdict, "unread")


class LivenessMapping(unittest.TestCase):
    """`check()` returns an exit CODE — measured bug: the first version compared it
    against a verdict WORD and every row silently read UNKNOWN against a store that
    was answering ABSENT correctly. That is the direction that hides a dead owner."""

    def probe(self, codes):
        calls = {"i": 0}

        class P:
            @staticmethod
            def check(sid, endpoint=None, registry_dir=None):
                code = codes[min(calls["i"], len(codes) - 1)]
                calls["i"] += 1
                return code, "", None

        return P

    def run_with(self, codes, ids=("x",)):
        rr._MODS["session-liveness"] = self.probe(codes)
        try:
            return rr.liveness(list(ids), None, None)
        finally:
            rr._MODS.pop("session-liveness", None)

    def test_exit_codes_map_to_verdicts(self):
        for code, want in ((0, "LIVE"), (1, "ABSENT"), (2, "UNKNOWN"),
                           (3, "UNKNOWN"), (4, "UNKNOWN")):
            self.assertEqual(self.run_with([code]), want, f"exit {code}")

    def test_any_live_id_wins(self):
        self.assertEqual(self.run_with([1, 0], ids=("a", "b")), "LIVE")

    def test_an_unknown_id_never_folds_into_absent(self):
        self.assertEqual(self.run_with([1, 2], ids=("a", "b")), "UNKNOWN")

    def test_no_ids_is_unknown(self):
        self.assertEqual(rr.liveness([], None, None), "UNKNOWN")


class TrackedSet(Tmp):
    def test_read_tracked_ignores_blank_lines(self):
        path = os.path.join(self.dir, "tracked.txt")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("A task\n\n  B task  \n")
        self.assertEqual(rr.read_tracked(path), ["A task", "B task"])

    def test_missing_tracked_file_is_an_empty_set(self):
        self.assertEqual(rr.read_tracked(os.path.join(self.dir, "nope.txt")), [])

    def test_readings_path_is_the_receipts_sibling(self):
        path = rr.readings_path("BRO-21389 MDM via REST")
        self.assertTrue(path.endswith("bro-21389-mdm-via-rest.readings"))


class ParkThreshold(Tmp):
    """The park cell is 2x the cadence; the frozen cell is 1x. One field, one rule."""

    def test_park_threshold_is_twice_the_clamped_interval(self):
        """A 15-minute tick must record 1800, not 900.

        The receipt recorded 1x the interval, so the sweep reader had to reconcile
        its declared park threshold against the 2x the gate derives — on every
        round. Reading this field as the frozen threshold is the same mistake in
        the other direction, which is why the pair is asserted together.
        """
        path = os.path.join(self.dir, "tracked.txt")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("A task\n")
        doc = rr.compute("A subject", path, None, 900, now=1_700_000_000,
                         attention_dir=self.attention, feed=CLEAN_FEED)
        self.assertEqual(doc["park_threshold_seconds"], 1800)

    def test_a_shorter_tick_scales_with_it(self):
        """300s tick -> 600s park threshold: the rule is a multiple, not a constant."""
        path = os.path.join(self.dir, "tracked.txt")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("A task\n")
        doc = rr.compute("A subject", path, None, 300, now=1_700_000_000,
                         attention_dir=self.attention, feed=CLEAN_FEED)
        self.assertEqual(doc["park_threshold_seconds"], 600)


class FeedRead(Tmp):
    """`feed_panes` retries a transient, then NAMES the failure.

    The measured incident (2026-11-08 05:06:29Z) is a transient: `unread=11` for 11
    rows, and a re-run 39 s later read `gated=1 · clear=10 · unread=0`. One retry
    absorbs a failure that clears in under a minute; after it, the failure is an
    `error` on the document, never a silent verdict about the rows.
    """

    def setUp(self):
        super().setUp()
        self._run = rr.subprocess.run

    def tearDown(self):
        rr.subprocess.run = self._run
        super().tearDown()

    @staticmethod
    def _result(returncode, stdout=""):
        return rr.subprocess.CompletedProcess([], returncode, stdout=stdout, stderr="boom")

    def test_a_transient_failure_recovers_on_the_retry(self):
        calls = []

        def fake(cmd, **kwargs):
            calls.append(cmd)
            return self._result(0, FEED) if len(calls) == 2 else self._result(1)

        rr.subprocess.run = fake
        feed = rr.feed_panes()
        self.assertEqual(feed["state"], "read")
        self.assertIsNone(feed["error"])
        self.assertEqual(len(calls), 2)

    def test_two_failures_are_named_not_rendered(self):
        rr.subprocess.run = lambda cmd, **kw: self._result(1)
        feed = rr.feed_panes()
        self.assertEqual(feed["state"], "unread")
        self.assertIn("who-needs-me.py", feed["error"])
        self.assertIn("failed twice", feed["error"])

    def test_a_timeout_is_retried_then_named(self):
        def fake(cmd, **kwargs):
            raise rr.subprocess.TimeoutExpired(cmd, 60)

        rr.subprocess.run = fake
        feed = rr.feed_panes()
        self.assertEqual(feed["state"], "unread")
        self.assertIn("TimeoutExpired", feed["error"])

    def test_a_fallback_render_is_retried(self):
        """A store that recovers inside the round must not leave a partial read."""
        fallback = "store unreachable — reading event log\nNeeds you (0)\n"
        calls = []

        def fake(cmd, **kwargs):
            calls.append(cmd)
            return self._result(0, FEED if len(calls) == 2 else fallback)

        rr.subprocess.run = fake
        feed = rr.feed_panes()
        self.assertEqual(feed["state"], "read")
        self.assertEqual(len(calls), 2)

    def test_two_fallbacks_are_partial_and_named(self):
        fallback = "store unreachable — reading event log\nNeeds you (0)\n"
        rr.subprocess.run = lambda cmd, **kw: self._result(0, fallback)
        feed = rr.feed_panes()
        self.assertEqual(feed["state"], "partial")
        self.assertIn("attention store was unreachable", feed["error"])


class FeedFamilyInvariant(Tmp):
    """At the document level: one failed read degrades per row.

    `gated + clear + unread` is counted from the rows themselves, so the invariant
    is structural — these tests pin that the *distribution* is right too: a failure
    that leaves some rows decidable must not blank the family.
    """

    def setUp(self):
        super().setUp()
        self.predispatch = rr._load("manager-predispatch")
        self.filt = rr._load("gate-owner-filter")
        self._resolve = self.predispatch.resolve_tracked_by_member
        self._checked = self.filt.headless_items_checked
        self._live = rr.liveness
        rr.liveness = lambda ids, endpoint, registry_dir: "ABSENT"

    def tearDown(self):
        self.predispatch.resolve_tracked_by_member = self._resolve
        self.filt.headless_items_checked = self._checked
        rr.liveness = self._live
        super().tearDown()

    def document(self, rows, feed):
        def resolve(vault, members):
            # `(tasks, matched members)` — every member here resolves, so the second
            # half is the member list itself. Patching `resolve_tracked_by_member`
            # rather than `resolve_tracked` is deliberate: `compute` reads the pair,
            # and a patch on the wrapper would leave the real resolver to run.
            return ([{"name": name, "mtime": 0, "session": None, "sessions": rows[name]}
                     for name in members], list(members))

        self.predispatch.resolve_tracked_by_member = resolve
        path = os.path.join(self.dir, "tracked.txt")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("\n".join(rows) + "\n")
        # A truthy vault is what routes through the resolver, which is patched above;
        # the path itself is never read.
        return rr.compute("A subject", path, self.dir, 900, now=1_700_000_000,
                          attention_dir=self.attention, feed=feed)

    def assert_invariant(self, doc):
        verdicts = [rec["feed"] for rec in doc["rows"].values()]
        counts = doc["feed_counts"]
        self.assertEqual(sum(counts.values()), len(doc["rows"]))
        for value in ("gated", "clear", "unread"):
            self.assertEqual(counts[value], verdicts.count(value), value)

    def test_a_failed_store_read_leaves_the_other_rows_decided(self):
        """The owner line makes the read complete even with the store down.

        The store is made to FAIL and is asserted never consulted: a key that carries
        its owner must not fall back to the store, which is the whole point of the
        owner line. Without the call assertion this test passes on a build that still
        reads the store, because nothing happens to be listening on the default port.
        """
        calls = []

        def dead_store(keys, endpoint=None):
            calls.append(list(keys))
            return [], "attention store read failed: URLError: timed out"

        self.filt.headless_items_checked = dead_store
        doc = self.document(
            {"Gated task": ["sid-gated"], "Clear task": ["sid-clear"]},
            {"panes": {"hst:k1"}, "owners": {"hst:k1": "sid-gated"},
             "orphan_owners": [], "state": "read", "error": None})
        self.assertEqual(calls, [], "an owner-carrying key must not read the store")
        self.assertEqual(doc["rows"]["Gated task"]["feed"], "gated")
        self.assertEqual(doc["rows"]["Clear task"]["feed"], "clear")
        self.assertEqual(doc["feed_counts"], {"gated": 1, "clear": 1, "unread": 0})
        self.assertEqual(doc["probes"], [])
        self.assert_invariant(doc)

    def test_an_ownerless_key_reads_the_store_and_a_failure_marks_the_family_partial(self):
        """The fallback survives for an older feed, and its failure is named."""
        calls = []

        def dead_store(keys, endpoint=None):
            calls.append(list(keys))
            return [], "attention store read failed: URLError: timed out"

        self.filt.headless_items_checked = dead_store
        doc = self.document(
            {"Maybe task": ["sid-maybe"], "Other task": ["sid-other"]},
            {"panes": {"hst:k1"}, "owners": {}, "orphan_owners": [],
             "state": "read", "error": None})
        self.assertEqual(calls, [["hst:k1"]])
        self.assertEqual(doc["feed_state"], "partial")
        self.assertEqual(doc["feed_counts"], {"gated": 0, "clear": 0, "unread": 2})
        self.assertIn("attention store read failed",
                      doc["probes"][0]["error"])
        self.assert_invariant(doc)

    def test_a_partial_read_keeps_gated_rows_and_marks_only_the_rest_unread(self):
        doc = self.document(
            {"Gated task": ["sid-gated"], "Maybe task": ["sid-maybe"]},
            {"panes": {"hst:k1"}, "owners": {"hst:k1": "sid-gated"},
             "orphan_owners": [], "state": "partial",
             "error": "1 feed row(s) name neither a key nor an owner"})
        self.assertEqual(doc["rows"]["Gated task"]["feed"], "gated")
        self.assertEqual(doc["rows"]["Maybe task"]["feed"], "unread")
        self.assertLess(doc["feed_counts"]["unread"], len(doc["rows"]))
        self.assertEqual(doc["probes"][0]["probe"],
                         "who-needs-me.py --section needs-you --all")
        self.assert_invariant(doc)

    def test_a_failed_feed_read_is_all_unread_and_names_the_probe(self):
        doc = self.document(
            {"A": ["s1"], "B": ["s2"]},
            {"panes": set(), "owners": {}, "orphan_owners": [], "state": "unread",
             "error": "who-needs-me.py --section needs-you --all failed twice: "
                      "TimeoutExpired: 60"})
        self.assertEqual(doc["feed_counts"], {"gated": 0, "clear": 0, "unread": 2})
        self.assertEqual(doc["probes"][0]["probe"],
                         "who-needs-me.py --section needs-you --all")
        self.assertIn("failed twice", doc["probes"][0]["error"])
        self.assert_invariant(doc)


class UnresolvedTrackedSet(Tmp):
    """A tracked set that resolves to nothing reads `unread`, never `clear`.

    Measured 2026-11-08 (Development Pipeline Throughput): the caller passed 38
    vault-relative page paths, `resolve_tracked` matched none of them against a task's
    name or its `goals:`, and the `if not rows` fallback synthesised one placeholder per
    member with `sessions: []`. Every family then read that empty list as evidence —
    `feed_verdict([], gated, "read")` answers `clear`, `external_wait([])` answers
    `none` — so the document reported `clear=38 · none=38` for a fleet of 543 real task
    rows (387 carrying a session id) it never looked at.

    The fixture's input is those 38 page paths, and the vault behind it is a real one:
    the resolver runs for real, so the test fails if the placeholder ever comes back.
    """

    PAGE_PATHS = ["24 Goals/Goal %02d.md" % n for n in range(1, 39)]

    def vault(self, tasks):
        """A real vault: `tasks` maps a task name to the goal names it declares."""
        tasks_dir = os.path.join(self.dir, "25 Tasks")
        os.makedirs(tasks_dir, exist_ok=True)
        for name, goals in tasks.items():
            goals_fm = "".join("    - '[[%s]]'\n" % g for g in goals)
            with open(os.path.join(tasks_dir, name + ".md"), "w", encoding="utf-8") as fh:
                fh.write("---\nstatus: in_progress\nphase: execution\ngoals:\n%s---\n"
                         "Tags: [[Task]]\n" % goals_fm)
        return self.dir

    def document(self, members, tasks=None):
        vault = self.vault(tasks or {})
        path = os.path.join(self.dir, "tracked.txt")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("\n".join(members) + ("\n" if members else ""))
        sessions = os.path.join(self.dir, "sessions")
        os.makedirs(sessions, exist_ok=True)
        return rr.compute("A subject", path, vault, 900, now=1_700_000_000,
                          attention_dir=self.attention, sessions_dir=sessions,
                          projects_dir=self.projects, feed=CLEAN_FEED)

    def test_the_real_2026_11_08_case_reads_unread_not_clear(self):
        """SC4: the fixture's input is the 38 page paths that started this."""
        doc = self.document(self.PAGE_PATHS)
        self.assertEqual(doc["resolution"]["state"], "unresolved")
        self.assertEqual(doc["resolution"]["tracked"], 38)
        self.assertEqual(doc["resolution"]["resolved_rows"], 0)
        self.assertEqual(len(doc["resolution"]["unresolved"]), 38)
        self.assertEqual(len(doc["rows"]), 38)
        self.assertEqual(doc["feed_counts"]["clear"], 0)
        for name, rec in doc["rows"].items():
            self.assertFalse(rec["resolved"], name)
            self.assertEqual(rec["feed"], "unread", name)
            self.assertEqual(rec["external_wait"], "unread", name)
            self.assertEqual(rec["stuck"], "unread", name)
            self.assertEqual(rec["liveness"], "UNKNOWN", name)
            self.assertEqual(rec["in_flight"], "unread", name)

    def test_no_unresolved_row_carries_a_negative(self):
        """SC2: the placeholder is gone, so no family can read one as evidence.

        Asserted on the ROWS, not the source: `clear` / `none` on an empty id set is a
        legitimate answer for a resolved ownerless task, and only the `resolved` marker
        separates that from a row nobody read.
        """
        doc = self.document(self.PAGE_PATHS)
        negatives = {"feed": "clear", "external_wait": "none",
                     "in_flight": "quiet", "stuck": "none"}
        for name, rec in doc["rows"].items():
            self.assertFalse(rec["resolved"], name)
            for family, negative in negatives.items():
                self.assertNotEqual(rec[family], negative,
                                    "%s read %s for unresolved %s" % (family, negative, name))

    def test_a_name_set_that_resolves_carries_no_unresolved_row(self):
        doc = self.document(["Task One", "Task Two"],
                            {"Task One": ["A Goal"], "Task Two": ["A Goal"]})
        self.assertEqual(doc["resolution"]["state"], "resolved")
        self.assertEqual(doc["resolution"]["resolved_rows"], 2)
        self.assertEqual(doc["resolution"]["unresolved"], [])
        self.assertEqual(sorted(doc["rows"]), ["Task One", "Task Two"])
        self.assertTrue(all(rec["resolved"] for rec in doc["rows"].values()))

    def test_a_goal_member_that_resolves_is_not_itself_unresolved(self):
        """A goal name never equals a row's `name`, so deriving the unresolved set from
        the row names alone reports every goal as unresolved and carries a spurious
        `unread` row beside the tasks it admitted."""
        doc = self.document(["A Goal"],
                            {"Task One": ["A Goal"], "Task Two": ["A Goal"]})
        self.assertEqual(doc["resolution"]["state"], "resolved")
        self.assertEqual(doc["resolution"]["unresolved"], [])
        self.assertEqual(sorted(doc["rows"]), ["Task One", "Task Two"])

    def test_a_partly_resolved_set_names_only_the_names_that_admitted_nothing(self):
        doc = self.document(["A Goal", "24 Goals/Gone.md"], {"Task One": ["A Goal"]})
        self.assertEqual(doc["resolution"]["state"], "partial")
        self.assertEqual(doc["resolution"]["unresolved"], ["24 Goals/Gone.md"])
        self.assertEqual(doc["rows"]["24 Goals/Gone.md"]["feed"], "unread")
        self.assertTrue(doc["rows"]["Task One"]["resolved"])

    def test_no_tracked_names_at_all_is_empty_not_a_quiet_fleet(self):
        doc = self.document([])
        self.assertEqual(doc["resolution"]["state"], "empty")
        self.assertEqual(doc["rows"], {})
        self.assertEqual(doc["feed_counts"], {"gated": 0, "clear": 0, "unread": 0})


class UnrecordedSessions(Tmp):
    """A live session whose id is on NO page is still a row, and its park is its own.

    Measured 2026-11-11: session `1c05c414` sat `waiting` on the fleet while the round
    rendered `parked=0`, because no page carried it. `rows` is built from resolved task
    files, so a session stamped on none of them entered no row's id set — and no `parked`
    row could be emitted for it however parked it was. The failure direction is the
    point: an unrecorded park dropped, or read as `quiet`, renders a clean fleet while a
    worker waits on a gate.

    The fixture drives the real registry reader — `{pid}.json` files carrying
    `sessionId`, `status` and `cwd`, exactly the shape `~/.claude/sessions/` holds — so
    the test fails if the reader or the join is ever replaced by a shortcut.
    """

    COVERED = "aaaaaaaa-0000-4000-8000-000000000001"
    PARKED = "bbbbbbbb-0000-4000-8000-000000000002"
    IDLE = "cccccccc-0000-4000-8000-000000000003"
    MANAGER = "dddddddd-0000-4000-8000-000000000004"
    OTHER_VAULT = "eeeeeeee-0000-4000-8000-000000000005"

    def setUp(self):
        super().setUp()
        self.predispatch = rr._load("manager-predispatch")
        self._resolve = self.predispatch.resolve_tracked_by_member
        self._live = rr.liveness
        rr.liveness = lambda ids, endpoint, registry_dir: "LIVE"
        self.sessions = os.path.join(self.dir, "sessions")
        os.makedirs(self.sessions, exist_ok=True)
        self._pid = 9000

    def tearDown(self):
        self.predispatch.resolve_tracked_by_member = self._resolve
        rr.liveness = self._live
        super().tearDown()

    def session(self, sid, status, cwd=None):
        """Register one live session. A record's presence IS its liveness here.

        ⚠️ The pid must be one the OS actually holds, or `session-identity` publishes
        `alive: False` and the unrecorded join (correctly) skips the record — a fixture
        with an invented pid would test the skip, not the join. This process's own pid is
        occupied; with no `procStart` the identity pass answers "cannot tell", which the
        join keeps.
        """
        self._pid += 1
        rec = {"pid": os.getpid(), "sessionId": sid, "status": status,
               "cwd": cwd if cwd is not None else self.dir}
        path = os.path.join(self.sessions, "s%d.json" % self._pid)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(rec, fh)
        return sid

    def document(self, rows, members=None, sessions_dir=None):
        def resolve(vault, names):
            return ([{"name": n, "mtime": 0, "session": None, "sessions": rows[n]}
                     for n in names if n in rows], list(names))

        self.predispatch.resolve_tracked_by_member = resolve
        path = os.path.join(self.dir, "tracked.txt")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("\n".join(members if members is not None else list(rows)) + "\n")
        return rr.compute("A subject", path, self.dir, 900, now=1_700_000_000,
                          attention_dir=self.attention,
                          sessions_dir=sessions_dir if sessions_dir is not None else self.sessions,
                          projects_dir=self.projects, feed=CLEAN_FEED)

    def test_a_parked_session_on_no_page_surfaces_as_a_parked_row(self):
        """SC1: the observed defect — `waiting` on the fleet, absent from the document."""
        self.session(self.COVERED, "idle")
        self.session(self.PARKED, "waiting")
        doc = self.document({"Covered task": [self.COVERED]})

        self.assertIn(self.PARKED, doc["resolution"]["unrecorded"])
        key = "unrecorded:" + self.PARKED[:8]
        self.assertIn(key, doc["rows"])
        self.assertEqual(doc["rows"][key]["in_flight"], "parked")
        self.assertTrue(doc["rows"][key]["unrecorded"])
        self.assertFalse(doc["rows"][key]["resolved"])
        # The row is a real observation, not the synthesised placeholder: it carries the
        # session's own id and the registry limb that decided it.
        self.assertEqual(doc["rows"][key]["ids"], [self.PARKED])
        self.assertIn("registry: waiting", doc["rows"][key]["in_flight_limb"])
        # And the park is not double-reported as stuck.
        self.assertEqual(doc["rows"][key]["stuck"], "none")

    def test_an_unrecorded_session_that_is_not_parked_fabricates_no_park(self):
        """SC2: the negative control — the path discriminates, it is not a catch-all."""
        self.session(self.COVERED, "idle")
        self.session(self.IDLE, "idle")
        doc = self.document({"Covered task": [self.COVERED]})

        self.assertIn(self.IDLE, doc["resolution"]["unrecorded"])
        key = "unrecorded:" + self.IDLE[:8]
        self.assertIn(key, doc["rows"])
        self.assertNotEqual(doc["rows"][key]["in_flight"], "parked")
        # Nothing anywhere in the document reads `parked` for a fleet with no park.
        self.assertEqual([n for n, r in doc["rows"].items() if r["in_flight"] == "parked"], [])

    def test_a_busy_unrecorded_session_also_fabricates_no_park(self):
        """SC2, the second half: `busy` is the other state the criterion names."""
        self.session(self.COVERED, "idle")
        self.session(self.IDLE, "busy")
        doc = self.document({"Covered task": [self.COVERED]})
        key = "unrecorded:" + self.IDLE[:8]
        self.assertNotEqual(doc["rows"][key]["in_flight"], "parked")

    def test_a_manager_id_in_metrics_sessions_stays_covered_and_is_not_re_owned(self):
        """SC3: the guard — a manager id is evidence about the reader, not a new row.

        `row["sessions"]` is `read_task`'s output from
        `manager-predispatch.session_id_set` (the one home for "which fields are a
        session id"). The unrecorded block only READS that set to learn which sessions a
        page already accounts for, so a manager id arriving through `metrics_sessions`
        neither widens into a second row nor gets dropped from the row it belongs to.
        """
        self.session(self.COVERED, "idle")
        self.session(self.MANAGER, "waiting")
        doc = self.document({"Owned task": [self.COVERED, self.MANAGER]})

        # The manager id is on a page, so it is NOT unrecorded — and its `waiting`
        # status therefore does not fabricate a park row beside the task that names it.
        self.assertEqual(doc["resolution"]["unrecorded"], [])
        self.assertEqual(list(doc["rows"]), ["Owned task"])
        # The derivation is untouched: the row still carries the WHOLE set, unchanged.
        self.assertEqual(doc["rows"]["Owned task"]["ids"], [self.COVERED, self.MANAGER])

    def test_a_session_outside_this_vault_is_not_swept_in(self):
        """A sibling vault's fleet is that vault's round to report, not this one's."""
        sibling = os.path.join(self.dir, "..", "another-vault")
        self.session(self.COVERED, "idle")
        self.session(self.OTHER_VAULT, "waiting", cwd=sibling)
        doc = self.document({"Covered task": [self.COVERED]})
        self.assertEqual(doc["resolution"]["unrecorded"], [])
        self.assertNotIn("unrecorded:" + self.OTHER_VAULT[:8], doc["rows"])

    def test_the_row_count_invariant_includes_unrecorded(self):
        """The reader's reconciliation, widened: every row is accounted for exactly once."""
        self.session(self.COVERED, "idle")
        self.session(self.PARKED, "waiting")
        doc = self.document({"Covered task": [self.COVERED], "24 Goals/Gone.md": []},
                            members=["Covered task", "24 Goals/Gone.md"])
        res = doc["resolution"]
        self.assertEqual(len(doc["rows"]),
                         res["resolved_rows"] + len(res["unresolved"]) + len(res["unrecorded"]))
        self.assertEqual(len(res["unrecorded"]), 1)

    def test_an_absent_registry_adds_no_unrecorded_rows(self):
        """No registry is "cannot tell", never "nothing is there" — and never a row."""
        self.session(self.COVERED, "idle")
        self.session(self.PARKED, "waiting")
        # A registry dir that does not exist: `registry_records` answers None, and the
        # unrecorded join must decline rather than invent a fleet.
        doc = self.document({"Covered task": [self.COVERED]},
                            sessions_dir=os.path.join(self.dir, "no-such-registry"))
        self.assertEqual(doc["resolution"]["unrecorded"], [])
        self.assertEqual(list(doc["rows"]), ["Covered task"])

    def test_a_record_the_pid_check_disproved_is_not_a_park(self):
        """A stale registry entry reads `alive: False` — a dead session, not a parked one.

        Emitting a `parked` row for it would be the false positive this path exists to
        avoid, so the join skips it. `alive: None` is "cannot tell" and stays.
        """
        self.session(self.COVERED, "idle")
        dead = "ffffffff-0000-4000-8000-000000000006"
        with open(os.path.join(self.sessions, "dead.json"), "w", encoding="utf-8") as fh:
            json.dump({"pid": 999999, "sessionId": dead, "status": "waiting",
                       "cwd": self.dir}, fh)
        doc = self.document({"Covered task": [self.COVERED]})
        self.assertEqual(doc["resolution"]["unrecorded"], [])
        self.assertNotIn("unrecorded:" + dead[:8], doc["rows"])


if __name__ == "__main__":
    unittest.main()
