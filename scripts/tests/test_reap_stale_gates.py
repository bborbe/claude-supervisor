#!/usr/bin/env python3
"""Tests for scripts/reap-stale-gates.py.

The reaper's whole risk is a wrong reap, so the cases pin the three decisions that
would cause one: which items are still open, which are reapable, and what the one
appended line looks like. The refusal case pins the crash a failed pane read would
otherwise cause (`wezterm_panes()` returns None, and `is_routable` would raise).
"""

import importlib.util
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(os.path.dirname(HERE), "reap-stale-gates.py")


def load_module():
    spec = importlib.util.spec_from_file_location("reap_stale_gates", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class FoldTest(unittest.TestCase):
    def setUp(self):
        self.mod = load_module()
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, name, lines):
        path = os.path.join(self.dir, name)
        with open(path, "w", encoding="utf-8") as f:
            for line in lines:
                f.write(json.dumps(line) + "\n")
        return path

    def test_open_item_survives(self):
        self.write("a.events.jsonl", [{"item_id": "x", "type": "open", "ts": 1}])
        items = list(self.mod.open_items(self.dir))
        self.assertEqual([r["item_id"] for _, r in items], ["x"])

    def test_close_line_retires_item(self):
        self.write("a.events.jsonl", [
            {"item_id": "x", "type": "open", "ts": 1},
            {"item_id": "x", "type": "close", "ts": 2},
        ])
        self.assertEqual(list(self.mod.open_items(self.dir)), [])

    def test_any_non_open_line_closes(self):
        # `load_events` folds on `type == "open"`; every other value closes.
        self.write("a.events.jsonl", [
            {"item_id": "x", "type": "open", "ts": 1},
            {"item_id": "x", "type": "answer", "ts": 2},
        ])
        self.assertEqual(list(self.mod.open_items(self.dir)), [])

    def test_torn_line_is_skipped(self):
        path = os.path.join(self.dir, "a.events.jsonl")
        with open(path, "w", encoding="utf-8") as f:
            f.write(json.dumps({"item_id": "x", "type": "open", "ts": 1}) + "\n")
            f.write('{"item_id": "y", "ty')
        items = list(self.mod.open_items(self.dir))
        self.assertEqual([r["item_id"] for _, r in items], ["x"])

    def test_items_without_id_are_ignored(self):
        self.write("a.events.jsonl", [{"type": "open", "ts": 1}])
        self.assertEqual(list(self.mod.open_items(self.dir)), [])


class LivenessTest(unittest.TestCase):
    def setUp(self):
        self.mod = load_module()

    def test_missing_session_is_unknown_not_absent(self):
        self.assertEqual(self.mod.liveness(""), "UNKNOWN")

    def test_absent_output_reads_absent(self):
        done = mock.Mock(stdout="ABSENT — no session id matches x\n", stderr="")
        with mock.patch.object(self.mod.subprocess, "run", return_value=done):
            self.assertEqual(self.mod.liveness("x"), "ABSENT")

    def test_failed_probe_is_unknown(self):
        with mock.patch.object(self.mod.subprocess, "run", side_effect=OSError("boom")):
            self.assertEqual(self.mod.liveness("x"), "UNKNOWN")

    def test_unparseable_output_is_unknown(self):
        done = mock.Mock(stdout="something else\n", stderr="")
        with mock.patch.object(self.mod.subprocess, "run", return_value=done):
            self.assertEqual(self.mod.liveness("x"), "UNKNOWN")


class CloseLineTest(unittest.TestCase):
    def test_shape_closes_the_item(self):
        mod = load_module()
        line = json.loads(mod.close_line({"item_id": "i", "session_id": "s"}, 42.0))
        self.assertEqual(line["item_id"], "i")
        self.assertEqual(line["session_id"], "s")
        self.assertEqual(line["type"], "close")
        self.assertEqual(line["cleared_by"], mod.REAPER)
        self.assertNotEqual(line["type"], "open")


