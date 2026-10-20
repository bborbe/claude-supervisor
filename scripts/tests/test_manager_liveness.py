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
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout

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
            self.m.arm(topic, "goal", interval)
        p = self.m.path(self.m.slug(topic, "goal"), "cadence")
        t = time.time() - age
        os.utime(p, (t, t))

    def check(self, now=None):
        out = io.StringIO()
        with redirect_stdout(out):
            rc = self.m.check(now)
        return rc, out.getvalue()

    def set_arm_time(self, topic, t):
        """Move a subject's last-arm marker to an explicit epoch."""
        p = self.m.path(self.m.slug(topic, "goal"), "cadence")
        os.utime(p, (t, t))

    def test_fresh_is_ok(self):
        self.arm("Manager Layer", 300, age=100)
        self.assertEqual(self.check(), (0, ""))

    def test_past_two_intervals_is_stale(self):
        self.arm("Manager Layer", 300, age=2 * 300 + 61)
        rc, out = self.check()
        self.assertEqual(rc, 10)
        self.assertTrue(out.startswith("STALE goal--manager-layer "), out)

    def test_slow_manager_uses_its_own_interval(self):
        # 19 min old on a 20-min cadence: healthy, not a lapse (2026-09-23 13:32 case).
        self.arm("Manager Layer", 1200, age=19 * 60)
        self.assertEqual(self.check(), (0, ""))

    def test_stop_marker_suppresses(self):
        self.arm("Manager Layer", 300, age=5000)
        with redirect_stdout(io.StringIO()):
            self.m.stop("Manager Layer", "goal")
        self.assertEqual(self.check(), (0, ""))

    def test_rearm_clears_stop_marker(self):
        with redirect_stdout(io.StringIO()):
            self.m.stop("Manager Layer", "goal")
        self.arm("Manager Layer", 300, age=5000)
        self.assertFalse(os.path.exists(self.m.path("goal--manager-layer", "stopped")))
        self.assertEqual(self.check()[0], 10)

    def test_one_lapse_notifies_once_then_recovers(self):
        self.arm("Manager Layer", 300, age=5000)
        self.assertEqual(self.check()[0], 10)
        self.assertEqual(self.check(), (0, ""))  # second poll: no repeat
        self.arm("Manager Layer", 300, age=0)
        rc, out = self.check()
        self.assertEqual(rc, 0)
        self.assertTrue(out.startswith("RECOVERED goal--manager-layer OK"), out)
        self.arm("Manager Layer", 300, age=5000)  # a NEW lapse notifies again
        self.assertEqual(self.check()[0], 10)

    def test_a_lapse_after_a_rearm_notifies_again(self):
        # The production shape, 2026-10-20: the manager re-armed and lapsed again
        # between two polls, so `check` never observed the RECOVERED transition —
        # its only other way to clear the entry. The entry then outlived the lapse
        # it described and every later lapse was silent: `check` returned 0 on a
        # subject `--state` read as STALE, while the topic had no live manager.
        t0 = time.time()
        self.arm("Manager Layer", 300, age=5000)
        self.assertEqual(self.check(t0)[0], 10)  # the first lapse notifies
        self.set_arm_time("Manager Layer", t0 + 100)  # the manager re-arms
        rc, out = self.check(t0 + 100 + 2000)  # ... and lapses again, unseen
        self.assertEqual(rc, 10, "a lapse after a re-arm must notify again")
        self.assertIn("STALE goal--manager-layer", out)

    def test_a_legacy_list_does_not_reannounce_known_lapses(self):
        # The upgrade path: a list-shaped file must not fire a burst for every
        # subject already known to be lapsed.
        t = time.time()
        self.arm("Manager Layer", 300, age=5000)
        self.set_arm_time("Manager Layer", t - 1000)  # armed BEFORE the file write
        p = os.path.join(self.m.STATE_DIR, self.m.NOTIFIED)
        with open(p, "w") as fh:
            fh.write('["goal--manager-layer"]\n')
        os.utime(p, (t, t))
        self.assertEqual(self.check(t + 100)[0], 0)  # stale, but already known

    def test_a_legacy_entry_still_renotifies_after_a_later_arm(self):
        # The half the upgrade must NOT swallow: a manager that armed after the
        # legacy write has a new lapse, and it has to be reported.
        t = time.time()
        self.arm("Manager Layer", 300, age=5000)
        p = os.path.join(self.m.STATE_DIR, self.m.NOTIFIED)
        with open(p, "w") as fh:
            fh.write('["goal--manager-layer"]\n')
        os.utime(p, (t, t))
        self.set_arm_time("Manager Layer", t + 50)  # re-armed AFTER the file write
        rc, out = self.check(t + 50 + 2000)  # ... then lapsed again
        self.assertEqual(rc, 10, "a post-upgrade re-arm must re-notify")
        self.assertIn("STALE goal--manager-layer", out)

    def test_a_rearm_does_not_disturb_a_siblings_entry(self):
        # The re-notify is scoped to the subject that armed; a sibling's entry must
        # survive, or one healthy manager would re-announce every other lapsed one.
        t0 = time.time()
        self.arm("Alpha", 300, age=5000)
        self.arm("Beta", 300, age=5000)
        self.assertEqual(self.check(t0)[0], 10)  # both notified
        self.set_arm_time("Alpha", t0 + 100)  # Alpha recovers
        rc, out = self.check(t0 + 200)
        self.assertEqual(rc, 0, out)  # Beta is still notified, so nothing new
        self.assertNotIn("STALE", out)
        self.assertIn("RECOVERED goal--alpha", out)

    def test_topics_are_independent(self):
        self.arm("Alpha", 300, age=5000)
        self.arm("Beta", 300, age=10)
        rc, out = self.check()
        self.assertEqual(rc, 10)
        self.assertIn("STALE goal--alpha", out)
        self.assertNotIn("beta", out)

    def plant_arms(self, topic, offsets):
        """Write a re-arm history: epochs at now - offset for each offset."""
        s = self.m.slug(topic, "goal")
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
        self.arm("Manager Layer", 300, age=3 * 1020)  # way past one observed period
        rc, out = self.check()
        self.assertEqual(rc, 10)
        self.assertIn("period=1020s(observed)", out)

    def test_one_cadence_plus_slack_is_still_ok(self):
        # The limit is ONE cadence plus slack, not two (2026-10-20). A manager that
        # has only just reached the limit has not yet missed its next re-arm.
        self.plant_arms("Manager Layer", [2700, 1800, 900])
        self.arm("Manager Layer", 900, age=900 + 180)
        self.assertEqual(self.check(), (0, ""))

    def test_past_one_cadence_plus_slack_is_stale(self):
        self.plant_arms("Manager Layer", [2700, 1800, 900])
        self.arm("Manager Layer", 900, age=900 + 181)
        rc, out = self.check()
        self.assertEqual(rc, 10)
        self.assertIn("limit=1080s", out)

    def test_a_slow_tick_does_not_trip_the_limit(self):
        # The period estimate is the p90 gap, not the median: a manager whose tick
        # once cost 20 min on a 15-min cadence is slow, not lapsed. A median-based
        # limit fires on it — measured 2026-10-20 at 5.02 events/manager-day against
        # the old rule's 1.35.
        self.plant_arms("Manager Layer", [4200, 2700, 1800, 900])  # one 25-min tick
        self.arm("Manager Layer", 900, age=1200)
        self.assertEqual(self.check(), (0, ""))

    def test_an_historic_outage_does_not_inflate_the_limit_forever(self):
        # One long stop stays in the retained history. Uncapped, its p90 lets that
        # subject fire at 13.4x its own median for ever; the cap keeps it bounded.
        self.plant_arms("Manager Layer", [90000, 2700, 1800, 900])
        self.arm("Manager Layer", 900, age=1000)
        state, detail = self.m.classify("goal--manager-layer", time.time())
        self.assertEqual(state, "OK")
        self.assertIn("period=2250s(observed)", detail)

    def test_rearm_after_a_stand_down_resets_the_history(self):
        # The outage gap must never count as cadence. It cannot be filtered at read
        # time — `arm` deletes the marker that records when the stop happened — so a
        # restart starts a fresh sample instead.
        self.plant_arms("Manager Layer", [11000, 10000, 9000, 8000])
        with redirect_stdout(io.StringIO()):
            self.m.stop("Manager Layer", "goal")
            self.m.arm("Manager Layer", "goal", 300)
        self.assertEqual(len(self.m.read_arms("goal--manager-layer")), 1)
        self.assertIsNone(self.m.observed_interval("goal--manager-layer"))

    def test_history_survives_ordinary_rearms(self):
        self.plant_arms("Manager Layer", [3000, 2000, 1000])
        self.arm("Manager Layer", 300, age=0)
        self.assertEqual(len(self.m.read_arms("goal--manager-layer")), 4)
        self.assertEqual(int(self.m.observed_interval("goal--manager-layer")), 1000)

    def test_unreadable_cadence_is_flagged(self):
        with open(self.m.path("broken", "cadence"), "w") as fh:
            fh.write("not-a-number\n")
        rc, out = self.check()
        self.assertEqual(rc, 10)
        self.assertTrue(out.startswith("INVALID broken"), out)


