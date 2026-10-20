#!/usr/bin/env python3
"""Tests for scripts/close-worker.py.

The script's whole claim is that a close happens only when the task is finished, so the
tests pin the three properties that make that claim true:

  1. **The task join is on `claude_session_id`, never on a body mention.** A session id
     quoted inside a task's prose is not a claim of ownership; treating it as one would
     let any task close any session.
  2. **Neither terminal nor deferred refuses.** `completed`/`aborted`/`hold` and a future
     `defer_date` are the two accepted end states; a past defer and a live status are not.
  3. **The guards are reused, and each refusal token is the first line of stdout.** The
     caller branches on that token, so a refusal that printed its report first would read
     as success.

Run: python3 -m unittest discover -s scripts/tests -v
"""

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_SCRIPTS = os.path.dirname(_HERE)
_spec = importlib.util.spec_from_file_location(
    "close_worker", os.path.join(_SCRIPTS, "close-worker.py")
)
cw = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cw)

SCRIPT = os.path.join(_SCRIPTS, "close-worker.py")
SID = "cccccccc-3333-4333-8333-333333333333"
OTHER = "dddddddd-4444-4444-8444-444444444444"


def entry(sid, **over):
    """A registry record with the fields the script reads."""
    rec = {
        "sessionId": sid,
        "pid": 4242,
        "status": "idle",
        "kind": "interactive",
        "name": "fixture",
        "cwd": "/tmp",
    }
    rec.update(over)
    return rec


