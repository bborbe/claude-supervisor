#!/usr/bin/env python3
"""Tests for scripts/fleet-snapshot.py.

Covers the way an empty session list destroyed snapshot continuity:

  * an empty payload was written as a valid snapshot and exited 0. The script
    had no empty-guard, so `fleet-snapshot.py` overwrote a real snapshot with
    `{"sessions": {}}` and printed `snapshot written: … - 0 sessions` — success,
    by its own report. Measured 2026-09-23 (v0.39.1): a sweep whose roster read
    came back empty clobbered the snapshot, so the next round diffed against
    nothing, every session read first-seen, and `stall_count` reset.

The defect is the same shape as the ledger misread this release also fixes: a
failed read and a true absence producing identical output. The refusal is only
half the fix — the negative-control cases below (a non-empty payload still
writes; a bare dict and the `{"sessions": …}` wrapper both count as non-empty)
exist because a script that refuses everything satisfies the refusal case while
breaking the sweep entirely.

The probe is deliberately two-sided: the empty payload must exit non-zero AND
leave the existing snapshot byte-identical, and a non-empty payload must still
exit 0. A guard that only checked the exit code would pass on a script that
crashed before writing anything.

Run: python3 -m unittest discover -s scripts/tests -v
"""

import calendar
import contextlib
import hashlib
import importlib.util
import io
import json
import os
import tempfile
import time
import unittest
import unittest.mock as mock

_HERE = os.path.dirname(os.path.abspath(__file__))
_SCRIPT = os.path.join(os.path.dirname(_HERE), "fleet-snapshot.py")

_spec = importlib.util.spec_from_file_location("fleet_snapshot", _SCRIPT)
fs = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fs)


def _digest(path):
    with open(path, "rb") as handle:
        return hashlib.sha256(handle.read()).hexdigest()


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.path = os.path.join(self.tmp, "state", "fleet-snapshot.json")

    def run_main(self, payload):
        """Invoke main() with a JSON payload on stdin; return (rc, stdout, stderr)."""
        stdin = io.StringIO(json.dumps(payload))
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = fs.main(stdin=stdin, path=self.path)
        return rc, out.getvalue(), err.getvalue()

    def write_existing(self, sessions=None):
        """Write a snapshot that must survive an empty-payload run."""
        sessions = sessions if sessions is not None else {"aaaa1111": {"name": "x"}}
        fs.write_snapshot(sessions, path=self.path, swept_at="2026-09-23T17:12:00Z")
        return _digest(self.path)


class Refusal(Base):
    def test_empty_sessions_wrapper_is_refused(self):
        before = self.write_existing()
        rc, out, err = self.run_main({"sessions": {}})
        self.assertEqual(rc, 1)
        self.assertEqual(_digest(self.path), before, "snapshot must be untouched")
        self.assertEqual(out, "", "nothing may be reported as written")
        self.assertIn("empty", err)

    def test_empty_bare_dict_is_refused(self):
        before = self.write_existing()
        rc, _, err = self.run_main({})
        self.assertEqual(rc, 1)
        self.assertEqual(_digest(self.path), before)
        self.assertIn("empty", err)

    def test_empty_list_is_refused(self):
        before = self.write_existing()
        rc, _, err = self.run_main([])
        self.assertEqual(rc, 1)
        self.assertEqual(_digest(self.path), before)
        self.assertIn("empty", err)

    def test_refusal_creates_no_file_when_none_exists(self):
        rc, _, _ = self.run_main({"sessions": {}})
        self.assertEqual(rc, 1)
        self.assertFalse(
            os.path.exists(self.path), "a refused run must not create a snapshot"
        )

    def test_refusal_is_the_scripts_own_not_a_crash(self):
        """A path error would also exit non-zero and leave the file alone."""
        before = self.write_existing()
        rc, _, err = self.run_main({"sessions": {}})
        self.assertEqual(rc, 1)
        self.assertEqual(_digest(self.path), before)
        self.assertNotIn("Traceback", err, "refusal must be deliberate, not a crash")
        self.assertIn("refusing to write an empty snapshot", err)


