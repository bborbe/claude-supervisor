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
        for step, p in (("needs-you", ny), ("sweep", sw), ("watcher", ev)):
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

    def test_sweep_shape_rejects_json_empty_and_truncated(self):
        # (a) the JSON the old predicate demanded — the shape no producer writes.
        self.assertFalse(self.m.sweep_shape(json.dumps({"header": ["Topic"], "rows": []})))
        # (b) an empty file, and a frame truncated before its bottom rule.
        self.assertFalse(self.m.sweep_shape(""))
        frame = render_table(["Topic"], [["row"]])
        lines = [ln for ln in frame.splitlines() if ln.strip()]
        self.assertFalse(self.m.sweep_shape("\n".join(lines[:-1])), "bottom rule dropped")
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
        for step, p in (("needs-you", ny), ("sweep", sw), ("watcher", ev)):
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


if __name__ == "__main__":
    unittest.main()
