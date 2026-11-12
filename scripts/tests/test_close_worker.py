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

import contextlib
import importlib.util
import io
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
        "entrypoint": "cli",
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

    def _verdict(self, handed_off=False, **fm):
        return cw.task_verdict(("/v/25 Tasks/x.md", fm, handed_off), self.today)

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

    def test_handed_off_passes_while_in_progress(self):
        """A session the task was handed off from closes without waiting for terminality."""
        self.assertIsNone(self._verdict(handed_off=True, status="in_progress"))

    def test_handed_off_passes_after_completion(self):
        self.assertIsNone(self._verdict(handed_off=True, status="completed"))


class TestFindTask(unittest.TestCase):
    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp())
        self.vaults = os.path.join(self.tmp, "vaults")
        self.tasks = os.path.join(self.vaults, "somevault", "25 Tasks")
        os.makedirs(self.tasks)
        self._config({"somevault": "25 Tasks"})

    def _config(self, mapping):
        """Pin the vault config to the fixture: {vault name: tasks_dir}."""
        prior = cw._VAULT_CONFIG
        cw._VAULT_CONFIG = {
            os.path.realpath(os.path.join(self.vaults, name)): tasks_dir
            for name, tasks_dir in mapping.items()
        }
        self.addCleanup(setattr, cw, "_VAULT_CONFIG", prior)

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

    def test_finds_by_metrics_session_membership(self):
        """The id in `metrics_sessions` while another session owns the task is a handoff."""
        path = self._write(
            "a.md",
            f"---\nclaude_session_id: {OTHER}\nstatus: in_progress\n"
            f"metrics_sessions:\n"
            f"    - session_id: {SID}\n"
            f"      started_at: \"2026-10-20T11:51:47+02:00\"\n"
            f"    - session_id: {OTHER}\n"
            f"      started_at: \"2026-10-20T11:52:00+02:00\"\n---\n",
        )
        found = cw.find_task(SID, self.tmp, self.vaults)
        self.assertIsNotNone(found)
        self.assertEqual(found[0], path)
        self.assertTrue(found[2])

    def test_own_anchor_wins_over_membership(self):
        path = self._write(
            "a.md",
            f"---\nclaude_session_id: {SID}\n"
            f"metrics_sessions:\n"
            f"    - session_id: {SID}\n"
            f"      started_at: \"2026-10-20T11:51:47+02:00\"\n---\n",
        )
        found = cw.find_task(SID, self.tmp, self.vaults)
        self.assertEqual(found[0], path)
        self.assertFalse(found[2])

    def test_vault_walk_up_finds_the_task(self):
        """A worker whose cwd is inside its vault resolves without the vault scan."""
        nested = os.path.join(self.vaults, "somevault", "deep", "dir")
        os.makedirs(nested)
        self._write("a.md", f"---\nclaude_session_id: {SID}\nstatus: completed\n---\n")
        found = cw.find_task(SID, nested, os.path.join(self.tmp, "elsewhere"))
        self.assertIsNotNone(found)

    def test_the_cwd_vault_answers_before_the_vault_scan(self):
        """The first pass must be the cwd-derived directories, not every known vault.

        One grep over all of them exceeds its timeout on a loaded host, and `find_task`
        then reads that as "no task claims this session" — a false `task-unresolved` for a
        session that has an anchor. Measured 2026-11-09: 29 directories, 15.0 s timeout
        exceeded, the same session flipping between `would close` and `task-unresolved`.
        """
        nested = os.path.join(self.vaults, "somevault", "deep")
        os.makedirs(nested)
        self._write("a.md", f"---\nclaude_session_id: {SID}\nstatus: completed\n---\n")
        calls = []
        real = cw._grep_task

        def spy(session_id, dirs, timeout=15):
            calls.append(list(dirs))
            return real(session_id, dirs, timeout)

        cw._grep_task = spy
        try:
            found = cw.find_task(SID, nested, self.vaults)
        finally:
            cw._grep_task = real
        self.assertIsNotNone(found)
        self.assertEqual(len(calls), 1, "the cwd vault answered; no full scan should run")
        self.assertEqual(calls[0], [self.tasks])

    def test_a_task_in_another_vault_is_still_found(self):
        """The repo-worktree case: a cwd with no task dir must still reach the vault scan."""
        self._config({"somevault": "25 Tasks", "othervault": "25 Tasks"})
        nested = os.path.join(self.vaults, "somevault", "deep")
        os.makedirs(nested)
        other = os.path.join(self.vaults, "othervault", "25 Tasks")
        os.makedirs(other)
        path = os.path.join(other, "b.md")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(f"---\nclaude_session_id: {SID}\nstatus: completed\n---\n")
        found = cw.find_task(SID, nested, self.vaults)
        self.assertIsNotNone(found)
        self.assertEqual(found[0], path)

    def test_a_novel_tasks_dir_resolves_without_a_code_change(self):
        """The config names the directory; the script carries no name list.

        The other fixtures use `25 Tasks`, which the old hardcoded `TASK_DIRS` also
        matched — so they would pass against the broken implementation. This one uses a
        name no code mentions, which is the only shape that proves the config is read.
        """
        novel = os.path.join(self.vaults, "novelvault", "Task Queue")
        os.makedirs(novel)
        path = os.path.join(novel, "a.md")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(f"---\nclaude_session_id: {SID}\nstatus: completed\n---\n")
        self._config({"somevault": "25 Tasks", "novelvault": "Task Queue"})
        found = cw.find_task(SID, self.tmp, self.vaults)
        self.assertIsNotNone(found)
        self.assertEqual(found[0], path)

    def test_a_vault_with_no_tasks_dir_contributes_nothing(self):
        """`tasks_dir: null` contributes no directory and is named as the excluded vault."""
        self._config({"somevault": None})
        self.assertEqual(cw.task_dirs_for(self.tmp, self.vaults), [])
        refusal = cw.resolution_refusal(SID, os.path.join(self.vaults, "somevault"))
        self.assertEqual(refusal[0], "task-unresolved")
        self.assertIn("not among them", refusal[1])

    def test_a_random_uuid_reports_the_no_page_cause(self):
        """The two `task-unresolved` causes are distinguishable in the message."""
        self._config({"somevault": "25 Tasks"})
        refusal = cw.resolution_refusal(OTHER, self.tmp, self.vaults)
        self.assertEqual(refusal[0], "task-unresolved")
        self.assertEqual(refusal[1], f"no page carries {OTHER}")


