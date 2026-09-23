#!/usr/bin/env python3
"""Tests for scripts/manager-liveness.py.

The load-bearing properties: a lapse is flagged only past 2 x the manager's OWN
recorded interval (a slow manager is not a lapsed one), a stop marker suppresses the
flag, the next re-arm clears the marker, and one lapse notifies exactly once.

Run: python3 -m unittest discover -s scripts/tests -v
"""

import importlib.util
import io
import os
import tempfile
import time
import unittest
from contextlib import redirect_stdout

_HERE = os.path.dirname(os.path.abspath(__file__))


def load(state_dir):
    os.environ["MANAGER_LIVENESS_STATE_DIR"] = state_dir
    spec = importlib.util.spec_from_file_location(
        "manager_liveness", os.path.join(os.path.dirname(_HERE), "manager-liveness.py")
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class Liveness(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.m = load(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()
        os.environ.pop("MANAGER_LIVENESS_STATE_DIR", None)

    def arm(self, topic, interval, age):
        with redirect_stdout(io.StringIO()):
            self.m.arm(topic, interval)
        p = self.m.path(self.m.slug(topic), "cadence")
        t = time.time() - age
        os.utime(p, (t, t))

    def check(self):
        out = io.StringIO()
        with redirect_stdout(out):
            rc = self.m.check()
        return rc, out.getvalue()

    def test_fresh_is_ok(self):
        self.arm("Manager Layer", 300, age=100)
        self.assertEqual(self.check(), (0, ""))

    def test_past_two_intervals_is_stale(self):
        self.arm("Manager Layer", 300, age=2 * 300 + 61)
        rc, out = self.check()
        self.assertEqual(rc, 10)
        self.assertTrue(out.startswith("STALE manager-layer "), out)

    def test_slow_manager_uses_its_own_interval(self):
        # 19 min old on a 20-min cadence: healthy, not a lapse (2026-09-23 13:32 case).
        self.arm("Manager Layer", 1200, age=19 * 60)
        self.assertEqual(self.check(), (0, ""))

    def test_stop_marker_suppresses(self):
        self.arm("Manager Layer", 300, age=5000)
        with redirect_stdout(io.StringIO()):
            self.m.stop("Manager Layer")
        self.assertEqual(self.check(), (0, ""))

    def test_rearm_clears_stop_marker(self):
        with redirect_stdout(io.StringIO()):
            self.m.stop("Manager Layer")
        self.arm("Manager Layer", 300, age=5000)
        self.assertFalse(os.path.exists(self.m.path("manager-layer", "stopped")))
        self.assertEqual(self.check()[0], 10)

    def test_one_lapse_notifies_once_then_recovers(self):
        self.arm("Manager Layer", 300, age=5000)
        self.assertEqual(self.check()[0], 10)
        self.assertEqual(self.check(), (0, ""))  # second poll: no repeat
        self.arm("Manager Layer", 300, age=0)
        rc, out = self.check()
        self.assertEqual(rc, 0)
        self.assertTrue(out.startswith("RECOVERED manager-layer OK"), out)
        self.arm("Manager Layer", 300, age=5000)  # a NEW lapse notifies again
        self.assertEqual(self.check()[0], 10)

    def test_topics_are_independent(self):
        self.arm("Alpha", 300, age=5000)
        self.arm("Beta", 300, age=10)
        rc, out = self.check()
        self.assertEqual(rc, 10)
        self.assertIn("STALE alpha", out)
        self.assertNotIn("beta", out)

    def plant_arms(self, topic, offsets):
        """Write a re-arm history: epochs at now - offset for each offset."""
        s = self.m.slug(topic)
        now = time.time()
        body = "".join(f"{now - o:.0f}\n" for o in offsets)
        with open(self.m.path(s, "arms"), "w") as fh:
            fh.write(body)

    def test_observed_spacing_beats_the_reported_delay(self):
        # The production case, 2026-09-23: the manager reported a 300 s delay while
        # really arming every 17 min. A limit built from the reported delay fires on
        # a live manager; one built from the observed spacing does not.
        self.plant_arms("Manager Layer", [3760, 2740, 1720, 700])
        self.arm("Manager Layer", 300, age=700)
        self.assertEqual(self.check(), (0, ""))  # 700 < 2*1020+60

    def test_reported_delay_still_applies_without_history(self):
        self.arm("Manager Layer", 300, age=700)  # one arm, no gaps to learn from
        rc, out = self.check()
        self.assertEqual(rc, 10)
        self.assertIn("period=300s(reported)", out)

    def test_genuinely_stopped_loop_still_flags_despite_history(self):
        self.plant_arms("Manager Layer", [3760, 2740, 1720, 700])
        self.arm("Manager Layer", 300, age=3 * 1020)  # way past 2 x observed period
        rc, out = self.check()
        self.assertEqual(rc, 10)
        self.assertIn("period=1020s(observed)", out)

    def test_rearm_after_a_stand_down_resets_the_history(self):
        # The outage gap must never count as cadence. It cannot be filtered at read
        # time — `arm` deletes the marker that records when the stop happened — so a
        # restart starts a fresh sample instead.
        self.plant_arms("Manager Layer", [11000, 10000, 9000, 8000])
        with redirect_stdout(io.StringIO()):
            self.m.stop("Manager Layer")
            self.m.arm("Manager Layer", 300)
        self.assertEqual(len(self.m.read_arms("manager-layer")), 1)
        self.assertIsNone(self.m.observed_interval("manager-layer"))

    def test_history_survives_ordinary_rearms(self):
        self.plant_arms("Manager Layer", [3000, 2000, 1000])
        self.arm("Manager Layer", 300, age=0)
        self.assertEqual(len(self.m.read_arms("manager-layer")), 4)
        self.assertEqual(int(self.m.observed_interval("manager-layer")), 1000)

    def test_unreadable_cadence_is_flagged(self):
        with open(self.m.path("broken", "cadence"), "w") as fh:
            fh.write("not-a-number\n")
        rc, out = self.check()
        self.assertEqual(rc, 10)
        self.assertTrue(out.startswith("INVALID broken"), out)


if __name__ == "__main__":
    unittest.main()
