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
import tempfile
import unittest
from contextlib import redirect_stdout

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
        self.transcript = os.path.join(self.tmp.name, "%s.jsonl" % SID)

    def tearDown(self):
        self.tmp.cleanup()

    def round(self, pane=None):
        """One round's transcript; `pane` non-empty means the manager has a pane."""
        rec = {"kind": "round"}
        if pane is not None:
            rec["pane"] = pane
        with open(self.transcript, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(rec) + "\n")

    def run_round(self):
        out = io.StringIO()
        with redirect_stdout(out):
            rc = self.m.main(["--transcript", self.transcript, "--state-dir", self.state])
        return rc, out.getvalue()

    def count(self):
        with open(os.path.join(self.state, "%s.json" % SID), encoding="utf-8") as fh:
            return json.load(fh)["count"]

    def test_the_state_file_is_written_on_the_first_round(self):
        # SC3's probe: one invocation against the fixture leaves `count: 1`.
        self.round()
        self.run_round()
        self.assertEqual(self.count(), 1)

    def test_the_condition_fires_on_round_three_and_not_before(self):
        self.round()
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
        self.round()
        self.run_round()
        self.run_round()
        self.round(pane="204")
        self.run_round()
        self.assertEqual(self.count(), 0)

    def test_a_pane_carrying_transcript_never_emits(self):
        for _ in range(5):
            self.round(pane="204")
            _, out = self.run_round()
            self.assertNotIn("pane-less manager", out)


class Wiring(unittest.TestCase):
    """SC3's wiring half: the sweep render step must actually invoke the producer.

    A producer nothing calls passes every behavioural test and changes nothing — the
    condition it reports would still depend on a model noticing the manager's own
    `↪️ no branch — no pane` prose. The caller is asserted directly against the step files.
    """

    STEPS = ("goal-sweep.md", "topic-sweep.md")

    def test_the_sweep_render_steps_invoke_the_producer(self):
        root = os.path.dirname(os.path.dirname(HERE))
        for name in self.STEPS:
            path = os.path.join(root, "commands", "steps", name)
            with open(path, encoding="utf-8") as fh:
                text = fh.read()
            self.assertIn("pane-less-report.py", text, name)


if __name__ == "__main__":
    unittest.main()
