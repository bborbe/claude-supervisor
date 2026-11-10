#!/usr/bin/env python3
"""Tests for scripts/reap-idle-headless.py.

The script's claim is narrow and load-bearing, so the tests pin exactly it:

  1. **It closes the process and keeps the transcript.** The resume consumer
     (`--resume <same id>`) needs the transcript and the task anchor; a change that turned
     this into a session delete must fail here.
  2. **Every guard refuses without signalling.** A refusal that killed anyway would be
     worse than no guard — the target is a live worker.
  3. **The task gate is inverted on purpose.** `task-not-terminal` is this verb's
     population; a resolved *terminal* task belongs to `close-worker.py` and is refused.

Real child processes stand in for workers, never fixture pids: the close path signals
whatever the registry names, and a fixture pid could name something else on the machine.

Run: python3 -m unittest discover -s scripts/tests -v
"""

import http.server
import importlib.util
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_SCRIPTS = os.path.dirname(_HERE)
_spec = importlib.util.spec_from_file_location(
    "reap_idle_headless", os.path.join(_SCRIPTS, "reap-idle-headless.py")
)
reap = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(reap)

SCRIPT = os.path.join(_SCRIPTS, "reap-idle-headless.py")
SID = "eeeeeeee-5555-4555-8555-555555555555"
VAULT = "private-personal"


class TestIdleSeconds(unittest.TestCase):
    def test_status_updated_at_wins(self):
        now = 1_000_000.0
        rec = {"statusUpdatedAt": int((now - 900) * 1000), "updatedAt": 0}
        self.assertAlmostEqual(reap.idle_seconds(rec, now), 900, places=1)

    def test_updated_at_is_the_fallback(self):
        now = 1_000_000.0
        rec = {"updatedAt": int((now - 60) * 1000)}
        self.assertAlmostEqual(reap.idle_seconds(rec, now), 60, places=1)

    def test_no_stamp_is_unknown(self):
        self.assertIsNone(reap.idle_seconds({}, 1_000_000.0))


class TestHelperFilter(unittest.TestCase):
    def test_mcp_and_stdio_helpers_are_helpers(self):
        for args in (
            "/Users/x/.local/bin/claude-wrapper --mcp-stdio -",
            "/Users/x/.local/bin/uv tool uvx mcp-obsidian",
            "npm exec @playwright/mcp@latest",
            "/Users/x/.bun/bin/bun run --cwd /w/claude-supervisor/server",
        ):
            self.assertTrue(reap.is_helper(args), args)

    def test_a_backgrounded_command_is_not_a_helper(self):
        for args in ("sleep 600", "/bin/sh -c 'tail -f x.log'", "python3 watch.py"):
            self.assertFalse(reap.is_helper(args), args)

    def test_a_backgrounded_uvx_is_not_a_helper(self):
        """The old substring form called this a helper; a watcher read as a helper reaps
        a process that was still working, so the match is anchored on the executable."""
        for args in ("/bin/bash -c 'uvx mcp-obsidian'", "sh -c 'npm exec evil'"):
            self.assertFalse(reap.is_helper(args), args)

    def test_a_listed_executable_running_a_watcher_is_not_a_helper(self):
        """`node mcp-watch.js` is a script, not an MCP package — the bare `mcp-` substring
        called it a helper and the watcher went unseen."""
        for args in (
            "node scripts/mcp-watch.js",
            "/usr/local/bin/node /w/repo/mcp-probe.js",
            "npx mcp-runner.py",
            "bun run mcp-thing.ts",
        ):
            self.assertFalse(reap.is_helper(args), args)

    def test_an_mcp_package_under_a_listed_executable_is_a_helper(self):
        for args in ("uvx mcp-obsidian", "npm exec @modelcontextprotocol/server-x"):
            self.assertTrue(reap.is_helper(args), args)