if __name__ == "__main__":
    unittest.main()


class StateVerb(unittest.TestCase):
    """The per-subject read the fleet sweep uses to tell stood-down from lapsed.

    `--check` cannot serve here, and the reason is structural rather than a
    preference: it prints only the topics that TURNED stale, so STOPPED -- the
    state a deliberate stand-down produces -- is absent from its output by
    construction. A reader that asked it, saw nothing and concluded the manager
    was fine has it exactly backwards. These tests pin the verb that can answer.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.m = load(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()
        os.environ.pop("MANAGER_LIVENESS_STATE_DIR", None)

    def state(self, *argv):
        """Run main() as the CLI would, capturing stdout/stderr and the exit code."""
        out, err, old = io.StringIO(), io.StringIO(), sys.argv
        sys.argv = ["manager-liveness.py", *argv]
        try:
            with redirect_stdout(out), redirect_stderr(err):
                rc = self.m.main()
        finally:
            sys.argv = old
        return rc, out.getvalue()

    def test_stopped_marker_reads_stopped(self):
        with redirect_stdout(io.StringIO()):
            self.m.arm("Manager Layer", "goal", 300)
            self.m.stop("Manager Layer", "goal")
        rc, out = self.state("--state", "--subject", "Manager Layer", "--branch", "goal")
        self.assertEqual(rc, 0)
        self.assertTrue(out.startswith("STOPPED\t"), out)

    def test_stale_cadence_reads_stale(self):
        with redirect_stdout(io.StringIO()):
            self.m.arm("Manager Layer", "goal", 300)
        p = self.m.path(self.m.slug("Manager Layer", "goal"), "cadence")
        t = time.time() - 100000
        os.utime(p, (t, t))
        rc, out = self.state("--state", "--subject", "Manager Layer", "--branch", "goal")
        self.assertEqual(rc, 0)
        self.assertTrue(out.startswith("STALE\t"), out)

    def test_fresh_cadence_reads_ok(self):
        with redirect_stdout(io.StringIO()):
            self.m.arm("Manager Layer", "goal", 300)
        rc, out = self.state("--state", "--subject", "Manager Layer", "--branch", "goal")
        self.assertEqual(rc, 0)
        self.assertTrue(out.startswith("OK\t"), out)

    def test_state_exits_zero_for_every_state(self):
        # The state IS the answer. A non-zero exit would make a stood-down
        # manager read as a failure to anything wrapping the call, which is the
        # opposite of what STOPPED means.
        for topic in ("Nothing Here",):
            rc, _ = self.state("--state", "--subject", topic, "--branch", "goal")
            self.assertEqual(rc, 0, topic)

    def test_state_requires_a_subject(self):
        with self.assertRaises(SystemExit):
            self.state("--state")

    def test_check_still_never_prints_stopped(self):
        # The property the new verb exists for, pinned so a later change to
        # --check cannot quietly make the two agree and hide the distinction.
        with redirect_stdout(io.StringIO()):
            self.m.arm("Manager Layer", "goal", 300)
            self.m.stop("Manager Layer", "goal")
        out = io.StringIO()
        with redirect_stdout(out):
            self.m.check()
        self.assertNotIn("STOPPED", out.getvalue())


class StoodDownVersusLapsed(unittest.TestCase):
    """SC4's fixture: one stood-down manager and one lapsed one, told apart.

    The predicate is mechanical (`--state`); the labels are the classification
    table's. The point of the fixture is that the two must NOT collapse into a
    single verdict -- a correct stand-down and a dead loop look identical on
    every signal the sweep had before this change, which is why a stood-down
    manager used to render as a fault and invite a hand nudge.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.m = load(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()
        os.environ.pop("MANAGER_LIVENESS_STATE_DIR", None)

    def state(self, topic):
        out, old = io.StringIO(), sys.argv
        sys.argv = ["manager-liveness.py", "--state", "--subject", topic, "--branch", "goal"]
        try:
            with redirect_stdout(out), redirect_stderr(io.StringIO()):
                self.m.main()
        finally:
            sys.argv = old
        return out.getvalue().split("\t")[0]

    def test_a_stand_down_and_a_lapse_are_told_apart(self):
        # Stood down: cadence frozen AND a marker saying so, deliberately.
        with redirect_stdout(io.StringIO()):
            self.m.arm("Stood Down Manager", "goal", 900)
            self.m.stop("Stood Down Manager", "goal")
        # Lapsed: the same frozen cadence, no marker -- nothing said it stopped.
        with redirect_stdout(io.StringIO()):
            self.m.arm("Lapsed Manager", "goal", 900)
        p = self.m.path(self.m.slug("Lapsed Manager", "goal"), "cadence")
        t = time.time() - 100000
        os.utime(p, (t, t))

        stood_down, lapsed = self.state("Stood Down Manager"), self.state("Lapsed Manager")
        self.assertEqual(stood_down, "STOPPED")
        self.assertEqual(lapsed, "STALE")
        self.assertNotEqual(stood_down, lapsed)

    def test_the_shipped_table_maps_each_state_to_its_own_label(self):
        # The labels are prose in the reader's classification table, so pin the
        # mapping there: both states present, both labels present, and each
        # label sitting after its own state so the pairing cannot be swapped.
        root = os.path.dirname(os.path.dirname(_HERE))
        table = io.open(
            os.path.join(root, "agents", "fleet-sweep-reader.md"), encoding="utf-8"
        ).read()
        row = [l for l in table.split("\n") if l.startswith("   | manager subject reads **`STOPPED`**")]
        self.assertEqual(len(row), 1, "the stood-down/lapsed row is missing from the table")
        row = row[0]
        for token in ("STOPPED", "STALE", "stood-down", "lapsed"):
            self.assertIn(token, row, token)
        self.assertLess(row.index("STOPPED"), row.index("stood-down"))
        self.assertLess(row.index("STALE"), row.index("lapsed"))


class DeprecatedTopicFlag(unittest.TestCase):
    """`--topic` was renamed to `--subject` in 0.187.0 with no compatibility path.

    ⚠️ Every armed manager loop's wakeup prompt carried `--topic`, so a reload without
    this alias makes each tick fail with `unrecognized arguments` and the liveness
    record silently stops updating.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.m = load(self.tmp.name)
        self.addCleanup(lambda: os.environ.pop("MANAGER_LIVENESS_STATE_DIR", None))

    def run_cli(self, *argv):
        out, old = io.StringIO(), sys.argv
        sys.argv = ["manager-liveness.py", *argv]
        try:
            with redirect_stdout(out), redirect_stderr(io.StringIO()):
                rc = self.m.main()
        finally:
            sys.argv = old
        return rc, out.getvalue()

    def test_arm_accepts_the_deprecated_topic_flag(self):
        rc, out = self.run_cli("--arm", "--topic", "Legacy Subject", "--interval", "60")
        self.assertEqual(0, rc)
        self.assertIn("armed goal--legacy-subject", out)

    def test_state_accepts_the_deprecated_topic_flag(self):
        self.run_cli("--arm", "--topic", "Legacy Subject", "--interval", "60")
        rc, out = self.run_cli("--state", "--topic", "Legacy Subject")
        self.assertEqual(0, rc)
        self.assertTrue(out.startswith("OK\tgoal--legacy-subject"), out)

    def test_an_existing_prefixed_record_decides_the_branch(self):
        # A topic manager that already armed under `topic--` keeps that branch even
        # though the default for an unresolvable subject is `goal`.
        open(self.m.path("topic--named-topic", "cadence"), "w").write("300\n")
        rc, out = self.run_cli("--arm", "--topic", "Named Topic", "--interval", "60")
        self.assertEqual(0, rc)
        self.assertIn("armed topic--named-topic", out)


class LegacyRecordMigration(unittest.TestCase):
    """The pre-split flat records must not survive to fire a false STALE.

    Measured 2026-10-20, after the release: **55** unprefixed `.cadence` records against
    **0** prefixed ones. Left alone, the first `--check` reports every live manager as
    lapsed.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.m = load(self.tmp.name)
        self.addCleanup(lambda: os.environ.pop("MANAGER_LIVENESS_STATE_DIR", None))

    def test_arming_retires_the_flat_record(self):
        flat = self.m.path("old-subject", "cadence")
        open(flat, "w").write("300\n")
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self.m.arm("Old Subject", "goal", 60)
        self.assertFalse(os.path.exists(flat), "the flat record must be migrated away")

    def test_a_leftover_flat_record_with_a_prefixed_twin_is_not_stale(self):
        # The read-side guard, for a flat record an older release already wrote and
        # nothing will re-arm.
        flat = self.m.path("leftover", "cadence")
        open(flat, "w").write("300\n")
        os.utime(flat, (time.time() - 100000, time.time() - 100000))
        open(self.m.path("goal--leftover", "cadence"), "w").write("300\n")
        out = io.StringIO()
        with redirect_stdout(out):
            rc = self.m.check()
        self.assertEqual(0, rc, out.getvalue())
        self.assertEqual("", out.getvalue())

    def test_a_flat_record_with_no_twin_still_reports_stale(self):
        # The control: the guard must not swallow a genuinely dead manager.
        flat = self.m.path("dead-subject", "cadence")
        open(flat, "w").write("300\n")
        os.utime(flat, (time.time() - 100000, time.time() - 100000))
        out = io.StringIO()
        with redirect_stdout(out):
            rc = self.m.check()
        self.assertEqual(self.m.EXIT_STALE, rc)
        self.assertIn("STALE dead-subject", out.getvalue())


class UnarmedAndLegacy(unittest.TestCase):
    """`UNARMED` vs `INVALID`, and the pre-split records a prefixed read used to miss.

    Measured 2026-10-20: every never-armed goal read `INVALID — unreadable cadence file`,
    so "no manager was ever armed here" was indistinguishable from "the record cannot be
    read". Three of the five census subjects were worse still — they HAD a record, under
    the pre-split bare name, including one that armed the same day.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.m = load(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()
        os.environ.pop("MANAGER_LIVENESS_STATE_DIR", None)

    def state(self, subject):
        s = self.m.slug(subject, "goal")
        return self.m.classify(s, time.time())

    def write(self, name, ext, text, age=0):
        p = self.m.path(name, ext)
        with open(p, "w", encoding="utf-8") as fh:
            fh.write(text)
        t = time.time() - age
        os.utime(p, (t, t))

    def test_no_record_under_either_name_is_unarmed(self):
        state, detail = self.state("Never Armed")
        self.assertEqual("UNARMED", state)
        self.assertIn("no cadence file", detail)

    def test_corrupt_record_is_invalid_not_unarmed(self):
        # The distinction this whole state exists for: a record EXISTS and cannot be
        # read, which is a probe failure, not a fact about the fleet.
        self.write("goal--corrupt", "cadence", "not-a-number\n")
        state, detail = self.state("Corrupt")
        self.assertEqual("INVALID", state)
        self.assertIn("unreadable cadence file", detail)

    def test_legacy_cadence_reads_stale_not_invalid(self):
        self.write("legacy-lapsed", "cadence", "300\n", age=100000)
        state, detail = self.state("Legacy Lapsed")
        self.assertEqual("STALE", state)
        self.assertIn("legacy-record", detail)

    def test_legacy_cadence_inside_its_limit_reads_ok(self):
        self.write("legacy-fresh", "cadence", "300\n", age=10)
        self.assertEqual("OK", self.state("Legacy Fresh")[0])

    def test_legacy_stop_marker_reads_stopped(self):
        # A deliberate stand-down must not be reported as a lapse or a probe failure.
        self.write("legacy-stood-down", "stopped", "2026-10-19T22:37:09+0200\n")
        state, detail = self.state("Legacy Stood Down")
        self.assertEqual("STOPPED", state)
        self.assertIn("legacy record", detail)

    def test_prefixed_record_wins_over_legacy(self):
        self.write("both-names", "cadence", "300\n", age=100000)
        self.write("goal--both-names", "cadence", "300\n", age=10)
        self.assertEqual("OK", self.state("Both Names")[0])

    def test_check_still_reports_a_legacy_record_stale(self):
        # The directory scan must not lose the record now that --state resolves it too.
        self.write("scanned-legacy", "cadence", "300\n", age=100000)
        out = io.StringIO()
        with redirect_stdout(out):
            rc = self.m.check()
        self.assertEqual(self.m.EXIT_STALE, rc)
        self.assertIn("STALE scanned-legacy", out.getvalue())