class _StubStore:
    """A local attention store: open list, per-item reads and delivery reports.

    ⚠️ Every CLI test points at one of these — without it a headless gate check would
    read the operator's live store and the verdict would depend on the machine.
    """

    def __init__(self, case):
        import http.server

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                path = self.path.split("?", 1)[0]
                prefix = "/api/1.0/attention"
                body = None
                if path == prefix:
                    body = case.store_open
                elif path.endswith("/attempt"):
                    body = case.store_attempts.get(path[len(prefix) + 1:-len("/attempt")])
                elif path.startswith(prefix + "/"):
                    body = case.store_items.get(path[len(prefix) + 1:])
                if body is None:
                    self.send_response(404)
                    self.end_headers()
                    return
                data = json.dumps(body).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(data)

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class TestCliRefusals(unittest.TestCase):
    """The exit code and first-line token are the caller's contract."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.sessions = os.path.join(self.tmp, "sessions")
        self.repo = os.path.join(self.tmp, "repo")
        self.vaults = os.path.join(self.tmp, "vaults")
        self.tasks = os.path.join(self.vaults, "somevault", "25 Tasks")
        self.attention = os.path.join(self.tmp, "attention")
        self.projects = os.path.join(self.tmp, "projects")
        os.makedirs(self.sessions)
        os.makedirs(self.tasks)
        os.makedirs(self.attention)
        os.makedirs(self.projects)
        os.makedirs(self.repo)
        self.vault_config = os.path.join(self.tmp, "vault-config.json")
        with open(self.vault_config, "w", encoding="utf-8") as handle:
            json.dump(
                [{"path": os.path.join(self.vaults, "somevault"),
                  "tasks_dir": "25 Tasks"}],
                handle,
            )
        subprocess.run(["git", "init", "-q", self.repo], check=True)
        with open(os.path.join(self.repo, "a.txt"), "w", encoding="utf-8") as handle:
            handle.write("one\n")
        subprocess.run(["git", "-C", self.repo, "add", "a.txt"], check=True)
        subprocess.run(
            ["git", "-C", self.repo, "-c", "user.email=t@t", "-c", "user.name=t",
             "commit", "-qm", "init"],
            check=True,
        )
        self.store_open = []
        self.store_items = {}
        self.store_attempts = {}
        self.store = _StubStore(self)
        self.addCleanup(self.store.close)
        self.store_url = self.store.url

    def _registry(self, **over):
        rec = entry(SID, cwd=self.repo)
        rec.update(over)
        with open(os.path.join(self.sessions, f"{rec['pid']}.json"), "w", encoding="utf-8") as handle:
            json.dump(rec, handle)

    def _task(self, **fm):
        lines = ["---"] + [f"{k}: {v}" for k, v in fm.items()] + ["---", ""]
        with open(os.path.join(self.tasks, "t.md"), "w", encoding="utf-8") as handle:
            handle.write("\n".join(lines))

    def _run(self, *extra, holds=None, bin_dir=None, home=None):
        env = dict(os.environ)
        env["SUPERVISOR_SESSIONS_DIR"] = self.sessions
        env["SUPERVISOR_VAULTS_ROOT"] = self.vaults
        env["SUPERVISOR_VAULT_CONFIG"] = self.vault_config
        env["SUPERVISOR_SESSION_HOLDS"] = holds or os.path.join(self.tmp, "no-holds.json")
        env["ATTENTION_STATE_DIR"] = self.attention
        env["ATTENTION_STORE_URL"] = self.store_url
        env["PROJECTS_DIR"] = self.projects
        env.pop("ATTENTION_STORE_TOKEN", None)
        if bin_dir:
            env["PATH"] = bin_dir + os.pathsep + env.get("PATH", "")
        if home:
            env["HOME"] = home
        return subprocess.run(
            [sys.executable, SCRIPT, SID, *extra],
            capture_output=True, text=True, env=env, timeout=60,
        )

    def test_unknown_session_id(self):
        done = self._run()
        self.assertEqual(done.returncode, 1)
        self.assertEqual(done.stdout.splitlines()[0], "unknown-session-id")

    def test_ambiguous_pid(self):
        """Two registry entries claiming one session id are refused, never guessed at."""
        self._registry(pid=4242)
        self._registry(pid=4343)
        done = self._run()
        self.assertEqual(done.returncode, 1)
        self.assertEqual(done.stdout.splitlines()[0], "ambiguous-pid")

    def _shim(self, name, body):
        d = os.path.join(self.tmp, "bin")
        os.makedirs(d, exist_ok=True)
        path = os.path.join(d, name)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("#!/bin/sh\n" + body + "\n")
        os.chmod(path, 0o755)
        return d

    def test_tab_close_prints_the_success_line(self):
        """The tab arm closes, and the round reads `✅ closed pane …`, never `would close`.

        This line is the round's close record — the one a caller quotes to prove a tab
        row was closed rather than messaged. `jump.py` resolves the pane join from the
        session registry under the home directory, not the script's own registry env
        var, so `HOME` is redirected too; the pid sits above macOS's ceiling so the
        liveness wait is a no-op and the line always reads `dead`.
        """
        pid = 999999
        self._registry(pid=pid)
        self._task(**{"claude_session_id": SID, "status": "completed"})
        home = os.path.join(self.tmp, "home")
        jump_sessions = os.path.join(home, ".claude", "sessions")
        os.makedirs(jump_sessions, exist_ok=True)
        with open(os.path.join(jump_sessions, "%d.json" % pid), "w", encoding="utf-8") as handle:
            json.dump(entry(SID, pid=pid), handle)
        bin_dir = self._shim(
            "wezterm",
            'case "$1 $2" in\n'
            '  "cli list") echo \'[{"pane_id": 238, "tty_name": "/dev/ttys042"}]\' ;;\n'
            '  *) exit 0 ;;\n'
            'esac',
        )
        self._shim("ps", 'echo "%d ttys042"' % pid)
        done = self._run(bin_dir=bin_dir, home=home)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertIn(
            "✅ closed pane 238 · pid %d dead · registry entry removed" % pid, done.stdout
        )
        self.assertNotIn("would close", done.stdout)

    def test_busy_target(self):
        self._registry(status="busy")
        done = self._run()
        self.assertEqual(done.stdout.splitlines()[0], "busy-target")

    def test_kind_is_not_the_headless_signal(self):
        # Pins the fix: a record claiming `kind: headless` but carrying a real tab entrypoint
        # is a TAB session. Keying on `kind` again would send it down the SIGTERM path.
        # `sdk-cli` is the value a real SDK worker writes; `kind` is `interactive` for both
        # (measured 2026-10-20), which is why the old `kind`-keyed guard was unreachable.
        self._registry(kind="headless", entrypoint="cli")
        self._task(**{"claude_session_id": SID, "status": "completed"})
        done = self._run("--dry-run")
        self.assertIn("pane", done.stdout)
        self.assertNotIn("no pane", done.stdout)

    # --- headless (`sdk-cli`) targets -------------------------------------------------
    #
    # The same guards, and SIGTERM where the pane kill used to be. Every one of these uses a
    # REAL child process rather than a fixture pid: a fixture pid could name something else
    # on the machine, and the close path signals whatever the registry names.

    def _spawn_child(self):
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        # Reap in the background. A terminated-but-unreaped child is a ZOMBIE, and `kill -0`
        # succeeds on a zombie — without this the script's liveness probe reads it as alive
        # for the whole deadline and reports `STILL ALIVE` on a close that worked. (A real
        # worker's parent reaps it; only a test process holds the corpse.)
        threading.Thread(target=child.wait, daemon=True).start()
        self.addCleanup(lambda: None if child.poll() is not None else child.kill())
        return child

    def _transcript(self, closer):
        """A transcript private to this test — `who-needs-me.py` reads `PROJECTS_DIR`.

        The override is what makes it private, and it is load-bearing: every gate-held case
        writes the same fixture session id, and `transcript_path()` resolves it as
        `next(iter(glob(PROJECTS_DIR/*/<sid>.jsonl)))` — an arbitrary pick among matches,
        memoized. Written into the shared user-level projects dir, a transcript collides with
        any concurrent run's and with any earlier run's whose cleanup never ran (SIGKILL, a
        timeout), and the gate check then reads the wrong closer. Measured 2026-11-09:
        38/50 failures with one such leftover present, 0/50 without it.
        """
        proj = tempfile.mkdtemp(prefix="close-worker-test-", dir=self.projects)
        self.addCleanup(shutil.rmtree, proj, True)
        with open(os.path.join(proj, f"{SID}.jsonl"), "w", encoding="utf-8") as handle:
            handle.write(json.dumps({
                "type": "assistant",
                "message": {"content": [{"type": "text", "text": f"Working.\n\n👤 You: {closer}"}]},
            }) + "\n")

    def test_sdk_cli_refuses_on_non_terminal_task(self):
        """No pane is not a finished task: the task gate still runs for a headless record."""
        child = self._spawn_child()
        self._registry(entrypoint="sdk-cli", pid=child.pid)
        self._task(**{"claude_session_id": SID, "status": "in_progress"})
        done = self._run()
        self.assertEqual(done.returncode, 1)
        self.assertEqual(done.stdout.splitlines()[0], "task-not-terminal")
        self.assertIsNone(child.poll(), "a refusal must send no signal")

    def test_sdk_cli_refuses_on_busy(self):
        child = self._spawn_child()
        self._registry(entrypoint="sdk-cli", pid=child.pid, status="busy")
        done = self._run()
        self.assertEqual(done.stdout.splitlines()[0], "busy-target")
        self.assertIsNone(child.poll(), "a refusal must send no signal")

    def test_sdk_cli_refuses_on_gate(self):
        """The gate check is not skipped just because the target has no pane.

        Its own mirrored card is open, so the board can still answer it.
        """
        child = self._spawn_child()
        self._registry(entrypoint="sdk-cli", pid=child.pid)
        self._task(**{"claude_session_id": SID, "status": "completed"})
        self._transcript("approve: rm -rf /tmp/nothing")
        self.store_open.append({"item_id": "f" * 32, "producer_id": SID, "state": "open"})
        done = self._run()
        self.assertEqual(done.stdout.splitlines()[0], "gate-held")
        self.assertIsNone(child.poll(), "a refusal must send no signal")

    def test_sdk_cli_refuses_on_a_named_open_card(self):
        child = self._spawn_child()
        self._registry(entrypoint="sdk-cli", pid=child.pid)
        self._task(**{"claude_session_id": SID, "status": "completed"})
        card = "a" * 32
        self.store_items[card] = {"item_id": card, "state": "open"}
        self._transcript(f"review: card {card} — reply `ok`")
        done = self._run()
        self.assertEqual(done.stdout.splitlines()[0], "gate-held")
        self.assertIsNone(child.poll(), "a refusal must send no signal")

    def test_sdk_cli_closes_on_a_gate_no_channel_can_answer(self):
        """No pane, no open card of its own, the named card closed unanswered."""
        child = self._spawn_child()
        self._registry(entrypoint="sdk-cli", pid=child.pid)
        self._task(**{"claude_session_id": SID, "status": "completed"})
        card = "b" * 32
        self.store_items[card] = {"item_id": card, "state": "closed"}
        self._transcript(f"review: dismiss card {card} — reply `dismissed`")
        done = self._run()
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertIn("closed", done.stdout)
        self.assertEqual(child.wait(timeout=10), -signal.SIGTERM)

    def test_sdk_cli_answered_but_never_delivered_is_unanswerable(self):
        """`ANSWERED` in the store is not a release until a delivery is recorded."""
        child = self._spawn_child()
        self._registry(entrypoint="sdk-cli", pid=child.pid)
        self._task(**{"claude_session_id": SID, "status": "completed"})
        card = "c" * 32
        self.store_items[card] = {"item_id": card, "state": "answered",
                                  "answered_at": "2026-11-05T20:00:00Z"}
        self.store_attempts[card] = {"item_id": card, "status": "never_attempted"}
        self._transcript(f"approve: card {card} — allow")
        done = self._run("--dry-run")
        self.assertNotEqual(done.stdout.splitlines()[0], "gate-held", done.stdout)
        self.assertIsNone(child.poll(), "a dry run must send no signal")

    def test_sdk_cli_store_down_fails_closed(self):
        child = self._spawn_child()
        self._registry(entrypoint="sdk-cli", pid=child.pid)
        self._task(**{"claude_session_id": SID, "status": "completed"})
        self._transcript("review: dismiss card " + "d" * 32)
        self.store_url = "http://127.0.0.1:9"
        done = self._run()
        self.assertEqual(done.stdout.splitlines()[0], "gate-held")
        self.assertIsNone(child.poll(), "a refusal must send no signal")

    def test_sdk_cli_closes_a_terminal_idle_worker(self):
        child = self._spawn_child()
        self._registry(entrypoint="sdk-cli", pid=child.pid)
        self._task(**{"claude_session_id": SID, "status": "completed"})
        done = self._run()
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertIn("closed", done.stdout)
        self.assertEqual(child.wait(timeout=10), -signal.SIGTERM)
        self.assertFalse(os.path.exists(os.path.join(self.sessions, f"{child.pid}.json")))

    def _marker(self, state="open", age=0.0, sid=SID):
        with open(os.path.join(self.attention, f"{sid}.tool.json"), "w", encoding="utf-8") as handle:
            json.dump({"session_id": sid, "state": state, "ts": time.time() - age,
                       "detail": "Bash"}, handle)

    def test_open_tool_marker_refuses_as_busy(self):
        """A session parked inside a call reads `idle` and writes no closer — the marker is it.

        Without this guard a headless worker parked on the supervisor's permission channel
        looks idle, looks ungated, and is SIGTERMed with its question discarded.
        """
        child = self._spawn_child()
        self._registry(entrypoint="sdk-cli", pid=child.pid)
        self._task(**{"claude_session_id": SID, "status": "completed"})
        self._marker(age=60)
        done = self._run()
        self.assertEqual(done.stdout.splitlines()[0], "busy-target")
        self.assertIsNone(child.poll(), "a refusal must send no signal")

    def test_tab_session_ignores_the_tool_marker(self):
        """`status` already moves for a tab session; the marker must not add a 20-min delay."""
        self._registry(entrypoint="cli")
        self._task(**{"claude_session_id": SID, "status": "completed"})
        self._marker(age=60)
        done = self._run("--dry-run")
        self.assertNotEqual(done.stdout.splitlines()[0], "busy-target")

    def test_stale_tool_marker_does_not_refuse(self):
        """Past the window an open marker is the stuck path's shape, not a live call."""
        child = self._spawn_child()
        self._registry(entrypoint="sdk-cli", pid=child.pid)
        self._task(**{"claude_session_id": SID, "status": "completed"})
        self._marker(age=cw.TOOL_MARKER_WINDOW + 60)
        done = self._run()
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertIn("closed", done.stdout)

    def test_unmeasured_entrypoint_is_never_signalled(self):
        """Only `sdk-cli` is measured pane-less; an unknown value must not aim a SIGTERM.

        It falls through to the pane join, which is fail-safe — an operational `❌ error:`,
        never a close.
        """
        child = self._spawn_child()
        self._registry(entrypoint="something-new", pid=child.pid)
        self._task(**{"claude_session_id": SID, "status": "completed"})
        done = self._run()
        self.assertIsNone(child.poll(), "an unmeasured entrypoint must not be signalled")
        self.assertIn("no single live pane", done.stdout)

    def test_sdk_cli_dry_run_signals_nothing(self):
        child = self._spawn_child()
        self._registry(entrypoint="sdk-cli", pid=child.pid)
        self._task(**{"claude_session_id": SID, "status": "completed"})
        done = self._run("--dry-run")
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertIn("would close", done.stdout)
        self.assertIsNone(child.poll(), "--dry-run must not signal")

    def test_the_close_record_names_the_full_session_id(self):
        """The round prints this header as its close record, and the refusal that follows a
        close (`restart-worker.py`'s `task-terminal`) names the same full id. The two are
        joined on it, so an 8-char prefix here leaves the refusal unjoinable and it reads as
        an unrelated event — the defect measured on session 2e540d65, 2026-11-11."""
        child = self._spawn_child()
        self._registry(entrypoint="sdk-cli", pid=child.pid)
        self._task(**{"claude_session_id": SID, "status": "completed"})
        done = self._run("--dry-run")
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertIn(f"session {SID}", done.stdout)

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
        # The round joins this refusal to the close line that follows it on the full id, so
        # a detail naming only the task path cannot be joined. Measured 2026-11-11: two of
        # four refusal lines in a live round were unjoinable for exactly this reason.
        self.assertIn(SID, done.stdout)

    def test_dry_run_passes_every_guard_and_closes_nothing(self):
        """A terminal task passes every guard; `--dry-run` must stop before the close."""
        self._registry()
        self._task(**{"claude_session_id": SID, "status": "completed"})
        done = self._run("--dry-run")
        out = done.stdout
        # The pane join is unreachable from a fixture registry (no tty), so the run ends
        # in the operational-error branch — never a refusal token, and never a close.
        self.assertNotIn(done.stdout.splitlines()[0], {
            "unknown-session-id", "ambiguous-pid", "held-session", "busy-target",
            "dirty-worktree", "gate-held", "task-unresolved", "task-not-terminal",
        })
        # Every guard passed, so the only remaining stop is the pane join. That branch is
        # an operational error (`❌ error:`), never a refusal token — the distinction the
        # command file's caller branches on.
        self.assertIn("❌ error:", out)
        self.assertIn("no single live pane", out)

    # --- goal / topic anchor -------------------------------------------------------------

    def _page(self, kind, **fm):
        sub = "24 Goals" if kind == "goal" else "23 Topics"
        d = os.path.join(self.vaults, "somevault", sub)
        os.makedirs(d, exist_ok=True)
        lines = ["---"] + [f"{k}: {v}" for k, v in fm.items()] + ["---", ""]
        path = os.path.join(d, "p.md")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("\n".join(lines))
        return path

    def test_goal_holder_on_live_goal_refuses(self):
        for kind in ("goal", "topic"):
            with self.subTest(kind=kind):
                self._registry()
                path = self._page(kind, claude_session_id=SID, status="in_progress")
                done = self._run()
                self.assertEqual(done.stdout.splitlines()[0], "task-not-terminal")
                self.assertIn(path, done.stdout)
                os.remove(path)

    def test_completed_goal_resolves_and_passes_every_guard(self):
        """A completed goal resolves the session; `--dry-run` names the page and closes nothing."""
        for kind in ("goal", "topic"):
            with self.subTest(kind=kind):
                child = self._spawn_child()
                self._registry(entrypoint="sdk-cli", pid=child.pid)
                path = self._page(kind, claude_session_id=SID, status="completed")
                done = self._run("--dry-run")
                self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
                self.assertIn(f"{kind}: {path} (status completed, held, session {SID})", done.stdout)
                self.assertIn("would close", done.stdout)
                self.assertIsNone(child.poll(), "--dry-run must not signal")
                os.remove(path)
                os.remove(os.path.join(self.sessions, f"{child.pid}.json"))

    def test_handed_off_task_does_not_outrank_a_held_live_goal(self):
        """Review finding: a metrics-only task match is weaker than a page still held."""
        self._registry()
        with open(os.path.join(self.tasks, "t.md"), "w", encoding="utf-8") as handle:
            handle.write(
                f"---\nclaude_session_id: {OTHER}\nstatus: in_progress\nmetrics_sessions:\n"
                f"    - session_id: {SID}\n      started_at: \"2026-11-04T10:00:00+01:00\"\n---\n"
            )
        path = self._page("goal", claude_session_id=SID, status="in_progress")
        done = self._run()
        self.assertEqual(done.stdout.splitlines()[0], "task-not-terminal")
        self.assertIn(path, done.stdout)

    def test_completed_goal_does_not_free_a_held_live_topic(self):
        self._registry()
        self._page("goal", claude_session_id=SID, status="completed")
        path = self._page("topic", claude_session_id=SID, status="in_progress")
        done = self._run()
        self.assertEqual(done.stdout.splitlines()[0], "task-not-terminal")
        self.assertIn(path, done.stdout)

    def test_handed_over_goal_closes_while_in_progress(self):
        child = self._spawn_child()
        self._registry(entrypoint="sdk-cli", pid=child.pid)
        d = os.path.join(self.vaults, "somevault", "24 Goals")
        os.makedirs(d, exist_ok=True)
        path = os.path.join(d, "p.md")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(
                f"---\nclaude_session_id: {OTHER}\nstatus: in_progress\nmetrics_sessions:\n"
                f"    - session_id: {SID}\n      started_at: \"2026-11-04T10:00:00+01:00\"\n---\n"
            )
        done = self._run("--dry-run")
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertIn(f"goal: {path} (status in_progress, handed off to {OTHER}, session {SID})", done.stdout)
        self.assertIsNone(child.poll(), "--dry-run must not signal")

    def test_live_task_wins_over_completed_goal(self):
        self._registry()
        self._page("goal", claude_session_id=SID, status="completed")
        self._task(**{"claude_session_id": SID, "status": "in_progress"})
        done = self._run()
        self.assertEqual(done.stdout.splitlines()[0], "task-not-terminal")
        self.assertIn("t.md", done.stdout)


