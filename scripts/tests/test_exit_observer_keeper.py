#!/usr/bin/env python3
"""Tests for scripts/exit-observer-keeper.py.

The load-bearing properties:

- a silent pane leaves ONE ledger line and is re-armed, with NO card (a routine death);
- the ledger line is written BEFORE the re-arm, so a re-armed pane cannot hide the death;
- one death is ONE ledger line across every pass and across a keeper restart — the dedup key
  is `(pane, beat_at)` read back from the ledger, not an in-memory set;
- a re-arm that returned rc 0 without restoring the writer is carded ONCE — it is the silent
  loss a ledger line alone would hide;
- a card is the fallback: exactly one when the record cannot be written or the re-arm fails;
- a failed re-arm is reported, never swallowed;
- a sweep that resolves nothing reports its `unknown` count instead of calling the fleet clean.

Run: python3 -m unittest discover -s scripts/tests -v
"""

import importlib.util
import json
import os
import pathlib
import tempfile
import unittest

_HERE = pathlib.Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location(
    "exit_observer_keeper", str(_HERE.parent / "exit-observer-keeper.py"))
k = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(k)

NOW = 1_800_000_000.0
SILENT = {"pane": "7", "name": "Fleet Manager",
          "session_id": "aaaaaaaa-0000-0000-0000-000000000000"}


class _Done:
    def __init__(self, stdout, returncode=0):
        self.stdout, self.stderr, self.returncode = stdout, "", returncode


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = pathlib.Path(self.tmp.name)
        saved = {n: getattr(k, n) for n in ("STATE_DIR", "HEARTBEAT")}
        saved_run, saved_alive = k.subprocess.run, k._alive
        self.addCleanup(lambda: [setattr(k, n, v) for n, v in saved.items()])
        self.addCleanup(lambda: setattr(k.subprocess, "run", saved_run))
        self.addCleanup(lambda: setattr(k, "_alive", saved_alive))
        k.STATE_DIR = str(self.store)
        k.HEARTBEAT = str(self.store / "keeper-heartbeat")
        k._alive = lambda pid: True
        k._ARMED.clear()  # the module-level verification map must not leak between tests
        self.addCleanup(k._ARMED.clear)
        self.calls = []

    def write_log(self, age=600, pid=4242):
        path = self.store / "pane-7.log"
        path.write_text("2026-11-17T00:00:00Z\tkind=spawn\tpane=7\tsession=sess\tpid=4242"
                        "\trc=0\tsignal=-\tsender=-\twriter=trap\n", encoding="utf-8")
        os.utime(path, (NOW - age, NOW - age))