class TestTranscriptPath(unittest.TestCase):
    def test_both_slash_and_dot_munge_to_dash(self):
        path = reap.transcript_path("/w/github.com/acme/repo", SID, projects_dir="/p")
        self.assertEqual(path, os.path.join("/p", "-w-github-com-acme-repo", f"{SID}.jsonl"))

    def test_resolve_falls_back_to_the_glob(self):
        tmp = tempfile.mkdtemp(prefix="reap-tp-")
        try:
            other = os.path.join(tmp, "-elsewhere")
            os.makedirs(other)
            with open(os.path.join(other, f"{SID}.jsonl"), "w", encoding="utf-8") as handle:
                handle.write("{}\n")
            self.assertEqual(
                reap.resolve_transcript("/w/x", SID, projects_dir=tmp),
                os.path.join(other, f"{SID}.jsonl"),
            )
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_resolve_returns_none_when_absent(self):
        tmp = tempfile.mkdtemp(prefix="reap-tp2-")
        try:
            self.assertIsNone(reap.resolve_transcript("/w/x", SID, projects_dir=tmp))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class TestProcessStart(unittest.TestCase):
    """The pid-identity read: a string when read, `None` when it could not be — never a
    collapsed "no process", which would make a recycled pid indistinguishable from a live
    one and let a signal go to an unvetted identity."""

    def test_a_live_pid_reads_its_start_time(self):
        child = subprocess.Popen(["sleep", "60"])
        try:
            self.assertTrue(reap.process_start(child.pid))
        finally:
            child.kill()
            child.wait(timeout=10)

    def test_a_dead_pid_reads_none(self):
        child = subprocess.Popen(["true"])
        child.wait(timeout=10)
        self.assertIsNone(reap.process_start(child.pid))


class TestGuard(unittest.TestCase):
    """The predicate, driven with injected readings — no live worker needed."""

    def rec(self, **over):
        base = {
            "sessionId": SID,
            "pid": os.getpid(),
            "status": "idle",
            "entrypoint": "sdk-cli",
            "statusUpdatedAt": int((time.time() - 1200) * 1000),
        }
        base.update(over)
        return base

    def ok(self, **over):
        return reap.guard(
            self.rec(**over),
            wrapper={"state": "idle", "pending_turns": 0, "question": None},
            cards=[],
            cards_readable=True,
            children=[],
        )

    def test_all_guards_pass(self):
        self.assertIsNone(self.ok())

    def test_busy_target(self):
        self.assertEqual(self.ok(status="busy")[0], "busy-target")

    def test_not_idle_long_enough(self):
        fresh = {"statusUpdatedAt": int(time.time() * 1000)}
        self.assertEqual(self.ok(**fresh)[0], "not-idle-long-enough")

    def test_pending_turns_refuses(self):
        verdict = reap.guard(
            self.rec(),
            wrapper={"state": "idle", "pending_turns": 1, "question": None},
            cards=[],
            cards_readable=True,
            children=[],
        )
        self.assertEqual(verdict[0], "wrapper-busy")

    def test_parked_question_refuses(self):
        verdict = reap.guard(
            self.rec(),
            wrapper={"state": "idle", "pending_turns": 0, "question": "yes?"},
            cards=[],
            cards_readable=True,
            children=[],
        )
        self.assertEqual(verdict[0], "wrapper-busy")

    def test_unreadable_wrapper_fails_closed(self):
        verdict = reap.guard(
            self.rec(), wrapper=None, cards=[], cards_readable=True, children=[]
        )
        self.assertEqual(verdict[0], "wrapper-busy")

    def test_open_card_refuses(self):
        verdict = reap.guard(
            self.rec(),
            wrapper={"state": "idle", "pending_turns": 0, "question": None},
            cards=[{"item_id": "a" * 32}],
            cards_readable=True,
            children=[],
        )
        self.assertEqual(verdict[0], "gate-held")

    def test_unreadable_store_fails_closed(self):
        verdict = reap.guard(
            self.rec(),
            wrapper={"state": "idle", "pending_turns": 0, "question": None},
            cards=None,
            cards_readable=False,
            children=[],
        )
        self.assertEqual(verdict[0], "gate-held")

    def test_watcher_refuses(self):
        verdict = reap.guard(
            self.rec(),
            wrapper={"state": "idle", "pending_turns": 0, "question": None},
            cards=[],
            cards_readable=True,
            children=["1234"],
        )
        self.assertEqual(verdict[0], "watcher-alive")


