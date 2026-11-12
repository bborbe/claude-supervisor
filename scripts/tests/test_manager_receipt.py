#!/usr/bin/env python3
"""Tests for scripts/manager-receipt.py.

The load-bearing property is the one the incident measured: a round that runs only
the wakeup, `manager-liveness.py --arm` and a box count must be RED, and a round
that did its work must be GREEN. Everything else here pins the fabricated-receipt
cases — a `touch`ed file, an arbitrary non-empty string, a stub written by the round
itself — because a check satisfied by those is satisfied by exactly the shortcut it
exists to catch.

`test_skip_everything_shortcut_is_red` is the red/green test: it fails if the check
is absent or inert (an inert check returns 0 on the shortcut), and passes only when
the shortcut is actually caught.

Run: python3 -m unittest discover -s scripts/tests -v
"""

import contextlib
import datetime
import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout

_HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.dirname(_HERE)
BOX_TABLE = os.path.join(SCRIPTS, "box-table.py")
PREDISPATCH = os.path.join(SCRIPTS, "manager-predispatch.py")
ATTENTION_WATCH = os.path.join(SCRIPTS, "manager-attention-watch.py")

NEEDS_YOU_OK = "Needs you (0)\n\nNothing needs you.\n"

# The shape `scripts/round-readings.py` writes: the four verdict families per row.
READINGS_OK = json.dumps({
    "subject": "a subject",
    "recorded_at": 1792578804.9,
    "feed_state": "read",
    "rows": {"A task": {"liveness": "LIVE", "in_flight": "quiet",
                        "feed": "clear", "external_wait": "none"}},
})


def load(receipt_dir, liveness_dir):
    os.environ["MANAGER_RECEIPT_STATE_DIR"] = receipt_dir
    os.environ["MANAGER_LIVENESS_STATE_DIR"] = liveness_dir
    spec = importlib.util.spec_from_file_location(
        "manager_receipt", os.path.join(os.path.dirname(_HERE), "manager-receipt.py")
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def load_module(name, path):
    """Import a sibling script by path — the producers are hyphenated, not packages."""
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def render_table(header, rows):
    """A frame rendered by the real producer, `box-table.py`, over its own stdin contract."""
    return subprocess.run(
        [sys.executable, BOX_TABLE],
        input=json.dumps({"header": header, "rows": rows}),
        capture_output=True, text=True, check=True,
        env=dict(os.environ, BOX_TABLE_NO_LINKS="1"),
    ).stdout


def iso_ts(epoch):
    """A `ts` in the producer's own format — `manager-attention-watch.py:1445`."""
    return time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(epoch))


# The sweep payload is a RENDERED TABLE, never JSON: `--write-payload` stores the frame
# `box-table.py` printed. A JSON fixture here would re-encode the very defect this task
# repairs — the old predicate accepted exactly the shape the producer never writes.
SWEEP_OK = render_table(["Topic", "Status"], [["Recurring Operator Gates", "running"]])

# The drive leg's own report, as `commands/steps/goal-drive.md` requires it to be
# reproduced under the table. The headers are the producer's vocabulary; a summary the
# caller wrote in its own words is what `drive_shape()` must refuse.
DRIVE_OK = "To open\n  none\nTo resume\n  none\nNot resumed\n  none\nEscalated\n  none\n"

# The same report from a leg that found nothing to do — still a real report, and still
# green. A predicate demanding a non-empty row set would fail here and report a healthy
# quiet round as `skipped: drive`, which is the permanent false red this file avoids.
DRIVE_QUIET = "To open\nTo resume\n"