class PositiveControl(Base):
    """A guard that refuses everything satisfies Refusal while breaking the sweep."""

    def test_non_empty_wrapper_is_written(self):
        rc, out, err = self.run_main({"sessions": {"aaaa1111": {"name": "x"}}})
        self.assertEqual(rc, 0, err)
        self.assertTrue(os.path.exists(self.path))
        self.assertIn("1 sessions", out)

    def test_non_empty_bare_dict_is_written(self):
        rc, out, _ = self.run_main({"aaaa1111": {"name": "x"}, "bbbb2222": {"name": "y"}})
        self.assertEqual(rc, 0)
        self.assertIn("2 sessions", out)

    def test_written_snapshot_round_trips(self):
        self.run_main({"sessions": {"aaaa1111": {"name": "x", "status": "busy"}}})
        with open(self.path) as handle:
            data = json.load(handle)
        self.assertEqual(list(data["sessions"]), ["aaaa1111"])
        self.assertEqual(data["sessions"]["aaaa1111"]["status"], "busy")
        self.assertTrue(data["swept_at"].endswith("Z"))

    def test_non_empty_run_advances_an_existing_snapshot(self):
        before = self.write_existing({"aaaa1111": {"name": "x"}})
        rc, _, _ = self.run_main({"sessions": {"aaaa1111": {"name": "x"}, "cccc3333": {}}})
        self.assertEqual(rc, 0)
        self.assertNotEqual(_digest(self.path), before, "a real write must land")


class AtomicWrite(Base):
    """A half-written snapshot is the same loss as an empty one, by another route.

    The write was a plain `open(path, "w")`, which truncates first — so a crash
    or a serialisation error part-way through left a truncated or zero-byte
    snapshot where a valid one stood, and the next round diffed against nothing
    exactly as it would after an empty clobber. `open-items.py` already
    documented this script as using tmp+`os.replace`; it did not.
    """

    def test_failed_serialisation_leaves_the_snapshot_intact(self):
        before = self.write_existing()
        with self.assertRaises(TypeError):
            fs.write_snapshot({"aaaa1111": object()}, path=self.path)
        self.assertEqual(
            _digest(self.path), before, "a failed write must not truncate the target"
        )
        self.assertFalse(
            os.path.exists(self.path + ".tmp"), "no tmp file may survive a failure"
        )

    def test_failed_serialisation_creates_no_snapshot_when_none_exists(self):
        with self.assertRaises(TypeError):
            fs.write_snapshot({"aaaa1111": object()}, path=self.path)
        self.assertFalse(os.path.exists(self.path))
        self.assertFalse(os.path.exists(self.path + ".tmp"))

    def test_successful_write_leaves_no_tmp_file(self):
        fs.write_snapshot({"aaaa1111": {}}, path=self.path)
        self.assertTrue(os.path.exists(self.path))
        self.assertFalse(os.path.exists(self.path + ".tmp"))


class MalformedPayload(Base):
    """Non-empty is not the same as *came from a roster read*.

    A failed read that returns an error envelope rather than nothing is
    non-empty, so it passed the empty guard, was written as a one-session
    snapshot and exited 0 — the same collapse the guard exists to prevent,
    reached with a payload that merely looks like data.
    """

    def test_error_envelope_is_refused(self):
        before = self.write_existing()
        rc, out, err = self.run_main({"error": "roster read failed"})
        self.assertEqual(rc, 1)
        self.assertEqual(_digest(self.path), before, "snapshot must be untouched")
        self.assertEqual(out, "", "nothing may be reported as written")
        self.assertIn("malformed", err)

    def test_scalar_entry_is_refused(self):
        before = self.write_existing()
        rc, _, err = self.run_main({"foo": 1})
        self.assertEqual(rc, 1)
        self.assertEqual(_digest(self.path), before)
        self.assertIn("malformed", err)

    def test_string_entry_inside_the_wrapper_is_refused(self):
        rc, _, _ = self.run_main({"sessions": {"aaaa1111": "oops"}})
        self.assertEqual(rc, 1)
        self.assertFalse(os.path.exists(self.path))

    def test_list_with_a_non_object_entry_is_refused(self):
        rc, _, _ = self.run_main([{"name": "x"}, "oops"])
        self.assertEqual(rc, 1)

    def test_a_dup_suffixed_key_still_writes(self):
        """Key-shape validation would reject live data — 1 of 45 keys is `…dup`."""
        rc, out, err = self.run_main(
            {"3d3b7835-8c20-46e7-8ec5-0c0782d238d7dup": {"name": "x"}}
        )
        self.assertEqual(rc, 0, err)
        self.assertIn("1 sessions", out)


