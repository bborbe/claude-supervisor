#!/usr/bin/env python3
"""Tests for scripts/exit-observer-keeper.py.

The load-bearing properties:

- a silent pane files ONE card and is re-armed only after that card exists;
- **no card, no re-arm** — the other order hides the death behind a covered pane;
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
        self.calls = []

    def write_log(self, age=600, pid=4242):
        path = self.store / "pane-7.log"
        path.write_text("2026-11-17T00:00:00Z\tkind=spawn\tpane=7\tsession=sess\tpid=4242"
                        "\trc=0\tsignal=-\tsender=-\twriter=trap\n", encoding="utf-8")
        os.utime(path, (NOW - age, NOW - age))


class CardBeforeRearm(_Base):
    def setUp(self):
        super().setUp()
        self.panes = {"7": {"pid": 4242, "session": "sess", "name": "Fleet Manager",
                            "session_id": SILENT["session_id"]}}

        def run(argv, **kw):
            self.calls.append(argv)
            return _Done("item-42") if pathlib.Path(argv[1]).name == "attention-ask.py" else _Done("")
        k.subprocess.run = run

    def test_silent_pane_is_carded_then_rearmed(self):
        self.write_log()
        self.assertEqual(k.check(NOW, self.panes), 0)
        scripts = [pathlib.Path(a[1]).name for a in self.calls]
        self.assertEqual(scripts, ["attention-ask.py", "session-exit-record.py"])
        self.assertIn("observe", self.calls[-1])
        self.assertIn("4242", self.calls[-1])

    def test_failed_card_means_no_rearm(self):
        def run(argv, **kw):
            self.calls.append(argv)
            if pathlib.Path(argv[1]).name == "attention-ask.py":
                return _Done("", returncode=1)
            return _Done("")
        k.subprocess.run = run
        self.write_log()
        self.assertEqual(k.check(NOW, self.panes), 1)
        self.assertNotIn("session-exit-record.py",
                         [pathlib.Path(a[1]).name for a in self.calls])

    def test_failed_rearm_is_reported(self):
        def run(argv, **kw):
            self.calls.append(argv)
            if pathlib.Path(argv[1]).name == "attention-ask.py":
                return _Done("item-42")
            return _Done("", returncode=1)
        k.subprocess.run = run
        self.write_log()
        self.assertEqual(k.check(NOW, self.panes), 1)

    def test_fresh_log_does_nothing(self):
        self.write_log(age=10)
        self.assertEqual(k.check(NOW, self.panes), 0)
        self.assertEqual(self.calls, [])


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
        k.check = lambda now, panes, dry_run=False: 0
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
