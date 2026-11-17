#!/usr/bin/env python3
"""Tests for scripts/exit-observer-keeper.py.

The load-bearing properties:

- a silent pane files ONE card and is re-armed only after that card exists;
- **no card, no re-arm** — the other order hides the death behind a covered pane;
- a sweep that cannot run is reported, never treated as "nothing is wrong";
- every tick rewrites the liveness file the coverage sweep reads.

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
SILENT = {
    "pane": "7",
    "name": "Fleet Manager",
    "session_id": "aaaaaaaa-0000-0000-0000-000000000000",
    "line": "⚠️ UNRECORDED: Fleet Manager — pane 7, session aaaaaaaa runs with a silent exit "
            "recorder (last heartbeat 600 s old); its death would leave no record",
}


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = pathlib.Path(self.tmp.name)
        self.saved = {n: getattr(k, n) for n in ("STATE_DIR", "HEARTBEAT")}
        self.addCleanup(lambda: [setattr(k, n, v) for n, v in self.saved.items()])
        k.STATE_DIR = str(self.store)
        k.HEARTBEAT = str(self.store / "keeper-heartbeat")
        self.saved_alive = k._alive
        self.addCleanup(lambda: setattr(k, "_alive", self.saved_alive))
        k._alive = lambda pid: True  # the fixture's pid is not a real process
        self.calls = []

    def fake_run(self, argv, **kwargs):
        self.calls.append(argv)
        script = pathlib.Path(argv[1]).name
        if script == "exit-record-coverage.py":
            return _Done(json.dumps({"unrecorded": [SILENT], "unknown": []}))
        if script == "attention-ask.py":
            return _Done("item-42")
        return _Done("")

    def write_store(self, age=600, pid=4242):
        log = self.store / "pane-7.log"
        log.write_text("2026-11-17T00:00:00Z\tkind=spawn\tpane=7\tsession=sess\tpid=4242"
                       "\trc=0\tsignal=-\tsender=-\twriter=trap\n", encoding="utf-8")
        os.utime(log, (NOW - age, NOW - age))


class _Done:
    def __init__(self, stdout, returncode=0):
        self.stdout, self.stderr, self.returncode = stdout, "", returncode


class CardBeforeRearm(_Base):
    def setUp(self):
        super().setUp()
        self.write_store()
        self.saved_run = k.subprocess.run
        self.addCleanup(lambda: setattr(k.subprocess, "run", self.saved_run))
        k.subprocess.run = self.fake_run

    def test_silent_pane_is_carded_then_rearmed(self):
        rc = k.main(["--now", str(NOW)])
        self.assertEqual(rc, 0)
        scripts = [pathlib.Path(a[1]).name for a in self.calls]
        self.assertEqual(scripts, ["exit-record-coverage.py", "attention-ask.py",
                                   "session-exit-record.py"])
        rearm = self.calls[-1]
        self.assertIn("observe", rearm)
        self.assertIn("7", rearm)
        self.assertIn("4242", rearm)
        self.assertIn("sess", rearm)

    def test_failed_card_means_no_rearm(self):
        def failing(argv, **kwargs):
            self.calls.append(argv)
            if pathlib.Path(argv[1]).name == "exit-record-coverage.py":
                return _Done(json.dumps({"unrecorded": [SILENT], "unknown": []}))
            return _Done("", returncode=1)

        k.subprocess.run = failing
        rc = k.main(["--now", str(NOW)])
        self.assertEqual(rc, 1)
        scripts = [pathlib.Path(a[1]).name for a in self.calls]
        self.assertNotIn("session-exit-record.py", scripts,
                         "a card that did not land must not be papered over by a re-arm")

    def test_failed_rearm_is_reported(self):
        """A card that says "re-armed" over a re-arm that failed is the same lie in reverse."""
        def failing(argv, **kwargs):
            self.calls.append(argv)
            name = pathlib.Path(argv[1]).name
            if name == "exit-record-coverage.py":
                return _Done(json.dumps({"unrecorded": [SILENT], "unknown": []}))
            if name == "attention-ask.py":
                return _Done("item-42")
            return _Done("", returncode=1)

        k.subprocess.run = failing
        self.assertEqual(k.main(["--now", str(NOW)]), 1)

    def test_every_tick_writes_the_liveness_file(self):
        k.main(["--now", str(NOW)])
        self.assertEqual(pathlib.Path(k.HEARTBEAT).read_text().strip(), str(int(NOW)))


class SweepFailure(_Base):
    def setUp(self):
        super().setUp()
        self.saved_run = k.subprocess.run
        self.addCleanup(lambda: setattr(k.subprocess, "run", self.saved_run))
        k.subprocess.run = lambda argv, **kw: _Done("", returncode=1)

    def test_an_unrunnable_sweep_is_reported_not_called_clean(self):
        self.assertEqual(k.main(["--now", str(NOW)]), 1)


class NothingSilent(_Base):
    def setUp(self):
        super().setUp()
        self.saved_run = k.subprocess.run
        self.addCleanup(lambda: setattr(k.subprocess, "run", self.saved_run))
        k.subprocess.run = lambda argv, **kw: (
            self.calls.append(argv) or _Done(json.dumps({"unrecorded": [], "unknown": []})))

    def test_no_silent_pane_cards_nothing(self):
        self.assertEqual(k.main(["--now", str(NOW)]), 0)
        self.assertEqual([pathlib.Path(a[1]).name for a in self.calls],
                         ["exit-record-coverage.py"])


class UnresolvablePanes(_Base):
    """Under launchd there is no `wezterm`, so the sweep resolves nothing — and must not read clean."""

    def setUp(self):
        super().setUp()
        self.saved_run = k.subprocess.run
        self.addCleanup(lambda: setattr(k.subprocess, "run", self.saved_run))
        k.subprocess.run = lambda argv, **kw: (
            self.calls.append(argv) or _Done(json.dumps({"unrecorded": [], "unknown": [{"name": "x"}]})))

    def test_an_unresolvable_sweep_is_not_a_healthy_fleet(self):
        self.assertEqual(k.main(["--now", str(NOW)]), 1)


if __name__ == "__main__":
    unittest.main()