class Receipt(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.live = tempfile.TemporaryDirectory()
        self.arts = tempfile.TemporaryDirectory()
        self.m = load(self.tmp.name, self.live.name)
        self.topic = "Recurring Operator Gates"

    def tearDown(self):
        for t in (self.tmp, self.live, self.arts):
            t.cleanup()
        os.environ.pop("MANAGER_RECEIPT_STATE_DIR", None)
        os.environ.pop("MANAGER_LIVENESS_STATE_DIR", None)

    # --- helpers ---------------------------------------------------------------

    def artifact(self, name, text, age=0.0):
        """Write an artifact file; `age` seconds old (positive = before the round)."""
        p = os.path.join(self.arts.name, name)
        with open(p, "w", encoding="utf-8") as fh:
            fh.write(text)
        if age:
            t = time.time() - age
            os.utime(p, (t, t))
        return p

    def events_log(self, name, newest_ts, age=0.0):
        """An events.jsonl whose NEWEST RECORD carries `newest_ts`.

        Written in the producer's own format — an ISO-8601 string with a numeric
        offset, exactly as `manager-attention-watch.py:1445` writes it. A numeric
        fixture here would let a predicate that only accepts numbers look correct,
        which is the defect: it matched 0 of the 526 records in the measured log.
        """
        p = os.path.join(self.arts.name, name)
        with open(p, "w", encoding="utf-8") as fh:
            for ts in sorted(newest_ts):
                fh.write(json.dumps({"kind": "LIVENESS", "ts": iso_ts(ts)}) + "\n")
        if age:
            t = time.time() - age
            os.utime(p, (t, t))
        return p

    def cli(self, *argv):
        out, err, old = io.StringIO(), io.StringIO(), sys.argv
        sys.argv = ["manager-receipt.py", *argv]
        try:
            with redirect_stdout(out), redirect_stderr(err):
                rc = self.m.main()
        finally:
            sys.argv = old
        return rc, out.getvalue()

    def start(self):
        return self.cli("--start", "--topic", self.topic)

    def record(self, step, path):
        return self.cli("--record", "--topic", self.topic, "--step", step, "--path", path)

    def check(self):
        return self.cli("--check", "--topic", self.topic)

    def full_round(self):
        """A round that did its work: every step recorded, every artifact real."""
        self.start()
        ny = self.artifact("needs-you.txt", NEEDS_YOU_OK)
        sw = self.artifact("sweep.json", SWEEP_OK)
        ev = self.events_log("events.jsonl", [time.time()])
        rd = self.artifact("readings.json", READINGS_OK)
        dv = self.artifact("drive.txt", DRIVE_OK)
        for step, p in (("needs-you", ny), ("sweep", sw), ("watcher", ev),
                        ("readings", rd), ("drive", dv)):
            self.record(step, p)

    # --- the red/green property ------------------------------------------------

    def test_skip_everything_shortcut_is_red(self):
        # The incident's round: wakeup + `manager-liveness.py --arm` + a box count,
        # nothing else. It writes no receipt at all, which is what makes it red —
        # an absent check would return 0 here and this test would fail.
        rc, out = self.check()
        self.assertEqual(rc, self.m.EXIT_SKIPPED)
        self.assertIn("skipped: receipt", out)

    def test_shortcut_that_starts_a_round_but_runs_no_step_is_red(self):
        # The subtler shortcut: it calls --start (so a receipt EXISTS and is fresh)
        # and then skips every step. Presence alone must not pass.
        self.start()
        rc, out = self.check()
        self.assertEqual(rc, self.m.EXIT_SKIPPED)
        for step in self.m.STEPS:
            self.assertIn(f"skipped: {step}", out)

    def test_full_round_is_green(self):
        self.full_round()
        rc, out = self.check()
        self.assertEqual(rc, 0)
        self.assertEqual(out, "")

    # --- the drive leg ---------------------------------------------------------

    def test_a_round_that_sweeps_and_skips_the_drive_leg_is_red(self):
        # The measured incident: readings, needs-you, sweep and watcher all present and
        # real, five sweep readers after the last drive dispatch, and `--check` green
        # because the tuple had no field for the leg that never ran.
        self.start()
        for step, p in (("needs-you", self.artifact("needs-you.txt", NEEDS_YOU_OK)),
                        ("sweep", self.artifact("sweep.json", SWEEP_OK)),
                        ("watcher", self.events_log("events.jsonl", [time.time()])),
                        ("readings", self.artifact("readings.json", READINGS_OK))):
            self.record(step, p)
        rc, out = self.check()
        self.assertEqual(rc, self.m.EXIT_SKIPPED)
        self.assertIn("skipped: drive", out)

    def test_a_quiet_drive_report_is_green(self):
        # A leg that found nothing still reports — it says so under `To open` / `To resume`
        # with no rows. A predicate demanding a non-empty row set would call this round
        # skipped, which is the permanent false red the tuple exists to avoid.
        self.full_round()
        self.record("drive", self.artifact("quiet-drive.txt", DRIVE_QUIET))
        rc, out = self.check()
        self.assertEqual(rc, 0)
        self.assertEqual(out, "")

    def test_a_stub_drive_artifact_is_red(self):
        # The shortcut this check exists to catch: record a path, write nothing real.
        self.full_round()
        self.record("drive", self.artifact("stub-drive.txt", "done\n"))
        rc, out = self.check()
        self.assertEqual(rc, self.m.EXIT_SKIPPED)
        self.assertIn("skipped: drive", out)

    def test_an_empty_drive_artifact_is_red(self):
        self.full_round()
        self.record("drive", self.artifact("empty-drive.txt", "   \n\n"))
        rc, out = self.check()
        self.assertEqual(rc, self.m.EXIT_SKIPPED)
        self.assertIn("skipped: drive", out)

    # --- the topic tier --------------------------------------------------------

    def topic_check(self):
        return self.cli("--check", "--topic", self.topic, "--tier", "topic")

    def test_a_topic_round_owes_no_drive_leg(self):
        # A topic round has no drive row at all (`commands/topic-drive.md` row 5 is
        # *Keep alive*), so it must not be asked for one. Without the tier split this
        # round would print `skipped: drive` forever — the permanent false red that
        # `FLEET_STEPS`' own comment names.
        self.full_round()
        rc, out = self.topic_check()
        self.assertEqual(rc, 0)
        self.assertEqual(out, "")

    def test_a_topic_round_still_owes_its_sweep(self):
        # `sweep` stays in the topic tuple: the topic round does sweep every tick, so
        # dropping the step would let a round that skipped its sweep report complete —
        # this check's own defect, one tier over.
        self.full_round()
        rc, out = self.topic_check()
        self.assertEqual(rc, 0)
        d = self.m.read_receipt(self.m.slug(self.topic))
        d["fields"].pop("sweep")
        self.m.write_atomic(self.m.path(self.m.slug(self.topic)), json.dumps(d) + "\n")
        rc, out = self.topic_check()
        self.assertEqual(rc, self.m.EXIT_SKIPPED)
        self.assertIn("skipped: sweep", out)
        self.assertNotIn("skipped: drive", out)

    def test_the_tier_selects_the_tuple_and_never_the_slug(self):
        # A `--tier` that reached `slug()` would re-key every receipt already on disk
        # from `<base>` to `topic--<base>` and orphan it.
        self.assertEqual(self.m.steps_for("", "goal"), self.m.STEPS)
        self.assertEqual(self.m.steps_for("", "topic"), self.m.TOPIC_STEPS)
        self.assertIn("drive", self.m.STEPS)
        self.assertNotIn("drive", self.m.TOPIC_STEPS)
        self.assertEqual(self.m.slug(self.topic, ""), self.m.slug(self.topic))
        self.assertNotIn("topic", self.m.BRANCHES)

    def test_record_drive_is_accepted_on_the_goal_tier_and_refused_on_topic(self):
        self.start()
        p = self.artifact("drive.txt", DRIVE_OK)
        rc, _ = self.record("drive", p)
        self.assertEqual(rc, 0)
        with self.assertRaises(SystemExit):
            self.cli("--record", "--topic", self.topic, "--tier", "topic",
                     "--step", "drive", "--path", p)

    def test_a_partial_feed_read_is_a_completed_reading(self):
        """`partial` is a read that RAN, so it must not be flagged as a skipped step.

        The rows it resolved keep their verdict and only the rest are `unread`, so the
        round did its work. Rejecting the value here flagged every such round
        `INCOMPLETE ROUND — skipped: readings` and recorded false debt for a reading
        that exists.
        """
        self.full_round()
        partial = json.loads(READINGS_OK)
        partial["feed_state"] = "partial"
        rd = self.artifact("readings.json", json.dumps(partial))
        self.record("readings", rd)
        rc, out = self.check()
        self.assertEqual(rc, 0, out)

    def test_an_unknown_feed_state_is_flagged(self):
        """The negative control: the vocabulary is still closed."""
        self.full_round()
        bad = json.loads(READINGS_OK)
        bad["feed_state"] = "partial-ish"
        rd = self.artifact("readings.json", json.dumps(bad))
        self.record("readings", rd)
        rc, out = self.check()
        self.assertEqual(rc, self.m.EXIT_SKIPPED)
        self.assertIn("skipped: readings", out)

    # --- fabricated receipts: the four cases subtask 4 names --------------------

    def test_sweep_payload_outside_the_round_window_is_flagged(self):
        self.start()
        ny = self.artifact("needs-you.txt", NEEDS_YOU_OK)
        sw = self.artifact("sweep.json", SWEEP_OK, age=3600)  # written before the round
        ev = self.events_log("events.jsonl", [time.time()])
        for step, p in (("needs-you", ny), ("sweep", sw), ("watcher", ev)):
            self.record(step, p)
        rc, out = self.check()
        self.assertEqual(rc, self.m.EXIT_SKIPPED)
        self.assertIn("skipped: sweep", out)
        self.assertNotIn("skipped: needs-you", out)

    def test_needs_you_path_outside_the_round_window_is_flagged(self):
        self.start()
        ny = self.artifact("needs-you.txt", NEEDS_YOU_OK, age=3600)
        sw = self.artifact("sweep.json", SWEEP_OK)
        ev = self.events_log("events.jsonl", [time.time()])
        for step, p in (("needs-you", ny), ("sweep", sw), ("watcher", ev)):
            self.record(step, p)
        rc, out = self.check()
        self.assertEqual(rc, self.m.EXIT_SKIPPED)
        self.assertIn("skipped: needs-you", out)

    def test_in_window_but_empty_files_are_flagged(self):
        # `touch` — the shortcut a mtime-only probe passes. Every field present,
        # every named file fresh, every named file EMPTY.
        self.start()
        ny = self.artifact("needs-you.txt", "")
        sw = self.artifact("sweep.json", "")
        ev = self.events_log("events.jsonl", [time.time()])
        for step, p in (("needs-you", ny), ("sweep", sw), ("watcher", ev)):
            self.record(step, p)
        rc, out = self.check()
        self.assertEqual(rc, self.m.EXIT_SKIPPED)
        self.assertIn("skipped: needs-you", out)
        self.assertIn("skipped: sweep", out)

    def test_in_window_non_empty_wrong_shape_is_flagged(self):
        # A round-written stub: fresh, non-empty, and not the producer's output.
        # This is the case that separates "non-empty" from "the producer's shape".
        self.start()
        ny = self.artifact("needs-you.txt", "done\n")
        # The shape the OLD predicate demanded — JSON with a `header` key — is now the
        # canonical wrong shape, because no producer writes it.
        sw = self.artifact("sweep.json", json.dumps({"header": ["Topic"], "rows": []}))
        ev = self.events_log("events.jsonl", [time.time()])
        for step, p in (("needs-you", ny), ("sweep", sw), ("watcher", ev)):
            self.record(step, p)
        rc, out = self.check()
        self.assertEqual(rc, self.m.EXIT_SKIPPED)
        self.assertIn("skipped: needs-you", out)
        self.assertIn("skipped: sweep", out)

    # --- each predicate against its OWN producer, and against the rejection case --
    #
    # These are the criteria's load-bearing half. A predicate that simply returns True
    # passes the positive case below and fails the negative one; a predicate that
    # keeps the old type check passes the negative one and fails the positive. Only a
    # predicate matched to what the producer actually writes passes both.

    def test_sweep_shape_accepts_a_payload_written_by_write_payload(self):
        # The full producer chain, no hand-built fixture: box-table.py renders the
        # frame, manager-predispatch.py --write-payload stores it, and the predicate
        # reads the stored file back — the same three steps the round performs.
        rendered = render_table(["Topic", "Status"], [["Recurring Operator Gates", "ok"]])
        with tempfile.TemporaryDirectory() as state, tempfile.TemporaryDirectory() as vault:
            # `--vault` is validated as a vault root; the payload lands in the state dir.
            os.makedirs(os.path.join(vault, "24 Goals"))
            r = subprocess.run(
                [sys.executable, PREDISPATCH, "--write-payload",
                 "--subject", "Recurring Operator Gates", "--vault", vault],
                input=rendered, capture_output=True, text=True,
                env=dict(os.environ, MANAGER_PREDISPATCH_STATE_DIR=state),
            )
            self.assertEqual(r.returncode, 0, r.stderr)
            stored = os.path.join(state, "recurring-operator-gates.table")
            with open(stored, encoding="utf-8") as fh:
                text = fh.read()
        self.assertTrue(self.m.sweep_shape(text), text[:200])

    def test_sweep_shape_accepts_a_marker_prefixed_payload(self):
        # The tick's output leads with its liveness marker and hangs the frame two
        # spaces under it (`docs/sweep-output.md` § Print the full table on every
        # tick), so a payload captured from a tick carries BOTH. Measured 2026-10-18/19:
        # consecutive rounds twenty minutes apart produced one payload of each shape on
        # the same install, so a predicate matching only the bare frame flaps. This is
        # the second shape, run through the same producer chain as the first.
        rendered = render_table(["Topic", "Status"], [["Recurring Operator Gates", "ok"]])
        marker = "2026-10-19T00:15:00+02:00 ✓ Recurring Operator Gates — 4 tasks · 1 ⌛"
        tick = marker + "\n" + "\n".join("  " + ln for ln in rendered.splitlines())
        with tempfile.TemporaryDirectory() as state, tempfile.TemporaryDirectory() as vault:
            os.makedirs(os.path.join(vault, "24 Goals"))
            r = subprocess.run(
                [sys.executable, PREDISPATCH, "--write-payload",
                 "--subject", "Recurring Operator Gates", "--vault", vault],
                input=tick, capture_output=True, text=True,
                env=dict(os.environ, MANAGER_PREDISPATCH_STATE_DIR=state),
            )
            self.assertEqual(r.returncode, 0, r.stderr)
            stored = os.path.join(state, "recurring-operator-gates.table")
            with open(stored, encoding="utf-8") as fh:
                text = fh.read()
        self.assertTrue(text.startswith(marker), text[:200])
        self.assertTrue(self.m.sweep_shape(text), text[:200])

    def test_sweep_shape_rejects_json_empty_and_truncated(self):
        # (a) the JSON the old predicate demanded — the shape no producer writes.
        self.assertFalse(self.m.sweep_shape(json.dumps({"header": ["Topic"], "rows": []})))
        # (b) an empty file, and a frame truncated before its bottom rule.
        self.assertFalse(self.m.sweep_shape(""))
        frame = render_table(["Topic"], [["row"]])
        lines = [ln for ln in frame.splitlines() if ln.strip()]
        self.assertFalse(self.m.sweep_shape("\n".join(lines[:-1])), "bottom rule dropped")
        # (c) a marker with no frame under it, and a marker-prefixed frame that lost
        # its bottom rule — the marker must not become a way to smuggle a stub in.
        marker = "2026-10-19T00:15:00+02:00 ✓ Recurring Operator Gates — 4 tasks"
        self.assertFalse(self.m.sweep_shape(marker), "marker alone")
        self.assertFalse(self.m.sweep_shape(marker + "\n" + "\n".join(lines[:-1])))
        # (d) arbitrary prose ahead of a real frame is not the producer's marker.
        self.assertFalse(self.m.sweep_shape("stub\n" + frame))
        # A `return True` stub fails every line above; so does one that only looks for
        # `│` anywhere, which a truncated frame still carries.

    def test_watcher_shape_accepts_a_log_written_by_the_attention_watch(self):
        # Call the producer's own writer, not a fixture: log_event is what appends
        # every LIVENESS record the arm produces.
        mw = load_module("manager_attention_watch", ATTENTION_WATCH)
        p = os.path.join(self.arts.name, "events.jsonl")
        now = time.time()
        mw.log_event(p, "LIVENESS", "", "recurring-operator-gates", "0 gated · 33 held")
        self.assertTrue(self.m.watcher_shape(p, now - 5, now + 5))

    def test_watcher_shape_rejects_every_record_outside_the_window(self):
        # The window excludes the log's newest record: the arm polled, but not this
        # round. This is the rejection half — a predicate that accepts any parseable
        # ts regardless of the window fails here.
        mw = load_module("manager_attention_watch", ATTENTION_WATCH)
        p = os.path.join(self.arts.name, "events.jsonl")
        mw.log_event(p, "LIVENESS", "", "recurring-operator-gates", "0 gated")
        now = time.time()
        self.assertFalse(self.m.watcher_shape(p, now - 7200, now - 3600))

    # --- the heartbeat reads the record's ts, not the file's mtime -------------

    def test_watcher_reads_record_ts_not_file_mtime(self):
        # A fresh mtime over an arm that stopped polling: the file was touched, the
        # arm was not alive. Only the newest RECORD's ts can tell these apart.
        self.start()
        ny = self.artifact("needs-you.txt", NEEDS_YOU_OK)
        sw = self.artifact("sweep.json", SWEEP_OK)
        ev = self.events_log("events.jsonl", [time.time() - 3600])  # fresh mtime
        for step, p in (("needs-you", ny), ("sweep", sw), ("watcher", ev)):
            self.record(step, p)
        rc, out = self.check()
        self.assertEqual(rc, self.m.EXIT_SKIPPED)
        self.assertIn("skipped: watcher", out)

    def test_watcher_absent_log_is_flagged(self):
        self.start()
        ny = self.artifact("needs-you.txt", NEEDS_YOU_OK)
        sw = self.artifact("sweep.json", SWEEP_OK)
        for step, p in (("needs-you", ny), ("sweep", sw)):
            self.record(step, p)
        self.record("watcher", os.path.join(self.arts.name, "nope.jsonl"))
        rc, out = self.check()
        self.assertEqual(rc, self.m.EXIT_SKIPPED)
        self.assertIn("skipped: watcher", out)

    def test_watcher_empty_log_is_flagged(self):
        # The arm armed but never appended: the file exists and its mtime is fresh,
        # so an existence-or-recency check passes while nothing ever polled. Only
        # the absent RECORD tells this apart from a live arm.
        self.start()
        ny = self.artifact("needs-you.txt", NEEDS_YOU_OK)
        sw = self.artifact("sweep.json", SWEEP_OK)
        ev = self.artifact("events.jsonl", "")
        for step, p in (("needs-you", ny), ("sweep", sw), ("watcher", ev)):
            self.record(step, p)
        rc, out = self.check()
        self.assertEqual(rc, self.m.EXIT_SKIPPED)
        self.assertIn("skipped: watcher", out)

    def test_watcher_malformed_records_do_not_mask_a_live_one(self):
        # A torn line must be SKIPPED, not fatal: the arm appends while the manager
        # reads, so a half-flushed record is an ordinary state and must not hide the
        # good record beside it.
        self.start()
        ny = self.artifact("needs-you.txt", NEEDS_YOU_OK)
        sw = self.artifact("sweep.json", SWEEP_OK)
        ev = os.path.join(self.arts.name, "events.jsonl")
        with open(ev, "w", encoding="utf-8") as fh:
            fh.write('{"kind": "LIVENESS", "ts": \n')  # torn write
            fh.write("not json at all\n")              # garbage
            fh.write("\n")                             # blank line
            fh.write(json.dumps({"kind": "LIVENESS", "ts": time.time()}) + "\n")
        rd = self.artifact("readings.json", READINGS_OK)
        dv = self.artifact("drive.txt", DRIVE_OK)
        for step, p in (("needs-you", ny), ("sweep", sw), ("watcher", ev),
                        ("readings", rd), ("drive", dv)):
            self.record(step, p)
        rc, out = self.check()
        self.assertEqual(rc, 0, out)

    def test_watcher_log_of_only_malformed_records_is_flagged(self):
        self.start()
        ny = self.artifact("needs-you.txt", NEEDS_YOU_OK)
        sw = self.artifact("sweep.json", SWEEP_OK)
        ev = os.path.join(self.arts.name, "events.jsonl")
        with open(ev, "w", encoding="utf-8") as fh:
            fh.write('{"kind": "LIVENESS", "ts": \n')          # torn write
            fh.write('{"kind": "LIVENESS"}\n')                 # no ts at all
            fh.write('{"kind": "LIVENESS", "ts": "soon"}\n')   # non-numeric ts
        for step, p in (("needs-you", ny), ("sweep", sw), ("watcher", ev)):
            self.record(step, p)
        rc, out = self.check()
        self.assertEqual(rc, self.m.EXIT_SKIPPED)
        self.assertIn("skipped: watcher", out)

    def test_watcher_future_record_is_not_in_the_window(self):
        # Clock skew, or a hand-written record: a ts AFTER `now` is not evidence the
        # arm polled during this round.
        self.start()
        ny = self.artifact("needs-you.txt", NEEDS_YOU_OK)
        sw = self.artifact("sweep.json", SWEEP_OK)
        ev = self.events_log("events.jsonl", [time.time() + 3600])
        for step, p in (("needs-you", ny), ("sweep", sw), ("watcher", ev)):
            self.record(step, p)
        rc, out = self.check()
        self.assertEqual(rc, self.m.EXIT_SKIPPED)
        self.assertIn("skipped: watcher", out)

    # --- the round window and receipt lifecycle --------------------------------

    def test_a_receipt_cannot_inherit_the_previous_rounds_artifacts(self):
        # Round 1 does its work; round 2 calls --start and skips everything. If
        # --start carried the fields forward, round 2 would pass on round 1's
        # artifacts — the exact silence this check exists to break.
        self.full_round()
        self.assertEqual(self.check()[0], 0)
        self.start()
        rc, out = self.check()
        self.assertEqual(rc, self.m.EXIT_SKIPPED)
        for step in self.m.STEPS:
            self.assertIn(f"skipped: {step}", out)

    def test_stale_round_window_is_flagged_once_not_per_step(self):
        self.full_round()
        # Age the whole round past its limit: every field is unjudgeable at once.
        p = self.m.path(self.m.slug(self.topic))
        with open(p) as fh:
            d = json.load(fh)
        d["round_started_at"] = time.time() - 100000
        with open(p, "w") as fh:
            fh.write(json.dumps(d))
        rc, out = self.check()
        self.assertEqual(rc, self.m.EXIT_SKIPPED)
        self.assertIn("skipped: receipt", out)

    def test_record_without_a_round_refuses(self):
        # A receipt with no start cannot say which round an artifact belongs to,
        # which is the whole question — refuse rather than invent a window.
        rc, _ = self.record("needs-you", "/tmp/whatever")
        self.assertEqual(rc, 2)

    def test_staleness_uses_the_liveness_observed_basis(self):
        # Same basis as manager-liveness.py: a manager arming every 17 min while
        # reporting 300 s must not have its round window read as lapsed.
        now = time.time()
        with open(os.path.join(self.live.name, "recurring-operator-gates.arms"), "w") as fh:
            fh.write("".join(f"{now - o:.0f}\n" for o in (3760, 2740, 1720, 700)))
        with open(os.path.join(self.live.name, "recurring-operator-gates.cadence"), "w") as fh:
            fh.write("300\n")
        self.full_round()
        p = self.m.path(self.m.slug(self.topic))
        with open(p) as fh:
            d = json.load(fh)
        d["round_started_at"] = now - 700  # < 1020 + 180
        with open(p, "w") as fh:
            fh.write(json.dumps(d))
        # The artifacts were written "now", so re-age them into the round window.
        for step in ("needs-you", "sweep"):
            fp = d["fields"][step]
            os.utime(fp, (now - 700, now - 700))
        d["fields"]["watcher"] = self.events_log("events2.jsonl", [now - 700])
        with open(p, "w") as fh:
            fh.write(json.dumps(d))
        rc, out = self.check()
        self.assertEqual(rc, 0, out)

    # --- the aligned window: one rule, and the boundary is the limit -------------

    def _history(self, name, now, gaps, cadence=None):
        """Write `<name>.arms` whose arm-to-arm gaps are exactly `gaps`, ending at `now`."""
        vals = [now]
        for g in reversed(gaps):
            vals.insert(0, vals[0] - g)
        with open(os.path.join(self.live.name, name + ".arms"), "w", encoding="utf-8") as fh:
            fh.write("".join(f"{v:.0f}\n" for v in vals))
        if cadence is not None:
            with open(os.path.join(self.live.name, name + ".cadence"), "w", encoding="utf-8") as fh:
                fh.write(f"{cadence}\n")
        return vals

    def _set_started(self, started):
        p = self.m.path(self.m.slug(self.topic))
        with open(p, encoding="utf-8") as fh:
            d = json.load(fh)
        d["round_started_at"] = started
        with open(p, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(d))
        return d

    def _check_at(self, now):
        """`check()` at an explicit `now`, with its stdout captured.

        `cli()` cannot do this: it goes through `main()`, which reads the clock.
        """
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = self.m.check(self.topic, now=now)
        return rc, buf.getvalue()

    def test_window_boundary_is_inclusive_at_the_limit(self):
        """`now - started == limit` is still inside the round; `limit + 1` is not.

        Pinned because the direction is invisible in a passing run: flipping `>` to
        `>=` would report a lapsed receipt for a round that arrived exactly on time,
        and every other test in this file would still pass.
        """
        now = 1_700_000_000.0
        gap = 900
        self._history("recurring-operator-gates", now, [gap, gap, gap], cadence=300)
        self.start()
        limit = gap + 180  # p90 == median == the only gap, so 1x + 180
        s = self.m.slug(self.topic)

        self._set_started(now - limit)
        self.assertEqual(self.m.window(s, now)[1], limit)
        _, out = self._check_at(now)
        self.assertNotIn("skipped: receipt", out, "a round landing exactly on its limit is judged")
        self._set_started(now - limit - 1)
        rc, out = self._check_at(now)
        self.assertEqual(rc, self.m.EXIT_SKIPPED)
        self.assertIn(f"limit {limit}s period={gap}s(observed,1x+180)", out)

    def test_period_is_read_from_the_branch_prefixed_record(self):
        """The liveness record has carried a `goal--` / `topic--` prefix since the
        goal/topic split; this file's slug never did.

        Measured 2026-10-21: **4 of the 9** live receipts named a subject whose only
        liveness record was prefixed, so the bare-base read found no history, the
        period came back unknown, and the limit silently fell back to the reported
        interval — a different number from the one `manager-liveness.py` used for the
        same subject. Pinned because the fallback is silent: the receipt still prints
        a plausible limit.
        """
        now = 1_700_000_000.0
        gap = 1200
        # ONLY the prefixed record exists; the bare base has nothing.
        self._history("goal--recurring-operator-gates", now, [gap, gap, gap], cadence=300)
        self.start()
        self._set_started(now - 10)

        s = self.m.slug(self.topic)
        self.assertEqual(self.m.liveness_slug(s), "goal--recurring-operator-gates")
        _, limit, basis = self.m.window(s, now)
        self.assertEqual(limit, gap + 180)
        self.assertEqual(basis, f"period={gap}s(observed,1x+180)")
        self.assertNotIn("reported", basis)

        # The claim this task repairs, asserted rather than written in prose: the
        # receipt's limit IS the liveness limit for the same subject.
        ml = self.m.liveness()
        self.assertEqual(limit, ml.observed_period("goal--recurring-operator-gates") + ml.SLACK)

    def test_an_already_prefixed_slug_is_not_prefixed_again(self):
        """A fleet round puts its own branch on the receipt slug, so the period must come
        from `fleet--<base>` — the record the arm step actually wrote.

        Covers the fleet tier's observed basis end to end; no other test reaches it.
        The early return in `liveness_slug()` that skips re-probing an already-prefixed
        slug is clarity, not a fix: without it the bad probes find nothing and the
        fallback returns the same name, so this test does not discriminate on it.
        """
        now = 1_700_000_000.0
        gap = 1200
        self._history("fleet--fleet-manager", now, [gap, gap, gap], cadence=300)

        fleet_slug = self.m.slug("Fleet Manager", "fleet")
        self.assertEqual(fleet_slug, "fleet--fleet-manager")
        self.assertEqual(self.m.liveness_slug(fleet_slug), "fleet--fleet-manager")

        with open(self.m.path(fleet_slug), "w", encoding="utf-8") as fh:
            json.dump({"topic": "Fleet Manager", "round_started_at": now - 10, "fields": {}}, fh)
        _, limit, basis = self.m.window(fleet_slug, now)
        self.assertEqual(limit, gap + 180)
        self.assertEqual(basis, f"period={gap}s(observed,1x+180)")

    def test_printed_limit_never_understates_a_fractional_boundary(self):
        """`observed_period()` interpolates a p90, so the limit is not always an integer.

        Truncating it on the way out lets a refused round print a limit below the
        enforced boundary, so the two printed numbers read as *not* lapsed. The invariant
        pinned here is one-directional: the printed limit is never below the enforced
        one, so rounding up (which can overstate by <1 s) is correct and truncating is
        not.
        """
        now = 1_700_000_000.0
        # p90 of (1000, 1101, 1200) interpolates to 1180.2, so the limit is 1360.2.
        self._history("recurring-operator-gates", now, [1000, 1101, 1200], cadence=300)
        self.start()
        self._set_started(now - 1400)

        _, limit, _ = self.m.window(self.m.slug(self.topic), now)
        self.assertNotEqual(limit, int(limit), "fixture must produce a fractional limit")

        rc, out = self._check_at(now)
        self.assertEqual(rc, self.m.EXIT_SKIPPED)
        printed = int(out.split("limit ")[1].split("s")[0])
        self.assertGreaterEqual(printed, limit)

    def test_reported_fallback_is_the_interval_plus_the_same_slack(self):
        """With no `.arms` history the limit is `interval + SLACK`, not a multiple of it.

        This is the branch every subject spends its first three arms in, and nothing else
        pins it: every other test supplies history, so reverting the fallback to a `2 x`
        multiple — or reading the wrong file for the declared cadence — would pass the
        whole suite. It also pins `liveness_slug()`'s bare-base return, the other half of
        the same fallback.
        """
        now = 1_700_000_000.0
        self.start()
        self._set_started(now - 10)
        # No `.arms` at all; only the declared cadence exists.
        with open(os.path.join(self.live.name, "recurring-operator-gates.cadence"), "w") as fh:
            fh.write("900\n")

        s = self.m.slug(self.topic)
        self.assertEqual(self.m.liveness_slug(s), s, "no prefixed record -> the bare base")
        _, limit, basis = self.m.window(s, now)
        self.assertEqual(limit, 900 + 180)
        self.assertEqual(basis, "period=900s(reported,1x+180)")



FLEET_SUBJECT = "Fleet Manager"
FLEET_SLUG = "fleet--fleet-manager"


class FleetTier(unittest.TestCase):
    """SC2a — the fleet round's own two steps, and the slug that makes them readable.

    The fleet round writes its liveness under `fleet--fleet-manager` (the slug
    `manager-liveness.py:101` builds from `--branch fleet`), so a receipt keyed on the
    bare `fleet-manager` reads a file the arm step never wrote and reports
    `skipped: re-arm` on a round that armed perfectly. Both halves are pinned here.

    Measured 2026-10-30: the fleet round ran its arm step twelve times across a window
    in which no digest was written at all, and nothing on that round's own tail said so.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.live = tempfile.TemporaryDirectory()
        self.arts = tempfile.TemporaryDirectory()
        self.m = load(self.tmp.name, self.live.name)

    def tearDown(self):
        for t in (self.tmp, self.live, self.arts):
            t.cleanup()
        os.environ.pop("MANAGER_RECEIPT_STATE_DIR", None)
        os.environ.pop("MANAGER_LIVENESS_STATE_DIR", None)

    # --- helpers ---------------------------------------------------------------

    def artifact(self, name, text, age=0.0):
        p = os.path.join(self.arts.name, name)
        with open(p, "w", encoding="utf-8") as fh:
            fh.write(text)
        if age:
            t = time.time() - age
            os.utime(p, (t, t))
        return p

    def cli(self, *argv):
        out, err, old = io.StringIO(), io.StringIO(), sys.argv
        sys.argv = ["manager-receipt.py", *argv]
        try:
            with redirect_stdout(out), redirect_stderr(err):
                rc = self.m.main()
        finally:
            sys.argv = old
        return rc, out.getvalue()

    def start(self):
        return self.cli("--start", "--subject", FLEET_SUBJECT, "--branch", "fleet")

    def record(self, step, path):
        return self.cli("--record", "--subject", FLEET_SUBJECT, "--branch", "fleet",
                        "--step", step, "--path", path)

    def check(self):
        return self.cli("--check", "--subject", FLEET_SUBJECT, "--branch", "fleet")

    def arms(self, epochs):
        """An `.arms` ring in `manager-liveness.py`'s own format: one epoch per line."""
        return self.artifact("fleet--fleet-manager.arms",
                             "".join("%.0f\n" % e for e in epochs))

    def digest(self, text=None, name="20261030T232317.digest"):
        """A digest in the sweep reader's rendered shape — its real first line."""
        return self.artifact(
            name,
            text if text is not None else
            "DIGEST 20261030T232317 · 39 sessions · snapshot written: "
            "2026-10-30T23:26:50Z\n",
        )

    def full_round(self):
        self.start()
        self.record("re-arm", self.arms([time.time()]))
        self.record("digest", self.digest())

    # --- the slug --------------------------------------------------------------

    def test_fleet_slug_carries_the_branch(self):
        self.assertEqual(self.m.slug(FLEET_SUBJECT, "fleet"), FLEET_SLUG)
        # The unprefixed slug is what every goal/topic receipt already uses, unchanged.
        self.assertEqual(self.m.slug("Recurring Operator Gates"), "recurring-operator-gates")

    def test_fleet_receipt_lands_under_the_branch_slug(self):
        self.start()
        self.assertTrue(os.path.exists(os.path.join(self.tmp.name, FLEET_SLUG + ".json")))
        self.assertFalse(os.path.exists(os.path.join(self.tmp.name, "fleet-manager.json")))

    # --- green -----------------------------------------------------------------

    def test_full_fleet_round_is_green(self):
        self.full_round()
        rc, out = self.check()
        self.assertEqual(rc, 0, out)
        self.assertEqual(out, "")

    # --- the two steps this tier owes ------------------------------------------

    def test_missing_arm_is_red(self):
        self.start()
        self.record("digest", self.digest())
        rc, out = self.check()
        self.assertEqual(rc, self.m.EXIT_SKIPPED)
        self.assertIn("skipped: re-arm", out)

    def test_missing_digest_is_red(self):
        self.start()
        self.record("re-arm", self.arms([time.time()]))
        rc, out = self.check()
        self.assertEqual(rc, self.m.EXIT_SKIPPED)
        self.assertIn("skipped: digest", out)

    def test_arm_ring_with_no_epoch_inside_the_window_is_red(self):
        # The ring survived from an earlier round. Its existence proves nothing; only
        # an epoch inside THIS round does.
        self.start()
        self.record("re-arm", self.arms([time.time() - 7200, time.time() - 5400]))
        self.record("digest", self.digest())
        rc, out = self.check()
        self.assertEqual(rc, self.m.EXIT_SKIPPED)
        self.assertIn("skipped: re-arm", out)

    def test_wiped_arm_ring_is_red(self):
        # `manager-liveness.py:345` empties `prior` at the first arm after a stand-down,
        # so an empty ring is the real shape of "the arm step did not run this round".
        self.start()
        self.record("re-arm", self.arms([]))
        self.record("digest", self.digest())
        rc, out = self.check()
        self.assertEqual(rc, self.m.EXIT_SKIPPED)
        self.assertIn("skipped: re-arm", out)

    def test_stub_digest_is_red(self):
        self.start()
        self.record("re-arm", self.arms([time.time()]))
        self.record("digest", self.digest("ok\n"))
        rc, out = self.check()
        self.assertEqual(rc, self.m.EXIT_SKIPPED)
        self.assertIn("skipped: digest", out)

    def test_digest_outside_the_round_window_is_red(self):
        self.start()
        self.record("re-arm", self.arms([time.time()]))
        p = self.digest()
        t = time.time() - 7200
        os.utime(p, (t, t))
        self.record("digest", p)
        rc, out = self.check()
        self.assertEqual(rc, self.m.EXIT_SKIPPED)
        self.assertIn("skipped: digest", out)

    # --- the regression guard --------------------------------------------------

    def test_goal_tier_still_owes_only_its_own_steps(self):
        # Adding the fleet steps to the goal/topic tuple would make every such round
        # report a permanent `skipped: digest` — a false red on a healthy round.
        self.assertNotIn("re-arm", self.m.STEPS)
        self.assertNotIn("digest", self.m.STEPS)
        self.assertEqual(self.m.steps_for(""), self.m.STEPS)
        self.assertEqual(self.m.steps_for("fleet"), ("re-arm", "digest"))

    def test_topic_and_branch_together_select_the_fleet_tier(self):
        # `--topic` is the older spelling of `--subject`; combined with `--branch` it
        # must resolve the fleet slug, not silently fall back to the unprefixed one.
        self.cli("--start", "--topic", FLEET_SUBJECT, "--branch", "fleet")
        self.assertTrue(os.path.exists(os.path.join(self.tmp.name, FLEET_SLUG + ".json")))

    def test_an_unknown_branch_is_refused(self):
        # manager-liveness.py validates its branch before building the slug; this module
        # claims parity, and an unvalidated branch interpolates into the state path.
        with self.assertRaises(ValueError):
            self.m.slug(FLEET_SUBJECT, "fleet/../../etc")

    def test_an_unknown_branch_is_a_usage_error_not_a_traceback(self):
        # The library guard alone would surface as an uncaught ValueError (exit 1 with a
        # traceback); the CLI owes an argparse usage error.
        with self.assertRaises(SystemExit) as caught:
            self.cli("--check", "--subject", FLEET_SUBJECT, "--branch", "fleet/../../etc")
        self.assertEqual(caught.exception.code, 2)

    def test_a_fleet_step_cannot_be_recorded_on_a_goal_receipt(self):
        # `steps_for("")` never looks for `digest`, so recording one there would be a
        # dead field the tail silently ignores.
        self.cli("--start", "--topic", "Recurring Operator Gates")
        with self.assertRaises(SystemExit) as caught:
            self.cli("--record", "--topic", "Recurring Operator Gates",
                     "--step", "digest", "--path", "/tmp/whatever")
        self.assertEqual(caught.exception.code, 2)

    def test_topic_check_never_reports_fleet_steps(self):
        self.cli("--start", "--topic", "Recurring Operator Gates")
        rc, out = self.cli("--check", "--topic", "Recurring Operator Gates")
        self.assertEqual(rc, self.m.EXIT_SKIPPED)
        self.assertNotIn("skipped: re-arm", out)
        self.assertNotIn("skipped: digest", out)


class FleetDebt(unittest.TestCase):
    """The fleet tier's incomplete-round sentinel, and the debt it carries.

    On `origin/master` the tail prints `skipped: <step> — <why>` and nothing else, and
    the skip dies with the receipt: `--start` clears `fields`, so one round later the
    evidence is gone and a round that skipped is indistinguishable from one that did
    not. These pin the three properties that close that gap — a positive sentinel, a
    debt that outlives the receipt, and exactly one documented way to pay it.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.live = tempfile.TemporaryDirectory()
        self.arts = tempfile.TemporaryDirectory()
        self.m = load(self.tmp.name, self.live.name)

    def tearDown(self):
        for t in (self.tmp, self.live, self.arts):
            t.cleanup()
        os.environ.pop("MANAGER_RECEIPT_STATE_DIR", None)
        os.environ.pop("MANAGER_LIVENESS_STATE_DIR", None)

    # --- helpers ---------------------------------------------------------------

    def artifact(self, name, text):
        p = os.path.join(self.arts.name, name)
        with open(p, "w", encoding="utf-8") as fh:
            fh.write(text)
        return p

    def cli(self, *argv):
        out, err, old = io.StringIO(), io.StringIO(), sys.argv
        sys.argv = ["manager-receipt.py", *argv]
        try:
            with redirect_stdout(out), redirect_stderr(err):
                rc = self.m.main()
        finally:
            sys.argv = old
        return rc, out.getvalue()

    def start(self):
        return self.cli("--start", "--subject", FLEET_SUBJECT, "--branch", "fleet")

    def record(self, step, path):
        return self.cli("--record", "--subject", FLEET_SUBJECT, "--branch", "fleet",
                        "--step", step, "--path", path)

    def check(self):
        return self.cli("--check", "--subject", FLEET_SUBJECT, "--branch", "fleet")

    def debt(self):
        return self.cli("--debt", "--subject", FLEET_SUBJECT, "--branch", "fleet")

    def abandon(self, step):
        return self.cli("--abandon", "--subject", FLEET_SUBJECT, "--branch", "fleet",
                        "--step", step)

    def arms(self, epochs):
        return self.artifact("fleet--fleet-manager.arms",
                             "".join("%.0f\n" % e for e in epochs))

    def digest(self):
        return self.artifact(
            "20261030T232317.digest",
            "DIGEST 20261030T232317 · 39 sessions · snapshot written: "
            "2026-10-30T23:26:50Z\n",
        )

    def skipped_round(self):
        """The measured defect: the arm ran, the digest never did."""
        self.start()
        self.record("re-arm", self.arms([time.time()]))

    def clean_round(self):
        self.start()
        self.record("re-arm", self.arms([time.time()]))
        self.record("digest", self.digest())

    def digest_only_round(self):
        """The mirror of `skipped_round`: the digest ran, the arm did not."""
        self.start()
        self.record("digest", self.digest())

    # --- the sentinel ----------------------------------------------------------

    def test_a_round_that_armed_and_never_digested_prints_the_sentinel(self):
        # A POSITIVE line, not an absence: the pre-change rounds already omitted
        # `Snapshot written` (measured 2026-10-31), so an absence discriminates nothing.
        self.skipped_round()
        rc, out = self.check()
        self.assertEqual(rc, 11, out)
        self.assertIn("INCOMPLETE ROUND — skipped: digest", out)

    def test_the_sentinel_names_digest_as_the_only_skipped_step(self):
        self.skipped_round()
        rc, out = self.check()
        self.assertEqual(rc, 11, out)
        self.assertEqual(out.count("skipped:"), 1, out)

    def test_a_clean_round_prints_no_sentinel(self):
        # The control: without it the sentinel could be a constant and every other
        # test in this class would still pass.
        self.clean_round()
        rc, out = self.check()
        self.assertEqual(rc, 0, out)
        self.assertNotIn("INCOMPLETE ROUND", out)
        self.assertEqual(out, "")

    # --- the debt outlives the receipt -----------------------------------------

    def test_the_debt_outlives_the_next_round_start(self):
        # `--start` clears `fields`, which is why the debt lives in its own file. A
        # debt stored in the receipt is wiped by the very round that must pay it.
        self.skipped_round()
        self.check()
        rc, out = self.debt()
        self.assertEqual(rc, 12, out)
        self.assertIn("digest", out)

        self.start()  # the next round opens; the receipt is cleared
        rc, out = self.debt()
        self.assertEqual(rc, 12, out)
        self.assertIn("digest", out)

    def test_a_clean_round_opens_no_debt(self):
        # The other half of read-vs-constant: a clear debt prints nothing and exits 0.
        self.clean_round()
        self.check()
        rc, out = self.debt()
        self.assertEqual(rc, 0, out)
        self.assertEqual(out.strip(), "")

    # --- the one recovery verb --------------------------------------------------

    def test_abandon_pays_the_debt_and_leaves_the_next_check_green(self):
        self.skipped_round()
        self.check()
        rc, out = self.abandon("digest")
        self.assertEqual(rc, 0, out)
        self.assertIn("abandoned", out)

        rc, out = self.debt()
        self.assertEqual(rc, 0, out)
        self.assertEqual(out.strip(), "")

        rc, out = self.check()
        self.assertEqual(rc, 0, out)

    def test_abandon_records_the_step_it_paid(self):
        self.skipped_round()
        self.check()
        self.abandon("digest")
        p = os.path.join(self.tmp.name, FLEET_SLUG + ".abandoned")
        self.assertTrue(os.path.exists(p))
        with open(p, encoding="utf-8") as fh:
            self.assertIn("digest", fh.read())

    def test_abandon_refuses_a_step_the_receipt_did_not_skip(self):
        # The recovery verb is scoped to a step THIS round skipped. Here `re-arm` is
        # owed and `digest` is not, so paying `digest` must be refused even though a
        # debt is open — otherwise the verb is a way to write anything into the ledger.
        # (The earlier shape of this test ran a clean round, so it only proved the
        # empty-debt refusal and never exercised the scoping it names.)
        self.digest_only_round()
        rc, out = self.check()
        self.assertEqual(rc, 11, out)
        self.assertIn("skipped: re-arm", out)

        rc, _ = self.abandon("digest")
        self.assertEqual(rc, 2)
        rc, out = self.debt()
        self.assertEqual(rc, 12, out)
        self.assertIn("re-arm", out)

    # --- pay by passing ---------------------------------------------------------

    def test_a_clean_round_pays_the_previous_rounds_debt_by_passing(self):
        # The mechanism that actually pays a debt in normal operation: `write_debt`
        # rewrites on every check, so doing the work clears it with no abandon at
        # all. `--abandon` is the exception path, not the everyday one.
        self.skipped_round()
        self.check()
        self.assertEqual(self.debt()[0], 12)

        self.clean_round()
        rc, out = self.check()
        self.assertEqual(rc, 0, out)
        rc, out = self.debt()
        self.assertEqual(rc, 0, out)
        self.assertEqual(out.strip(), "")

    def test_a_second_skipping_check_keeps_the_debt_open(self):
        # The control for pay-by-passing. If `write_debt` appended instead of
        # rewriting, a clean round could never clear the debt; if a repeat skip
        # wrote nothing, the debt would silently vanish after one round.
        self.skipped_round()
        self.check()
        self.skipped_round()
        rc, out = self.check()
        self.assertEqual(rc, 11, out)
        rc, out = self.debt()
        self.assertEqual(rc, 12, out)
        self.assertIn("digest", out)

    # --- the payment is scoped to its round -------------------------------------

    def test_a_round_that_skips_is_gated_by_its_own_receipt(self):
        # The prevention the gate exists for: the sweep is row 2 and the gate is row
        # 5, so a round that skipped its digest must be blocked on ITS OWN receipt —
        # not on whatever the previous round left behind.
        self.skipped_round()
        rc, out = self.debt()
        self.assertEqual(rc, 12, out)
        self.assertIn("digest", out)
        self.assertNotIn("re-arm", out)

    def test_the_gate_judges_the_round_in_progress_not_the_last_one(self):
        # The mirror: a round that HAS swept must not be blocked by a debt it has
        # already paid. A file-only `--debt` fails this, because the file is only
        # rewritten at the tail (row 19) — after the legs (rows 5/5b/5c) have run.
        self.skipped_round()
        self.check()
        self.assertEqual(self.debt()[0], 12)

        self.clean_round()
        rc, out = self.debt()
        self.assertEqual(rc, 0, out)
        self.assertEqual(out.strip(), "")

    # --- a leg gates on the steps it consumes (--only) ---------------------------

    def debt_only(self, *steps):
        argv = ["--debt", "--subject", FLEET_SUBJECT, "--branch", "fleet"]
        for step in steps:
            argv += ["--only", step]
        return self.cli(*argv)

    def test_a_leg_scoped_to_re_arm_runs_while_the_digest_is_still_owed(self):
        # The measured defect, 2026-11-05: row 5b reached the gate with the arm paid
        # and the sweep reader still running, so the whole-round gate read
        # `debt: digest` and skipped the keepalive leg (2 of 25 gate calls). The
        # keepalive procedures never read the digest, so scoped to `re-arm` it runs.
        self.skipped_round()
        rc, out = self.debt()
        self.assertEqual(rc, 12, out)  # the control: unscoped, the leg is stopped
        rc, out = self.debt_only("re-arm")
        self.assertEqual(rc, 0, out)
        self.assertEqual(out.strip(), "")

    def test_a_scoped_gate_still_fires_on_the_step_it_consumes(self):
        # The mirror. Without it `--only` could be a constant 0 and the test above
        # would still pass: a round that never armed must still stop a re-arm leg.
        self.digest_only_round()
        rc, out = self.debt_only("re-arm")
        self.assertEqual(rc, 12, out)
        self.assertIn("debt: re-arm", out)
        self.assertNotIn("digest", out)

    def test_a_scoped_gate_scopes_the_exit_never_the_record(self):
        # `--only` narrows what can stop THIS leg. The debt file is what `--check` and
        # the unscoped legs read, so a scoped call that rewrote it to the subset would
        # silently pay the digest for every other gated leg.
        self.skipped_round()
        self.assertEqual(self.debt_only("re-arm")[0], 0)
        with open(os.path.join(self.tmp.name, FLEET_SLUG + ".debt"), encoding="utf-8") as fh:
            self.assertIn("digest", fh.read())
        rc, out = self.debt()
        self.assertEqual(rc, 12, out)
        self.assertIn("digest", out)

    def test_a_scoped_gate_reads_the_carried_file_when_the_round_is_unjudgeable(self):
        # The other branch of `debt()`: with no receipt the carried file is the only
        # evidence, and the scope must apply there too — in both directions.
        self.skipped_round()
        self.check()
        os.unlink(os.path.join(self.tmp.name, FLEET_SLUG + ".json"))
        self.assertEqual(self.debt_only("re-arm")[0], 0)
        rc, out = self.debt_only("digest")
        self.assertEqual(rc, 12, out)
        self.assertIn("debt: digest", out)

    def test_only_is_a_usage_error_outside_debt(self):
        with self.assertRaises(SystemExit) as caught:
            self.cli("--check", "--subject", FLEET_SUBJECT, "--branch", "fleet",
                     "--only", "re-arm")
        self.assertEqual(caught.exception.code, 2)

    def test_only_refuses_a_step_the_fleet_round_does_not_owe(self):
        # `sweep` is a goal/topic step. Accepting it would scope the gate to a step the
        # fleet debt never contains — a gate that always passes.
        with self.assertRaises(SystemExit) as caught:
            self.debt_only("sweep")
        self.assertEqual(caught.exception.code, 2)

    def test_a_non_finite_abandonment_is_ignored(self):
        # `float()` accepts `inf`, and the honouring test has no upper bound: one
        # tampered line would excuse the step on every later round.
        self.skipped_round()
        self.check()
        p = os.path.join(self.tmp.name, FLEET_SLUG + ".abandoned")
        with open(p, "a", encoding="utf-8") as fh:
            fh.write("inf digest\n")
        rc, out = self.check()
        self.assertEqual(rc, 11, out)
        self.assertIn("skipped: digest", out)

    def test_an_abandonment_stamped_in_the_same_instant_is_honoured(self):
        # The two ends of the window carry the same one-second tolerance. Without
        # the upper slack a payment stamped a fraction before a read — `abandon()`
        # writes "%.3f", and the read follows in microseconds — lands past that
        # read's `now` and the recovery silently fails. This reddened in CI on the
        # first cut of the upper bound, which used a bare `now`.
        self.skipped_round()
        self.check()
        p = os.path.join(self.tmp.name, FLEET_SLUG + ".abandoned")
        with open(p, "a", encoding="utf-8") as fh:
            fh.write("%.3f digest\n" % (time.time() + 0.5))
        rc, out = self.debt()
        self.assertEqual(rc, 0, out)

    def test_an_abandonment_past_the_tolerance_is_rejected(self):
        # The other side of the edge. Without this pair the previous test passes for
        # any tolerance at all, including an unbounded one — it would only prove the
        # happy side.
        #
        # +60, not a value just past the slack: the rejection holds only while `now`
        # is still below the record's stamp minus the slack, so a record dated a
        # couple of seconds ahead tolerates just one second between the write and the
        # read — a stall on a loaded runner would advance `now` past it and flip the
        # test green for the wrong reason. Sixty seconds keeps the boundary check and
        # removes the window; the +0.5 case brackets the other side.
        self.skipped_round()
        self.check()
        p = os.path.join(self.tmp.name, FLEET_SLUG + ".abandoned")
        with open(p, "a", encoding="utf-8") as fh:
            fh.write("%.3f digest\n" % (time.time() + 60))
        rc, out = self.debt()
        self.assertEqual(rc, 12, out)
        self.assertIn("digest", out)

    def test_a_future_dated_abandonment_is_ignored(self):
        # The other unbounded direction: a finite timestamp in the future — a
        # forward clock jump when `abandon()` stamped it, or a hand-edited line —
        # would otherwise excuse the step on every round until wall-clock caught up.
        self.skipped_round()
        self.check()
        p = os.path.join(self.tmp.name, FLEET_SLUG + ".abandoned")
        with open(p, "a", encoding="utf-8") as fh:
            fh.write("%.3f digest\n" % (time.time() + 86400))
        rc, out = self.check()
        self.assertEqual(rc, 11, out)
        self.assertIn("skipped: digest", out)

    def test_an_abandonment_from_an_earlier_round_is_not_honoured(self):
        # `check()` honours a payment only from the round it was paid in. Without
        # that filter one abandon would excuse the step for the rest of the task's
        # life — the permanent-green failure this whole file exists to prevent.
        self.skipped_round()
        self.check()
        p = os.path.join(self.tmp.name, FLEET_SLUG + ".abandoned")
        with open(p, "a", encoding="utf-8") as fh:
            fh.write("%.3f digest\n" % (time.time() - 3600))
        rc, out = self.check()
        self.assertEqual(rc, 11, out)
        self.assertIn("skipped: digest", out)

    # --- the debt is fleet-only -------------------------------------------------

    def test_the_debt_survives_a_round_with_no_receipt(self):
        # The early returns run BEFORE `write_debt`, deliberately: a round that never
        # wrote a receipt has judged nothing, so it must not clear a debt an earlier
        # round left. Moving the write above them would silently pay every debt.
        self.skipped_round()
        self.check()
        os.unlink(os.path.join(self.tmp.name, FLEET_SLUG + ".json"))

        rc, out = self.check()
        self.assertEqual(rc, 11, out)
        self.assertIn("skipped: receipt", out)
        rc, out = self.debt()
        self.assertEqual(rc, 12, out)
        self.assertIn("digest", out)

    def test_an_unreadable_debt_file_is_not_a_clear_debt(self):
        # A fail-open read here runs the leg while a debt is owed — the exact failure
        # the gate exists to stop. Only "the file does not exist" means "no debt".
        # A directory at the debt path raises `IsADirectoryError`, which is an
        # `OSError` but not a `FileNotFoundError` — portable and deterministic.
        os.makedirs(os.path.join(self.tmp.name, FLEET_SLUG + ".debt"))
        rc, out = self.debt()
        self.assertEqual(rc, 2, out)

    def test_debt_refuses_a_non_fleet_branch(self):
        # The goal/topic tier writes no debt, so an empty read there is not "clear" —
        # it is a gate that always passes. Refuse rather than report a false green.
        with self.assertRaises(SystemExit) as caught:
            self.cli("--debt", "--topic", "Some Goal")
        self.assertEqual(caught.exception.code, 2)

    def test_abandon_without_a_step_is_a_usage_error(self):
        self.skipped_round()
        self.check()
        with self.assertRaises(SystemExit) as caught:
            self.cli("--abandon", "--subject", FLEET_SUBJECT, "--branch", "fleet")
        self.assertEqual(caught.exception.code, 2)

    def test_the_goal_tier_writes_no_debt(self):
        # `--check` is shared with the goal/topic tier, but only the three fleet
        # legs chain `--debt`. Writing the file there would leave state no command
        # reads, which is worse than no state at all.
        self.cli("--start", "--topic", "Some Goal")
        rc, out = self.cli("--check", "--topic", "Some Goal")
        self.assertEqual(rc, 11, out)
        self.assertIn("INCOMPLETE ROUND", out)
        self.assertFalse(os.path.exists(os.path.join(self.tmp.name, "some-goal.debt")))


class KeepAliveVerdicts(unittest.TestCase):
    """A topic round that reads `resume` / `restart` owes an outcome for each verdict.

    The incident (2026-11-08, topic manager eab58671): three `resume` verdicts read on two
    consecutive ticks, none executed, and `--check` green because every field resolved. The
    load-bearing pair is (a) verdict with no outcome → red and (c) `executed` backed by the
    resume's own stamp → green; (b) is what keeps the word `executed` alone from clearing it.
    """

    VAULT = "/vaults/private-personal"
    GOAL = "Auto-Merge Trust-Gated PRs"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.live = tempfile.TemporaryDirectory()
        self.arts = tempfile.TemporaryDirectory()
        self.resumes = tempfile.TemporaryDirectory()
        self.exits = tempfile.TemporaryDirectory()
        os.environ["MANAGER_RESUMES_STATE_DIR"] = self.resumes.name
        os.environ["SUPERVISOR_SESSION_EXIT_DIR"] = self.exits.name
        self.m = load(self.tmp.name, self.live.name)
        self.topic = "Development Pipeline Throughput"

    def tearDown(self):
        for t in (self.tmp, self.live, self.arts, self.resumes, self.exits):
            t.cleanup()
        for k in ("MANAGER_RECEIPT_STATE_DIR", "MANAGER_LIVENESS_STATE_DIR",
                  "MANAGER_RESUMES_STATE_DIR", "SUPERVISOR_SESSION_EXIT_DIR"):
            os.environ.pop(k, None)

    # --- helpers ---------------------------------------------------------------

    def cli(self, *argv):
        out, err, old = io.StringIO(), io.StringIO(), sys.argv
        sys.argv = ["manager-receipt.py", *argv]
        try:
            with redirect_stdout(out), redirect_stderr(err):
                rc = self.m.main()
        finally:
            sys.argv = old
        return rc, out.getvalue()

    def artifact(self, name, text):
        p = os.path.join(self.arts.name, name)
        with open(p, "w", encoding="utf-8") as fh:
            fh.write(text)
        return p

    def full_round(self):
        """Every STEPS field resolves — so any red below is the keep-alive's alone."""
        self.cli("--start", "--topic", self.topic)
        ev = os.path.join(self.arts.name, "events.jsonl")
        with open(ev, "w", encoding="utf-8") as fh:
            fh.write(json.dumps({"kind": "LIVENESS", "ts": iso_ts(time.time())}) + "\n")
        for step, p in (("needs-you", self.artifact("needs-you.txt", NEEDS_YOU_OK)),
                        ("sweep", self.artifact("sweep.txt", SWEEP_OK)),
                        ("watcher", ev),
                        ("readings", self.artifact("readings.json", READINGS_OK))):
            self.cli("--record", "--topic", self.topic, "--step", step, "--path", p)

    def verdict(self, verdict="resume", goal=GOAL, session="sid-1"):
        return self.cli("--record-verdict", "--topic", self.topic, "--goal", goal,
                        "--vault", self.VAULT, "--verdict", verdict, "--session", session)

    def outcome(self, outcome, goal=GOAL):
        return self.cli("--record-outcome", "--topic", self.topic, "--goal", goal,
                        "--outcome", outcome)

    def check(self):
        # A topic round owes TOPIC_STEPS (no `drive`), so it is judged with `--tier topic`.
        return self.cli("--check", "--topic", self.topic, "--tier", "topic")

    def resume_stamp(self, epoch, session="sid-1"):
        """The stamp `topic-keepalive.py --record-resume` writes — via its own `record()`."""
        km = self.m.keepalive_module()
        p = km.state_path(self.VAULT, self.GOAL, "goal")
        km.record(p, [], epoch, session)

    def spawn_line(self, session, epoch):
        """The `kind=spawn` line `session-exit-record.py` writes for a relaunched pane."""
        ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch))
        with open(os.path.join(self.exits.name, "pane-42.log"), "a", encoding="utf-8") as fh:
            fh.write("\t".join((ts, "kind=spawn", "pane=42", f"session={session}", "pid=1",
                                "rc=-", "signal=-", "sender=-", "writer=-")) + "\n")

    # --- the four pinned fixtures ------------------------------------------------

    def test_a_verdict_with_no_outcome_is_red(self):
        self.full_round()
        self.assertEqual(self.verdict()[0], 0)
        rc, out = self.check()
        self.assertEqual(rc, self.m.EXIT_SKIPPED)
        self.assertIn("INCOMPLETE ROUND — skipped: keepalive", out)
        self.assertIn(self.GOAL, out)

    def test_b_executed_without_the_resume_stamp_is_red(self):
        self.full_round()
        self.verdict()
        self.assertEqual(self.outcome("executed")[0], 0)
        rc, out = self.check()
        self.assertEqual(rc, self.m.EXIT_SKIPPED)
        self.assertIn("skipped: keepalive", out)
        self.assertIn("no --record-resume stamp", out)

    def test_c_executed_with_an_in_window_resume_stamp_is_green(self):
        self.full_round()
        self.verdict()
        self.outcome("executed")
        self.resume_stamp(time.time())
        rc, out = self.check()
        self.assertEqual((rc, out), (0, ""))

    def test_d_a_round_with_no_verdict_owes_no_keepalive(self):
        # The goal round and the topic round whose goals all read `jump` / `leave`: the step
        # is conditional, so a full round stays green and never names `keepalive`.
        self.full_round()
        rc, out = self.check()
        self.assertEqual((rc, out), (0, ""))
        self.assertNotIn("keepalive", self.m.STEPS)

    # --- the edges -----------------------------------------------------------------

    def test_a_stamp_for_another_live_session_does_not_count(self):
        # The round's own session is live too; stamping it must not prove the ABSENT one ran.
        self.full_round()
        self.verdict(session="sid-1")
        self.outcome("executed")
        self.resume_stamp(time.time(), session="the-rounds-own-session-id")
        rc, out = self.check()
        self.assertEqual(rc, self.m.EXIT_SKIPPED)
        self.assertIn("no --record-resume stamp for its session", out)

    def test_a_bare_stamp_without_a_session_does_not_count(self):
        # A stamp file in the pre-change shape carries the cap's floats only.
        self.full_round()
        self.verdict()
        self.outcome("executed")
        p = self.m.keepalive_module().state_path(self.VAULT, self.GOAL, "goal")
        with open(p, "w", encoding="utf-8") as fh:
            json.dump({"resumes": [time.time()]}, fh)
        self.assertEqual(self.check()[0], self.m.EXIT_SKIPPED)

    def test_the_cap_still_reads_the_resumes_list(self):
        km = self.m.keepalive_module()
        p = km.state_path(self.VAULT, self.GOAL, "goal")
        km.record(p, [], time.time() - 10, "a")
        km.record(p, km.load_resumes(p), time.time(), "b")
        self.assertEqual(len(km.load_resumes(p)), 2)
        self.assertEqual([s for _t, s in km.load_sessions(p)], ["a", "b"])

    def test_a_stamp_from_before_the_round_does_not_count(self):
        self.full_round()
        self.verdict()
        self.outcome("executed")
        self.resume_stamp(time.time() - 3600)
        rc, out = self.check()
        self.assertEqual(rc, self.m.EXIT_SKIPPED)
        self.assertIn("skipped: keepalive", out)

    def test_restart_executed_needs_its_spawn_record(self):
        self.full_round()
        self.verdict("restart", session="sid-r")
        self.outcome("executed")
        self.assertEqual(self.check()[0], self.m.EXIT_SKIPPED)
        self.spawn_line("other-sid", time.time())
        self.assertEqual(self.check()[0], self.m.EXIT_SKIPPED)
        self.spawn_line("sid-r", time.time())
        self.assertEqual(self.check(), (0, ""))

    def test_each_verdicts_own_tokens_clear_it(self):
        for verdict, outcome in (("resume", "refused:start-rate"),
                                 ("resume", "refused:waiting-approval"),
                                 ("restart", "refused:held-session"),
                                 ("restart", "refused:dirty-worktree"),
                                 ("restart", "deferred:busy-target")):
            with self.subTest(verdict=verdict, outcome=outcome):
                self.full_round()
                self.verdict(verdict)
                self.assertEqual(self.outcome(outcome)[0], 0)
                self.assertEqual(self.check(), (0, ""))

    def test_an_unknown_token_is_refused_at_the_write(self):
        self.full_round()
        self.verdict()
        for bad in ("refused", "refused:whatever", "deferred:cap", "refused:busy-target", "done"):
            with self.subTest(outcome=bad):
                self.assertEqual(self.outcome(bad)[0], 2)
        self.assertEqual(self.check()[0], self.m.EXIT_SKIPPED)

    def test_the_incidents_own_excuses_are_not_tokens(self):
        # The 2026-11-08 round blamed the fleet cap, then N. The resume path never reaches
        # either gate, so no spelling of them may clear a resume verdict.
        self.full_round()
        self.verdict("resume")
        for excuse in ("refused:concurrency-limit", "refused:fleet-full", "refused:manager-cap",
                       "refused:n-cap", "deferred:busy-target", "refused:held-session"):
            with self.subTest(outcome=excuse):
                self.assertEqual(self.outcome(excuse)[0], 2)
        self.assertEqual(self.check()[0], self.m.EXIT_SKIPPED)

    def test_a_token_from_the_other_verdict_reads_red_when_written_around_the_verb(self):
        self.full_round()
        self.verdict("resume")
        p = self.m.path(self.m.slug(self.topic))
        with open(p, encoding="utf-8") as fh:
            d = json.load(fh)
        d["keepalive"][0]["outcome"] = "deferred:busy-target"
        with open(p, "w", encoding="utf-8") as fh:
            json.dump(d, fh)
        rc, out = self.check()
        self.assertEqual(rc, self.m.EXIT_SKIPPED)
        self.assertIn("not a known outcome for `resume`", out)

    def test_a_reread_verdict_is_owed_once(self):
        self.full_round()
        self.verdict()
        self.verdict()
        self.outcome("refused:start-rate")
        self.assertEqual(self.check(), (0, ""))

    def test_two_open_verdicts_on_one_goal_are_paid_separately(self):
        self.full_round()
        self.verdict("resume", session="sid-1")
        self.verdict("restart", session="sid-2")
        # Unprefixed, the outcome answers the NEWEST open entry — the restart — so a
        # resume token there is refused rather than silently applied to the wrong verdict.
        self.assertEqual(self.outcome("refused:start-rate", goal=self.GOAL)[0], 2)
        self.assertEqual(self.outcome("refused:held-session", goal=self.GOAL)[0], 0)
        rc, out = self.check()
        self.assertEqual(rc, self.m.EXIT_SKIPPED)
        self.assertIn("`resume` verdict has no outcome", out)
        self.assertNotIn("`restart` verdict has no outcome", out)
        # `--verdict` names the older open entry.
        self.assertEqual(self.cli("--record-outcome", "--topic", self.topic, "--goal", self.GOAL,
                                  "--verdict", "resume", "--outcome", "refused:start-rate")[0], 0)
        self.assertEqual(self.check(), (0, ""))
        # Nothing is open for `resume` now, so the same call is refused.
        self.assertEqual(self.cli("--record-outcome", "--topic", self.topic, "--goal", self.GOAL,
                                  "--verdict", "resume", "--outcome", "refused:start-rate")[0], 2)

    def test_two_open_verdicts_of_the_same_type_are_owed_separately(self):
        # A goal can be read twice in one round with two different recorded ids (a resume
        # that landed and then a second one). Dedupe keys on (goal, verdict, session), so
        # both stay open and each needs its own outcome and its own stamp.
        self.full_round()
        self.verdict("resume", session="sid-a")
        self.verdict("resume", session="sid-b")
        self.outcome("executed")
        self.resume_stamp(time.time(), session="sid-b")
        rc, out = self.check()
        self.assertEqual(rc, self.m.EXIT_SKIPPED)
        self.assertEqual(out.count("skipped: keepalive"), 1)
        self.outcome("executed")
        self.resume_stamp(time.time(), session="sid-a")
        self.assertEqual(self.check(), (0, ""))

    def test_the_outcome_matches_the_goal_by_slug(self):
        self.full_round()
        self.verdict(goal="Auto-Merge Trust-Gated PRs")
        self.assertEqual(self.outcome("refused:start-rate", goal="auto-merge trust-gated prs")[0], 0)
        self.assertEqual(self.check(), (0, ""))

    def test_a_restart_by_prefix_matches_its_spawn_record(self):
        full = "0123456789abcdef-0000-1111"
        self.full_round()
        self.verdict("restart", session=full)
        self.outcome("executed")
        self.spawn_line(full[:8], time.time())
        self.assertEqual(self.check(), (0, ""))

    def test_a_too_short_prefix_does_not_match(self):
        self.full_round()
        self.verdict("restart", session="0123456789abcdef")
        self.outcome("executed")
        self.spawn_line("0123", time.time())
        self.assertEqual(self.check()[0], self.m.EXIT_SKIPPED)

    def test_an_unknown_token_written_around_the_verb_still_reads_red(self):
        self.full_round()
        self.verdict()
        p = self.m.path(self.m.slug(self.topic))
        with open(p, encoding="utf-8") as fh:
            d = json.load(fh)
        d["keepalive"][0]["outcome"] = "refused:whatever"
        with open(p, "w", encoding="utf-8") as fh:
            json.dump(d, fh)
        rc, out = self.check()
        self.assertEqual(rc, self.m.EXIT_SKIPPED)
        self.assertIn("not a known outcome", out)

    def test_each_verdict_is_owed_separately(self):
        self.full_round()
        self.verdict(goal="Goal A")
        self.verdict(goal="Goal B")
        self.outcome("refused:start-rate", goal="Goal A")
        rc, out = self.check()
        self.assertEqual(rc, self.m.EXIT_SKIPPED)
        self.assertIn("Goal B", out)
        self.assertNotIn("Goal A", out)

    def test_start_clears_the_previous_rounds_verdicts(self):
        self.full_round()
        self.verdict()
        self.full_round()
        self.assertEqual(self.check(), (0, ""))

    def test_a_verdict_needs_a_round_in_progress(self):
        self.assertEqual(self.verdict()[0], 2)

    def test_a_verdict_without_a_session_is_refused(self):
        # `executed` is proved against the session id, so an id-less entry is unpayable.
        self.full_round()
        self.assertEqual(self.verdict(session="")[0], 2)
        self.assertEqual(self.check(), (0, ""))

    def test_a_spawn_log_last_written_before_the_round_is_not_read(self):
        self.full_round()
        self.verdict("restart", session="sid-r")
        self.outcome("executed")
        self.spawn_line("sid-r", time.time())
        p = os.path.join(self.exits.name, "pane-42.log")
        old = time.time() - 3600
        os.utime(p, (old, old))
        self.assertEqual(self.check()[0], self.m.EXIT_SKIPPED)

    def test_the_verbs_are_topic_tier_only(self):
        self.full_round()
        with self.assertRaises(SystemExit):
            self.cli("--record-verdict", "--subject", "Fleet Manager", "--branch", "fleet",
                     "--goal", "g", "--vault", "/v", "--verdict", "resume")

    # --- the producer ------------------------------------------------------------

    def test_the_probe_writes_the_verdict_onto_the_receipt(self):
        self.full_round()
        k = load_module("topic_keepalive", os.path.join(SCRIPTS, "topic-keepalive.py"))
        err = io.StringIO()
        with redirect_stderr(err):
            k.record_on_receipt(self.topic, self.GOAL, self.VAULT, "resume", "sid-1")
        self.assertEqual(err.getvalue(), "")
        rc, out = self.check()
        self.assertEqual(rc, self.m.EXIT_SKIPPED)
        self.assertIn("`resume` verdict has no outcome", out)

    def test_a_receipt_fault_warns_and_never_raises(self):
        # No `--start`: the write is refused, and the probe must still answer its verdict.
        k = load_module("topic_keepalive", os.path.join(SCRIPTS, "topic-keepalive.py"))
        err = io.StringIO()
        with redirect_stderr(err):
            k.record_on_receipt(self.topic, self.GOAL, self.VAULT, "resume", "sid-1")
        self.assertIn("not recorded", err.getvalue())

    def test_probe_and_receipt_agree_on_the_resume_path(self):
        k = load_module("topic_keepalive", os.path.join(SCRIPTS, "topic-keepalive.py"))
        self.assertEqual(k.state_path(self.VAULT, self.GOAL, "goal"),
                         self.m.keepalive_module().resume_path(self.VAULT, self.GOAL, "goal"))

    def test_a_symlinked_vault_spelling_is_the_same_store(self):
        k = load_module("topic_keepalive", os.path.join(SCRIPTS, "topic-keepalive.py"))
        real = os.path.join(self.arts.name, "private-personal")
        os.makedirs(real)
        link = os.path.join(self.arts.name, "Personal")
        os.symlink(real, link)
        self.assertEqual(k.resume_path(link, self.GOAL), k.resume_path(real, self.GOAL))

    # --- the probe's own CLI, end to end in-process ----------------------------------

    def probe(self, liveness, loop, stamps=0, sid="sid-9"):
        """Run `topic-keepalive.py main(... --receipt)` against a one-goal vault."""
        k = load_module("topic_keepalive", os.path.join(SCRIPTS, "topic-keepalive.py"))
        vault = os.path.join(self.arts.name, "vault")
        os.makedirs(os.path.join(vault, "24 Goals"), exist_ok=True)
        with open(os.path.join(vault, "24 Goals", f"{self.GOAL}.md"), "w", encoding="utf-8") as fh:
            fh.write(f"---\nclaude_session_id: {sid}\n---\nbody\n")
        if stamps:
            p = k.state_path(vault, self.GOAL)
            with open(p, "w", encoding="utf-8") as fh:
                json.dump({"resumes": [time.time() - 60] * stamps}, fh)
        k.session_verdict = lambda _sid: liveness
        k.cadence_state_detail = lambda _subject, _branch="goal", _vault="": (loop, "")
        out = io.StringIO()
        with redirect_stdout(out):
            rc = k.main(["--vault", vault, "--goal", self.GOAL, "--receipt", self.topic])
        return rc, out.getvalue().splitlines()

    def owed(self):
        return self.m.read_receipt(self.m.slug(self.topic)).get("keepalive") or []

    def test_the_probe_owes_a_resume_and_keeps_its_two_line_output(self):
        self.full_round()
        rc, lines = self.probe("ABSENT", "STALE")
        self.assertEqual((rc, lines[0], len(lines)), (0, "resume", 2))
        self.assertEqual([(e["verdict"], e["session"]) for e in self.owed()], [("resume", "sid-9")])
        self.assertEqual(self.check()[0], self.m.EXIT_SKIPPED)

    def test_the_probe_owes_a_restart(self):
        self.full_round()
        rc, lines = self.probe("LIVE", "STALE")
        self.assertEqual((lines[0], len(lines)), ("restart", 2))
        self.assertEqual([e["verdict"] for e in self.owed()], ["restart"])

    def test_jump_leave_and_crash_loop_owe_nothing(self):
        for liveness, loop, stamps, verdict in (("LIVE", "OK", 0, "jump"),
                                               ("ABSENT", "STOPPED", 0, "leave"),
                                               ("ABSENT", "STALE", 3, "crash-loop")):
            with self.subTest(verdict=verdict):
                self.full_round()
                _rc, lines = self.probe(liveness, loop, stamps)
                self.assertEqual(lines[0], verdict)
                self.assertEqual(self.owed(), [])
                self.assertEqual(self.check(), (0, ""))


