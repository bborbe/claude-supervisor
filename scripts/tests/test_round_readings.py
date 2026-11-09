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
        self._resolve = self.predispatch.resolve_tracked
        self._checked = self.filt.headless_items_checked
        self._live = rr.liveness
        rr.liveness = lambda ids, endpoint, registry_dir: "ABSENT"

    def tearDown(self):
        self.predispatch.resolve_tracked = self._resolve
        self.filt.headless_items_checked = self._checked
        rr.liveness = self._live
        super().tearDown()

    def document(self, rows, feed):
        self.predispatch.resolve_tracked = lambda vault, members: [
            {"name": name, "mtime": 0, "session": None, "sessions": rows[name]}
            for name in members
        ]
        path = os.path.join(self.dir, "tracked.txt")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("\n".join(rows) + "\n")
        # A truthy vault is what routes through `resolve_tracked`, which is patched
        # above; the path itself is never read.
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


if __name__ == "__main__":
    unittest.main()
