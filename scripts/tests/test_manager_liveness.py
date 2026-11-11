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
from datetime import datetime, timedelta, timezone
from unittest import mock

_HERE = os.path.dirname(os.path.abspath(__file__))


def load(state_dir):
    os.environ["MANAGER_LIVENESS_STATE_DIR"] = state_dir
    # Isolate the gate heartbeat read from the real detached loop, so a live loop on the
    # host can never make a synthetic stale subject read OK. Not created here: the
    # unreadable-state-dir test relies on the path staying absent.
    os.environ["MANAGER_GATE_LOOP_STATE_DIR"] = os.path.join(state_dir, "gate-loop")
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

    def test_the_fleet_branch_is_its_own_tier(self):
        # The Fleet Manager manages no subject page, so it registers under its own branch
        # rather than borrowing `goal` or `topic` — and the two must never share a slug, or
        # one would overwrite the other's cadence in this flat directory.
        with redirect_stdout(io.StringIO()):
            self.m.arm("Fleet Manager", "fleet", 900)
        fleet = self.m.slug("Fleet Manager", "fleet")
        self.assertEqual(fleet, "fleet--fleet-manager")
        self.assertNotEqual(fleet, self.m.slug("Fleet Manager", "goal"))
        self.assertNotEqual(fleet, self.m.slug("Fleet Manager", "topic"))
        self.assertTrue(os.path.exists(self.m.path(fleet, "cadence")))
        # A fresh arm reads OK — the state `scripts/answer-path-check.py` requires to pass.
        self.assertEqual(self.m.classify(fleet, time.time())[0], "OK")

    def test_an_unknown_branch_is_still_refused(self):
        with self.assertRaises(ValueError):
            self.m.slug("Fleet Manager", "workers")

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

    def test_rearm_preserves_stop_marker(self):
        # ⚠️ THE DEFECT THIS FILE EXISTS TO PIN. An arm is a READING — every round takes
        # one — so an arm that cleared the marker made measuring a stand-down the act
        # that erased it. Measured 2026-11-07: stood down 12:13, read 12:51, marker gone,
        # the Fleet Manager read STALE and restarted the session at 13:14.
        with redirect_stdout(io.StringIO()):
            self.m.stop("Manager Layer", "goal")
        self.arm("Manager Layer", 300, age=5000)
        self.assertTrue(
            os.path.exists(self.m.path("goal--manager-layer", "stopped")),
            "an arm must never clear a deliberate stand-down",
        )
        self.assertEqual(self.m.classify("goal--manager-layer", time.time())[0], "STOPPED")
        # And the state verb agrees: the reading still says the subject is stood down.
        self.assertEqual(self.check()[0], 0)

    def test_resume_clears_the_stop_marker(self):
        with redirect_stdout(io.StringIO()):
            self.m.stop("Manager Layer", "goal")
            self.m.arm("Manager Layer", "goal", 300, resume=True)
        self.assertFalse(os.path.exists(self.m.path("goal--manager-layer", "stopped")))
        self.assertEqual(self.m.classify("goal--manager-layer", time.time())[0], "OK")

    def test_resume_is_the_only_thing_that_clears_the_marker(self):
        # Two plain arms and then a resume: the marker survives the first two, so a
        # stood-down manager cannot be un-stood-down by taking readings.
        with redirect_stdout(io.StringIO()):
            self.m.stop("Manager Layer", "goal")
            self.m.arm("Manager Layer", "goal", 300)
            self.m.arm("Manager Layer", "goal", 300)
        self.assertTrue(os.path.exists(self.m.path("goal--manager-layer", "stopped")))
        with redirect_stdout(io.StringIO()):
            self.m.arm("Manager Layer", "goal", 300, resume=True)
        self.assertFalse(os.path.exists(self.m.path("goal--manager-layer", "stopped")))

    def deliver(self, s):
        """Ack a lapse the way the sender does after the transport returns success."""
        with redirect_stdout(io.StringIO()):
            return self.m.delivered(s)

    def fail_send(self, s, error):
        """Record a failed delivery attempt the way the sender does."""
        with redirect_stdout(io.StringIO()):
            return self.m.failed(s, error)

    def test_one_lapse_notifies_once_then_recovers(self):
        self.arm("Manager Layer", 300, age=5000)
        self.assertEqual(self.check()[0], 10)  # detected: now OWED a report
        self.assertEqual(self.check()[0], 10, "an owed lapse is re-emitted (the retry)")
        self.deliver("goal--manager-layer")  # the transport carried it
        self.assertEqual(self.check(), (0, ""))  # second poll after the ack: no repeat
        self.arm("Manager Layer", 300, age=0)
        rc, out = self.check()
        self.assertEqual(rc, 0)
        self.assertTrue(out.startswith("RECOVERED goal--manager-layer OK"), out)
        self.arm("Manager Layer", 300, age=5000)  # a NEW lapse notifies again
        self.assertEqual(self.check()[0], 10)

    def test_a_failed_send_leaves_the_lapse_owed_and_re_emitted(self):
        # The defect this file's `--check` fixes: 2026-11-07 03:26Z, the detector fired,
        # the transport timed out, and the next poll was silent for ever because the
        # entry had been written at DETECTION time. A failed send must not consume it.
        self.arm("Manager Layer", 300, age=5000)
        self.assertEqual(self.check()[0], 10)
        self.fail_send("goal--manager-layer", "prod unreachable — timed out")
        rc, out = self.check()
        self.assertEqual(rc, 10, "a failed delivery must be retried on the next poll")
        self.assertIn("STALE goal--manager-layer", out)
        entry = self.m.read_pending()["goal--manager-layer"]
        self.assertEqual(entry["attempts"], 1)
        self.assertIn("prod unreachable", entry["last_error"])

    def test_the_ack_is_the_only_thing_that_stops_the_re_emission(self):
        # `notified` is what the sender writes; `pending` is what the detector owes.
        # Detection alone must not write `notified`, or the lapse is reported to nobody.
        self.arm("Manager Layer", 300, age=5000)
        self.assertEqual(self.check()[0], 10)
        self.assertNotIn("goal--manager-layer", self.m.read_notified())
        self.assertIn("goal--manager-layer", self.m.read_pending())
        self.deliver("goal--manager-layer")
        self.assertIn("goal--manager-layer", self.m.read_notified())
        self.assertNotIn("goal--manager-layer", self.m.read_pending())

    def test_the_first_emission_is_a_firing_and_later_ones_are_marked_retries(self):
        # One crossing, many polls. The emitted STREAM must say which line is which, or
        # a consumer counting rows reports the failure rate once per poll — measured
        # 2026-11-08: 165 firings for ONE `fleet--fleet` crossing.
        self.arm("Manager Layer", 300, age=5000)
        rc, first = self.check()
        self.assertEqual(rc, 10)
        self.assertIn("STALE goal--manager-layer", first)
        self.assertNotIn("retry=1", first, "the first emission IS the firing")
        rc, second = self.check()
        self.assertEqual(rc, 10, "the retry must still exit 10 or delivery stops")
        self.assertIn("STALE goal--manager-layer", second)
        self.assertIn("retry=1", second, "the second emission is a retry of the first")
        # Same crossing both times: one episode, two emitted lines.
        self.assertEqual(
            first.split("crossing=")[1].split()[0],
            second.split("crossing=")[1].split()[0],
        )

    def test_the_sender_still_parses_a_retry_line(self):
        # The only consumer lives outside this repo (`manager-liveness-notify-tick.sh`,
        # tracked in bborbe/claude) and parses positionally: `grep -E '^(STALE|INVALID|
        # RECOVERED-PENDING) '` then `awk '{print $2}'`. A prefix marker such as
        # `RETRY STALE …` would satisfy the retry test above and silently drop every
        # retry from delivery, so the shape is asserted, not assumed.
        self.arm("Manager Layer", 300, age=5000)
        self.check()
        _, retry_line = self.check()
        fields = retry_line.split()
        self.assertIn(fields[0], ("STALE", "INVALID", "RECOVERED-PENDING"))
        self.assertEqual(fields[1], "goal--manager-layer")

    def test_a_second_crossing_after_the_ack_is_a_new_firing(self):
        # Deduping an episode must not suppress a LATER one: the new crossing opens its
        # own `emits` counter and prints without the retry marker. The manager has to
        # come back and lapse again — backdating the cadence after the ack leaves
        # `notified` newer than `last_arm` and no new lapse would ever open.
        self.arm("Manager Layer", 300, age=5000)
        self.check()  # first crossing
        self.check()  # a retry of it
        self.deliver("goal--manager-layer")
        self.arm("Manager Layer", 300, age=0)  # the manager came back
        self.check()  # RECOVERED, which clears `notified`
        self.arm("Manager Layer", 300, age=5000)  # and then lapsed again
        rc, out = self.check()
        self.assertEqual(rc, 10)
        self.assertIn("STALE goal--manager-layer", out)
        self.assertNotIn("retry=1", out, "a new crossing is a new firing, not a retry")

    def test_a_failed_attempt_records_its_producer(self):
        # The durable record must be showable as written by a real failed send: a
        # hand-written file reproduces every other field byte for byte.
        self.arm("Manager Layer", 300, age=5000)
        self.assertEqual(self.check()[0], 10)
        with redirect_stdout(io.StringIO()):
            self.m.failed("goal--manager-layer", "prod unreachable", pid="4242", row_ts="2026-11-07T03:26:09Z")
        entry = self.m.read_pending()["goal--manager-layer"]
        self.assertEqual(entry["pid"], "4242")
        self.assertEqual(entry["row_ts"], "2026-11-07T03:26:09Z")
        # ... and a later poll's rewrite must not drop them.
        self.assertEqual(self.check()[0], 10)
        entry = self.m.read_pending()["goal--manager-layer"]
        self.assertEqual(entry["pid"], "4242")
        self.assertEqual(entry["row_ts"], "2026-11-07T03:26:09Z")

    def test_the_ack_records_the_crossing_not_the_ack_time(self):        # The report says when the lapse CROSSED, and that is what dedup compares
        # against `last_arm`. Storing the ack time would make a manager that re-armed
        # while the report was owed look already-reported on its next lapse.
        t0 = time.time()
        self.arm("Manager Layer", 300, age=5000)
        self.assertEqual(self.check(t0)[0], 10)
        crossing = self.m.read_pending()["goal--manager-layer"]["detected_at"]
        self.assertEqual(crossing, t0)
        self.deliver("goal--manager-layer")
        self.assertEqual(self.m.read_notified()["goal--manager-layer"], t0)

    def test_a_recovered_pending_lapse_is_still_reported(self):
        # The manager came back before anyone was told. The lapse still happened, so it
        # is still owed — but printing STALE would be a lie about the current state.
        self.arm("Manager Layer", 300, age=5000)
        self.assertEqual(self.check()[0], 10)
        self.arm("Manager Layer", 300, age=0)  # recovered, still undelivered
        rc, out = self.check()
        self.assertEqual(rc, 10, "a recovery does not cancel an unreported lapse")
        self.assertTrue(out.startswith("RECOVERED-PENDING goal--manager-layer"), out)
        self.deliver("goal--manager-layer")
        rc, out = self.check()
        self.assertEqual(rc, 0, out)
        self.assertNotIn("STALE", out)

    def test_a_re_arm_supersedes_a_stale_pending_entry(self):
        # A pending entry older than the subject's last arm describes a lapse it has
        # already come back from; the newer crossing must replace it, not stack behind.
        t0 = time.time()
        self.arm("Manager Layer", 300, age=5000)
        self.assertEqual(self.check(t0)[0], 10)
        self.set_arm_time("Manager Layer", t0 + 100)
        self.assertEqual(self.check(t0 + 100 + 2000)[0], 10)
        self.assertEqual(self.m.read_pending()["goal--manager-layer"]["detected_at"], t0 + 2100)

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
        self.assertEqual(self.check(t0)[0], 10)  # both owed
        self.deliver("goal--alpha")
        self.deliver("goal--beta")  # both delivered
        self.set_arm_time("Alpha", t0 + 100)  # Alpha recovers
        rc, out = self.check(t0 + 200)
        self.assertEqual(rc, 0, out)  # Beta is still reported, so nothing new
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
        # time — the stop is not a gap the reader can see — so `--stop` leaves a
        # `.reset` epoch and the first arm consumes it to start a fresh sample. The
        # MARKER is a separate fact and survives, which is why the reset needed its
        # own file rather than being inferred from it.
        self.plant_arms("Manager Layer", [11000, 10000, 9000, 8000])
        with redirect_stdout(io.StringIO()):
            self.m.stop("Manager Layer", "goal")
            self.m.arm("Manager Layer", "goal", 300)
        self.assertEqual(len(self.m.read_arms("goal--manager-layer")), 1)
        self.assertIsNone(self.m.observed_interval("goal--manager-layer"))
        # The stand-down itself is still on disk: resetting the sample must not resume.
        self.assertTrue(os.path.exists(self.m.path("goal--manager-layer", "stopped")))

    def test_the_reset_is_consumed_so_the_next_arm_keeps_its_history(self):
        # One-shot, not sticky. A `.reset` that survived would make every arm drop the
        # history, `observed_gaps` would never return, and every subject would sit on
        # its reported interval for ever — the frozen-period failure the reset exists to
        # avoid, reached from the other side.
        self.plant_arms("Manager Layer", [11000, 10000, 9000, 8000])
        with redirect_stdout(io.StringIO()):
            self.m.stop("Manager Layer", "goal")
            self.m.arm("Manager Layer", "goal", 300)
        self.assertFalse(os.path.exists(self.m.path("goal--manager-layer", "reset")))
        self.arm("Manager Layer", 300, age=0)
        self.assertEqual(len(self.m.read_arms("goal--manager-layer")), 2)

    def plant_beats(self, topic, interval, round_seconds, rounds):
        """A ring of head+tail beats: `rounds` rounds of `round_seconds` at `interval`."""
        s = self.m.slug(topic, "goal")
        now = time.time()
        start = now - (rounds - 1) * interval
        lines = []
        for r in range(rounds):
            head = start + r * interval
            lines.append(f"{head:.0f} {interval}\n")
            lines.append(f"{head + round_seconds:.0f} {interval} tail\n")
        with open(self.m.path(s, "arms"), "w") as fh:
            fh.write("".join(lines))

    def test_observed_period_ignores_intra_round_tail_beats(self):
        # A two-beat ring must measure the SAME cadence as the head-only ring it
        # replaces. The tail beat exists so a long round does not reach its own end
        # already reading STALE; if it also redefines the period, it manufactures the
        # very STALE it was added to remove.
        #
        # Against the beat-to-beat population the gaps alternate [D, I-D]: six of 120
        # and five of 780 on a 900 s interval, so median=120, the 2.5x cap=300, p90=780
        # and `observed_period()` returns min(780, 300) = 300 — while the round-to-round
        # gap it has to cover is 900. limit=480 < 900: every quiet round reads STALE.
        self.plant_beats("Manager Layer", interval=900, round_seconds=120, rounds=6)
        self.assertEqual(int(self.m.observed_period("goal--manager-layer")), 900)

    def test_observed_period_is_unchanged_without_tail_beats(self):
        # The fix must not move a ring that has no tail beats: every `.arms` file on
        # every machine today is head-only, and their periods must be untouched.
        self.plant_arms("Manager Layer", [2700, 1800, 900, 0])
        self.assertEqual(int(self.m.observed_period("goal--manager-layer")), 900)

    def test_a_legacy_two_column_line_reads_as_a_head_beat(self):
        # The original single-column format carries no marker at all. It must keep
        # counting as a head, or every pre-existing ring would contribute no gaps and
        # every subject would fall back to its declared interval for ever.
        s = self.m.slug("Manager Layer", "goal")
        now = time.time()
        with open(self.m.path(s, "arms"), "w") as fh:
            fh.write("".join(f"{now - o:.0f}\n" for o in (2700, 1800, 900, 0)))
        self.assertEqual(int(self.m.observed_period("goal--manager-layer")), 900)

    def test_observed_period_returns_after_four_further_arms(self):
        # The criterion's own probe: stop, arm, then four more arms — `observed_gaps`
        # must come back, or the adaptive stale limit never returns and every subject is
        # measured against a value its manager declared, not the one it runs at.
        self.plant_arms("Manager Layer", [11000, 10000, 9000, 8000])
        with redirect_stdout(io.StringIO()):
            self.m.stop("Manager Layer", "goal")
            self.m.arm("Manager Layer", "goal", 300)
        self.assertIsNone(self.m.observed_gaps("goal--manager-layer"))
        # A controllable clock: four real arms in one test would all land in the same
        # second, so the gaps would be zero and the assertion would pass without ever
        # exercising the period arithmetic.
        clock = [1_700_000_000.0]
        real = self.m.time.time
        self.m.time.time = lambda: clock[0]
        try:
            for _ in range(4):
                clock[0] += 900
                with redirect_stdout(io.StringIO()):
                    self.m.arm("Manager Layer", "goal", 300)
        finally:
            self.m.time.time = real
        self.assertEqual(len(self.m.read_arms("goal--manager-layer")), 5)
        self.assertIsNotNone(self.m.observed_gaps("goal--manager-layer"))
        self.assertEqual(int(self.m.observed_period("goal--manager-layer")), 900)

    def test_history_survives_ordinary_rearms(self):
        self.plant_arms("Manager Layer", [3000, 2000, 1000])
        self.arm("Manager Layer", 300, age=0)
        self.assertEqual(len(self.m.read_arms("goal--manager-layer")), 4)
        self.assertEqual(int(self.m.observed_interval("goal--manager-layer")), 1000)

    # --- the conditional floor: a LENGTHENED interval is not a lapse -------------
    #
    # A manager that lengthens its `ScheduleWakeup` interval keeps the old short gaps
    # in its 12-arm window, so a limit built from them sits BELOW the interval it just
    # declared and every healthy tick reads STALE. The floor is conditional on a
    # RECORDED shorter-interval gap still being in the window, and a completed gap at
    # the new interval shorter than half of it switches the floor off — otherwise a
    # subject that declares one interval and ticks at another (measured 2026-11-08 on
    # `goal--deploy-and-merge-gates-become-standing-allow-rules`, declared 3600, gaps
    # ~900) would have its lapse detection loosened, the opposite of what this file
    # exists for.

    # Real `.arms` epochs, read 2026-11-08 from the machine's sweep-gate state dir.
    WEEKLY = [1794093605, 1794094567, 1794095525, 1794096495, 1794097745,
              1794128286, 1794129254, 1794130333, 1794130564, 1794134105,
              1794137848, 1794141605]
    CLAIM = [1793272319, 1793281339, 1793372864, 1793373995, 1793375081,
             1793376171, 1793377295, 1793378428, 1793379651, 1793442283,
             1793445014, 1793448709]
    DEPLOY = [1794121268, 1794122169, 1794123068, 1794124030, 1794124931,
              1794125769, 1794126669, 1794127695, 1794146792, 1794147727,
              1794149287, 1794150041]

    def arms(self, epochs, intervals, topic="Manager Layer"):
        """Write `<slug>.arms`; an interval of None writes a LEGACY single-column line."""
        s = self.m.slug(topic, "goal")
        body = "".join(
            f"{e}\n" if iv is None else f"{e} {iv}\n"
            for e, iv in zip(epochs, intervals)
        )
        with open(self.m.path(s, "arms"), "w") as fh:
            fh.write(body)

    def limit_at(self, age, interval, topic="Manager Layer"):
        """Write `<interval>` to `.cadence`, age it by `age`, and read the detail."""
        with open(self.m.path(self.m.slug(topic, "goal"), "cadence"), "w") as fh:
            fh.write(f"{interval}\n")
        self.set_arm_time(topic, time.time() - age)
        return self.m.classify(self.m.slug(topic, "goal"), time.time())

    def test_a_lengthened_interval_floors_the_limit_from_the_first_tick(self):
        # SC1(a). 11 arms at 900, then the 12th declares 3600 and ends a 960 s gap
        # that was armed under 900 — so the window still holds the old cadence.
        epochs = [1794000000 + 960 * i for i in range(12)]
        self.arms(epochs, [900] * 11 + [3600])
        state, detail = self.limit_at(3650, 3600)
        self.assertEqual(state, "OK", detail)
        self.assertIn("limit=3780s", detail)  # uncapped code says 1140

    def test_the_floor_holds_on_the_second_tick_after_a_lengthening(self):
        # One completed gap at the new interval (3650 s) does not disprove it: 3650
        # is not shorter than 3600/2, so the floor stays until the 900 s gaps age out.
        epochs = [1794000000 + 960 * i for i in range(12)] + [1794000000 + 960 * 11 + 3650]
        self.arms(epochs, [900] * 11 + [3600] * 2)
        state, detail = self.limit_at(3650, 3600)
        self.assertEqual(state, "OK", detail)
        self.assertIn("limit=3780s", detail)

    def test_the_real_weekly_subject_reads_ok_on_its_new_cadence(self):
        # SC1(b). The live history behind the row, written EXACTLY as it is on disk —
        # a legacy single-column ring. Annotating the intervals here would test a file
        # the format never produced, and the floor would look like it worked on the
        # motivating subject when the real one still read STALE.
        self.arms(self.WEEKLY, [None] * 12)
        state, detail = self.limit_at(3757, 3600)
        self.assertEqual(state, "OK", detail)
        self.assertIn("limit=3937s", detail)  # uncapped code says 2877

    def test_a_legacy_window_is_floored_by_its_gap_quantile(self):
        # The same history, one arm later: the ring is now 11 legacy lines plus one
        # recorded 3600, so the RECORDED rise test still sees nothing shorter. The
        # gap-quantile limb is what keeps the floor on — p75 3642 s against 3600.
        self.arms(self.WEEKLY, [None] * 11 + [3600])
        state, detail = self.limit_at(3757, 3600)
        self.assertEqual(state, "OK", detail)
        self.assertIn("limit=3937s", detail)

    def test_arm_keeps_each_lines_own_interval(self):
        # SC1(c). The rise can only fire if `arm` PRESERVES earlier lines' intervals.
        # Rebuilding the ring from epoch-only `read_arms()` would leave only the newest
        # line carrying one, and the floor would never apply to a real subject.
        clock = [1794000000.0]
        with mock.patch.object(self.m.time, "time", lambda: clock[0]):
            for _ in range(11):
                with redirect_stdout(io.StringIO()):
                    self.m.arm("Manager Layer", "goal", 900)
                clock[0] += 960
            with redirect_stdout(io.StringIO()):
                self.m.arm("Manager Layer", "goal", 3600)
        s = self.m.slug("Manager Layer", "goal")
        with open(self.m.path(s, "arms")) as fh:
            lines = [ln.split() for ln in fh.read().splitlines() if ln.strip()]
        self.assertEqual(len(lines), 12)
        self.assertEqual([ln[1] for ln in lines], ["900"] * 11 + ["3600"])
        # ... and the floored limit is what a live subject would then read.
        state, detail = self.limit_at(3650, 3600)
        self.assertEqual(state, "OK", detail)
        self.assertIn("limit=3780s", detail)

    def test_a_legacy_line_stays_single_column_when_the_ring_is_rewritten(self):
        # SC2(c)/SC3. Existing files on this machine are single-column; a rewrite must
        # not invent an interval for them, and `read_arms` must keep returning epochs.
        self.arms([1794000000 + 960 * i for i in range(11)], [None] * 11)
        with redirect_stdout(io.StringIO()):
            self.m.arm("Manager Layer", "goal", 3600)
        s = self.m.slug("Manager Layer", "goal")
        with open(self.m.path(s, "arms")) as fh:
            lines = [ln.split() for ln in fh.read().splitlines() if ln.strip()]
        self.assertEqual([len(ln) for ln in lines], [1] * 11 + [2])
        self.assertEqual(len(self.m.read_arms(s)), 12)

    def test_a_historic_stop_is_still_bounded(self):
        # SC2(a). A constant 900 s interval, one 91 525 s stop in the window: the floor
        # must not apply (no recorded shorter interval), so the cap still bounds it.
        self.arms(self.CLAIM, [900] * 12)
        state, detail = self.limit_at(3300, 900)  # just past the 3237 s limit
        self.assertEqual(state, "STALE", detail)
        self.assertIn("limit=3237s", detail)

    def test_a_declared_interval_that_never_ticks_at_it_is_not_floored(self):
        # SC2(b). deploy-and-merge declares 3600 and ticks at ~900. Neither rise limb
        # fires — no recorded interval below 3600, and its p75 is 994 s against the
        # 1800 s threshold — so the limit stays where it is today.
        for label, intervals in (("recorded", [3600] * 12), ("legacy", [None] * 12)):
            with self.subTest(window=label):
                self.arms(self.DEPLOY, intervals)
                state, detail = self.limit_at(1800, 3600)
                self.assertEqual(state, "STALE", detail)
                self.assertIn("limit=1740s", detail)

    def test_the_first_arm_after_deploy_does_not_loosen_a_mixed_file(self):
        # SC2(d). Every live file becomes a mixed legacy+new file at its first post-
        # deploy arm. A legacy line carries no interval, so it is neither shorter nor
        # longer than the declared one — an implementation that treats "unknown" as
        # "shorter" would floor deploy-and-merge to 3780 and hide its lapse.
        self.arms(self.DEPLOY + [self.DEPLOY[-1] + 900], [None] * 12 + [3600])
        state, detail = self.limit_at(1800, 3600)
        self.assertEqual(state, "STALE", detail)
        self.assertIn("limit=1740s", detail)

    def test_a_rise_then_a_shorter_actual_cadence_is_not_floored(self):
        # SC2(e). A real rise, but the manager keeps ticking at 900. The first completed
        # 900 s gap armed under 3600 disproves the new interval, so the floor holds for
        # exactly one tick rather than for eleven.
        epochs = [1794000000 + 900 * i for i in range(12)]
        self.arms(epochs, [900] * 6 + [3600] * 6)
        state, detail = self.limit_at(1800, 3600)
        self.assertEqual(state, "STALE", detail)
        self.assertIn("limit=1080s", detail)

    def test_an_interval_decrease_is_not_a_rise(self):
        # SC2(f). 3600 -> 900. No recorded interval is BELOW the declared one, so a
        # rule written with `!=` instead of `<` would floor here and loosen the limit.
        epochs = [1794000000 + 3600 * i for i in range(6)]
        epochs += [epochs[-1] + 960 * (i + 1) for i in range(6)]
        self.arms(epochs, [3600] * 6 + [900] * 6)
        _, detail = self.limit_at(1800, 900)
        self.assertIn("limit=2580s", detail)

    def test_the_floor_survives_a_gap_above_half_the_new_interval(self):
        # SC2(g). The accepted residue: gaps between I/2 and I do not disprove the new
        # interval, so the floor holds until the short gaps age out. Pinned as
        # deliberate rather than accidental.
        epochs = [1794000000 + 900 * i for i in range(6)]
        epochs += [epochs[-1] + 1900 * (i + 1) for i in range(6)]
        self.arms(epochs, [900] * 6 + [3600] * 6)
        _, detail = self.limit_at(1800, 3600)
        self.assertIn("limit=3780s", detail)

    def test_the_floor_lifts_a_limit_that_sits_just_below_the_declared_interval(self):
        # The only subject the fleet census moved (2026-11-09): it declares 3600 and
        # ticks at ~3540, so today's limit of 3768 s sits 12 s BELOW its own declared
        # interval — the same defect in miniature. The floor lifts it to exactly 3780.
        # Pinned because a rise this small is easy to mistake for a regression.
        epochs = [1792747773, 1792750092, 1792751053, 1792752013, 1792752565,
                  1792756151, 1792759690, 1792763232, 1792766768]
        self.arms(epochs + [1792766768 + 3600], [None] * 10)
        _, detail = self.limit_at(1000, 3600)
        self.assertIn("limit=3780s", detail)

    def test_read_arms_returns_epochs_from_a_mixed_file(self):
        # SC3. The epoch reader is what `observed_gaps` and `manager-receipt.py` use;
        # an interval column must never be read as an epoch.
        self.arms([1794000000, 1794000960, 1794001920], [900, None, 3600])
        self.assertEqual(self.m.read_arms("goal--manager-layer"),
                         [1794000000.0, 1794000960.0, 1794001920.0])

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
        # opposite of what STOPPED means. A never-armed subject is a state only once
        # its page resolves — without one it is the unresolvable-name refusal
        # (`StateResolution`), which is not a state.
        with tempfile.TemporaryDirectory() as vault:
            os.makedirs(os.path.join(vault, "24 Goals"))
            open(os.path.join(vault, "24 Goals", "Nothing Here.md"), "w").close()
            rc, out = self.state(
                "--state", "--subject", "Nothing Here", "--branch", "goal", "--vault", vault
            )
        self.assertEqual(rc, 0)
        self.assertTrue(out.startswith("UNARMED\t"), out)

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