class RearmShapeReadsEpochsOnly(unittest.TestCase):
    """SC3 — `rearm_shape` reads epochs, never the interval column.

    `manager-liveness.py --arm` writes `<epoch> <interval>` per line now, because the
    conditional floor that keeps a lengthened interval from reading STALE needs the
    interval each gap was armed under. `rearm_shape` only asks whether an arm landed
    inside the round window, so it must go through the shared epoch reader: the old
    `fh.read().split()` put the interval on the epoch list, and any small interval
    falls inside a narrow window and reports an arm that never happened.

    The shape that discriminates: `started=0, now=4000` against a line carrying a real
    wall-clock epoch — the interval 3600 sits inside `[0, 4001]`, the epoch does not.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.live = tempfile.TemporaryDirectory()
        self.m = load(self.tmp.name, self.live.name)

    def tearDown(self):
        self.tmp.cleanup()
        self.live.cleanup()
        os.environ.pop("MANAGER_RECEIPT_STATE_DIR", None)
        os.environ.pop("MANAGER_LIVENESS_STATE_DIR", None)

    def arms(self, text):
        p = os.path.join(self.live.name, "fleet--fleet-manager.arms")
        with open(p, "w", encoding="utf-8") as fh:
            fh.write(text)
        return p

    def test_an_interval_is_not_read_as_an_epoch(self):
        p = self.arms("1794141605 3600\n")
        self.assertFalse(self.m.rearm_shape(p, 0, 4000))

    def test_a_real_in_window_arm_is_still_found(self):
        p = self.arms("1794141605 3600\n")
        self.assertTrue(self.m.rearm_shape(p, 1794141600, 1794141700))

    def test_a_legacy_single_column_ring_still_reads(self):
        p = self.arms("1794141605\n")
        self.assertTrue(self.m.rearm_shape(p, 1794141600, 1794141700))


class RearmShapeCountsHeadBeatsOnly(unittest.TestCase):
    """A tail beat must not pay the `re-arm` step for a round that never armed its head.

    Since the round grew a closing beat, one round writes two epochs into the same
    `<slug>.arms` ring inside the same window. Counting them alike let the tail beat
    clear a head arm that never ran: `--check` reports `skipped: re-arm`, then
    `output-shape.md` item 13's row-20 `--debt` re-reads the ring, finds the tail epoch
    inside the window, and pays the debt — so the round that skipped its head arm reads
    complete one step later, which is the false green this predicate exists to prevent.

    The shape that discriminates: a ring whose ONLY in-window epoch carries the tail
    marker. It must not satisfy the step; the same epoch unmarked must.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.live = tempfile.TemporaryDirectory()
        self.m = load(self.tmp.name, self.live.name)

    def tearDown(self):
        self.tmp.cleanup()
        self.live.cleanup()
        os.environ.pop("MANAGER_RECEIPT_STATE_DIR", None)
        os.environ.pop("MANAGER_LIVENESS_STATE_DIR", None)

    def arms(self, text):
        p = os.path.join(self.live.name, "fleet--fleet-manager.arms")
        with open(p, "w", encoding="utf-8") as fh:
            fh.write(text)
        return p

    def tail(self, text):
        p = os.path.join(self.live.name, "fleet--fleet-manager.tail")
        with open(p, "w", encoding="utf-8") as fh:
            fh.write(text)
        return p

    def test_a_stored_tail_epoch_does_not_pay_the_step(self):
        # The production shape: a TWO-column ring with the marker in the sibling store.
        # A reader that honours only the retired third column sees is_tail False here
        # and counts the closing beat as a head — the no-op this test exists to catch.
        p = self.arms("1794141605 900\n")
        self.tail("1794141605\n")
        self.assertFalse(self.m.rearm_shape(p, 1794141600, 1794141700))

    def test_the_same_epoch_with_no_store_does_pay_the_step(self):
        p = self.arms("1794141605 900\n")
        self.assertTrue(self.m.rearm_shape(p, 1794141600, 1794141700))

    def test_a_head_beside_a_stored_tail_still_pays(self):
        p = self.arms("1794141605 900\n1794141650 900\n")
        self.tail("1794141650\n")
        self.assertTrue(self.m.rearm_shape(p, 1794141600, 1794141700))

    def test_a_legacy_third_column_tail_is_also_excluded(self):
        # Rings written before the marker moved carry it as a third column.
        p = self.arms("1794141605 900 tail\n")
        self.assertFalse(self.m.rearm_shape(p, 1794141600, 1794141700))


