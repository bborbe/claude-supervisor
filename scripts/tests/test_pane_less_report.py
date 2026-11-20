#!/usr/bin/env python3
"""Tests for scripts/pane-less-report.py — the pane-less manager report.

The counter lives on disk because the round render invokes this as a fresh process each
round; these cases drive it the same way (one invocation per round) so an in-memory counter
could not pass them.
"""
import importlib.util
import io
import json
import os
import re
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(os.path.dirname(HERE), "pane-less-report.py")

SID = "616a52dd-833b-475b-889f-ee1580c73c37"


def load():
    spec = importlib.util.spec_from_file_location("pane_less_report", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class PaneLessReport(unittest.TestCase):
    def setUp(self):
        self.m = load()
        self.tmp = tempfile.TemporaryDirectory()
        self.state = os.path.join(self.tmp.name, "state")

    def tearDown(self):
        self.tmp.cleanup()

    def run_round(self, pane=""):
        out = io.StringIO()
        with redirect_stdout(out):
            rc = self.m.main(
                ["--session", SID, "--pane", pane, "--state-dir", self.state]
            )
        return rc, out.getvalue()

    def count(self):
        with open(os.path.join(self.state, "%s.json" % SID), encoding="utf-8") as fh:
            return json.load(fh)["count"]

    def test_the_state_file_is_written_on_the_first_round(self):
        self.run_round()
        self.assertEqual(self.count(), 1)

    def test_the_condition_fires_on_round_three_and_not_before(self):
        _, first = self.run_round()
        _, second = self.run_round()
        _, third = self.run_round()
        self.assertNotIn("pane-less manager", first)
        self.assertNotIn("pane-less manager", second)
        self.assertIn("pane-less manager %s" % SID, third)
        self.assertIn("/supervisor:restart %s" % SID, third)

    def test_a_pane_resets_the_counter(self):
        # The control: a manager that acquires a pane is reachable again, so the run ends
        # rather than continuing to accumulate toward the threshold.
        self.run_round()
        self.run_round()
        self.run_round(pane="204")
        self.assertEqual(self.count(), 0)

    def test_a_pane_carrying_round_never_emits(self):
        for _ in range(5):
            _, out = self.run_round(pane="204")
            self.assertNotIn("pane-less manager", out)

    def test_a_custom_threshold_is_honoured(self):
        out = io.StringIO()
        with redirect_stdout(out):
            self.m.main(["--session", SID, "--pane", "", "--state-dir", self.state,
                         "--threshold", "1"])
        self.assertIn("pane-less manager %s" % SID, out.getvalue())

    def test_a_malformed_state_file_reads_as_zero(self):
        # A corrupt or unreadable counter must not crash the round, and must not be read as
        # a high count that fires the condition early.
        os.makedirs(self.state, exist_ok=True)
        with open(os.path.join(self.state, "%s.json" % SID), "w", encoding="utf-8") as fh:
            fh.write("not json")
        self.run_round()
        self.assertEqual(self.count(), 1)

    def test_an_empty_session_is_a_usage_error(self):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            rc = self.m.main(["--session", "", "--state-dir", self.state])
        self.assertEqual(rc, 2)


class Wiring(unittest.TestCase):
    """SC3's wiring half: the sweep render step must actually INVOKE the producer.

    ⚠️ **A bare substring is not the assertion.** The filename appearing in prose or a
    comment would satisfy a text match while the render never runs the script, so the
    command is extracted from its backticked span and required to carry its two flags. A
    producer nothing calls passes every behavioural test and changes nothing — the condition
    would still depend on a model noticing the manager's own `↪️ no branch — no pane` prose.
    """

    STEPS = ("goal-sweep.md", "topic-sweep.md")
    INVOCATION = re.compile(r"`(python3 [^`]*pane-less-report\.py[^`]*)`")

    def test_the_sweep_render_steps_invoke_the_producer(self):
        root = os.path.dirname(os.path.dirname(HERE))
        for name in self.STEPS:
            path = os.path.join(root, "commands", "steps", name)
            with open(path, encoding="utf-8") as fh:
                text = fh.read()
            found = self.INVOCATION.findall(text)
            self.assertTrue(found, "%s never invokes the producer" % name)
            self.assertIn("--session", found[0], name)
            self.assertIn("--pane", found[0], name)


if __name__ == "__main__":
    unittest.main()