class _Store(http.server.BaseHTTPRequestHandler):
    """A stub wrapper + attention store; the test sets the payloads."""

    wrapper = {}
    items = []
    mutate = None

    def do_GET(self):
        if self.path.startswith("/api/1.0/sessions/"):
            # Fires while the guard chain is still running, which is exactly the window the
            # pre-signal re-read exists for: a fixture that could only change state BEFORE
            # the run would leave that window untested.
            if _Store.mutate is not None:
                _Store.mutate()
            body = json.dumps(_Store.wrapper).encode()
        elif self.path == "/api/1.0/attention":
            body = json.dumps(_Store.items).encode()
        else:
            self.send_response(404)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


class TestScript(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="reap-idle-")
        self.sessions = os.path.join(self.tmp, "sessions")
        self.vaults = os.path.join(self.tmp, "vaults")
        self.tasks = os.path.join(self.vaults, VAULT, "25 Tasks")
        self.projects = os.path.join(self.tmp, "projects")
        os.makedirs(self.sessions)
        os.makedirs(self.tasks)
        os.makedirs(self.projects)
        self.vault_config = os.path.join(self.tmp, "vault-config.json")
        with open(self.vault_config, "w", encoding="utf-8") as handle:
            json.dump(
                [{"path": os.path.join(self.vaults, VAULT), "tasks_dir": "25 Tasks"}],
                handle,
            )

        _Store.wrapper = {"id": SID, "state": "idle", "pending_turns": 0, "question": None}
        _Store.items = []
        _Store.mutate = None
        self.httpd = http.server.HTTPServer(("127.0.0.1", 0), _Store)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.children = []

    def tearDown(self):
        for child in self.children:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=10)
        self.httpd.shutdown()
        self.httpd.server_close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    # --- fixtures ---------------------------------------------------------------------

    def _spawn(self, argv=None):
        child = subprocess.Popen(argv or ["sleep", "60"])
        self.children.append(child)
        # Reap the child the moment it dies. In production the worker's parent is the
        # wrapper, which reaps it; here the parent is this test process, and an unreaped
        # SIGTERMed child stays a zombie whose pid still answers `kill -0` — which would
        # read as "STILL ALIVE" for a close that worked.
        threading.Thread(target=child.wait, daemon=True).start()
        return child

    def _registry(self, pid, path=None, **over):
        rec = {
            "sessionId": SID,
            "pid": pid,
            "status": "idle",
            "entrypoint": "sdk-cli",
            "name": "fixture",
            "cwd": os.path.join(self.vaults, VAULT),
            "statusUpdatedAt": int((time.time() - 20 * 60) * 1000),
        }
        rec.update(over)
        path = path or os.path.join(self.sessions, f"{pid}.json")
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(rec, handle)
        return path

    def _task(self, status="in_progress", session_id=SID):
        lines = ["---", f"claude_session_id: {session_id}", f"status: {status}", "---", ""]
        path = os.path.join(self.tasks, "t.md")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("\n".join(lines))
        return path

    def _transcript(self):
        # Deliberately the implementation's own resolver, so the fixture lands where the
        # script looks — the escaping rule is pinned separately in TestTranscriptPath.
        path = reap.transcript_path(
            os.path.join(self.vaults, VAULT), SID, projects_dir=self.projects
        )
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write('{"type":"assistant"}\n')
        return path

    def _env(self):
        env = dict(os.environ)
        env["SUPERVISOR_SESSIONS_DIR"] = self.sessions
        env["SUPERVISOR_VAULTS_ROOT"] = self.vaults
        env["SUPERVISOR_VAULT_CONFIG"] = self.vault_config
        env["SUPERVISOR_PROJECTS_DIR"] = self.projects
        env["SUPERVISOR_WRAPPER_URL"] = f"http://127.0.0.1:{self.port}"
        env["ATTENTION_STORE_URL"] = f"http://127.0.0.1:{self.port}"
        return env

    def _run_env(self, env, *extra):
        return subprocess.run(
            [sys.executable, SCRIPT, SID, *extra],
            capture_output=True,
            text=True,
            env=env,
            timeout=60,
        )

    def _run(self, *extra, sessions_dir=None):
        return self._run_env(self._env(), *extra)

    def _run_with_unreadable_start_time(self):
        """Run with a `ps` shim that answers nothing for `-o lstart=`.

        The fail-closed arm is otherwise unreachable — a live pid always has a readable
        start time. The shim delegates every other form to the real `ps`, so the watcher
        read still works.
        """
        bindir = os.path.join(self.tmp, "bin")
        os.makedirs(bindir, exist_ok=True)
        shim = os.path.join(bindir, "ps")
        with open(shim, "w", encoding="utf-8") as handle:
            handle.write(
                "#!/bin/sh\n"
                'case "$*" in\n'
                "  *lstart*) exit 0 ;;\n"
                "esac\n"
                'exec /bin/ps "$@"\n'
            )
        os.chmod(shim, 0o755)
        env = self._env()
        env["PATH"] = bindir + os.pathsep + env.get("PATH", "")
        return self._run_env(env)

    # --- refusals ---------------------------------------------------------------------

    def test_unknown_session_id(self):
        done = self._run()
        self.assertEqual(done.returncode, 1)
        self.assertEqual(done.stdout.splitlines()[0], "unknown-session-id")

    def test_ambiguous_pid(self):
        child = self._spawn()
        self._registry(child.pid)
        # A second entry claiming the same session id, from a pid that is also alive.
        other = self._spawn()
        self._registry(other.pid)
        self._task()
        done = self._run()
        self.assertEqual(done.stdout.splitlines()[0], "ambiguous-pid")
        self.assertIsNone(child.poll(), "a refusal must send no signal")

    def test_already_gone_removes_the_stale_entry(self):
        """The token promises the entry is gone, so it must be gone — otherwise the same
        dead pid re-refuses on every run and the docstring is a lie."""
        child = self._spawn(["true"])
        child.wait(timeout=10)
        entry = self._registry(child.pid)
        self._task()
        done = self._run()
        self.assertEqual(done.stdout.splitlines()[0], "already-gone")
        self.assertFalse(os.path.exists(entry), "the stale registry entry must be removed")

    def test_not_headless_refuses(self):
        """A tab worker's id here would be SIGTERMed as a process, pane left open behind it."""
        child = self._spawn()
        self._registry(child.pid, entrypoint="interactive")
        self._task()
        done = self._run()
        self.assertEqual(done.stdout.splitlines()[0], "not-headless")
        self.assertIsNone(child.poll(), "a refusal must send no signal")

    def test_registry_entry_changing_during_the_guards_refuses(self):
        """The pre-signal re-read exists for this window, and its token must be the FIRST
        line — a refusal printed under the `🧹` header reads as a successful reap."""
        child = self._spawn()
        entry = self._registry(child.pid)
        other = self._spawn()
        self._task()
        self._transcript()
        _Store.mutate = lambda: self._registry(other.pid, path=entry)
        done = self._run()
        self.assertEqual(done.stdout.splitlines()[0], "ambiguous-pid")
        self.assertNotIn("🧹", done.stdout, "no success-looking header may precede a refusal")
        self.assertIsNone(child.poll(), "a refusal must send no signal")

    def test_a_turn_arriving_during_the_guards_refuses(self):
        """A worker that becomes busy mid-chain must not be signalled on the stale read."""
        child = self._spawn()
        self._registry(child.pid)
        self._task()
        self._transcript()
        _Store.mutate = lambda: self._registry(child.pid, status="busy")
        done = self._run()
        self.assertEqual(done.stdout.splitlines()[0], "busy-target")
        self.assertIsNone(child.poll(), "a refusal must send no signal")

    def test_a_continuation_arriving_during_the_guards_refuses(self):
        """The wrapper record is the authoritative 'nothing pending' read."""
        child = self._spawn()
        self._registry(child.pid)
        self._task()
        self._transcript()

        def continuation():
            _Store.wrapper = {
                "id": SID,
                "state": "idle",
                "pending_turns": 1,
                "question": None,
            }

        _Store.mutate = continuation
        done = self._run()
        self.assertEqual(done.stdout.splitlines()[0], "wrapper-busy")
        self.assertIsNone(child.poll(), "a refusal must send no signal")

    def test_an_unreadable_start_time_fails_closed(self):
        """An identity never established is never signalled — there is no fallback path."""
        child = self._spawn()
        self._registry(child.pid)
        self._task()
        self._transcript()
        done = self._run_with_unreadable_start_time()
        self.assertEqual(done.stdout.splitlines()[0], "ambiguous-pid")
        self.assertIsNone(child.poll(), "a refusal must send no signal")

    def test_busy_target_sends_no_signal(self):
        child = self._spawn()
        self._registry(child.pid, status="busy")
        self._task()
        done = self._run()
        self.assertEqual(done.stdout.splitlines()[0], "busy-target")
        self.assertIsNone(child.poll(), "a refusal must send no signal")

    def test_not_idle_long_enough_sends_no_signal(self):
        child = self._spawn()
        self._registry(child.pid, statusUpdatedAt=int(time.time() * 1000))
        self._task()
        done = self._run()
        self.assertEqual(done.stdout.splitlines()[0], "not-idle-long-enough")
        self.assertIsNone(child.poll(), "a refusal must send no signal")

    def test_pending_turn_refuses(self):
        child = self._spawn()
        self._registry(child.pid)
        self._task()
        _Store.wrapper = {"id": SID, "state": "idle", "pending_turns": 1, "question": None}
        done = self._run()
        self.assertEqual(done.stdout.splitlines()[0], "wrapper-busy")
        self.assertIsNone(child.poll(), "a refusal must send no signal")

    def test_open_card_refuses(self):
        child = self._spawn()
        self._registry(child.pid)
        self._task()
        card = "a" * 32
        _Store.items = [{"item_id": card, "producer_id": SID, "state": "open"}]
        done = self._run()
        self.assertEqual(done.stdout.splitlines()[0], "gate-held")
        self.assertIsNone(child.poll(), "a refusal must send no signal")

    def test_live_watcher_refuses(self):
        child = self._spawn(["/bin/sh", "-c", "sleep 60 & wait"])
        self._registry(child.pid)
        self._task()
        done = self._run()
        self.assertEqual(done.stdout.splitlines()[0], "watcher-alive")
        self.assertIsNone(child.poll(), "a refusal must send no signal")

    def test_terminal_task_is_refused(self):
        """A finished task belongs to close-worker.py, not here."""
        child = self._spawn()
        self._registry(child.pid)
        self._task(status="completed")
        done = self._run()
        self.assertEqual(done.stdout.splitlines()[0], "task-terminal")
        self.assertIsNone(child.poll(), "a refusal must send no signal")

    def test_unresolved_task_is_refused(self):
        child = self._spawn()
        self._registry(child.pid)
        done = self._run()
        self.assertEqual(done.stdout.splitlines()[0], "task-unresolved")

    # --- the close --------------------------------------------------------------------

    def test_dry_run_signals_nothing(self):
        child = self._spawn()
        self._registry(child.pid)
        self._task()
        self._transcript()
        done = self._run("--dry-run")
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertIn("would close", done.stdout)
        self.assertIsNone(child.poll(), "a dry run must send no signal")

    def test_reaps_the_process_and_keeps_the_transcript(self):
        child = self._spawn()
        entry = self._registry(child.pid)
        self._task()
        transcript = self._transcript()
        done = self._run()
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertEqual(child.wait(timeout=10), -signal.SIGTERM)
        self.assertFalse(os.path.exists(entry), "the registry entry must be gone")
        self.assertTrue(os.path.exists(transcript), "the transcript must survive")
        self.assertIn("transcript kept", done.stdout)

    def test_a_missing_transcript_fails_the_reap(self):
        """The transcript is what `--resume` reads; a reap that loses it must not report OK."""
        child = self._spawn()
        self._registry(child.pid)
        self._task()
        done = self._run()
        self.assertEqual(done.returncode, 1, done.stdout + done.stderr)
        self.assertIn("transcript MISSING", done.stdout)


if __name__ == "__main__":
    unittest.main()