class DigestMarker(Base):
    """The marker that makes a round's file land at the instant it stamps.

    Measured 2026-10-22: `snapshot written: 15:43:58Z`, the `.digest` on disk at
    `15:46:28Z`, and the `15:44:30Z` tick printed `STALE … age=1083s` on a round
    whose successor gap was 1051 s. The sweep reader writes the render at the end
    of its own turn, so the gap is that turn's remaining work. Writing the marker
    inside the snapshot's own call collapses it.
    """

    def setUp(self):
        super().setUp()
        self.digest = os.path.join(self.tmp, "handback", "round.digest")

    def run_with_digest(self, payload, **kwargs):
        stdin = io.StringIO(json.dumps(payload))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = fs.main(stdin=stdin, path=self.path, **kwargs)
        return rc, out.getvalue()

    def test_the_marker_exists_at_the_instant_the_snapshot_is_stamped(self):
        rc, out = self.run_with_digest({"a": {"name": "s1"}}, digest_path=self.digest)
        self.assertEqual(rc, 0)
        self.assertIn("snapshot written:", out)
        self.assertTrue(os.path.exists(self.digest), "no .digest beside the snapshot")

        swept_at = out.split("snapshot written:")[1].split("-")[0].strip()
        with open(self.digest, encoding="utf-8") as handle:
            self.assertIn("snapshot written: %s" % swept_at, handle.read())

    def test_the_marker_parses_as_this_rounds_baseline(self):
        self.run_with_digest({"a": {"name": "s1"}}, digest_path=self.digest)
        spec = importlib.util.spec_from_file_location(
            "fleet_digest_for_marker",
            os.path.join(os.path.dirname(_HERE), "fleet-digest.py"),
        )
        digest = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(digest)
        epoch, quality, raw = digest.read_round(self.digest)
        self.assertEqual(quality, "parsed")
        self.assertIsNotNone(epoch)
        with open(self.path, encoding="utf-8") as handle:
            self.assertEqual(raw, json.load(handle)["swept_at"])

    def test_no_marker_without_a_digest_path(self):
        self.run_with_digest({"a": {"name": "s1"}})
        self.assertFalse(os.path.exists(self.digest))

    def test_the_env_var_is_honoured(self):
        os.environ["FLEET_SNAPSHOT_DIGEST_PATH"] = self.digest
        try:
            self.run_with_digest({"a": {"name": "s1"}})
        finally:
            os.environ.pop("FLEET_SNAPSHOT_DIGEST_PATH", None)
        self.assertTrue(os.path.exists(self.digest))

    def test_a_refused_snapshot_writes_no_marker(self):
        # A marker on a refused round would tell the watcher the loop is alive on a
        # round that persisted nothing — the same silent-miss the empty guard exists
        # to prevent, one file over.
        rc, _ = self.run_with_digest({"sessions": {}}, digest_path=self.digest)
        self.assertEqual(rc, 1)
        self.assertFalse(os.path.exists(self.digest))


class HeaderComposition(Base):
    """The `.digest` first line is composed here, once — never by the prompt.

    Measured 2026-11-05: a round's first line read
    `snapshot written: snapshot written: 2026-11-05T17:34:30Z - 43 sessions`.
    The sweep prompt told the reader to quote `fleet-snapshot.py`'s own
    already-labelled stdout line into a template slot that carried the label
    itself, so two instructions in one prompt disagreed and the agent followed
    the wrong one. Composing the line in code removes the disagreement.
    """

    VALUE = "2026-11-05T17:34:30Z"

    def test_the_composer_writes_the_label_once(self):
        line = fs.compose_header(43, self.VALUE)
        self.assertEqual(line.count("snapshot written:"), 1, line)
        self.assertEqual(
            line,
            "DIGEST 2026-11-05T17:34:30Z · 43 sessions · snapshot written: 2026-11-05T17:34:30Z",
        )

    def test_the_2026_11_05_value_yields_one_label(self):
        # The value the script itself prints, which is what a caller quotes.
        line = fs.compose_header(
            43, "snapshot written: 2026-11-05T17:34:30Z - 43 sessions"
        )
        self.assertEqual(line.count("snapshot written:"), 1, line)
        self.assertTrue(
            line.endswith("snapshot written: 2026-11-05T17:34:30Z - 43 sessions"), line
        )

    def test_the_doubled_form_normalises_to_one_label(self):
        line = fs.compose_header(
            43,
            "snapshot written: snapshot written: 2026-11-05T17:34:30Z - 43 sessions",
        )
        self.assertEqual(line.count("snapshot written:"), 1, line)
        self.assertTrue(
            line.endswith("snapshot written: 2026-11-05T17:34:30Z - 43 sessions"), line
        )

    def test_the_header_carries_the_shape_the_receipt_reads(self):
        # manager-receipt.py's digest_shape(): these three markers are the shape.
        line = fs.compose_header(43, self.VALUE)
        self.assertTrue(line.startswith("DIGEST "), line)
        self.assertIn("sessions", line)
        self.assertIn("snapshot written:", line)

    def test_a_naive_concatenation_still_doubles(self):
        # The control: proves the one-label assertion above can go red. This is
        # exactly what the prompt's two instructions produced together.
        naive = "snapshot written: " + "snapshot written: 2026-11-05T17:34:30Z - 43 sessions"
        self.assertEqual(naive.count("snapshot written:"), 2)

    def test_the_marker_is_the_rounds_full_header(self):
        digest = os.path.join(self.tmp, "handback", "round.digest")
        stdin = io.StringIO(json.dumps({"a": {"name": "s1"}}))
        with contextlib.redirect_stdout(io.StringIO()):
            rc = fs.main(stdin=stdin, path=self.path, digest_path=digest)
        self.assertEqual(rc, 0)
        with open(digest, encoding="utf-8") as handle:
            first = handle.readline().rstrip("\n")
        self.assertIn("· 1 sessions · snapshot written: ", first)
        self.assertEqual(first.count("snapshot written:"), 1, first)
        # The prefix names the same stamp the label carries, so a reader that
        # shows line 1 cannot report two different times for one round.
        prefix, _, labelled = first.partition(" · ")
        self.assertEqual(
            prefix[len("DIGEST "):], labelled.split("snapshot written:", 1)[1].strip(), first
        )