class TestPageVerdict(unittest.TestCase):
    def setUp(self):
        import datetime
        self.today = datetime.date(2026, 10, 20)

    def _verdict(self, handed_off=False, kind="goal", **fm):
        return cw.page_verdict((kind, "/v/24 Goals/g.md", fm, handed_off), self.today)

    def test_holder_of_live_goal_refuses(self):
        refusal, detail = self._verdict(status="in_progress")
        self.assertEqual(refusal, "task-not-terminal")
        self.assertIn("still holds it", detail)

    def test_hold_is_not_a_goal_end_state(self):
        """Unlike a task, a goal on `hold` is still its manager's — parked, not finished."""
        refusal, _ = self._verdict(status="hold")
        self.assertEqual(refusal, "task-not-terminal")

    def test_completed_and_aborted_pass(self):
        self.assertIsNone(self._verdict(status="completed"))
        self.assertIsNone(self._verdict(status="aborted"))

    def test_future_defer_passes(self):
        self.assertIsNone(self._verdict(status="in_progress", defer_date="2026-12-01"))

    def test_past_defer_refuses(self):
        refusal, _ = self._verdict(status="in_progress", defer_date="2026-01-01")
        self.assertEqual(refusal, "task-not-terminal")

    def test_handed_over_passes_while_in_progress(self):
        self.assertIsNone(self._verdict(handed_off=True, status="in_progress"))

    def test_topic_same_terms(self):
        self.assertIsNone(self._verdict(kind="topic", status="completed"))
        refusal, _ = self._verdict(kind="topic", status="in_progress")
        self.assertEqual(refusal, "task-not-terminal")


