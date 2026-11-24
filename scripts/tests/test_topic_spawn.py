#!/usr/bin/env python3
"""Tests for scripts/topic-spawn.py — the topic manager ceiling (`--topic-open`).

After the goal-manager tier merged into the topic tier (2026-11-24), the script answers one
question: may a manager be opened for THIS topic page? Each refusal is a named token on the
first line, paired with a positive control.
"""
import io
import pathlib
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout

_HERE = pathlib.Path(__file__).resolve().parent

import importlib.util

_spec = importlib.util.spec_from_file_location("topic_spawn", str(_HERE.parent / "topic-spawn.py"))
ts = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ts)

TOPIC = """---
page_type: topic
status: in_progress
---
## Goals
- [[Alpha]]
"""


class TopicOpenTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.vault = pathlib.Path(self.tmp.name)
        (self.vault / "23 Topics").mkdir()
        (self.vault / "23 Topics" / "A Topic.md").write_text(TOPIC, encoding="utf-8")
        # The fleet probes are stubbed: this suite tests the GATE, not the live fleet.
        self._orig = (ts.fleet_manager_count, ts.fleet_manager_ceiling)
        ts.fleet_manager_count = lambda: 0
        ts.fleet_manager_ceiling = lambda: 10
        self.addCleanup(self._restore)

    def _restore(self):
        ts.fleet_manager_count, ts.fleet_manager_ceiling = self._orig

    def _flag_topic(self, setter="operator"):
        page = self.vault / "23 Topics" / "A Topic.md"
        page.write_text(
            page.read_text(encoding="utf-8").replace(
                "status: in_progress",
                f"status: in_progress\nflag: true\nflag_set_by: {setter}",
            ),
            encoding="utf-8",
        )

    def run_gate(self, *argv):
        out = io.StringIO()
        with redirect_stdout(out):
            rc = ts.main(["--vault", str(self.vault), "--topic", "A Topic", *argv])
        return rc, out.getvalue().splitlines()

    def test_an_unflagged_topic_is_refused(self):
        rc, out = self.run_gate("--topic-open")
        self.assertEqual(1, rc)
        self.assertEqual("topic-unflagged", out[0])

    def test_a_flagged_topic_opens(self):
        self._flag_topic()
        rc, out = self.run_gate("--topic-open")
        self.assertEqual(0, rc)
        self.assertEqual("open-ok: A Topic", out[0])

    def test_a_topic_flagged_by_an_agent_is_refused(self):
        self._flag_topic(setter="agent")
        rc, out = self.run_gate("--topic-open")
        self.assertEqual(1, rc)
        self.assertEqual("topic-unflagged", out[0])

    def test_a_flagged_topic_is_refused_at_the_fleet_ceiling(self):
        self._flag_topic()
        ts.fleet_manager_count = lambda: 10
        ts.fleet_manager_ceiling = lambda: 10
        rc, out = self.run_gate("--topic-open")
        self.assertEqual(1, rc)
        self.assertEqual("manager-cap", out[0])

    def test_a_hand_run_opens_an_unflagged_topic(self):
        rc, out = self.run_gate("--topic-open", "--hand-run")
        self.assertEqual(0, rc)
        self.assertEqual("open-ok: A Topic", out[0])

    def test_a_hand_run_opens_an_agent_flagged_topic(self):
        self._flag_topic(setter="agent")
        rc, out = self.run_gate("--topic-open", "--hand-run")
        self.assertEqual(0, rc)

    def test_a_hand_run_is_still_refused_at_the_fleet_ceiling(self):
        ts.fleet_manager_count = lambda: 10
        ts.fleet_manager_ceiling = lambda: 10
        rc, out = self.run_gate("--topic-open", "--hand-run")
        self.assertEqual(1, rc)
        self.assertEqual("manager-cap", out[0])

    def test_hand_run_without_topic_open_is_a_usage_error(self):
        """`--hand-run` never loosens the topic-open gate; it only skips the flag check."""
        with self.assertRaises(SystemExit) as raised, redirect_stderr(io.StringIO()):
            self.run_gate("--hand-run")
        self.assertEqual(2, raised.exception.code)

    def test_a_missing_topic_page_is_topic_not_found(self):
        out = io.StringIO()
        with redirect_stdout(out):
            rc = ts.main(["--vault", str(self.vault), "--topic", "No Such", "--topic-open"])
        self.assertEqual(1, rc)
        self.assertEqual("topic-not-found", out.getvalue().splitlines()[0])


if __name__ == "__main__":
    unittest.main()
