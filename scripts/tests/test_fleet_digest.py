#!/usr/bin/env python3
"""Tests for scripts/fleet-digest.py.

The load-bearing properties, each one a defect this watcher exists to prevent:

  * the newest round is chosen by its parsed `snapshot written:` timestamp, never by its
    filename — `2026-10-20T08-43-00-02-00.digest` holds `06:43:35Z`, so ordering by name
    would judge a stale round by a fresh filename;
  * the nine non-timestamp forms resolve to parsed / unknown, never to "fresh" (a silent
    miss) and never to "parse failure" (a false report);
  * one lapse reports once, and the following healthy round clears it;
  * a gap the watcher itself did not run across is UNOBSERVED, not a lapse — a sleeping
    host must not be reported as a lapsed loop;
  * a broken read exits 2, never 0, so a failure can never read as healthy.

Run: python3 -m unittest discover -s scripts/tests -v
"""

import importlib.util
import io
import os
import tempfile
import time
import unittest
import unittest.mock as mock
from contextlib import redirect_stdout

_HERE = os.path.dirname(os.path.abspath(__file__))


def load(digest_dir, state_dir, limit=1080, tick=60):
    os.environ["FLEET_DIGEST_DIR"] = digest_dir
    os.environ["FLEET_DIGEST_STATE_DIR"] = state_dir
    os.environ["FLEET_DIGEST_LIMIT"] = str(limit)
    os.environ["FLEET_DIGEST_TICK"] = str(tick)
    spec = importlib.util.spec_from_file_location(
        "fleet_digest", os.path.join(os.path.dirname(_HERE), "fleet-digest.py")
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.digests = os.path.join(self.tmp.name, "handback")
        self.state = os.path.join(self.tmp.name, "state")
        os.makedirs(self.digests)
        os.makedirs(self.state)
        self.m = load(self.digests, self.state)

    def tearDown(self):
        self.tmp.cleanup()
        for k in ("FLEET_DIGEST_DIR", "FLEET_DIGEST_STATE_DIR",
                  "FLEET_DIGEST_LIMIT", "FLEET_DIGEST_TICK"):
            os.environ.pop(k, None)

    def digest(self, name, snapshot, mtime=None, body_lines=()):
        path = os.path.join(self.digests, name)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("DIGEST x · 1 sessions · snapshot written: %s\n" % snapshot)
            for line in body_lines:
                fh.write(line + "\n")
        if mtime is not None:
            os.utime(path, (mtime, mtime))
        return path

    def ago(self, seconds):
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - seconds))

    def tick(self, seconds_ago):
        with open(os.path.join(self.state, "ticks"), "w", encoding="utf-8") as fh:
            fh.write("%.3f\n" % (time.time() - seconds_ago))

    def run_check(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = self.m.cmd_check()
        return rc, buf.getvalue().strip()


class Parse(Base):
    def test_full_iso(self):
        epoch, quality = self.m.parse_snapshot("2026-10-19T21:58:31Z")
        self.assertEqual(quality, "parsed")
        self.assertEqual(epoch, 1792447111)

    def test_minute_precision(self):
        epoch, quality = self.m.parse_snapshot("2026-10-12T17:15")
        self.assertEqual(quality, "parsed")
        self.assertIsNotNone(epoch)

    def test_timestamp_with_a_suffix_still_parses(self):
        # The live digest writes `2026-10-20T11:39:16Z - 23 sessions`. A whole-value match
        # rejected it and carried an older baseline forward.
        epoch, quality = self.m.parse_snapshot("2026-10-20T11:39:16Z - 23 sessions")
        self.assertEqual(quality, "parsed")
        self.assertEqual(epoch, 1792496356)

    def test_day_precision_is_unknown(self):
        epoch, quality = self.m.parse_snapshot("2026-10-12 (fleet-snapshot.py)")
        self.assertIsNone(epoch)
        self.assertEqual(quality, "day-precision")

    def test_baseline_kept_names_its_baseline(self):
        epoch, quality = self.m.parse_snapshot(
            "skipped (persist not requested by caller; baseline kept at 08:03:02Z)"
        )
        self.assertIsNone(epoch)  # no date in that form: not a usable baseline
        self.assertEqual(quality, "no-baseline")

    def test_baseline_kept_with_full_date(self):
        epoch, quality = self.m.parse_snapshot(
            "skipped (persist not requested by caller; baseline kept at 2026-10-20T08:03:02Z)"
        )
        self.assertEqual(quality, "parsed")
        self.assertEqual(epoch, 1792483382)

    def test_no_baseline_forms_are_not_fresh(self):
        for raw in (
            "skipped (persist not requested by caller; baseline read ok)",
            "skipped (roster join failed; previous snapshot left intact)",
            "skipped (roster not supplied; round failed)",
            "skipped (pass not run: task-file/stall pass not run; roster-only round)",
            "skipped (not persisted by this agent run; ledger scope unresolved)",
            "(pending, set by fleet-snapshot.py)",
        ):
            with self.subTest(raw=raw):
                epoch, quality = self.m.parse_snapshot(raw)
                self.assertIsNone(epoch)
                self.assertEqual(quality, "no-baseline")


class NewestRound(Base):
    def test_content_beats_filename(self):
        # The stale round carries the LATER filename; ordering by name would miss it.
        self.digest("2026-10-20T09-00-00Z.digest", self.ago(7200))
        self.digest("2026-10-20T08-00-00Z.digest", self.ago(30))
        epoch, quality, path, _ = self.m.newest_round()
        self.assertEqual(os.path.basename(path), "2026-10-20T08-00-00Z.digest")
        self.assertEqual(quality, "parsed")

    def test_carries_forward_when_newest_names_no_baseline(self):
        self.digest("a.digest", self.ago(4000))
        self.digest("b.digest", "skipped (roster not supplied; round failed)",
                    mtime=time.time())
        epoch, quality, path, _ = self.m.newest_round()
        self.assertEqual(quality, "carried-forward")
        self.assertEqual(os.path.basename(path), "a.digest")
        self.assertGreater(time.time() - epoch, 3000)

    def test_snapshot_line_is_read_from_a_multi_line_digest(self):
        # Real digests carry many lines after the snapshot line. A `$` without MULTILINE
        # matches only at end-of-string, so the whole match failed on every real file
        # while passing on these single-line fixtures — caught by probing the live
        # directory, not by this suite. The body is the regression.
        self.digest("a.digest", self.ago(30), body_lines=[
            "CLASSIFICATION idle=3 parked=0",
            "ROUND 14",
        ])
        epoch, quality, _, _ = self.m.newest_round()
        self.assertEqual(quality, "parsed")
        self.assertIsNotNone(epoch)

    def test_missing_dir_raises(self):
        self.m.DIGEST_DIR = os.path.join(self.tmp.name, "nope")
        with self.assertRaises(RuntimeError):
            self.m.newest_round()


class Check(Base):
    def test_first_tick_is_unobserved(self):
        self.digest("a.digest", self.ago(99999))
        rc, out = self.run_check()
        self.assertEqual(rc, 0)
        self.assertIn("UNOBSERVED", out)

    def test_stale_transition_reports_once(self):
        self.digest("a.digest", self.ago(5000))
        self.tick(60)
        rc, out = self.run_check()
        self.assertEqual(rc, self.m.EXIT_STALE)
        self.assertIn("STALE", out)

        self.tick(60)
        rc, out = self.run_check()
        self.assertEqual(rc, 0)
        self.assertIn("SUPPRESSED", out)

    def test_healthy_round_clears_the_report(self):
        self.digest("a.digest", self.ago(5000))
        self.tick(60)
        self.run_check()  # STALE, notified written

        self.digest("b.digest", self.ago(5))
        self.tick(60)
        rc, out = self.run_check()
        self.assertEqual(rc, 0)
        self.assertIn("RECOVERED", out)
        self.assertFalse(os.path.exists(os.path.join(self.state, "notified.json")))

    def test_second_lapse_reports_again(self):
        self.digest("a.digest", self.ago(5000))
        self.tick(60)
        self.run_check()
        self.digest("b.digest", self.ago(5))
        self.tick(60)
        self.run_check()  # RECOVERED

        # Age the newest round again — a second lapse is a stale NEWEST round, so the
        # round that recovered has to go stale with it, not sit there fresh.
        self.digest("b.digest", self.ago(5000))
        self.tick(60)
        rc, out = self.run_check()
        self.assertEqual(rc, self.m.EXIT_STALE)

    def test_resume_after_own_gap_is_unobserved(self):
        self.digest("a.digest", self.ago(28800))  # 8 h, the overnight shape
        self.tick(28800)
        rc, out = self.run_check()
        self.assertEqual(rc, 0)
        self.assertIn("UNOBSERVED", out)

    def test_broken_read_exits_two(self):
        self.m.DIGEST_DIR = os.path.join(self.tmp.name, "nope")
        self.tick(60)
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = self.m.main(["--check"])
        self.assertEqual(rc, self.m.EXIT_FAILED)
        self.assertIn("FAILED", buf.getvalue())

    def test_heartbeat_written(self):
        self.digest("a.digest", self.ago(5))
        self.tick(60)
        self.run_check()
        self.assertTrue(os.path.exists(os.path.join(self.state, "heartbeat")))


class State(Base):
    def test_state_exits_zero_when_missing(self):
        self.m.DIGEST_DIR = os.path.join(self.tmp.name, "nope")
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = self.m.cmd_state()
        self.assertEqual(rc, 0)
        self.assertIn("MISSING", buf.getvalue())

    def test_state_reports_stale_without_notifying(self):
        self.digest("a.digest", self.ago(5000))
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = self.m.cmd_state()
        self.assertEqual(rc, 0)
        self.assertIn("STALE", buf.getvalue())
        self.assertFalse(os.path.exists(os.path.join(self.state, "notified.json")))


class Stale(Base):
    def test_missing_heartbeat_exits_one(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = self.m.cmd_stale()
        self.assertEqual(rc, 1)
        self.assertIn("MISSING", buf.getvalue())

    def test_fresh_heartbeat_is_ok(self):
        with open(os.path.join(self.state, "heartbeat"), "w", encoding="utf-8") as fh:
            fh.write("%d %s rc=0\n" % (int(time.time()) - 5, self.ago(5)))
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = self.m.cmd_stale()
        self.assertEqual(rc, 0)
        self.assertIn("OK", buf.getvalue())


class RisingAge(Base):
    """SC2b — a lapse that never recovers must not silence itself after one report.

    Measured 2026-10-30: the ledger carried `SUPPRESSED … reason=already-reported` on
    every 60-second tick while the age climbed 1,374 s → 23,064 s over 6h24m. The
    digest was never written, and the one detector on that path had gone quiet.

    The clock is faked so the age can rise without the test sleeping, and so the digest
    file — and therefore its parsed epoch — stays byte-identical across the run. That is
    the live shape: in a real lapse nothing rewrites the digest, only the clock moves.
    """

    def setUp(self):
        super().setUp()
        self.now = 1800000000.0
        self._clock = mock.patch.object(self.m.time, "time", side_effect=lambda: self.now)
        self._clock.start()

    def tearDown(self):
        self._clock.stop()
        super().tearDown()

    def test_a_rising_age_refires(self):
        # LIMIT is 1080 s: 4325 s of age is the fourth lapse interval.
        self.digest("a.digest", self.ago(4325))
        self.tick(60)
        rc, out = self.run_check()
        self.assertEqual(rc, self.m.EXIT_STALE)
        self.assertIn("STALE", out)

        # Still the same interval: a flat age keeps the dedup's original job.
        self.now += 60
        self.tick(60)
        rc, out = self.run_check()
        self.assertEqual(rc, 0)
        self.assertIn("SUPPRESSED", out)

        # A further interval has elapsed and the digest is still missing: re-fire.
        self.now += 3000
        self.tick(60)
        rc, out = self.run_check()
        self.assertEqual(rc, self.m.EXIT_STALE)
        self.assertIn("STALE", out)
        self.assertNotIn("SUPPRESSED", out)

    def test_a_zero_or_negative_limit_does_not_crash(self):
        # The re-fire step divides by LIMIT, so a misconfigured 0 must not become a
        # ZeroDivisionError where the old code merely reported stale, and a negative
        # value must not pin every lapse at step 1.
        self.assertEqual(load(self.digests, self.state, limit=0).LIMIT, 1)
        self.assertEqual(load(self.digests, self.state, limit=-5).LIMIT, 1)

    def test_a_recovery_then_a_higher_lapse_reports_again(self):
        self.digest("a.digest", self.ago(4325))  # step 4
        self.tick(60)
        rc, out = self.run_check()
        self.assertEqual(rc, self.m.EXIT_STALE)

        self.digest("b.digest", self.ago(5))  # a healthy round clears the marker
        self.tick(60)
        rc, out = self.run_check()
        self.assertEqual(rc, 0)
        self.assertIn("RECOVERED", out)

        # Drop the old round first: `newest_round()` orders by parsed timestamp, so a
        # surviving 4325s-old `a.digest` would be selected over a 9000s-old `b.digest`
        # and the "higher lapse on the new round" this test names would never run.
        os.unlink(os.path.join(self.digests, "a.digest"))
        self.digest("b.digest", self.ago(9000))  # step 8, a fresh lapse
        self.tick(60)
        rc, out = self.run_check()
        self.assertEqual(rc, self.m.EXIT_STALE)

    def test_the_refire_does_not_wait_for_a_new_digest(self):
        # The whole point: the epoch never changes in a real lapse, so a re-fire keyed
        # on it alone would stay silent forever.
        self.digest("a.digest", self.ago(4325))
        self.tick(60)
        first_epoch, _, _, _ = self.m.newest_round()
        self.run_check()
        self.now += 3000
        self.tick(60)
        self.run_check()
        second_epoch, _, _, _ = self.m.newest_round()
        self.assertEqual(first_epoch, second_epoch)


if __name__ == "__main__":
    unittest.main()
