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


def load_keepalive():
    """The keepalive module, loaded the same way `load()` loads the digest.

    SC4 has to cross the seam the keeper actually reads — the digest's verdict reaches
    the keeper through `fleet-keepalive.py`, never directly — so the test runs the real
    keepalive over a real ledger line rather than asserting the detail string alone.
    """
    spec = importlib.util.spec_from_file_location(
        "fleet_keepalive", os.path.join(os.path.dirname(_HERE), "fleet-keepalive.py")
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

    def test_doubled_snapshot_label_still_parses(self):
        # A render that copies the whole `snapshot written: …` tail into the value emits
        # `snapshot written: snapshot written: <iso>`. Measured 2026-11-03 on
        # `2026-11-03T07-44Z.digest`: it parsed as `no-baseline`, the previous round's
        # baseline was carried forward, and the watcher reported `STALE … age=1129s` at
        # `08:00:48Z` on a round that had run on time.
        epoch, quality = self.m.parse_snapshot(
            "snapshot written: 2026-11-03T07:48:58Z - 48 sessions"
        )
        self.assertEqual(quality, "parsed")
        self.assertEqual(epoch, self.m.parse_snapshot("2026-11-03T07:48:58Z")[0])

    def test_doubled_label_without_a_timestamp_stays_unknown(self):
        epoch, quality = self.m.parse_snapshot("snapshot written:")
        self.assertIsNone(epoch)
        self.assertEqual(quality, "no-baseline")

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

    def test_roster_join_forms_are_join_degraded(self):
        # Both renders mean the round RAN and could not join the roster. They are
        # deliberately not `no-baseline`: that quality carries an older round's baseline
        # forward, which is what made a working manager read as a lapsed one on
        # 2026-11-01. The round's own mtime is the honest age (see NewestRound below).
        for raw in (
            "skipped (roster join incomplete: 5/52 resolved)",
            "skipped (roster join failed; previous snapshot left intact)",
        ):
            with self.subTest(raw=raw):
                epoch, quality = self.m.parse_snapshot(raw)
                self.assertIsNone(epoch)
                self.assertEqual(quality, "join-degraded")

    def test_roster_not_supplied_is_not_join_degraded(self):
        # A substring match on "roster join" would swallow this one. It is a different
        # failure — the round did not run — so it must keep carrying forward.
        epoch, quality = self.m.parse_snapshot("skipped (roster not supplied; round failed)")
        self.assertIsNone(epoch)
        self.assertEqual(quality, "no-baseline")

    def test_no_baseline_forms_are_not_fresh(self):
        for raw in (
            "skipped (persist not requested by caller; baseline read ok)",
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

    def test_join_degraded_is_aged_by_its_own_mtime(self):
        # The defect measured 2026-11-01: a round that RAN on a degraded roster join sat
        # beside a 15-hour-old baseline, was aged against that baseline, and the keepalive
        # declared STALE while the round had just run. Its own mtime is the honest age.
        self.digest("baseline.digest", self.ago(4000))
        path = self.digest(
            "degraded.digest",
            "skipped (roster join incomplete: 5/52 resolved)",
            mtime=time.time() - 5,
        )
        epoch, quality, got, raw = self.m.newest_round()
        self.assertEqual(quality, "join-degraded")
        self.assertEqual(os.path.basename(got), "degraded.digest")
        self.assertAlmostEqual(epoch, os.path.getmtime(path), delta=1)
        self.assertIn("roster join incomplete", raw)

    def test_a_baseline_naming_no_join_still_carries_forward(self):
        # The join-degraded exception must not widen into "any newest skipped round is
        # fresh": a round that did not run still proves nothing about the loop.
        self.digest("a.digest", self.ago(4000))
        self.digest("b.digest", "skipped (roster not supplied; round failed)",
                    mtime=time.time())
        epoch, quality, got, _ = self.m.newest_round()
        self.assertEqual(quality, "carried-forward")
        self.assertEqual(os.path.basename(got), "a.digest")

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
        rc, _ = self.run_check()
        self.assertEqual(rc, self.m.EXIT_STALE)

        self.now += 3000
        self.tick(60)
        rc, out = self.run_check()
        # The assertion that makes this test non-vacuous: the epoch is unchanged, so
        # only the rising step can produce a second STALE rather than SUPPRESSED.
        self.assertEqual(rc, self.m.EXIT_STALE)
        self.assertIn("STALE", out)
        second_epoch, _, _, _ = self.m.newest_round()
        self.assertEqual(first_epoch, second_epoch)


class JoinDegraded(Base):
    """SC4 — a degraded round is fresh by its own mtime, and a run of them stays `ok`.

    The criterion this pins is the one a one-run test cannot reach: three rounds whose
    mtimes span MORE than LIMIT, each classified `join-degraded` and each feeding
    `fleet-keepalive.py` an `ok`. A carried-forward baseline fails from the first run —
    the baseline is older than LIMIT, so the keepalive answers `stale`. The inverse is
    pinned too (`test_an_old_degraded_round_still_reports`): the fix must not blanket-
    suppress, or a genuinely lapsed round would never report again.
    """

    def setUp(self):
        super().setUp()
        self.now = 1800000000.0
        self._clock = mock.patch.object(self.m.time, "time", side_effect=lambda: self.now)
        self._clock.start()
        self.ka = load_keepalive()
        self.ledger = os.path.join(self.tmp.name, "ledger.txt")

    def tearDown(self):
        self._clock.stop()
        super().tearDown()

    def degraded(self, name, mtime):
        return self.digest(
            name, "skipped (roster join incomplete: 5/52 resolved)", mtime=mtime
        )

    def keepalive(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = self.ka.main(["--ledger", self.ledger])
        return rc, buf.getvalue().splitlines()[0]

    def write_ledger(self, line):
        # The tick script's own shape: `<ts>\t<rc>\t<out>\t<notified>`.
        with open(self.ledger, "w", encoding="utf-8") as fh:
            fh.write("%.3f\t0\t%s\t\n" % (self.now, line))

    def test_three_degraded_rounds_spanning_the_limit_stay_ok(self):
        self.digest("baseline.digest", self.ago(5000), mtime=self.now - 5000)
        stamps = []
        for i in range(3):
            self.degraded("d%d.digest" % i, self.now)
            self.tick(60)
            rc, out = self.run_check()
            self.assertIn("quality=join-degraded", out)
            self.assertTrue(out.startswith("OK "), out)
            self.write_ledger(out)
            self.assertEqual(self.keepalive(), (0, "ok"))
            stamps.append(self.now)
            self.now += 600
        # The span the criterion is built on, asserted rather than assumed.
        self.assertGreater(stamps[-1] - stamps[0], 1080)

    def test_an_old_degraded_round_still_reports(self):
        # Without this, "age a degraded round by itself" could be implemented as
        # "a degraded round is always fresh" and every other test here would pass.
        self.digest("baseline.digest", self.ago(9000), mtime=self.now - 9000)
        self.degraded("d.digest", self.now - 5000)  # 5000 s old, LIMIT is 1080
        self.tick(60)
        rc, out = self.run_check()
        self.assertEqual(rc, self.m.EXIT_STALE)
        self.assertIn("quality=join-degraded", out)
        self.write_ledger(out)
        self.assertEqual(self.keepalive(), (10, "stale"))


if __name__ == "__main__":
    unittest.main()
