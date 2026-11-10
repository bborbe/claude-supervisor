#!/usr/bin/env python3
"""Tests for scripts/idle-reaper-tick.py.

The tick's claim is narrow: it runs the reaper over every `--all` candidate, routes only
`task-terminal` refusals to `close-worker.py`, never runs both verbs on a candidate the
reaper closed, and reports an unexpected verb exit as a fault instead of a refusal.

The verbs are stand-in scripts that append `<verb> <sid> <args>` to a call log and answer
with a scripted exit code and first line. The real verbs signal whatever the registry
names, so they never run here.

Run: python3 -m unittest discover -s scripts/tests -v
"""

import importlib.util
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_SCRIPTS = os.path.dirname(_HERE)
_spec = importlib.util.spec_from_file_location(
    "idle_reaper_tick", os.path.join(_SCRIPTS, "idle-reaper-tick.py")
)
tick_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(tick_mod)
SCRIPT = os.path.join(_SCRIPTS, "idle-reaper-tick.py")


class TestTick(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="idle-reaper-tick-")
        self.sessions = os.path.join(self.tmp, "sessions")
        os.makedirs(self.sessions)
        self.calls = os.path.join(self.tmp, "calls.log")
        # sid -> (exit code, first line) for each stand-in verb
        self.reap_answers = {}
        self.close_answers = {}

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _session(self, sid, entrypoint="sdk-cli", status="idle"):
        with open(os.path.join(self.sessions, f"{sid}.json"), "w", encoding="utf-8") as h:
            json.dump({"sessionId": sid, "entrypoint": entrypoint, "status": status}, h)

    def _verb(self, name, answers):
        path = os.path.join(self.tmp, f"{name}.py")
        table = json.dumps(answers)
        with open(path, "w", encoding="utf-8") as h:
            h.write(
                "import json, sys\n"
                f"answers = json.loads({table!r})\n"
                f"with open({self.calls!r}, 'a') as log:\n"
                f"    log.write({name!r} + ' ' + ' '.join(sys.argv[1:]) + '\\n')\n"
                "code, first = answers.get(sys.argv[1], [0, 'ok'])\n"
                "print(first)\n"
                "sys.exit(code)\n"
            )
        os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR)
        return path

    def _run(self, dry_run=False):
        reaper = self._verb("reap", self.reap_answers)
        closer = self._verb("close", self.close_answers)
        return tick_mod.tick(self.sessions, dry_run, reaper_path=reaper, closer_path=closer)

    def _calls(self):
        if not os.path.exists(self.calls):
            return []
        with open(self.calls, encoding="utf-8") as h:
            return [line.strip() for line in h if line.strip()]

    def test_a_closed_candidate_never_reaches_close_worker(self):
        self._session("sid-a")
        rows, ok = self._run()
        self.assertTrue(ok)
        self.assertEqual(rows, [("sid-a", "reap", "closed")])
        self.assertEqual(self._calls(), ["reap sid-a"])

    def test_task_terminal_routes_to_close_worker(self):
        self._session("sid-a")
        self.reap_answers["sid-a"] = [1, "task-terminal"]
        rows, ok = self._run()
        self.assertTrue(ok)
        self.assertEqual(rows, [("sid-a", "close-worker", "closed")])
        self.assertEqual(self._calls(), ["reap sid-a", "close sid-a"])

    def test_any_other_refusal_is_final(self):
        self._session("sid-a")
        self.reap_answers["sid-a"] = [1, "wrapper-busy"]
        rows, ok = self._run()
        self.assertTrue(ok, "a refusal is the guards working, not a fault")
        self.assertEqual(rows, [("sid-a", "reap", "wrapper-busy")])
        self.assertEqual(self._calls(), ["reap sid-a"])

    def test_close_worker_refusal_is_reported(self):
        self._session("sid-a")
        self.reap_answers["sid-a"] = [1, "task-terminal"]
        self.close_answers["sid-a"] = [1, "gate-held"]
        rows, ok = self._run()
        self.assertTrue(ok)
        self.assertEqual(rows, [("sid-a", "close-worker", "gate-held")])

    def test_an_unexpected_exit_is_a_fault(self):
        self._session("sid-a")
        self.reap_answers["sid-a"] = [2, "usage"]
        rows, ok = self._run()
        self.assertFalse(ok)
        self.assertEqual(rows[0][2], "error:usage")

    def test_dry_run_reaches_both_verbs(self):
        self._session("sid-a")
        self._session("sid-b")
        self.reap_answers["sid-b"] = [1, "task-terminal"]
        rows, ok = self._run(dry_run=True)
        self.assertTrue(ok)
        self.assertEqual(
            sorted(rows),
            [("sid-a", "reap", "would-close"), ("sid-b", "close-worker", "would-close")],
        )
        self.assertIn("reap sid-a --dry-run", self._calls())
        self.assertIn("close sid-b --dry-run", self._calls())

    def test_tab_and_busy_sessions_are_never_candidates(self):
        self._session("sid-tab", entrypoint="cli")
        self._session("sid-busy", status="busy")
        rows, ok = self._run()
        self.assertTrue(ok)
        self.assertEqual(rows, [])
        self.assertEqual(self._calls(), [])


class TestMain(unittest.TestCase):
    def test_an_empty_registry_exits_zero_with_a_summary(self):
        tmp = tempfile.mkdtemp(prefix="idle-reaper-main-")
        try:
            done = subprocess.run(
                [sys.executable, SCRIPT, "--sessions-dir", tmp, "--dry-run"],
                capture_output=True,
                text=True,
                timeout=60,
            )
            self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
            self.assertIn("candidates=0 closed=0 refused=0", done.stdout)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
