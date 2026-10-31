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
        os.makedirs(self.attention, exist_ok=True)

    def tearDown(self):
        self._dir.cleanup()

    def write(self, name, payload):
        path = os.path.join(self.attention, name)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(payload if isinstance(payload, str) else json.dumps(payload))
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

    def test_denial_marker_yields_stuck_denied(self):
        sid = "eeee5555"
        self.write(f"{sid}.denial.json",
                   {"tool": "Bash", "reason": "auto-mode classifier denied gh pr merge"})
        verdict, reason = rr.stuck_verdict({"mtime": time.time()}, [sid], "LIVE",
                                           "clear", 900, time.time(), self.attention)
        self.assertEqual(verdict, "denied")
        self.assertIn("Bash", reason)

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
                         attention_dir=self.attention)
        self.assertEqual(doc["park_threshold_seconds"], 1800)

    def test_a_shorter_tick_scales_with_it(self):
        """300s tick -> 600s park threshold: the rule is a multiple, not a constant."""
        path = os.path.join(self.dir, "tracked.txt")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("A task\n")
        doc = rr.compute("A subject", path, None, 300, now=1_700_000_000,
                         attention_dir=self.attention)
        self.assertEqual(doc["park_threshold_seconds"], 600)


if __name__ == "__main__":
    unittest.main()