class SleepState(unittest.TestCase):
    """SLEEPING — a date-gated subject that is deliberately not re-arming.

    A subject whose remaining work is all future `defer_date`s does not arm until that
    instant, so its cadence file ages past the stale limit BY DESIGN. Without this state
    the fleet reads that as a lapse and restarts it — the defect measured 2026-11-07.
    """

    SUBJECT = "Several Clusters"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.m = load(self.tmp.name)
        self.s = self.m.slug(self.SUBJECT, "goal")

    def tearDown(self):
        self.tmp.cleanup()
        os.environ.pop("MANAGER_LIVENESS_STATE_DIR", None)

    def arm(self, age):
        with redirect_stdout(io.StringIO()):
            self.m.arm(self.SUBJECT, "goal", 300)
        p = self.m.path(self.s, "cadence")
        t = time.time() - age
        os.utime(p, (t, t))

    def sleep(self, delta_seconds):
        until = (datetime.now(timezone.utc) + timedelta(seconds=delta_seconds)).isoformat()
        with redirect_stdout(io.StringIO()):
            rc = self.m.sleep(self.SUBJECT, "goal", until)
        return rc

    def test_a_future_sleep_reads_sleeping_not_stale(self):
        self.arm(age=5000)
        self.sleep(3600)
        state, detail = self.m.classify(self.s, time.time())
        self.assertEqual(state, "SLEEPING")
        self.assertIn("remaining", detail)

    def test_a_sleeping_subject_does_not_notify_as_a_lapse(self):
        self.arm(age=5000)
        self.sleep(3600)
        out = io.StringIO()
        with redirect_stdout(out):
            rc = self.m.check()
        self.assertEqual((rc, out.getvalue()), (0, ""))

    def test_a_passed_sleep_still_reads_stale(self):
        """The negative control: a manager that failed to wake must not be hidden."""
        self.arm(age=5000)
        self.sleep(-60)
        self.assertEqual(self.m.classify(self.s, time.time())[0], "STALE")

    def test_an_arm_clears_the_sleep_marker(self):
        self.sleep(3600)
        self.arm(age=0)
        self.assertFalse(os.path.exists(self.m.path(self.s, "sleeping")))

    def test_stopped_beats_sleeping(self):
        """A deliberate stand-down is the stronger statement; it must not be masked."""
        self.sleep(3600)
        with redirect_stdout(io.StringIO()):
            self.m.stop(self.SUBJECT, "goal")
        self.assertEqual(self.m.classify(self.s, time.time())[0], "STOPPED")

    def test_an_unreadable_sleep_marker_reads_as_absent(self):
        """Fail toward ARMED: a corrupt marker must not suppress a real STALE."""
        self.arm(age=5000)
        with open(self.m.path(self.s, "sleeping"), "w", encoding="utf-8") as fh:
            fh.write("someday\n")
        self.assertEqual(self.m.classify(self.s, time.time())[0], "STALE")

    def test_sleep_refuses_a_bad_until_and_writes_nothing(self):
        err = io.StringIO()
        with redirect_stderr(err), redirect_stdout(io.StringIO()):
            rc = self.m.sleep(self.SUBJECT, "goal", "tomorrow-ish")
        self.assertEqual(rc, 2)
        self.assertFalse(os.path.exists(self.m.path(self.s, "sleeping")))
        self.assertIn("ISO 8601", err.getvalue())


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

    def test_a_legacy_stand_down_survives_an_arm(self):
        # ⚠️ The same defect one surface out. `migrate_legacy` used to DELETE the flat
        # `.stopped`, so the first arm after the upgrade erased a pre-split stand-down —
        # an arm that must never resume anything, doing exactly that through migration.
        # It is renamed onto the prefixed path instead: the marker lives, the flat file
        # is retired, and `--arm --resume` can still clear it.
        open(self.m.path("old-subject", "stopped"), "w").write("2026-10-19T22:37:09+0200\n")
        open(self.m.path("old-subject", "cadence"), "w").write("300\n")
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self.m.arm("Old Subject", "goal", 60)
        self.assertFalse(os.path.exists(self.m.path("old-subject", "stopped")))
        self.assertTrue(os.path.exists(self.m.path("goal--old-subject", "stopped")))
        self.assertEqual(
            self.m.classify("goal--old-subject", time.time())[0], "STOPPED"
        )

    def test_resume_clears_a_migrated_legacy_stand_down(self):
        open(self.m.path("old-subject", "stopped"), "w").write("2026-10-19T22:37:09+0200\n")
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self.m.arm("Old Subject", "goal", 60, resume=True)
        self.assertFalse(os.path.exists(self.m.path("goal--old-subject", "stopped")))
        self.assertEqual(self.m.classify("goal--old-subject", time.time())[0], "OK")

    def test_the_pre_split_probe_still_migrates_and_reads_stopped(self):
        # The criterion's own probe, end to end through the CLI: plant the pre-split
        # shape (cadence, arms, reset, stopped), arm, then read the subject back.
        for ext, body in (
            ("cadence", "300\n"),
            ("arms", "1700000000\n1700000900\n1700001800\n1700002700\n"),
            ("reset", "1700002800\n"),
            ("stopped", "2026-11-07T12:13:00+0100\n"),
        ):
            with open(self.m.path("probe-subject", ext), "w") as fh:
                fh.write(body)
        out, err, old = io.StringIO(), io.StringIO(), sys.argv
        sys.argv = ["manager-liveness.py", "--arm", "--subject", "Probe Subject",
                    "--branch", "goal", "--interval", "900"]
        try:
            with redirect_stdout(out), redirect_stderr(err):
                rc = self.m.main()
        finally:
            sys.argv = old
        self.assertEqual(0, rc)
        self.assertIn(
            "migrated legacy probe-subject (cadence, arms, reset)", err.getvalue()
        )
        for ext in ("cadence", "arms", "reset", "stopped"):
            self.assertFalse(
                os.path.exists(self.m.path("probe-subject", ext)),
                f"the unprefixed {ext} must be retired",
            )
        stated = io.StringIO()
        with redirect_stdout(stated):
            self.m.state("Probe Subject", "goal", self.tmp.name, time.time())
        self.assertTrue(stated.getvalue().startswith("STOPPED\t"), stated.getvalue())
        checked = io.StringIO()
        with redirect_stdout(checked):
            self.m.check()
        self.assertNotIn("STALE goal--probe-subject", checked.getvalue())

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