class RecordThenRearm(_Base):
    def setUp(self):
        super().setUp()
        self._orig_record = k.record
        self.addCleanup(lambda: setattr(k, "record", self._orig_record))
        self.panes = {"7": {"pid": 4242, "session": "sess", "name": "Fleet Manager",
                            "session_id": SILENT["session_id"]}}
        self.cards = 0

        def run(argv, **kw):
            self.calls.append(argv)
            if pathlib.Path(argv[1]).name == "attention-ask.py":
                self.cards += 1
                return _Done("item-42")
            return _Done("")
        k.subprocess.run = run

    def ledger(self):
        path = self.store / "keeper-rearms.jsonl"
        if not path.exists():
            return []
        return [json.loads(ln) for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]

    def scripts(self):
        return [pathlib.Path(a[1]).name for a in self.calls]

    def test_routine_death_is_recorded_then_rearmed_with_no_card(self):
        self.write_log()
        self.assertEqual(k.check(NOW, self.panes), 0)
        rows = self.ledger()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["pane"], "7")
        self.assertEqual(rows[0]["session"], SILENT["session_id"])
        self.assertEqual(rows[0]["beat_at"], int(NOW - 600))
        self.assertEqual(self.cards, 0, "a routine death must not reach the board")
        self.assertIn("session-exit-record.py", self.scripts())

    def test_record_is_written_before_the_rearm(self):
        seen = {}

        def run(argv, **kw):
            self.calls.append(argv)
            if pathlib.Path(argv[1]).name == "session-exit-record.py":
                seen["rows"] = self.ledger()
            return _Done("")
        k.subprocess.run = run
        self.write_log()
        self.assertEqual(k.check(NOW, self.panes), 0)
        self.assertEqual(len(seen.get("rows", [])), 1,
                         "the ledger line must exist before the re-arm runs")

    def test_failed_record_posts_one_card_and_still_rearms(self):
        k.record = lambda *a, **kw: False
        self.write_log()
        self.assertEqual(k.check(NOW, self.panes), 0)
        self.assertEqual(self.cards, 1)
        self.assertIn("session-exit-record.py", self.scripts())

    def test_failed_rearm_posts_exactly_one_card(self):
        def run(argv, **kw):
            self.calls.append(argv)
            if pathlib.Path(argv[1]).name == "attention-ask.py":
                self.cards += 1
                return _Done("item-42")
            return _Done("", returncode=1)
        k.subprocess.run = run
        self.write_log()
        self.assertEqual(k.check(NOW, self.panes), 1)
        self.assertEqual(self.cards, 1)

    def test_both_failures_post_exactly_one_card(self):
        k.record = lambda *a, **kw: False

        def run(argv, **kw):
            self.calls.append(argv)
            if pathlib.Path(argv[1]).name == "attention-ask.py":
                self.cards += 1
                return _Done("item-42")
            return _Done("", returncode=1)
        k.subprocess.run = run
        self.write_log()
        self.assertEqual(k.check(NOW, self.panes), 1)
        self.assertEqual(self.cards, 1)

    def test_fresh_log_does_nothing(self):
        self.write_log(age=10)
        self.assertEqual(k.check(NOW, self.panes), 0)
        self.assertEqual(self.calls, [])
        self.assertEqual(self.ledger(), [])

    def test_record_dedups_on_pane_and_beat_at(self):
        """One death is one line: `record()` called twice with the same key writes once."""
        beat = int(NOW - 600)
        self.assertTrue(k.record("7", "sess", beat))
        self.assertTrue(k.record("7", "sess", beat))
        self.assertEqual(len(self.ledger()), 1)
        self.assertTrue(k.record("7", "sess", beat + 1),
                        "a different beat_at is a different death")
        self.assertEqual(len(self.ledger()), 2)

    def test_a_keeper_restart_does_not_re_record(self):
        """The dedup state is the ledger: a restart loses the memory, not the line."""
        self.write_log()
        self.assertEqual(k.check(NOW, self.panes, armed={}), 0)
        self.assertEqual(len(self.ledger()), 1)
        self.assertEqual(k.check(NOW + 1, self.panes, armed={}), 0)
        self.assertEqual(len(self.ledger()), 1, "one death is one line, across a restart")

    def test_unrestored_rearm_cards_once(self):
        """A re-arm that returned rc 0 without restoring the writer is the silent loss."""
        self.write_log()
        self.assertEqual(k.check(NOW, self.panes), 0,
                         "the first pass records and re-arms")
        self.assertEqual(self.cards, 0,
                         "a re-arm still inside its window is not yet a failure")
        self.assertEqual(len(self.ledger()), 1)
        self.assertEqual(k.check(NOW + k.VERIFY_SECONDS, self.panes), 1,
                         "a re-arm that did not restore the writer is a failure")
        self.assertEqual(self.cards, 1)
        self.assertEqual(len(self.ledger()), 1, "still one death, one line")
        self.assertEqual(k.check(NOW + 2 * k.VERIFY_SECONDS, self.panes), 0,
                         "a carded death is not retried")
        self.assertEqual(self.cards, 1, "exactly one card call for one un-restorable death")

    def test_a_restored_rearm_clears_the_verification_and_cards_nothing(self):
        self.write_log()
        armed = {}
        self.assertEqual(k.check(NOW, self.panes, armed=armed), 0)
        self.write_log(age=0)  # the re-armed observer wrote its first heartbeat
        self.assertEqual(k.check(NOW + k.VERIFY_SECONDS, self.panes, armed=armed), 0)
        self.assertEqual(self.cards, 0)
        self.assertEqual(armed, {}, "a fresh log clears the death from the verification state")


