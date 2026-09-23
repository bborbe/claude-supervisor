#!/usr/bin/env python3
"""Tests for scripts/stop-probe.py's launchd gate check and per-vault path resolution.

A `StartInterval` launchd job has no standing pid between ticks, so presence is
loaded AND heartbeat fresh. Loaded alone would call a wedged job alive; a fresh
heartbeat alone would call a booted-out job alive for up to two intervals.

The path tests pin the defect this file was extended for: the gate writes under
`sweep-gate-loop/<vault>/`, the vault arrives from the session record in whatever
case that record was written with, and a probe that joins either segment wrongly
reports a live gate as missing. The regression assertion is therefore not "the path
looks right" but "the MISSING detail names the path that was actually looked at".

Run: python3 -m unittest discover -s scripts/tests -v
"""

import contextlib
import importlib.util
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("stop_probe", os.path.join(HERE, "..", "stop-probe.py"))
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)

HB = "/tmp/some/heartbeat"


class LaunchdGateTest(unittest.TestCase):
    def test_loaded_and_fresh_is_present(self):
        present, detail = probe.launchd_gate(True, 120, 900, HB)
        self.assertTrue(present)
        self.assertIn("loaded", detail)

    def test_booted_out_is_absent_even_with_fresh_heartbeat(self):
        present, detail = probe.launchd_gate(False, 10, 900, HB)
        self.assertFalse(present)
        self.assertIn("not loaded", detail)

    def test_loaded_but_stale_is_absent(self):
        present, detail = probe.launchd_gate(True, 1801, 900, HB)
        self.assertFalse(present)
        self.assertIn("STALE", detail)

    def test_loaded_heartbeat_missing_is_absent(self):
        present, detail = probe.launchd_gate(True, None, 900, HB)
        self.assertFalse(present)
        self.assertIn("MISSING", detail)

    def test_missing_detail_names_the_path_it_looked_at(self):
        """A MISSING that hides its path is the defect: the reader cannot tell a
        dead gate from a wrong path."""
        _, detail = probe.launchd_gate(True, None, 900, HB)
        self.assertIn(HB, detail)

    def test_unresolved_record_is_not_reported_as_missing(self):
        """No vault/subject in the record is a third state — folding it into MISSING
        would re-create the false negative under a different name."""
        present, detail = probe.launchd_gate(True, None, 900, "")
        self.assertFalse(present)
        self.assertIn("UNRESOLVED", detail)
        self.assertNotIn("MISSING", detail)


class GateFileTest(unittest.TestCase):
    def test_resolves_per_vault_heartbeat(self):
        self.assertEqual(
            probe.gate_file("personal", "Manager Layer", "heartbeat"),
            os.path.join(probe.STATE, "sweep-gate-loop", "personal", "manager-layer.heartbeat"),
        )

    def test_lowercases_a_display_case_vault(self):
        """Records written before 2026-09-23 carry `Personal`; the directory is
        lowercase. A strict join resolves a path the gate never writes."""
        self.assertEqual(
            probe.gate_file("Personal", "Manager Layer", "heartbeat"),
            probe.gate_file("personal", "Manager Layer", "heartbeat"),
        )

    def test_slugs_the_subject(self):
        self.assertTrue(
            probe.gate_file("personal", "Attention Routing", "tick.txt").endswith(
                "/attention-routing.tick.txt"
            )
        )

    def test_tick_and_ledger_are_siblings_of_the_heartbeat(self):
        base = os.path.join(probe.STATE, "sweep-gate-loop", "personal")
        self.assertEqual(probe.gate_file("personal", "Manager Layer", "tick.txt"),
                         os.path.join(base, "manager-layer.tick.txt"))
        self.assertEqual(probe.gate_file("personal", "Manager Layer", "ledger.txt"),
                         os.path.join(base, "manager-layer.ledger.txt"))

    def test_empty_vault_or_subject_is_unresolved(self):
        self.assertEqual(probe.gate_file("", "Manager Layer", "heartbeat"), "")
        self.assertEqual(probe.gate_file("personal", "", "heartbeat"), "")


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

    def test_empty_path_is_none(self):
        self.assertIsNone(probe.heartbeat_age(""))


class LastLineTest(unittest.TestCase):
    def test_returns_last_non_blank_line(self):
        with tempfile.NamedTemporaryFile("w", delete=False) as fh:
            fh.write("2026-09-23T17:05:57Z, ppid=1(launchd)\n\n")
        try:
            self.assertEqual(probe.last_line(fh.name), "2026-09-23T17:05:57Z, ppid=1(launchd)")
        finally:
            os.unlink(fh.name)

    def test_missing_file_is_empty(self):
        self.assertEqual(probe.last_line("/nonexistent/ledger.txt"), "")


class FileMtimeTest(unittest.TestCase):
    def test_missing_file_is_none(self):
        self.assertIsNone(probe.file_mtime("/nonexistent/tick.txt"))

    def test_empty_path_is_none(self):
        self.assertIsNone(probe.file_mtime(""))

    def test_reads_the_mtime_once(self):
        """The call site must never stat-then-stat-again: the gate rewrites its tick
        file, and a second stat on a vanished path would raise out of the probe."""
        with tempfile.NamedTemporaryFile("w", delete=False) as fh:
            fh.write("x")
        try:
            self.assertEqual(probe.file_mtime(fh.name), os.path.getmtime(fh.name))
        finally:
            os.unlink(fh.name)


class IntervalTest(unittest.TestCase):
    def test_missing_plist_falls_back(self):
        self.assertEqual(probe.launchd_interval("/nonexistent.plist"), probe.DEFAULT_INTERVAL)


class MainEndToEndTest(unittest.TestCase):
    """The negative case end to end, under a scratch state dir.

    This is the shape the task's SC2 asks for: a subject whose gate state does not
    exist must still read MISSING/ABSENT — and the MISSING line must name the path
    that was actually resolved, so a wrong path stays distinguishable from a dead
    gate. `launchd_loaded` is pinned so the result does not depend on whether this
    machine happens to have the job installed.
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._state = probe.STATE
        probe.STATE = self.tmp
        os.makedirs(os.path.join(self.tmp, "worker-manager"))
        self.record({"subject": "Scratch Subject", "vault": "personal", "branch": "topic"})

    def tearDown(self):
        probe.STATE = self._state
        shutil.rmtree(self.tmp, ignore_errors=True)

    def record(self, payload):
        with open(os.path.join(self.tmp, "worker-manager", "scratch.json"), "w") as fh:
            json.dump(payload, fh)

    def run_probe(self):
        argv = sys.argv
        sys.argv = ["stop-probe.py", "--session", "scratch"]
        try:
            with mock.patch.object(probe, "launchd_loaded", return_value=True), \
                    mock.patch.object(probe, "launchd_interval", return_value=900), \
                    contextlib.redirect_stdout(io.StringIO()) as out:
                probe.main()
        finally:
            sys.argv = argv
        return out.getvalue()

    def test_absent_gate_reports_missing_and_names_the_path(self):
        text = self.run_probe()
        want = os.path.join(self.tmp, "sweep-gate-loop", "personal", "scratch-subject.heartbeat")
        self.assertIn(f"MISSING {want}", text)
        self.assertIn("ABSENT", text)

    def test_resolved_path_is_lowercase_for_a_display_case_vault(self):
        self.record({"subject": "Scratch Subject", "vault": "Personal", "branch": "topic"})
        text = self.run_probe()
        self.assertIn("/sweep-gate-loop/personal/scratch-subject.heartbeat", text)
        self.assertNotIn("/sweep-gate-loop/Personal/", text)


if __name__ == "__main__":
    unittest.main()