class StateResolution(unittest.TestCase):
    """`--state` tells every answer apart by its first token — and refuses an unknown name.

    Measured 2026-11-07: a cadence table named five managers `UNARMED`. Four were `OK` under
    their full names and one was `STOPPED` on the goal branch. Two causes read identically
    as `UNARMED`: an abbreviated label (its slug finds no record) and a probe on the wrong
    branch. Acting on `UNARMED` arms a loop, so both now answer something else. Each test
    runs in its own `MANAGER_LIVENESS_STATE_DIR` and asserts the first token only.
    """

    FULL = "Several Clusters and the Mac Write One Vault"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.vault = tempfile.TemporaryDirectory()
        self.addCleanup(self.vault.cleanup)
        self.m = load(self.tmp.name)
        self.addCleanup(lambda: os.environ.pop("MANAGER_LIVENESS_STATE_DIR", None))
        for folder, name in (("24 Goals", self.FULL), ("23 Topics", "A Topic")):
            os.makedirs(os.path.join(self.vault.name, folder), exist_ok=True)
            open(os.path.join(self.vault.name, folder, name + ".md"), "w").close()

    def run_cli(self, *argv):
        out, err, old = io.StringIO(), io.StringIO(), sys.argv
        sys.argv = ["manager-liveness.py", *argv, "--vault", self.vault.name]
        try:
            with redirect_stdout(out), redirect_stderr(err):
                rc = self.m.main()
        finally:
            sys.argv = old
        return rc, out.getvalue(), err.getvalue()

    def first_token(self, *argv):
        rc, out, _ = self.run_cli("--state", *argv)
        self.assertEqual(0, rc, out)
        return out.split("\t", 1)[0]

    def quiet(self, fn, *args):
        with redirect_stdout(io.StringIO()):
            fn(*args)

    def age(self, subject, branch, seconds):
        p = self.m.path(self.m.slug(subject, branch), "cadence")
        t = time.time() - seconds
        os.utime(p, (t, t))

    # --- one fixture per state -------------------------------------------------

    def test_page_resolves_and_no_record_anywhere_is_unarmed(self):
        self.assertEqual("UNARMED", self.first_token("--subject", self.FULL, "--branch", "goal"))

    def test_a_topic_page_also_resolves_the_subject(self):
        self.assertEqual("UNARMED", self.first_token("--subject", "a topic", "--branch", "topic"))

    def test_record_on_goal_probed_on_topic_is_wrong_branch(self):
        self.quiet(self.m.arm, self.FULL, "goal", 900)
        self.quiet(self.m.stop, self.FULL, "goal")
        rc, out, _ = self.run_cli("--state", "--subject", self.FULL, "--branch", "topic")
        self.assertEqual(0, rc)
        self.assertTrue(out.startswith("WRONG-BRANCH\t"), out)
        self.assertIn("record is on branch goal", out)
        # Control on the same record: the right branch still reads the real state.
        self.assertEqual("STOPPED", self.first_token("--subject", self.FULL, "--branch", "goal"))

    def test_wrong_branch_wins_even_without_a_page(self):
        # The record proves the subject exists; the page test is never reached.
        self.quiet(self.m.arm, "Pageless Goal", "goal", 900)
        self.assertEqual(
            "WRONG-BRANCH", self.first_token("--subject", "Pageless Goal", "--branch", "topic")
        )

    def test_unresolvable_name_is_refused_not_unarmed(self):
        self.quiet(self.m.arm, self.FULL, "goal", 900)
        rc, out, err = self.run_cli("--state", "--subject", "Several Clusters")
        self.assertEqual(self.m.EXIT_UNRESOLVED, rc)
        self.assertNotEqual(0, rc)
        self.assertEqual("", out, "a refusal prints no state for a reader to parse")
        self.assertIn("'Several Clusters'", err)
        # Control on the same run: the full name answers.
        rc, out, _ = self.run_cli("--state", "--subject", self.FULL)
        self.assertEqual(0, rc)
        self.assertTrue(out.startswith("OK\t"), out)

    def test_unresolvable_name_is_refused_with_an_explicit_branch_too(self):
        rc, out, _ = self.run_cli("--state", "--subject", "Several Clusters", "--branch", "goal")
        self.assertEqual(self.m.EXIT_UNRESOLVED, rc)
        self.assertEqual("", out)

    def test_the_fleet_branch_has_no_page_and_still_reads_unarmed(self):
        self.assertEqual(
            "UNARMED", self.first_token("--subject", "Fleet Manager", "--branch", "fleet")
        )

    def test_a_goal_page_under_23_goals_resolves(self):
        # `private-*` / `seibert-*` vaults keep goals in `24 Goals`; the `24 Tasks` ones in
        # `23 Goals`. A page in the second folder is proof the subject exists.
        os.makedirs(os.path.join(self.vault.name, "23 Goals"), exist_ok=True)
        open(os.path.join(self.vault.name, "23 Goals", "Legacy Goal.md"), "w").close()
        self.assertEqual("UNARMED", self.first_token("--subject", "Legacy Goal", "--branch", "goal"))

    def test_a_name_collision_across_tiers_does_not_read_wrong_branch(self):
        # Goal and topic pages are named independently, so an armed TOPIC may share a name
        # with a never-armed GOAL. The goal probe asked about the goal tier, which has no
        # record: that is UNARMED, and round-liveness arms it. Reporting WRONG-BRANCH here
        # would leave the goal's loop unarmed forever.
        open(os.path.join(self.vault.name, "24 Goals", "Shared Name.md"), "w").close()
        self.quiet(self.m.arm, "Shared Name", "topic", 900)
        self.assertEqual(
            "UNARMED", self.first_token("--subject", "Shared Name", "--branch", "goal")
        )
        # Control: the topic probe on the same name reads the armed record.
        self.assertEqual("OK", self.first_token("--subject", "Shared Name", "--branch", "topic"))

    def test_a_page_on_the_probed_branch_wins_over_a_same_named_page_elsewhere(self):
        # Both tiers hold a page under one name: a goal (armed, with a record) and a topic
        # (never armed). Probing the TOPIC must read UNARMED -- the goal is a different
        # subject that shares the name. A goal-first page test that reports WRONG-BRANCH here
        # leaves the topic's loop unarmed forever.
        open(os.path.join(self.vault.name, "24 Goals", "Twin Name.md"), "w").close()
        os.makedirs(os.path.join(self.vault.name, "23 Topics"), exist_ok=True)
        open(os.path.join(self.vault.name, "23 Topics", "Twin Name.md"), "w").close()
        self.quiet(self.m.arm, "Twin Name", "goal", 900)
        self.assertEqual("UNARMED", self.first_token("--subject", "Twin Name", "--branch", "topic"))
        # Control: the goal probe still reads its own armed record.
        self.assertEqual("OK", self.first_token("--subject", "Twin Name", "--branch", "goal"))

    def test_wrong_branch_names_every_other_branch_carrying_a_record(self):
        open(os.path.join(self.vault.name, "24 Goals", "Wide Goal.md"), "w").close()
        self.quiet(self.m.arm, "Wide Goal", "goal", 900)
        self.quiet(self.m.arm, "Wide Goal", "fleet", 900)
        rc, out, _ = self.run_cli("--state", "--subject", "Wide Goal", "--branch", "topic")
        self.assertEqual(0, rc)
        self.assertTrue(out.startswith("WRONG-BRANCH\t"), out)
        self.assertIn("record is on branch goal", out)
        self.assertIn("also fleet", out)

    def test_the_fleet_branch_is_exempt_from_the_wrong_branch_check(self):
        # The fleet manager has no page by design, so another tier's record must not turn a
        # fleet probe into WRONG-BRANCH — the documented exemption, which the tier check
        # must not run ahead of.
        self.quiet(self.m.arm, self.FULL, "goal", 900)
        self.assertEqual(
            "UNARMED", self.first_token("--subject", self.FULL, "--branch", "fleet")
        )

    def test_ok(self):
        self.quiet(self.m.arm, self.FULL, "goal", 900)
        self.assertEqual("OK", self.first_token("--subject", self.FULL, "--branch", "goal"))

    def test_stale(self):
        self.quiet(self.m.arm, self.FULL, "goal", 300)
        self.age(self.FULL, "goal", 100000)
        self.assertEqual("STALE", self.first_token("--subject", self.FULL, "--branch", "goal"))

    def test_stopped(self):
        self.quiet(self.m.arm, self.FULL, "goal", 900)
        self.quiet(self.m.stop, self.FULL, "goal")
        self.assertEqual("STOPPED", self.first_token("--subject", self.FULL, "--branch", "goal"))

    # --- SC4 regression guard: classify() measures the OBSERVED period ---------

    def test_requested_interval_is_not_the_limit(self):
        # `.cadence` says 900 s; the manager really re-arms every ~1244 s because each round
        # waits on its sweep and drive legs. Age 1300 s is inside one observed period +
        # SLACK (1424 s) and outside the requested 900 + SLACK (1080 s): a reader that used
        # the requested value would say STALE between two healthy rounds.
        s = self.m.slug(self.FULL, "goal")
        now = time.time()
        self.m.write_atomic(self.m.path(s, "cadence"), "900\n")
        arms = [now - 1300 - 1244 * k for k in range(5, -1, -1)]
        self.m.write_atomic(self.m.path(s, "arms"), "".join(f"{v:.0f}\n" for v in arms))
        os.utime(self.m.path(s, "cadence"), (now - 1300, now - 1300))
        state, detail = self.m.classify(s, now)
        self.assertEqual("OK", state, detail)
        self.assertIn("(observed)", detail)