class Sweep(_Base):
    def test_unknown_count_is_carried_out(self):
        k.subprocess.run = lambda argv, **kw: _Done(
            json.dumps({"unrecorded": [], "unknown": [{"name": "x"}]}))
        self.assertEqual(k.sweep(NOW), ({}, 1))

    def test_a_recorded_row_lands_in_the_watch_set(self):
        """A COVERED pane must be watched — it is the one that can newly fail.

        Building the watch set from `unrecorded` + `unknown` alone watches only the
        panes already broken, so the first death of a healthy writer stays invisible
        until the next full sweep (up to `REFRESH_SECONDS`). Measured 2026-11-18: the
        watch set was empty while twelve manager panes carried live writers.
        """
        self.write_log()
        k.subprocess.run = lambda argv, **kw: _Done(json.dumps(
            {"unrecorded": [], "unknown": [],
             "recorded": [{"pane": "7", "session_id": "sess", "name": "Fleet Manager"}]}))
        panes, unknown = k.sweep(NOW)
        self.assertIn("7", panes)
        self.assertEqual(unknown, 0)

    def test_unrunnable_sweep_is_none(self):
        k.subprocess.run = lambda argv, **kw: _Done("", returncode=1)
        self.assertIsNone(k.sweep(NOW))

    def test_a_nonzero_exit_names_the_code_and_stderr(self):
        """The reason must reach the log: four theories about this failure were wrong without it."""
        import io as _io
        from contextlib import redirect_stderr
        done = _Done("", returncode=2)
        done.stderr = "boom: registry unreadable"
        k.subprocess.run = lambda argv, **kw: done
        err = _io.StringIO()
        with redirect_stderr(err):
            self.assertIsNone(k.sweep(NOW))
        self.assertIn("exited 2", err.getvalue())
        self.assertIn("registry unreadable", err.getvalue())

    def test_undecodable_output_names_the_failure(self):
        import io as _io
        from contextlib import redirect_stderr

        def boom(argv, **kw):
            raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte")
        k.subprocess.run = boom
        err = _io.StringIO()
        with redirect_stderr(err):
            self.assertIsNone(k.sweep(NOW))
        self.assertIn("could not be decoded", err.getvalue())


class Liveness(_Base):
    def test_every_pass_writes_the_heartbeat(self):
        k.subprocess.run = lambda argv, **kw: _Done(
            json.dumps({"unrecorded": [], "unknown": []}))
        k.main(["--once", "--now", str(NOW)])
        self.assertEqual(pathlib.Path(k.HEARTBEAT).read_text().strip(), str(int(NOW)))


class HealthyFleetRefresh(unittest.TestCase):
    """An empty pane set IS the healthy fleet — the state this process lives in nearly always.

    A refresh predicate that also fires when the pane set is empty therefore re-runs the ~24 s
    sweep on every poll, which is worse than the 60 s tick this shape replaces.
    """

    def setUp(self):
        self.sweeps = []
        saved = {n: getattr(k, n) for n in ("sweep", "check", "beat")}
        self.addCleanup(lambda: [setattr(k, n, v) for n, v in saved.items()])
        k.sweep = lambda now: (self.sweeps.append(now) or ({}, 0))
        k.check = lambda now, panes, dry_run=False, **kw: 0
        k.beat = lambda now: None

    def test_empty_panes_do_not_force_a_resweep(self):
        loop = k.Loop(dry_run=True)
        loop.step(NOW)
        loop.step(NOW + k.POLL_SECONDS)
        self.assertEqual(len(self.sweeps), 1,
                         "the refresh is time-driven; an empty pane set must not re-sweep")

    def test_the_refresh_does_fire_when_it_is_due(self):
        loop = k.Loop(dry_run=True)
        loop.step(NOW)
        loop.step(NOW + k.REFRESH_SECONDS)
        self.assertEqual(len(self.sweeps), 2)


if __name__ == "__main__":
    unittest.main()