class TestFrontmatter(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def _write(self, body):
        path = os.path.join(self.tmp, "task.md")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(body)
        return path

    def test_parses_flat_scalars(self):
        path = self._write("---\nstatus: completed\nphase: done\n---\n\n# Body\n")
        self.assertEqual(cw.frontmatter(path)["status"], "completed")

    def test_strips_quotes(self):
        """`vault-cli` quotes date scalars to keep them date-only."""
        path = self._write('---\ndefer_date: "2026-12-01"\n---\n')
        self.assertEqual(cw.frontmatter(path)["defer_date"], "2026-12-01")

    def test_body_keys_are_not_frontmatter(self):
        path = self._write("---\nstatus: next\n---\n\nstatus: completed\n")
        self.assertEqual(cw.frontmatter(path)["status"], "next")

    def test_no_frontmatter_is_empty(self):
        self.assertEqual(cw.frontmatter(self._write("# no frontmatter\n")), {})


class TestIsFuture(unittest.TestCase):
    def setUp(self):
        import datetime
        self.today = datetime.date(2026, 10, 20)

    def test_future_date_only(self):
        self.assertTrue(cw.is_future("2026-12-01", self.today))

    def test_past_date_only(self):
        self.assertFalse(cw.is_future("2026-01-01", self.today))

    def test_today_is_not_future(self):
        self.assertFalse(cw.is_future("2026-10-20", self.today))

    def test_rfc3339_form(self):
        """An unquoted date scalar is normalised to RFC3339 by `vault-cli`."""
        self.assertTrue(cw.is_future("2026-12-01T00:00:00Z", self.today))

    def test_unparseable_is_not_a_deferral(self):
        self.assertFalse(cw.is_future("soon", self.today))

    def test_empty_is_not_a_deferral(self):
        self.assertFalse(cw.is_future("", self.today))


class TestTaskVerdict(unittest.TestCase):
    def setUp(self):
        import datetime
        self.today = datetime.date(2026, 10, 20)

    def _verdict(self, **fm):
        return cw.task_verdict(("/v/25 Tasks/x.md", fm), self.today)

    def test_unresolved_task_refuses(self):
        refusal, _ = cw.task_verdict(None, self.today)
        self.assertEqual(refusal, "task-unresolved")

    def test_completed_passes(self):
        self.assertIsNone(self._verdict(status="completed"))

    def test_aborted_passes(self):
        self.assertIsNone(self._verdict(status="aborted"))

    def test_hold_passes(self):
        self.assertIsNone(self._verdict(status="hold"))

    def test_future_defer_passes(self):
        self.assertIsNone(self._verdict(status="in_progress", defer_date="2026-12-01"))

    def test_live_task_refuses(self):
        refusal, _ = self._verdict(status="in_progress")
        self.assertEqual(refusal, "task-not-terminal")

    def test_past_defer_refuses(self):
        refusal, _ = self._verdict(status="in_progress", defer_date="2026-01-01")
        self.assertEqual(refusal, "task-not-terminal")

    def test_missing_status_refuses(self):
        """An absent status is not a terminal one — fail closed."""
        refusal, _ = self._verdict()
        self.assertEqual(refusal, "task-not-terminal")


class TestFindTask(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.vaults = os.path.join(self.tmp, "vaults")
        self.tasks = os.path.join(self.vaults, "somevault", "25 Tasks")
        os.makedirs(self.tasks)

    def _write(self, name, body):
        path = os.path.join(self.tasks, name)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(body)
        return path

    def test_finds_by_frontmatter_field(self):
        path = self._write("a.md", f"---\nclaude_session_id: {SID}\nstatus: completed\n---\n")
        found = cw.find_task(SID, self.tmp, self.vaults)
        self.assertIsNotNone(found)
        self.assertEqual(found[0], path)

    def test_body_mention_is_not_ownership(self):
        """A session id quoted in prose must not let that task close the session."""
        self._write("a.md", f"---\nstatus: completed\n---\n\nsee session {SID}\n")
        self.assertIsNone(cw.find_task(SID, self.tmp, self.vaults))

    def test_other_session_does_not_match(self):
        self._write("a.md", f"---\nclaude_session_id: {OTHER}\nstatus: completed\n---\n")
        self.assertIsNone(cw.find_task(SID, self.tmp, self.vaults))

    def test_vault_walk_up_finds_the_task(self):
        """A worker whose cwd is inside its vault resolves without the vault scan."""
        nested = os.path.join(self.vaults, "somevault", "deep", "dir")
        os.makedirs(nested)
        self._write("a.md", f"---\nclaude_session_id: {SID}\nstatus: completed\n---\n")
        found = cw.find_task(SID, nested, os.path.join(self.tmp, "elsewhere"))
        self.assertIsNotNone(found)


class TestCliRefusals(unittest.TestCase):
    """The exit code and first-line token are the caller's contract."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.sessions = os.path.join(self.tmp, "sessions")
        self.repo = os.path.join(self.tmp, "repo")
        self.vaults = os.path.join(self.tmp, "vaults")
        self.tasks = os.path.join(self.vaults, "somevault", "25 Tasks")
        os.makedirs(self.sessions)
        os.makedirs(self.tasks)
        os.makedirs(self.repo)
        subprocess.run(["git", "init", "-q", self.repo], check=True)
        with open(os.path.join(self.repo, "a.txt"), "w", encoding="utf-8") as handle:
            handle.write("one\n")
        subprocess.run(["git", "-C", self.repo, "add", "a.txt"], check=True)
        subprocess.run(
            ["git", "-C", self.repo, "-c", "user.email=t@t", "-c", "user.name=t",
             "commit", "-qm", "init"],
            check=True,
        )

    def _registry(self, **over):
        rec = entry(SID, cwd=self.repo)
        rec.update(over)
        with open(os.path.join(self.sessions, f"{rec['pid']}.json"), "w", encoding="utf-8") as handle:
            json.dump(rec, handle)

    def _task(self, **fm):
        lines = ["---"] + [f"{k}: {v}" for k, v in fm.items()] + ["---", ""]
        with open(os.path.join(self.tasks, "t.md"), "w", encoding="utf-8") as handle:
            handle.write("\n".join(lines))

    def _run(self, *extra, holds=None):
        env = dict(os.environ)
        env["SUPERVISOR_SESSIONS_DIR"] = self.sessions
        env["SUPERVISOR_VAULTS_ROOT"] = self.vaults
        env["SUPERVISOR_SESSION_HOLDS"] = holds or os.path.join(self.tmp, "no-holds.json")
        return subprocess.run(
            [sys.executable, SCRIPT, SID, *extra],
            capture_output=True, text=True, env=env, timeout=60,
        )

    def test_unknown_session_id(self):
        done = self._run()
        self.assertEqual(done.returncode, 1)
        self.assertEqual(done.stdout.splitlines()[0], "unknown-session-id")

    def test_busy_target(self):
        self._registry(status="busy")
        done = self._run()
        self.assertEqual(done.stdout.splitlines()[0], "busy-target")

    def test_headless_target(self):
        self._registry(kind="headless")
        done = self._run()
        self.assertEqual(done.stdout.splitlines()[0], "headless-target")

    def test_held_session(self):
        self._registry()
        holds = os.path.join(self.tmp, "holds.json")
        with open(holds, "w", encoding="utf-8") as handle:
            json.dump({"holds": {SID: {"reason": "fixture hold"}}}, handle)
        done = self._run(holds=holds)
        self.assertEqual(done.stdout.splitlines()[0], "held-session")

    def test_dirty_worktree(self):
        self._registry()
        with open(os.path.join(self.repo, "a.txt"), "a", encoding="utf-8") as handle:
            handle.write("two\n")
        done = self._run()
        self.assertEqual(done.stdout.splitlines()[0], "dirty-worktree")

    def test_task_unresolved(self):
        """Clean worktree, no task claims the session — fail closed."""
        self._registry()
        done = self._run()
        self.assertEqual(done.stdout.splitlines()[0], "task-unresolved")

    def test_task_not_terminal(self):
        self._registry()
        self._task(**{"claude_session_id": SID, "status": "in_progress"})
        done = self._run()
        self.assertEqual(done.stdout.splitlines()[0], "task-not-terminal")

    def test_dry_run_passes_every_guard_and_closes_nothing(self):
        """A terminal task passes all seven guards; `--dry-run` must stop before the pane."""
        self._registry()
        self._task(**{"claude_session_id": SID, "status": "completed"})
        done = self._run("--dry-run")
        out = done.stdout
        # The pane join is unreachable from a fixture registry (no tty), so the run ends
        # in the operational-error branch — never a refusal token, and never a close.
        self.assertNotIn(done.stdout.splitlines()[0], {
            "unknown-session-id", "ambiguous-pid", "held-session", "busy-target",
            "headless-target", "dirty-worktree", "gate-held", "task-unresolved",
            "task-not-terminal",
        })
        # Every guard passed, so the only remaining stop is the pane join. That branch is
        # an operational error (`❌ error:`), never a refusal token — the distinction the
        # command file's caller branches on.
        self.assertIn("❌ error:", out)
        self.assertIn("no single live pane", out)


if __name__ == "__main__":
    unittest.main()
