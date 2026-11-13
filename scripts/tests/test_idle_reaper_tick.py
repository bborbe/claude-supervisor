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
        self.assertEqual(rows, [("sid-a", "reap", "closed", tick_mod.TRIGGER_IDLE)])
        self.assertEqual(self._calls(), ["reap sid-a"])

    def test_task_terminal_routes_to_close_worker(self):
        self._session("sid-a")
        self.reap_answers["sid-a"] = [1, "task-terminal"]
        rows, ok = self._run()
        self.assertTrue(ok)
        self.assertEqual(rows, [("sid-a", "close-worker", "closed", tick_mod.TRIGGER_IDLE)])
        self.assertEqual(self._calls(), ["reap sid-a", "close sid-a"])

    def test_any_other_refusal_is_final(self):
        self._session("sid-a")
        self.reap_answers["sid-a"] = [1, "wrapper-busy"]
        rows, ok = self._run()
        self.assertTrue(ok, "a refusal is the guards working, not a fault")
        self.assertEqual(rows, [("sid-a", "reap", "wrapper-busy", tick_mod.TRIGGER_IDLE)])
        self.assertEqual(self._calls(), ["reap sid-a"])

    def test_an_unanchored_close_carries_its_own_token(self):
        self._session("sid-a")
        self.reap_answers["sid-a"] = [0, "unanchored-closed"]
        rows, ok = self._run()
        self.assertTrue(ok)
        self.assertEqual(
            rows, [("sid-a", "reap", "unanchored-closed", tick_mod.TRIGGER_IDLE)]
        )
        self.assertEqual(self._calls(), ["reap sid-a"])

    def test_an_unanchored_close_in_dry_run_keeps_the_distinction(self):
        self._session("sid-a")
        self.reap_answers["sid-a"] = [0, "unanchored-closed"]
        rows, ok = self._run(dry_run=True)
        self.assertTrue(ok)
        self.assertEqual(
            rows, [("sid-a", "reap", "would-unanchored-close", tick_mod.TRIGGER_IDLE)]
        )

    def test_close_worker_refusal_is_reported(self):
        self._session("sid-a")
        self.reap_answers["sid-a"] = [1, "task-terminal"]
        self.close_answers["sid-a"] = [1, "gate-held"]
        rows, ok = self._run()
        self.assertTrue(ok)
        self.assertEqual(rows, [("sid-a", "close-worker", "gate-held", tick_mod.TRIGGER_IDLE)])

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
            [
                ("sid-a", "reap", "would-close", tick_mod.TRIGGER_IDLE),
                ("sid-b", "close-worker", "would-close", tick_mod.TRIGGER_IDLE),
            ],
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
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="idle-reaper-main-")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run(self, env):
        return subprocess.run(
            [sys.executable, SCRIPT, "--sessions-dir", self.tmp, "--dry-run"],
            capture_output=True,
            text=True,
            env=env,
            timeout=60,
        )

    def test_an_empty_registry_exits_zero_with_a_summary(self):
        env = dict(os.environ)
        # The verbs' own override, so the test does not depend on vault-cli being installed.
        env["SUPERVISOR_VAULT_CONFIG"] = os.path.join(self.tmp, "vault-config.json")
        done = self._run(env)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertIn("candidates=0 closed 0 past the 15-min trigger · closed 0 with no wait · refused 0", done.stdout)

    def test_the_summary_counts_reconcile_with_the_lines_written(self):
        """The accounting must equal the per-session lines, not a second tally that can drift.

        `summary_line` takes the very rows the tick printed, so this pins the two counts
        against a row set rather than re-deriving them.
        """
        rows = [
            ("sid-a", "reap", "closed", tick_mod.TRIGGER_IDLE),
            ("sid-b", "close-worker", "closed", tick_mod.TRIGGER_IDLE),
            ("sid-c", "reap", "wrapper-busy", tick_mod.TRIGGER_IDLE),
        ]
        line = tick_mod.summary_line(rows)
        self.assertEqual(
            line,
            "candidates=3 closed 2 past the 15-min trigger · closed 0 with no wait · refused 1",
        )

    def test_a_no_wait_close_moves_the_second_count_not_the_first(self):
        """The point of deriving it: a close that skips IDLE_FLOOR must land in the other count."""
        rows = [
            ("sid-a", "reap", "closed", tick_mod.TRIGGER_IDLE),
            ("sid-b", "reap", "closed", "no-wait"),
        ]
        line = tick_mod.summary_line(rows)
        self.assertIn("closed 1 past the 15-min trigger", line)
        self.assertIn("closed 1 with no wait", line)

    def test_a_dry_run_summary_counts_would_close_as_a_close(self):
        rows = [("sid-a", "reap", "would-close", tick_mod.TRIGGER_IDLE), ("sid-b", "reap", "watcher-alive", tick_mod.TRIGGER_IDLE)]
        self.assertIn("candidates=2 closed 1 past the 15-min trigger", tick_mod.summary_line(rows))

    def test_an_unanchored_close_counts_as_a_close(self):
        rows = [("sid-a", "reap", "unanchored-closed", tick_mod.TRIGGER_IDLE)]
        self.assertIn("closed 1 past the 15-min trigger", tick_mod.summary_line(rows))
        self.assertIn("refused 0", tick_mod.summary_line(rows))

    def test_a_missing_vault_cli_is_a_fault_not_a_quiet_tick(self):
        """Measured 2026-11-10: with no `vault-cli` on PATH, every candidate reads
        `task-unresolved`. A tick that closes nothing must not exit 0 as if it were healthy."""
        empty_bin = os.path.join(self.tmp, "bin")
        os.makedirs(empty_bin)
        env = {"HOME": os.environ.get("HOME", self.tmp), "PATH": empty_bin}
        done = self._run(env)
        self.assertEqual(done.returncode, 1, done.stdout + done.stderr)
        self.assertIn("vault-cli", done.stdout)
        self.assertNotIn("candidates=", done.stdout, "it must stop before running any verb")


if __name__ == "__main__":
    unittest.main()