class PaneReadRefusalTest(unittest.TestCase):
    """A failed pane read must refuse, never crash on `str(pane) not in None`."""

    def setUp(self):
        self.mod = load_module()
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name
        with open(os.path.join(self.dir, "a.events.jsonl"), "w", encoding="utf-8") as f:
            f.write(json.dumps({"item_id": "x", "type": "open",
                                "ts": 0, "session_id": "s", "pane": "1"}) + "\n")

    def tearDown(self):
        self.tmp.cleanup()

    def test_none_pmap_refuses_with_exit_2(self):
        fake = mock.Mock()
        fake.wezterm_panes.return_value = None
        fake.read_registry.return_value = {}
        with mock.patch.object(self.mod, "load_who_needs_me", return_value=fake):
            err = io.StringIO()
            with redirect_stderr(err):
                rc = self.mod.main(["--state", self.dir])
        self.assertEqual(rc, 2)
        self.assertIn("pane read failed", err.getvalue())

    def test_apply_writes_one_close_line(self):
        fake = mock.Mock()
        fake.wezterm_panes.return_value = {}
        fake.read_registry.return_value = {}
        fake.CAUSE_OPERATOR = "awaiting-operator"
        fake.cause_class.return_value = "awaiting-operator"
        fake.row.return_value = "row"
        with mock.patch.object(self.mod, "load_who_needs_me", return_value=fake), \
             mock.patch.object(self.mod, "liveness", return_value="ABSENT"):
            with redirect_stderr(io.StringIO()):
                rc = self.mod.main(["--state", self.dir, "--apply", "--max-age-hours", "0"])
        self.assertEqual(rc, 0)
        with open(os.path.join(self.dir, "a.events.jsonl"), encoding="utf-8") as f:
            types = [json.loads(l)["type"] for l in f if l.strip()]
        self.assertEqual(types, ["open", "close"])


class SelectionTest(unittest.TestCase):
    """The two cases review round 1 named as priority: the cutoff boundary and a
    non-`awaiting-operator` cause. Both are the difference between reaping a live
    gate and leaving a dead one, so each is pinned by a dry-run count."""

    NOW = 10000.0

    def setUp(self):
        self.mod = load_module()
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def dry_run(self, ts, cause, max_age_hours=1):
        with open(os.path.join(self.dir, "a.events.jsonl"), "w", encoding="utf-8") as f:
            f.write(json.dumps({"item_id": "x", "type": "open", "ts": ts,
                                "session_id": "s", "pane": "1"}) + "\n")
        fake = mock.Mock()
        fake.wezterm_panes.return_value = {}
        fake.read_registry.return_value = {}
        fake.CAUSE_OPERATOR = "awaiting-operator"
        fake.cause_class.return_value = cause
        fake.row.return_value = "row"
        with mock.patch.object(self.mod, "load_who_needs_me", return_value=fake), \
             mock.patch.object(self.mod, "liveness", return_value="ABSENT"), \
             mock.patch.object(self.mod.time, "time", return_value=self.NOW):
            out = io.StringIO()
            with redirect_stdout(out), redirect_stderr(io.StringIO()):
                rc = self.mod.main(["--state", self.dir,
                                    "--max-age-hours", str(max_age_hours)])
        self.assertEqual(rc, 0)
        return out.getvalue()

    def test_item_exactly_at_cutoff_is_included(self):
        # cutoff = NOW - 1h; `<=` admits the boundary item.
        self.assertIn("would reap 1", self.dry_run(self.NOW - 3600, "awaiting-operator"))

    def test_item_inside_cutoff_is_excluded(self):
        self.assertIn("would reap 0", self.dry_run(self.NOW - 3599, "awaiting-operator"))

    def test_non_operator_cause_is_kept(self):
        self.assertIn("would reap 0", self.dry_run(0, "task-completed"))


if __name__ == "__main__":
    unittest.main()