class TestFindPages(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.vaults = os.path.join(self.tmp, "vaults")
        self.goals = os.path.join(self.vaults, "somevault", "24 Goals")
        self.topics = os.path.join(self.vaults, "somevault", "23 Topics")
        os.makedirs(self.goals)
        os.makedirs(self.topics)

    def _write(self, d, body, name="p.md"):
        path = os.path.join(d, name)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(body)
        return path

    def _find(self):
        return cw.find_pages(SID, self.tmp, self.vaults)

    def test_finds_goal_by_claude_session_id(self):
        path = self._write(self.goals, f"---\nclaude_session_id: {SID}\nstatus: in_progress\n---\n")
        self.assertEqual(self._find(), ([("goal", path, cw.frontmatter(path), False)], [], None))

    def test_finds_topic_by_claude_session_id(self):
        path = self._write(self.topics, f"---\nclaude_session_id: {SID}\n---\n")
        held, _, _ = self._find()
        self.assertEqual([h[:2] for h in held], [("topic", path)])

    def test_finds_handover_by_metrics_session(self):
        path = self._write(
            self.goals,
            f"---\nclaude_session_id: {OTHER}\nstatus: in_progress\n"
            f"metrics_sessions:\n    - session_id: {SID}\n"
            f"      started_at: \"2026-11-04T10:00:00+01:00\"\n---\n",
        )
        held, handed, _ = self._find()
        self.assertEqual(held, [])
        self.assertEqual([h[:2] for h in handed], [("goal", path)])
        self.assertTrue(handed[0][3])

    def test_collects_every_held_page(self):
        """Pages stay stamped after they end, so all of them are returned, not the first."""
        self._write(self.goals, f"---\nclaude_session_id: {SID}\nstatus: completed\n---\n")
        self._write(self.topics, f"---\nclaude_session_id: {SID}\nstatus: in_progress\n---\n")
        held, _, _ = self._find()
        self.assertEqual([h[0] for h in held], ["goal", "topic"])

    def test_body_mention_is_not_ownership(self):
        self._write(self.goals, f"---\nclaude_session_id: {OTHER}\n---\n\nsee {SID}\n")
        self.assertEqual(self._find(), ([], [], None))

    def test_23_goals_is_a_goal_dir(self):
        d = os.path.join(self.vaults, "othervault", "23 Goals")
        os.makedirs(d)
        path = self._write(d, f"---\nclaude_session_id: {SID}\n---\n")
        held, _, _ = self._find()
        self.assertEqual([h[:2] for h in held], [("goal", path)])

    def test_grep_error_is_reported_not_skipped(self):
        """grep exit 2 is an error, not "no match" — the caller must refuse on it."""
        path = self._write(self.goals, f"---\nclaude_session_id: {SID}\n---\n")
        os.chmod(path, 0)
        self.addCleanup(os.chmod, path, 0o644)
        _, _, error = self._find()
        self.assertIsNotNone(error)
        self.assertIn("goal lookup failed", error)


class _FakeJump:
    """A `jump.py` stand-in whose WezTerm listing changes between calls.

    `pane_for_session` is exercised for real — only the transport is faked — so the
    successive-listing race is driven without a `sleep` or a live mux.
    """

    def __init__(self, *listings):
        self._listings = list(listings)
        self.list_calls = 0

    def wezterm_panes(self):
        index = min(self.list_calls, len(self._listings) - 1)
        self.list_calls += 1
        return self._listings[index]

    def pane_sessions(self, pmap):
        return {pane: p["session"] for pane, p in pmap.items() if p.get("session")}


def _pane(pane_id, session):
    return {"pane_id": pane_id, "tty_name": f"/dev/ttys{pane_id}", "session": session}


class _Kill:
    """Records every `wezterm` invocation, so a refusal can prove none happened."""

    def __init__(self, returncode=0, stderr=""):
        self.calls = []
        self.returncode = returncode
        self.stderr = stderr

    def __call__(self, argv, **_kwargs):
        self.calls.append(argv)
        return self


class TestTabKill(unittest.TestCase):
    """A pane id is a lease: the tab arm must re-resolve it before it kills."""

    def _kill(self, jump, pane):
        out = io.StringIO()
        runner = _Kill()
        with contextlib.redirect_stdout(out):
            rc = cw.tab_kill(jump, SID, pane, runner=runner)
        return rc, runner, out.getvalue()

    def test_unchanged_pane_is_killed(self):
        """`tab_kill` re-resolves; the same id means the lease still holds."""
        rc, runner, out = self._kill(_FakeJump({"238": _pane(238, SID)}), "238")
        self.assertEqual(rc, 0, out)
        self.assertEqual(runner.calls, [["wezterm", "cli", "kill-pane", "--pane-id", "238"]])

    def test_reassigned_pane_id_refuses_and_never_kills(self):
        """The race: the session moved to 301 and 238 now belongs to someone else."""
        after = {"238": _pane(238, OTHER), "301": _pane(301, SID)}
        rc, runner, out = self._kill(_FakeJump(after), "238")
        self.assertEqual(rc, 1, out)
        self.assertEqual(runner.calls, [], "a reassigned id must never be killed")
        self.assertIn("changed before the kill", out)
        self.assertIn("pane 238 → 301", out)

    def test_pane_gone_refuses(self):
        after = {"238": _pane(238, OTHER)}
        rc, runner, out = self._kill(_FakeJump(after), "238")
        self.assertEqual(rc, 1, out)
        self.assertEqual(runner.calls, [])
        self.assertIn("no single live pane maps to it", out)

    def test_unreadable_listing_refuses(self):
        """A failed `wezterm cli list` is not an empty answer — it is not a clearance."""
        rc, runner, out = self._kill(_FakeJump(None), "238")
        self.assertEqual(rc, 1, out)
        self.assertEqual(runner.calls, [])
        self.assertIn("wezterm cli list unavailable", out)

    def test_wezterm_refusal_is_an_error(self):
        runner = _Kill(returncode=1, stderr="no such pane")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = cw.tab_kill(_FakeJump({"238": _pane(238, SID)}), SID, "238", runner=runner)
        self.assertEqual(rc, 1)
        self.assertIn("wezterm refused to close pane 238", out.getvalue())


class TestGrepTask(unittest.TestCase):
    """The task lookup must not silently miss a task that is there.

    Measured 2026-11-12: `grep -rl` over the 11 375-file `private-personal/25 Tasks` took
    19.6 s, past `_grep_task`'s 15 s timeout, and a timeout is indistinguishable from "no
    task claims this session" — a false `task-unresolved` on the close side, and on the
    resume side `task is None` means "no anchor, allow the resume", so the terminal-task
    guard went inert for a task that was plainly there. `rg -l -F` over the same dir: 0.7 s.
    """

    def test_finds_a_matching_file(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "A Task.md")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write("---\nclaude_session_id: abc-123\nstatus: completed\n---\n")
            own, handed = cw._grep_task("abc-123", [d])
            self.assertIsNotNone(own, "the matcher missed a task that is plainly there")
            self.assertEqual(own[0], path)
            self.assertIsNone(handed)

    def test_prefers_ripgrep_when_it_is_installed(self):
        seen = {}

        class _Done:
            stdout = ""
            returncode = 1

        def fake_run(argv, **kw):
            seen["argv"] = argv
            return _Done()

        orig_which, orig_run = cw.shutil.which, cw.subprocess.run
        cw.shutil.which = lambda name: "/opt/homebrew/bin/rg" if name == "rg" else None
        cw.subprocess.run = fake_run
        try:
            cw._grep_task("sid", ["/tmp"])
        finally:
            cw.shutil.which, cw.subprocess.run = orig_which, orig_run
        self.assertEqual(seen["argv"][0], "rg", seen["argv"])

    def test_falls_back_to_grep_without_ripgrep(self):
        seen = {}

        class _Done:
            stdout = ""
            returncode = 1

        def fake_run(argv, **kw):
            seen["argv"] = argv
            return _Done()

        orig_which, orig_run = cw.shutil.which, cw.subprocess.run
        cw.shutil.which = lambda name: None
        cw.subprocess.run = fake_run
        try:
            cw._grep_task("sid", ["/tmp"])
        finally:
            cw.shutil.which, cw.subprocess.run = orig_which, orig_run
        self.assertEqual(seen["argv"][0], "grep", seen["argv"])


if __name__ == "__main__":
    unittest.main()