class GatedState(unittest.TestCase):
    """A step the frozen-tree predicate suppressed is excused for THAT round only.

    `.gated` is a different claim from `.abandoned`: an abandonment pays a leg that was
    owed, a gate records that the leg was never owed this tick. Both are round-scoped,
    because a gate that survived every later round would let a round skip a leg it owed
    — the permanent-green failure the round window exists to prevent.
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.m = load(self.tmp, self.tmp)

    def _skips(self, started, now):
        d = {"topic": "T", "round_started_at": started, "fields": {}}
        return [step for step, _ in self.m._skips("t", "", d, started, 600, now, "topic")]

    def test_a_gated_step_is_excused_this_round(self):
        now = time.time()
        self.m.gate("T", "", "readings")
        self.assertNotIn("readings", self._skips(now - 1, now))

    def test_an_ungated_step_still_reads_as_skipped(self):
        # The negative control: the excuse must be per-step, not a blanket pass.
        now = time.time()
        self.m.gate("T", "", "readings")
        self.assertIn("sweep", self._skips(now - 1, now))

    def test_a_gate_from_a_previous_round_does_not_excuse(self):
        now = time.time()
        with open(self.m.gated_path("t"), "w", encoding="utf-8") as fh:
            fh.write(f"{now - 3600:.3f} readings\n")
        self.assertIn("readings", self._skips(now - 1, now))

    def test_a_non_finite_epoch_is_ignored(self):
        # Same tamper guard as `read_abandoned`: one `inf` line would excuse the step on
        # every later round.
        now = time.time()
        with open(self.m.gated_path("t"), "w", encoding="utf-8") as fh:
            fh.write("inf readings\n")
        self.assertIn("readings", self._skips(now - 1, now))


class RenderCached(unittest.TestCase):
    """A gated tick still prints the table — with a marker that says THIS tick.

    Replaying the cached payload verbatim prints a header claiming a time the tick did
    not run, which makes a dead loop and a gated one look identical. The marker is part
    of the frame (`docs/sweep-output.md`), so it is replaced, never kept.
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.m = load(self.tmp, self.tmp)

    def payload(self, text):
        with open(os.path.join(self.tmp, "t.sweep"), "w", encoding="utf-8") as fh:
            fh.write(text)

    def render(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            # This class exercises the TOPIC tier's cached render, whose payload is
            # `manager-receipts/<slug>.sweep`; the goal tier's is `.table`.
            rc = self.m.render_cached("T", tier="topic")
        return rc, buf.getvalue()

    def test_the_stale_marker_is_replaced(self):
        self.payload("22:12 ✓ T — 53 goals\n┌────┐\n│ row│\n└────┘\n")
        rc, out = self.render()
        self.assertEqual(rc, 0)
        self.assertNotIn("22:12", out)
        self.assertIn("sweep gated this tick", out)

    def test_the_box_survives(self):
        self.payload("22:12 ✓ T\n┌────┐\n│ row│\n└────┘\n")
        _, out = self.render()
        self.assertIn("│ row│", out)
        self.assertIn("└────┘", out)

    def test_a_missing_payload_is_an_error_not_an_empty_table(self):
        # An empty render would read as "the table printed and had no rows".
        rc, out = self.render()
        self.assertEqual(rc, 1)
        self.assertEqual(out, "")


class SpendReadout(unittest.TestCase):
    """`--tokens`, `--snapshot` and `--count` — the spend-per-change readout.

    The two halves are deliberately independent: the numerator comes from the
    transcripts, the denominator from the sweep payload's own diff. A test that let
    either read the other would pass on the defect the criteria name — a census echo
    in the numerator, or a leg certifying its own moved-row count.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.live = tempfile.TemporaryDirectory()
        self.projects = tempfile.TemporaryDirectory()
        os.environ["MANAGER_RECEIPT_PROJECTS_ROOT"] = self.projects.name
        # The goal tier's payload is `manager-predispatch/<slug>.table`, resolved through
        # the producer's own `payload_path()`, so its state dir points at the same temp
        # tree the receipt uses.
        os.environ["MANAGER_PREDISPATCH_STATE_DIR"] = self.tmp.name
        self.m = load(self.tmp.name, self.live.name)
        self.topic = "Recurring Operator Gates"
        self.slug = "recurring-operator-gates"
        self.session = "11111111-2222-3333-4444-555555555555"
        # The lag window's two ends: the previous round started 600 s ago and the
        # current round started 300 s ago, so records in [started, ended) belong to
        # the previous round.
        self.started = time.time() - 600
        self.ended = time.time() - 300

    def tearDown(self):
        for t in (self.tmp, self.live, self.projects):
            t.cleanup()
        os.environ.pop("MANAGER_RECEIPT_STATE_DIR", None)
        os.environ.pop("MANAGER_LIVENESS_STATE_DIR", None)
        os.environ.pop("MANAGER_RECEIPT_PROJECTS_ROOT", None)
        os.environ.pop("MANAGER_PREDISPATCH_STATE_DIR", None)

    # --- helpers ---------------------------------------------------------------

    def cli(self, *argv):
        out, err, old = io.StringIO(), io.StringIO(), sys.argv
        sys.argv = ["manager-receipt.py", *argv]
        try:
            with redirect_stdout(out), redirect_stderr(err):
                rc = self.m.main()
        finally:
            sys.argv = old
        return rc, out.getvalue()

    def open_round(self):
        """The receipt for a round whose PREVIOUS round is the measured window."""
        p = os.path.join(self.tmp.name, self.slug + ".json")
        with open(p, "w", encoding="utf-8") as fh:
            json.dump({"topic": self.topic, "round_started_at": self.ended,
                       "previous_round_started_at": self.started, "fields": {}}, fh)
        return p

    def first_tick(self):
        """A receipt with no previous round — the first tick."""
        p = os.path.join(self.tmp.name, self.slug + ".json")
        with open(p, "w", encoding="utf-8") as fh:
            json.dump({"topic": self.topic, "round_started_at": self.ended,
                       "fields": {}}, fh)
        return p

    def transcript(self, relpath, records):
        p = os.path.join(self.projects.name, "proj", relpath)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as fh:
            for r in records:
                fh.write(json.dumps(r) + "\n")
        return p

    @staticmethod
    def rec(mid, tokens, epoch):
        """A transcript record in the real shape: `timestamp` (Z) + `message.usage`."""
        ts = datetime.datetime.fromtimestamp(epoch, tz=datetime.timezone.utc)
        return {"timestamp": ts.isoformat().replace("+00:00", "Z"),
                "message": {"id": mid, "usage": {"output_tokens": tokens}}}

    def payload(self, rows, marker="09:00 ✓ T — no change"):
        """A rendered sweep payload at the GOAL tier's own path (`<slug>.table`)."""
        out = [marker,
               "  ┌──────┬────────┬────────┬────────┬──────────┐",
               "  │ Goal │ Status │ Phase  │ Met    │ Manager  │",
               "  ├──────┼────────┼────────┼────────┼──────────┤"]
        for goal, status, phase, met, mgr in rows:
            out.append("  │ %-4s │ %-6s │ %-6s │ %-6s │ %-8s │"
                       % (goal, status, phase, met, mgr))
        out.append("  └──────┴────────┴────────┴────────┴──────────┘")
        p = os.path.join(self.tmp.name, self.slug + ".table")
        with open(p, "w", encoding="utf-8") as fh:
            fh.write("\n".join(out) + "\n")
        return p

    def goal_payload(self, rows):
        """A GOAL-tier payload: `Topic / Goal / Task | Session | Status | Phase | Met`.

        The exact frame `manager-predispatch --write-payload` renders for the goal
        tier — different columns from the topic tier's, which is the shape the first
        `_sweep_rows` missed.
        """
        out = ["┌────┬────┬────┬────┬────┐",
               "│ Topic / Goal / Task │ Session │ Status │ Phase │ Met │",
               "├────┼────┼────┼────┼────┤"]
        for name, sess, status, phase, met in rows:
            out.append("│ %s │ %s │ %s │ %s │ %s │" % (name, sess, status, phase, met))
        out.append("└────┴────┴────┴────┴────┘")
        p = os.path.join(self.tmp.name, self.slug + ".table")
        with open(p, "w", encoding="utf-8") as fh:
            fh.write("\n".join(out) + "\n")
        return p

    # --- the numerator ---------------------------------------------------------

    def test_tokens_sums_parent_and_subagent_files(self):
        self.open_round()
        self.transcript(self.session + ".jsonl",
                        [self.rec("msg_p", 100, self.started + 100)])
        self.transcript(os.path.join(self.session, "subagents", "a.jsonl"),
                        [self.rec("msg_a", 200, self.started + 90)])
        rc, out = self.cli("--tokens", "--topic", self.topic, "--session", self.session)
        self.assertEqual(rc, 0)
        # 300, not 100: the parent alone omits the subagent leg.
        self.assertEqual(out.strip(), "300")

    def test_tokens_dedups_by_message_id(self):
        self.open_round()
        # One assistant message spans several records that repeat `usage` verbatim.
        self.transcript(self.session + ".jsonl",
                        [self.rec("msg_same", 100, self.started + 100),
                         self.rec("msg_same", 100, self.started + 99),
                         self.rec("msg_other", 50, self.started + 98)])
        rc, out = self.cli("--tokens", "--topic", self.topic, "--session", self.session)
        self.assertEqual(rc, 0)
        self.assertEqual(out.strip(), "150")

    def test_tokens_windows_the_previous_round(self):
        self.open_round()
        self.transcript(self.session + ".jsonl",
                        [self.rec("msg_before", 999, self.started - 60),
                         self.rec("msg_inside", 42, self.started + 60),
                         self.rec("msg_after", 777, self.ended + 10)])
        rc, out = self.cli("--tokens", "--topic", self.topic, "--session", self.session)
        self.assertEqual(rc, 0)
        self.assertEqual(out.strip(), "42")

    def test_tokens_first_tick_is_baseline(self):
        # No previous round: `n/a (baseline)`, never `0`.
        self.first_tick()
        rc, out = self.cli("--tokens", "--topic", self.topic, "--session", self.session)
        self.assertEqual(rc, 0)
        self.assertEqual(out.strip(), "n/a (baseline)")

    def receipt(self):
        with open(os.path.join(self.tmp.name, self.slug + ".json"), encoding="utf-8") as fh:
            return json.load(fh)

    def test_start_carries_the_previous_round_started_at(self):
        # The core of the fix: `start()` reads the prior receipt and carries its
        # `round_started_at` into `previous_round_started_at`. If that carry
        # regressed, `--tokens` would silently print `n/a (baseline)` forever and no
        # other test would fail — `open_round()` hand-writes the field.
        self.cli("--start", "--topic", self.topic)
        first = self.receipt()
        self.assertNotIn("previous_round_started_at", first)
        self.cli("--start", "--topic", self.topic)
        second = self.receipt()
        self.assertEqual(second["previous_round_started_at"], first["round_started_at"])

    def test_tokens_without_a_round_refuses(self):
        rc, out = self.cli("--tokens", "--topic", self.topic, "--session", self.session)
        self.assertEqual(rc, 2)
        self.assertEqual(out, "")

    def test_tokens_with_no_transcript_files_refuses(self):
        # A missing transcript must not read as "spent nothing".
        self.open_round()
        rc, out = self.cli("--tokens", "--topic", self.topic, "--session", self.session)
        self.assertEqual(rc, 2)
        self.assertEqual(out, "")

    # --- the denominator -------------------------------------------------------

    def test_count_prints_baseline_when_no_prev(self):
        self.payload([("A", "done", "done", "SC 2/2", "OK")])
        rc, out = self.cli("--count", "--topic", self.topic)
        self.assertEqual(rc, 0)
        self.assertEqual(out.strip(), "n/a (baseline)")

    def test_snapshot_then_count_reports_a_moved_row(self):
        self.payload([("A", "done", "done", "SC 2/2", "OK"),
                      ("B", "ready", "exec", "SC 1/2", "OK")])
        self.cli("--snapshot", "--topic", self.topic)
        self.payload([("A", "done", "done", "SC 2/2", "OK"),
                      ("B", "waiting", "exec", "SC 1/2", "OK")])
        rc, out = self.cli("--count", "--topic", self.topic)
        self.assertEqual(rc, 0)
        self.assertEqual(out.strip(), "1")

    def test_count_ignores_the_manager_column(self):
        # A Manager-column churn (`STALE → OK`) is liveness, not change.
        self.payload([("A", "done", "done", "SC 2/2", "STALE")])
        self.cli("--snapshot", "--topic", self.topic)
        self.payload([("A", "done", "done", "SC 2/2", "OK")])
        _, out = self.cli("--count", "--topic", self.topic)
        self.assertEqual(out.strip(), "0")

    def test_count_is_zero_on_no_change(self):
        rows = [("A", "done", "done", "SC 2/2", "OK")]
        self.payload(rows)
        self.cli("--snapshot", "--topic", self.topic)
        self.payload(rows)
        _, out = self.cli("--count", "--topic", self.topic)
        self.assertEqual(out.strip(), "0")

    def test_count_reads_the_goal_tiers_own_frame(self):
        # The goal tier renders `Topic / Goal / Task | Session | Status | Phase | Met`,
        # not the topic tier's `Goal | Status | Phase | Met | Manager`. Recognising
        # only the latter made a goal-tier `--count` read 0 on every tick, so the
        # headline criterion could never fire.
        name = "Manager Rounds Cost What They Change"
        self.goal_payload([(name, "—", "progressing", "—", "0/3")])
        self.cli("--snapshot", "--topic", self.topic)
        self.goal_payload([(name, "—", "progressing", "—", "1/3")])
        rc, out = self.cli("--count", "--topic", self.topic)
        self.assertEqual(rc, 0)
        self.assertEqual(out.strip(), "1")

    def test_count_refuses_an_unparseable_frame(self):
        # A shape change must error, never read as `0 moved` — the silent zero this
        # counter exists to avoid.
        self.payload([("A", "done", "done", "SC 2/2", "OK")])
        self.cli("--snapshot", "--topic", self.topic)
        with open(os.path.join(self.tmp.name, self.slug + ".table"),
                  "w", encoding="utf-8") as fh:
            fh.write("not a table\n")
        rc, out = self.cli("--count", "--topic", self.topic)
        self.assertEqual(rc, 2)
        self.assertEqual(out, "")

    def test_count_does_not_re_snapshot_before_diffing(self):
        # The load-bearing order: snapshot → sweep → diff. A `--count` that
        # re-snapshotted first would overwrite `.prev` and read 0 forever.
        self.payload([("A", "done", "done", "SC 2/2", "OK")])
        self.cli("--snapshot", "--topic", self.topic)
        self.payload([("A", "waiting", "done", "SC 2/2", "OK")])
        _, first = self.cli("--count", "--topic", self.topic)
        _, second = self.cli("--count", "--topic", self.topic)
        self.assertEqual(first.strip(), "1")
        self.assertEqual(second.strip(), "1")


    def test_payload_file_matches_the_producer_per_tier(self):
        # The defect this pins: `render_cached()` opened a `.sweep` path no goal-tier
        # round writes, so a goal-tier round printed a denominator nothing produced.
        goal = self.m.payload_file(self.topic, "goal")
        self.assertTrue(goal.endswith(self.slug + ".table"), goal)
        topic = self.m.payload_file(self.topic, "topic")
        self.assertTrue(topic.endswith(self.slug + ".sweep"), topic)


if __name__ == "__main__":
    unittest.main()