class RecordedCase(unittest.TestCase):
    """The 2026-10-22 window, replayed against the watcher at a controlled clock."""

    TICK = "2026-10-22T15:44:30Z"
    PREV = "2026-10-22T15:26:27Z"
    NEXT = "2026-10-22T15:43:58Z"

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.digests = os.path.join(self.tmp, "handback")
        self.state = os.path.join(self.tmp, "state")
        os.makedirs(self.digests)
        os.makedirs(self.state)
        os.environ["FLEET_DIGEST_DIR"] = self.digests
        os.environ["FLEET_DIGEST_STATE_DIR"] = self.state
        os.environ["FLEET_DIGEST_LIMIT"] = "1080"
        os.environ["FLEET_DIGEST_TICK"] = "60"
        spec = importlib.util.spec_from_file_location(
            "fleet_digest_recorded", os.path.join(os.path.dirname(_HERE), "fleet-digest.py")
        )
        self.m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.m)
        self.now = float(calendar.timegm(time.strptime(self.TICK, "%Y-%m-%dT%H:%M:%SZ")))
        self._clock = mock.patch.object(self.m.time, "time", side_effect=lambda: self.now)
        self._clock.start()

    def tearDown(self):
        self._clock.stop()
        for key in ("FLEET_DIGEST_DIR", "FLEET_DIGEST_STATE_DIR",
                    "FLEET_DIGEST_LIMIT", "FLEET_DIGEST_TICK"):
            os.environ.pop(key, None)

    def write(self, name, snapshot):
        with open(os.path.join(self.digests, name), "w", encoding="utf-8") as handle:
            handle.write("DIGEST x · 1 sessions · snapshot written: %s\n" % snapshot)

    def tick(self):
        with open(os.path.join(self.state, "ticks"), "w", encoding="utf-8") as handle:
            handle.write("%.3f\n" % (self.now - 60))

    def check(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = self.m.cmd_check()
        return rc, out.getvalue().strip()

    def test_a_round_whose_file_lands_with_its_stamp_is_not_a_lapse(self):
        self.write("prev.digest", self.PREV)
        self.write("next.digest", self.NEXT)
        self.tick()
        rc, out = self.check()
        self.assertEqual(rc, 0, out)
        self.assertIn("OK", out)
        self.assertNotIn("STALE", out)

    def test_the_same_tick_without_the_successor_still_reports(self):
        # The control: the criterion above must be able to go red. Absent the
        # successor's file this tick is a lapse, and it reported as one live.
        self.write("prev.digest", self.PREV)
        self.tick()
        rc, out = self.check()
        self.assertEqual(rc, self.m.EXIT_STALE, out)
        self.assertIn("age=1083s", out)

    def test_the_second_1083_report_is_genuine_and_still_fires(self):
        # 2026-10-22T10:29:36Z judged against 10:11:33Z. Its successor was not taken
        # until 10:31:47Z — a 1214 s gap — so this report was correct. A fix that
        # silenced the whole 1080–1160 s band would wrongly swallow it.
        self.now = float(calendar.timegm(
            time.strptime("2026-10-22T10:29:36Z", "%Y-%m-%dT%H:%M:%SZ")))
        self.write("prev.digest", "2026-10-22T10:11:33Z")
        self.tick()
        rc, out = self.check()
        self.assertEqual(rc, self.m.EXIT_STALE, out)
        self.assertIn("age=1083s", out)

    def test_a_genuine_gap_still_fires_within_one_poll_of_the_limit(self):
        # The parent task's bound: 1080 s limit, 60 s tick, so no more than 1140 s
        # of age may pass before the report.
        self.now = float(calendar.timegm(
            time.strptime("2026-10-18T13:11:46Z", "%Y-%m-%dT%H:%M:%SZ")))
        self.write("prev.digest", "2026-10-18T12:52:46Z")
        self.tick()
        rc, out = self.check()
        self.assertEqual(rc, self.m.EXIT_STALE, out)
        self.assertIn("age=1140s", out)


if __name__ == "__main__":
    unittest.main()
