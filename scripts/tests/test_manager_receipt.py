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
        for step, p in (("needs-you", ny), ("sweep", sw), ("watcher", ev), ("readings", rd)):
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
        for step, p in (("needs-you", ny), ("sweep", sw), ("watcher", ev), ("readings", rd)):
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
        d["round_started_at"] = now - 700  # < 2*1020+60
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
        # The recovery verb is scoped to a step this round actually skipped; paying a
        # debt nobody owes turns the ledger into a place to write anything.
        self.clean_round()
        self.check()
        rc, _ = self.abandon("digest")
        self.assertEqual(rc, 2)


if __name__ == "__main__":
    unittest.main()
