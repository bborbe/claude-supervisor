#!/usr/bin/env python3
"""Tests for scripts/stop-probe.py's launchd gate check.

A `StartInterval` launchd job has no standing pid between ticks, so presence is
loaded AND heartbeat fresh. Loaded alone would call a wedged job alive; a fresh
heartbeat alone would call a booted-out job alive for up to two intervals.

Run: python3 -m unittest discover -s scripts/tests -v
"""

import importlib.util
import os
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("stop_probe", os.path.join(HERE, "..", "stop-probe.py"))
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)


class LaunchdGateTest(unittest.TestCase):
    def test_loaded_and_fresh_is_present(self):
        present, detail = probe.launchd_gate(True, 120, 900)
        self.assertTrue(present)
        self.assertIn("loaded", detail)

    def test_booted_out_is_absent_even_with_fresh_heartbeat(self):
        present, detail = probe.launchd_gate(False, 10, 900)
        self.assertFalse(present)
        self.assertIn("not loaded", detail)

    def test_loaded_but_stale_is_absent(self):
        present, detail = probe.launchd_gate(True, 1801, 900)
        self.assertFalse(present)
        self.assertIn("STALE", detail)

    def test_loaded_heartbeat_missing_is_absent(self):
        present, detail = probe.launchd_gate(True, None, 900)
        self.assertFalse(present)
        self.assertIn("MISSING", detail)


class HeartbeatAgeTest(unittest.TestCase):
    def test_reads_epoch_field(self):
        with tempfile.NamedTemporaryFile("w", delete=False) as fh:
            fh.write("1000 2026-09-23T12:00:00Z rc=0\n")
        try:
            self.assertEqual(probe.heartbeat_age(fh.name, now=1300), 300)
        finally:
            os.unlink(fh.name)

    def test_missing_file_is_none(self):
        self.assertIsNone(probe.heartbeat_age("/nonexistent/heartbeat"))


class IntervalTest(unittest.TestCase):
    def test_missing_plist_falls_back(self):
        self.assertEqual(probe.launchd_interval("/nonexistent.plist"), probe.DEFAULT_INTERVAL)


if __name__ == "__main__":
    unittest.main()