class StoodDownPast(unittest.TestCase):
    """--stood-down-past: the selection half of the stood-down-manager close.

    The load-bearing properties: the bound is on the marker's own timestamp, not on any
    file mtime; a SLEEPING subject is excluded structurally because a sleep writes
    `.sleeping` and never `.stopped`; and an empty or unreadable state dir is reported,
    never rendered as "nothing stood down".
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.m = load(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()
        os.environ.pop("MANAGER_LIVENESS_STATE_DIR", None)

    def marker(self, slug, age_seconds):
        when = datetime.now(timezone.utc) - timedelta(seconds=age_seconds)
        self.m.write_atomic(
            self.m.path(slug, "stopped"), when.strftime("%Y-%m-%dT%H:%M:%S%z") + "\n"
        )

    def run_verb(self, minutes):
        out = io.StringIO()
        with redirect_stdout(out):
            rc = self.m.stood_down_past(minutes)
        return rc, [ln for ln in out.getvalue().splitlines() if ln]

    def test_selects_only_past_bound(self):
        self.marker("goal--old", 3 * 3600)
        self.marker("goal--fresh", 10 * 60)
        rc, rows = self.run_verb(60)
        self.assertEqual(0, rc)
        self.assertEqual(["goal--old"], [r.split("\t")[1] for r in rows])

    def test_oldest_first(self):
        self.marker("goal--newer", 2 * 3600)
        self.marker("goal--oldest", 9 * 3600)
        rc, rows = self.run_verb(60)
        self.assertEqual(0, rc)
        self.assertEqual(["goal--oldest", "goal--newer"], [r.split("\t")[1] for r in rows])
        self.assertGreater(int(rows[0].split("\t")[0]), int(rows[1].split("\t")[0]))

    def test_sleeping_is_never_selected(self):
        # A date-gated wait writes .sleeping and never .stopped, however long it lasts.
        self.m.write_atomic(self.m.path("goal--asleep", "sleeping"), "2099-01-01T00:00:00+0000\n")
        self.marker("goal--stopped", 5 * 3600)
        rc, rows = self.run_verb(60)
        self.assertEqual(0, rc)
        self.assertEqual(["goal--stopped"], [r.split("\t")[1] for r in rows])

    def test_bound_is_inclusive_of_nothing_and_empty_is_a_real_answer(self):
        self.marker("goal--old", 3 * 3600)
        rc, rows = self.run_verb(100000)
        self.assertEqual(0, rc)
        self.assertEqual([], rows)

    def test_unreadable_marker_is_reported_not_guessed(self):
        self.marker("goal--good", 3 * 3600)
        self.m.write_atomic(self.m.path("goal--junk", "stopped"), "not a timestamp\n")
        err = io.StringIO()
        out = io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            rc = self.m.stood_down_past(60)
        self.assertEqual(0, rc)
        self.assertEqual(["goal--good"], [r.split("\t")[1] for r in out.getvalue().splitlines() if r])
        self.assertIn("unreadable stopped marker", err.getvalue())

    def test_unreadable_state_dir_exits_two(self):
        with tempfile.TemporaryDirectory() as gone:
            m = load(os.path.join(gone, "does-not-exist"))
            err = io.StringIO()
            with redirect_stdout(io.StringIO()), redirect_stderr(err):
                rc = m.stood_down_past(60)
        self.assertEqual(2, rc)
        self.assertIn("state dir unreadable", err.getvalue())


class GateDrivenLiveness(unittest.TestCase):
    """A goal manager whose only wake source is the detached sweep gate.

    Removing the model cadence leaves `.cadence` ageing between gate wakes. The gate's
    own per-subject heartbeat is then the liveness signal, so a watched manager reads
    OK instead of STALE — the reading that makes `topic-keepalive.py` restart it.
    """

    SUBJECT = "Gate Driven Subject"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.m = load(self.tmp.name)
        self.s = self.m.slug(self.SUBJECT, "goal")
        self.gate_dir = os.path.join(self.m.GATE_LOOP_STATE_DIR, "private-personal")
        os.makedirs(self.gate_dir, exist_ok=True)

    def tearDown(self):
        self.tmp.cleanup()
        os.environ.pop("MANAGER_LIVENESS_STATE_DIR", None)
        os.environ.pop("MANAGER_GATE_LOOP_STATE_DIR", None)

    def stale_arm(self, branch="goal", age=5000):
        with redirect_stdout(io.StringIO()):
            self.m.arm(self.SUBJECT, branch, 900)
        p = self.m.path(self.m.slug(self.SUBJECT, branch), "cadence")
        t = time.time() - age
        os.utime(p, (t, t))

    def heartbeat(self, name, age):
        p = os.path.join(self.gate_dir, name)
        with open(p, "w", encoding="utf-8") as fh:
            fh.write("0 x rc=0\n")
        t = time.time() - age
        os.utime(p, (t, t))

    def test_a_watched_goal_with_a_stale_cadence_reads_ok(self):
        self.stale_arm()
        now = time.time()
        self.heartbeat(f"{self.m.base_slug(self.SUBJECT)}.heartbeat", 5)
        self.heartbeat("some-other-subject.heartbeat", 5)
        state, detail = self.m.classify(self.s, now)
        self.assertEqual("OK", state)
        self.assertIn("gate-driven", detail)

    def test_a_stale_cadence_without_any_heartbeat_stays_stale(self):
        self.stale_arm()
        self.assertEqual("STALE", self.m.classify(self.s, time.time())[0])

    def test_a_dead_gate_does_not_mask_a_stale_goal(self):
        self.stale_arm()
        self.heartbeat(f"{self.m.base_slug(self.SUBJECT)}.heartbeat", 4000)
        self.heartbeat("some-other-subject.heartbeat", 4000)
        self.assertEqual("STALE", self.m.classify(self.s, time.time())[0])

    def test_a_dropped_subject_is_not_gate_driven(self):
        self.stale_arm()
        self.heartbeat(f"{self.m.base_slug(self.SUBJECT)}.heartbeat", 8000)
        self.heartbeat("some-other-subject.heartbeat", 5)
        self.assertEqual("STALE", self.m.classify(self.s, time.time())[0])

    def test_the_topic_tier_is_not_gate_driven(self):
        self.stale_arm(branch="topic")
        now = time.time()
        self.heartbeat(f"{self.m.base_slug(self.SUBJECT)}.heartbeat", 5)
        self.heartbeat("some-other-subject.heartbeat", 5)
        self.assertEqual(
            "STALE", self.m.classify(self.m.slug(self.SUBJECT, "topic"), now)[0]
        )
