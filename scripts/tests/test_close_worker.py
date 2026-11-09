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

    def _run(self, *extra, holds=None):
        env = dict(os.environ)
        env["SUPERVISOR_SESSIONS_DIR"] = self.sessions
        env["SUPERVISOR_VAULTS_ROOT"] = self.vaults
        env["SUPERVISOR_SESSION_HOLDS"] = holds or os.path.join(self.tmp, "no-holds.json")
        env["ATTENTION_STATE_DIR"] = self.attention
        env["ATTENTION_STORE_URL"] = self.store_url
        env["PROJECTS_DIR"] = self.projects
        env.pop("ATTENTION_STORE_TOKEN", None)
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


if __name__ == "__main__":
    unittest.main()
