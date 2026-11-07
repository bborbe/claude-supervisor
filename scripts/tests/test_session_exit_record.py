#!/usr/bin/env python3
"""Tests for scripts/session-exit-record.py.

The load-bearing properties are the ones a death would expose:

- a line's field order never moves, because every reader splits on it;
- `rc` past 128 is classified as a **signal** death and named, never folded into `exited`;
- `sender` is honest in three directions: `-` on a marker, `none` on a self-inflicted exit,
  and `unavailable(<the probe's own output>)` where a signal was delivered;
- a record path is derived from the pane id and nothing else, and a pane id that could
  escape the store is refused rather than joined into a filename;
- appending never truncates, so the trap and the observer cannot lose each other's line;
- the observer names a signal only from a kernel-delivered wait status (`kqueue`
  `NOTE_EXITSTATUS`); a pid already gone when it starts is an `unknown-end` inference,
  never `signalled kill`; and it stays silent when the parent already reported an end;
- an absent record reads as *nothing*, not as an error — absence is the state this tool
  exists to make visible.

Run: python3 -m unittest discover -s scripts/tests -v
"""

import datetime
import importlib.util
import json
import os
import select
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_SCRIPTS = os.path.dirname(_HERE)
_SCRIPT = os.path.join(_SCRIPTS, "session-exit-record.py")
_spec = importlib.util.spec_from_file_location("session_exit_record", _SCRIPT)
sx = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sx)


class Classify(unittest.TestCase):
    def test_zero_is_a_clean_exit(self):
        self.assertEqual(sx.classify(0), (sx.KIND_EXITED, "-"))

    def test_a_nonzero_exit_is_still_an_exit_not_a_signal(self):
        """`rc=1` is an error exit. Calling it a signal would invent a cause."""
        self.assertEqual(sx.classify(1), (sx.KIND_EXITED, "-"))
        self.assertEqual(sx.classify(128), (sx.KIND_EXITED, "-"))

    def test_137_is_sigkill(self):
        """The one case the whole design exists for: a SIGKILLed child is observable."""
        self.assertEqual(sx.classify(137), (sx.KIND_SIGNALLED, "SIGKILL"))

    def test_the_other_named_signals(self):
        for rc, name in ((129, "SIGHUP"), (130, "SIGINT"), (143, "SIGTERM")):
            with self.subTest(rc=rc):
                self.assertEqual(sx.classify(rc), (sx.KIND_SIGNALLED, name))

    def test_an_unnamed_signal_prints_its_number(self):
        self.assertEqual(sx.classify(137 + 20), (sx.KIND_SIGNALLED, "29"))


class FormatLine(unittest.TestCase):
    def test_field_order_is_fixed(self):
        line = sx.format_line(
            "2026-10-27T00:00:00Z",
            sx.KIND_SIGNALLED,
            "2156",
            "sid",
            "41234",
            137,
            "SIGKILL",
            "unavailable(eslogger rc=1 ES_NEW_CLIENT_RESULT_ERR_NOT_PRIVILEGED)",
            sx.WRITER_TRAP,
        )
        self.assertEqual(
            line.split("\t"),
            [
                "2026-10-27T00:00:00Z",
                "kind=signalled kill",
                "pane=2156",
                "session=sid",
                "pid=41234",
                "rc=137",
                "signal=SIGKILL",
                "sender=unavailable(eslogger rc=1 ES_NEW_CLIENT_RESULT_ERR_NOT_PRIVILEGED)",
                "writer=trap",
            ],
        )

    def test_an_unknown_session_is_a_dash_never_an_empty_field(self):
        line = sx.format_line("t", sx.KIND_EXITED, "1")
        self.assertIn("\tsession=-\t", line)

    def test_an_unknown_pid_is_a_dash_never_an_empty_field(self):
        self.assertIn("\tpid=-\t", sx.format_line("t", sx.KIND_EXITED, "1"))

    def test_the_spawn_marker_is_a_start_not_an_end(self):
        """Without it an empty record cannot be told from an unwired writer."""
        line = sx.format_line("t", sx.KIND_SPAWN, "1", "sid")
        self.assertIn("kind=spawn", line)
        self.assertNotIn(sx.KIND_SPAWN, sx.END_KINDS)

    def test_the_heartbeat_is_a_marker_not_an_end(self):
        """A heartbeat proves liveness; counting it as an end would close a live record."""
        self.assertNotIn(sx.KIND_HEARTBEAT, sx.END_KINDS)
        self.assertIn(sx.KIND_HEARTBEAT, sx.KINDS)

    def test_every_end_kind_is_a_known_kind(self):
        for kind in sx.END_KINDS:
            with self.subTest(kind=kind):
                self.assertIn(kind, sx.KINDS)


class Sender(unittest.TestCase):
    """`sender` answers "who ended this process" and must never guess."""

    def test_a_marker_has_no_sender(self):
        for kind in (sx.KIND_SPAWN, sx.KIND_HEARTBEAT):
            with self.subTest(kind=kind):
                self.assertEqual(sx.sender_for(kind), "-")

    def test_a_normal_exit_was_sent_by_nobody(self):
        """`none` is a fact, not an absence: the process ended itself."""
        self.assertEqual(sx.sender_for(sx.KIND_EXITED), "none")

    def test_a_signal_death_reports_the_probe_output_not_a_guess(self):
        sender = sx.sender_for(sx.KIND_SIGNALLED)
        self.assertTrue(sender.startswith("unavailable("), sender)
        self.assertTrue(sender.endswith(")"), sender)

    def test_the_reason_is_quoted_from_the_probe_itself(self):
        """SC2: the reason is evidenced by the failing probe's own output, never a doc claim.

        On this machine the probe refuses a non-root caller; the recorded reason must then
        be that refusal, not a sentence this file composed.
        """
        sender = sx.sender_for(sx.KIND_SIGNALLED)
        self.assertRegex(sender, r"^unavailable\(eslogger ")
        self.assertNotIn("root-only", sender)
        self.assertNotIn("documentation", sender)


class RecordPath(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.mkdtemp()
        self._saved = os.environ.get("SUPERVISOR_SESSION_EXIT_DIR")
        os.environ["SUPERVISOR_SESSION_EXIT_DIR"] = self._dir

    def tearDown(self):
        if self._saved is None:
            os.environ.pop("SUPERVISOR_SESSION_EXIT_DIR", None)
        else:
            os.environ["SUPERVISOR_SESSION_EXIT_DIR"] = self._saved

    def test_keyed_on_the_pane(self):
        self.assertEqual(sx.record_path("2156"), os.path.join(self._dir, "pane-2156.log"))

    def test_a_pane_id_that_could_escape_the_store_is_refused(self):
        for bad in ("", "../etc/passwd", "a/b", "..", "."):
            with self.subTest(pane=bad):
                with self.assertRaises(ValueError):
                    sx.record_path(bad)


class StoreTestCase(unittest.TestCase):
    """Shared: an isolated store, and a helper that runs the script as a subprocess."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self._saved = os.environ.get("SUPERVISOR_SESSION_EXIT_DIR")
        os.environ["SUPERVISOR_SESSION_EXIT_DIR"] = self.dir

    def tearDown(self):
        if self._saved is None:
            os.environ.pop("SUPERVISOR_SESSION_EXIT_DIR", None)
        else:
            os.environ["SUPERVISOR_SESSION_EXIT_DIR"] = self._saved

    def _run(self, *args, env=None):
        return subprocess.run(
            [sys.executable, _SCRIPT, *args],
            capture_output=True,
            text=True,
            env={**os.environ, **(env or {})},
        )

    def _lines(self, pane="2156"):
        with open(os.path.join(self.dir, f"pane-{pane}.log"), encoding="utf-8") as handle:
            return handle.read().splitlines()


class AppendAndRead(StoreTestCase):
    def test_append_creates_the_store_and_writes_one_line(self):
        result = self._run("append", "--pane", "2156", "--rc", "0", "--now", "2026-10-27T00:00:00Z")
        self.assertEqual(result.returncode, 0, result.stderr)
        lines = self._lines()
        self.assertEqual(len(lines), 1)
        self.assertIn("kind=exited", lines[0])
        self.assertIn("pane=2156", lines[0])
        self.assertIn("rc=0", lines[0])

    def test_a_second_append_does_not_truncate_the_first(self):
        """Two writers can fire for one pane (trap + observer); neither may lose the other."""
        self._run("append", "--pane", "2156", "--rc", "0", "--now", "t1")
        self._run("append", "--pane", "2156", "--rc", "137", "--now", "t2")
        lines = self._lines()
        self.assertEqual(len(lines), 2)
        self.assertIn("rc=0", lines[0])
        self.assertIn("kind=signalled kill", lines[1])
        self.assertIn("signal=SIGKILL", lines[1])

    def test_the_appended_line_is_echoed_to_stdout(self):
        """The writer's own output is the pre-kill control a test can capture.

        Reading it from the record path instead would make "the writer was never wired"
        indistinguishable from "the writer wrote nothing".
        """
        result = self._run("append", "--pane", "2156", "--rc", "0", "--now", "t1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), self._lines()[0])

    def test_the_pid_and_writer_are_recorded(self):
        self._run("append", "--pane", "2156", "--pid", "41234", "--rc", "137", "--now", "t")
        line = self._lines()[0]
        self.assertIn("pid=41234", line)
        self.assertIn("writer=trap", line)

    def test_append_refuses_an_escaping_pane_and_writes_nothing(self):
        result = self._run("append", "--pane", "../evil", "--rc", "0")
        self.assertEqual(result.returncode, 2)
        self.assertIn("bare token", result.stderr)
        self.assertEqual(os.listdir(self.dir), [])

    def test_read_of_an_absent_record_is_empty_and_zero(self):
        result = self._run("read", "--pane", "9999")
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")

    def test_list_reports_panes_and_is_json_parseable(self):
        self._run("append", "--pane", "2156", "--rc", "0", "--now", "t1")
        result = self._run("list", "--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual([row["pane"] for row in payload["records"]], ["2156"])

    def test_list_on_a_missing_store_is_empty_not_an_error(self):
        os.environ["SUPERVISOR_SESSION_EXIT_DIR"] = os.path.join(self.dir, "absent")
        result = self._run("list", "--json")
        self.assertEqual(result.returncode, 0)
        self.assertEqual(json.loads(result.stdout)["records"], [])


class Observer(StoreTestCase):
    """The out-of-group writer: what it may claim, and what it must refuse to claim."""

    def _gone_pid(self):
        """A pid that certainly no longer exists: a child that has run and been reaped."""
        proc = subprocess.Popen([sys.executable, "-c", "pass"])
        proc.wait()
        return proc.pid

    def test_a_live_pid_gets_a_heartbeat_and_no_end(self):
        result = self._run(
            "observe",
            "--pane",
            "2156",
            "--pid",
            str(os.getpid()),
            "--ticks",
            "1",
            "--interval",
            "0",
            "--foreground",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        lines = self._lines()
        self.assertEqual(len(lines), 1)
        self.assertIn("kind=heartbeat", lines[0])
        self.assertIn("writer=observer", lines[0])

    def test_a_vanished_pid_is_recorded_as_unknown_end(self):
        result = self._run(
            "observe", "--pane", "2156", "--pid", str(self._gone_pid()), "--foreground"
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        lines = self._lines()
        self.assertEqual(len(lines), 1)
        self.assertIn("kind=unknown-end", lines[0])

    def test_a_pid_gone_before_the_watch_never_claims_a_signal(self):
        """No wait status was delivered, so absence alone is an inference, not a signal."""
        self._run("observe", "--pane", "2156", "--pid", str(self._gone_pid()), "--foreground")
        line = self._lines()[0]
        self.assertNotIn("signalled kill", line)
        self.assertIn("rc=unavailable", line)
        self.assertIn("signal=unavailable", line)

    @staticmethod
    def _skip_without_kqueue():
        if not hasattr(select, "kqueue"):
            raise unittest.SkipTest("kqueue exit-status watch is macOS/BSD only")

    def _observe_then_kill(self, sig):
        """A live target in its own group, an observer that is NOT its parent, a group kill.

        The pane-243 shape (2026-11-06): a group kill takes the trap's shell down with the
        child, so the observer is the only writer left.
        """
        self._skip_without_kqueue()
        target = subprocess.Popen(["/bin/sleep", "30"], start_new_session=True)
        observer = subprocess.Popen(
            [sys.executable, _SCRIPT, "observe", "--pane", "2156", "--pid", str(target.pid),
             "--interval", "5", "--foreground"],
            env=dict(os.environ),
        )
        log = os.path.join(self.dir, "pane-2156.log")
        deadline = time.time() + 5
        while not os.path.exists(log) and time.time() < deadline:
            time.sleep(0.05)
        if not os.path.exists(log):
            target.kill()
            observer.kill()
            self.fail("observer wrote no heartbeat within 5 s — watch never armed")
        os.killpg(target.pid, sig)
        target.wait()
        observer.wait(timeout=10)
        return [line for line in self._lines() if "kind=heartbeat" not in line]

    def test_a_group_kill_of_a_live_pid_records_the_signal(self):
        """`kqueue` `NOTE_EXITSTATUS` hands a non-parent the wait status, with no privilege."""
        ends = self._observe_then_kill(signal.SIGTERM)
        self.assertEqual(len(ends), 1, ends)
        self.assertIn("kind=signalled kill", ends[0])
        self.assertIn("rc=143", ends[0])
        self.assertIn("signal=SIGTERM", ends[0])
        self.assertIn("writer=observer", ends[0])

    def test_a_group_sigkill_is_named(self):
        ends = self._observe_then_kill(signal.SIGKILL)
        self.assertIn("signal=SIGKILL", ends[0])
        self.assertIn("rc=137", ends[0])

    def test_the_observers_end_carries_an_unavailable_sender(self):
        self._run("observe", "--pane", "2156", "--pid", str(self._gone_pid()), "--foreground")
        self.assertIn("sender=unavailable(", self._lines()[0])

    def test_the_parents_report_wins_over_the_observers_inference(self):
        """The trap runs before the shell exits, so its line is already on disk.

        The trap passes `--pid $$`, the pid the observer watches, so the end it wrote is
        this run's end.
        """
        pid = str(self._gone_pid())
        self._run("append", "--pane", "2156", "--pid", pid, "--rc", "137", "--now", "t1")
        self._run("observe", "--pane", "2156", "--pid", pid, "--foreground")
        lines = self._lines()
        self.assertEqual(len(lines), 1)
        self.assertIn("kind=signalled kill", lines[0])

    def test_an_earlier_runs_end_does_not_silence_this_run(self):
        """A pane id is reused, so one log holds many runs.

        Measured 2026-11-05 on pane 115: a 2026-10-28 `kind=exited` for another pid sat in
        the file, the observer read it as this run's end, and a killed pane left no record.
        """
        self._run("append", "--pane", "2156", "--pid", "56446", "--rc", "0", "--now", "t0")
        pid = str(self._gone_pid())
        self._run("append", "--pane", "2156", "--kind", "spawn", "--pid", pid, "--now", "t1")
        self._run("observe", "--pane", "2156", "--pid", pid, "--foreground")
        lines = self._lines()
        self.assertEqual(len(lines), 3)
        self.assertIn("kind=unknown-end", lines[2])
        self.assertIn(f"pid={pid}", lines[2])

    def test_another_pids_end_after_this_spawn_does_not_silence_this_run(self):
        """An older run's observer can outlive its pane and write into the reused log."""
        pid = str(self._gone_pid())
        self._run("append", "--pane", "2156", "--kind", "spawn", "--pid", pid, "--now", "t1")
        self._run("append", "--pane", "2156", "--pid", "56446", "--rc", "0", "--now", "t2")
        self._run("observe", "--pane", "2156", "--pid", pid, "--foreground")
        lines = self._lines()
        self.assertEqual(len(lines), 3)
        self.assertIn("kind=unknown-end", lines[2])
        self.assertIn(f"pid={pid}", lines[2])

    def test_a_log_with_an_undecodable_byte_still_gets_this_runs_end(self):
        """A bad byte must not raise inside the observer and lose the end line."""
        pid = str(self._gone_pid())
        with open(os.path.join(self.dir, "pane-2156.log"), "wb") as handle:
            handle.write(b"garbage \xff\xfe line\n")
        self._run("observe", "--pane", "2156", "--pid", pid, "--foreground")
        with open(os.path.join(self.dir, "pane-2156.log"), encoding="utf-8", errors="replace") as handle:
            tail = handle.read().splitlines()[-1]
        self.assertIn("kind=unknown-end", tail)

    def test_a_reused_pid_does_not_borrow_its_earlier_lifes_end(self):
        """A `spawn` line for the pid starts a new run; an end before it belongs to the old one."""
        pid = str(self._gone_pid())
        self._run("append", "--pane", "2156", "--pid", pid, "--rc", "0", "--now", "t0")
        self._run("append", "--pane", "2156", "--kind", "spawn", "--pid", pid, "--now", "t1")
        self._run("observe", "--pane", "2156", "--pid", pid, "--foreground")
        lines = self._lines()
        self.assertEqual(len(lines), 3)
        self.assertIn("kind=unknown-end", lines[2])

    def test_a_nonpositive_pid_is_refused(self):
        result = self._run("observe", "--pane", "2156", "--pid", "0", "--foreground")
        self.assertEqual(result.returncode, 2)
        self.assertEqual(os.listdir(self.dir), [])


#: A stand-in for `claude`: writes its own registry record, keyed on its own pid exactly as
#: Claude Code writes `~/.claude/sessions/<pid>.json`, then lives until killed.
_FAKE_CLAUDE = (
    "import json, os, sys, time\n"
    "d, sid = sys.argv[1], os.environ['FAKE_SESSION_ID']\n"
    "with open(os.path.join(d, f'{os.getpid()}.json'), 'w') as h:\n"
    "    json.dump({'pid': os.getpid(), 'sessionId': sid, 'kind': 'interactive',\n"
    "               'entrypoint': 'cli'}, h)\n"
    "time.sleep(60)\n"
)


class ObserverBindsSession(StoreTestCase):
    """A fresh spawn does not know its session id; the observer must learn it while it can.

    Measured 2026-11-06: the 06:44 and 08:50 deaths carry `session=-`, and the registry that
    could have named them (`~/.claude/sessions/<pid>.json`) is deleted when the process dies.
    So the identity has to be copied into the append-only log while the session is alive.
    """

    def setUp(self):
        super().setUp()
        self.sessions = tempfile.mkdtemp()
        self._saved_sessions = os.environ.get("SUPERVISOR_SESSIONS_DIR")
        os.environ["SUPERVISOR_SESSIONS_DIR"] = self.sessions
        self.procs = []

    def tearDown(self):
        # Kill each pane's whole group: `proc.kill()` reaches only the outer shell and would
        # leave the inner bash and the fake claude running for up to a minute.
        for proc in self.procs:
            if proc.poll() is None:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    proc.kill()
            proc.wait()
        shutil.rmtree(self.sessions, ignore_errors=True)
        if self._saved_sessions is None:
            os.environ.pop("SUPERVISOR_SESSIONS_DIR", None)
        else:
            os.environ["SUPERVISOR_SESSIONS_DIR"] = self._saved_sessions
        super().tearDown()

    def _pane(self, sid):
        """shell → bash → claude: the supervisor-spawn shape, where claude is a GRANDCHILD.

        Pane 344 (2026-11-06) is this shape. The Fleet Manager's claude is a direct child, so
        a binding that read children only would resolve on one and silently miss the other.
        """
        # Its own directory: a path through `self.sessions/..` stops resolving once tearDown
        # removes `self.sessions`.
        scratch = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, scratch, True)
        fake = os.path.join(scratch, "fake-claude.py")
        with open(fake, "w", encoding="utf-8") as handle:
            handle.write(_FAKE_CLAUDE)
        # shell → bash → python: the inner bash is a real intermediate process because the
        # trailing `; wait` stops it from exec-ing its last command.
        # The id travels in the environment: argv would be word-split by the inner shell,
        # and the malformed-id case carries a tab.
        shell = subprocess.Popen(
            ["/bin/bash", "-c", '/bin/bash -c "$0 $1 $2; wait"; wait',
             sys.executable, fake, self.sessions],
            env={**os.environ, "FAKE_SESSION_ID": sid},
            start_new_session=True,
        )
        self.procs.append(shell)
        deadline = time.time() + 5
        while len(os.listdir(self.sessions)) == 0 and time.time() < deadline:
            time.sleep(0.05)
        self.assertTrue(os.listdir(self.sessions), "fake claude never wrote its registry record")
        return shell

    def _observe(self, shell, *extra):
        observer = subprocess.Popen(
            [sys.executable, _SCRIPT, "observe", "--pane", "2156", "--pid", str(shell.pid),
             "--interval", "1", "--foreground", *extra],
            env=dict(os.environ),
        )
        self.procs.append(observer)
        return observer

    def _wait_for(self, needle, timeout=5):
        log = os.path.join(self.dir, "pane-2156.log")
        deadline = time.time() + timeout
        while time.time() < deadline:
            if os.path.exists(log) and needle in open(log, encoding="utf-8").read():
                return
            time.sleep(0.05)
        self.fail(f"{needle!r} never appeared in the record")

    def test_a_heartbeat_carries_the_session_of_a_grandchild(self):
        shell = self._pane("aaaa1111-2222-3333-4444-555566667777")
        self._observe(shell)
        self._wait_for("session=aaaa1111-2222-3333-4444-555566667777")
        line = next(ln for ln in self._lines() if "aaaa1111" in ln)
        self.assertIn("kind=heartbeat", line)

    def test_the_end_line_keeps_the_session_after_the_registry_is_gone(self):
        """The registry dies with the process; the record must not."""
        Observer._skip_without_kqueue()
        shell = self._pane("bbbb1111-2222-3333-4444-555566667777")
        observer = self._observe(shell)
        self._wait_for("session=bbbb1111-2222-3333-4444-555566667777")
        for name in os.listdir(self.sessions):  # Claude deletes its record on exit
            os.remove(os.path.join(self.sessions, name))
        # Wait out at least one heartbeat that ran with nothing to resolve. Without this the
        # kill lands first, no lookup ever misses, and dropping the carry (`or session`)
        # would still pass.
        before = sum("kind=heartbeat" in ln for ln in self._lines())
        deadline = time.time() + 5
        while sum("kind=heartbeat" in ln for ln in self._lines()) < before + 2:
            self.assertLess(time.time(), deadline, "no heartbeat after the registry was gone")
            time.sleep(0.1)
        self.assertIn("session=bbbb1111", self._lines()[-1], "the carry dropped the id")
        os.killpg(shell.pid, signal.SIGHUP)
        shell.wait()
        observer.wait(timeout=10)
        ends = [ln for ln in self._lines() if "kind=heartbeat" not in ln]
        self.assertEqual(len(ends), 1, ends)
        self.assertIn("signal=SIGHUP", ends[0])
        self.assertIn("session=bbbb1111-2222-3333-4444-555566667777", ends[0])

    def test_an_explicit_session_stands_when_no_descendant_has_a_record(self):
        shell = subprocess.Popen(["/bin/sleep", "30"], start_new_session=True)
        self.procs.append(shell)
        self._observe(shell, "--session", "cccc1111-2222-3333-4444-555566667777")
        self._wait_for("session=cccc1111-2222-3333-4444-555566667777")

    def test_a_malformed_session_id_is_never_written(self):
        """The record is tab-separated; a hostile id must not be able to forge a field."""
        shell = self._pane("evil\tkind=exited")
        self._observe(shell)
        self._wait_for("kind=heartbeat")
        time.sleep(0.5)
        for line in self._lines():
            self.assertNotIn("evil", line)
            self.assertEqual(line.count("\t"), 8, line)


class SessionOfTree(unittest.TestCase):
    """`session_of` over a constructed process table — the shapes real processes cannot fake.

    A fixture process cannot be named `claude` on this platform (a shebang script reports its
    interpreter, a framework Python reports `Python`), so the stop-at-claude rule is pinned
    against a table rather than a live tree.
    """

    SHELL, CLAUDE, BUN, WORKER = 100, 200, 300, 400

    def setUp(self):
        self.sessions = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.sessions, True)
        self._saved = os.environ.get("SUPERVISOR_SESSIONS_DIR")
        os.environ["SUPERVISOR_SESSIONS_DIR"] = self.sessions
        self.addCleanup(self._restore)
        self._real_table = sx._process_table
        self.addCleanup(setattr, sx, "_process_table", self._real_table)

    def _restore(self):
        if self._saved is None:
            os.environ.pop("SUPERVISOR_SESSIONS_DIR", None)
        else:
            os.environ["SUPERVISOR_SESSIONS_DIR"] = self._saved

    def _table(self, rows):
        sx._process_table = lambda: dict(rows)

    def _record(self, pid, sid, entrypoint="cli"):
        with open(os.path.join(self.sessions, f"{pid}.json"), "w", encoding="utf-8") as fh:
            json.dump({"pid": pid, "sessionId": sid, "kind": "interactive",
                       "entrypoint": entrypoint}, fh)

    def _manager_tree(self):
        """shell → claude → bun → claude: a manager whose supervisor server spawned a worker.

        Measured 2026-11-06 on the live table — each manager runs its own supervisor server
        (`claude → bun → node supervisor.mjs`), and headless workers start below it.
        """
        self._table({
            self.SHELL: (1, "bash"),
            self.CLAUDE: (self.SHELL, "claude"),
            self.BUN: (self.CLAUDE, "bun"),
            self.WORKER: (self.BUN, "claude"),
        })

    def test_the_panes_own_claude_is_chosen(self):
        self._manager_tree()
        self._record(self.CLAUDE, "aaaa0000-0000-0000-0000-000000000001")
        self._record(self.WORKER, "bbbb0000-0000-0000-0000-000000000002")
        self.assertEqual(sx.session_of(self.SHELL), "aaaa0000-0000-0000-0000-000000000001")

    def test_an_unreadable_pane_claude_never_lends_a_workers_id(self):
        """The Must-Fix: the pane's record missing or mid-rewrite must not reach the worker.

        Even a worker record that passes the `entrypoint` filter is never reached, because
        the descent stops at the first `claude`.
        """
        self._manager_tree()
        self._record(self.WORKER, "bbbb0000-0000-0000-0000-000000000002", entrypoint="cli")
        self.assertEqual(sx.session_of(self.SHELL), "")

    def test_a_headless_worker_record_is_never_chosen(self):
        """`kind` cannot tell them apart (all 29 live records said `interactive`); `entrypoint` can."""
        self._table({self.SHELL: (1, "bash"), self.WORKER: (self.SHELL, "node")})
        self._record(self.WORKER, "bbbb0000-0000-0000-0000-000000000002", entrypoint="sdk-cli")
        self.assertEqual(sx.session_of(self.SHELL), "")

    def test_an_id_with_a_trailing_newline_is_refused(self):
        """`$` matches before a final newline; such an id would split one record line in two."""
        self._table({self.SHELL: (1, "bash"), self.CLAUDE: (self.SHELL, "claude")})
        self._record(self.CLAUDE, "aaaa0000-0000-0000-0000-000000000001\n")
        self.assertEqual(sx.session_of(self.SHELL), "")

    def test_an_unreadable_table_answers_nothing_rather_than_raising(self):
        sx._process_table = lambda: None
        self.assertEqual(sx.session_of(self.SHELL), "")

    def test_an_empty_table_answers_nothing(self):
        sx._process_table = lambda: {}
        self.assertEqual(sx.session_of(self.SHELL), "")


class TrapEndCarriesSession(StoreTestCase):
    """The trap writes its end after `claude` exited, so it cannot read the registry.

    It passes `--session ""`; `append` must recover the id from this pid's heartbeats, or
    every normal exit and trap-caught signal still reads `session=-`.
    """

    SID = "dddd0000-0000-0000-0000-000000000004"

    def test_a_trap_end_reads_the_id_back_from_the_heartbeats(self):
        self._run("append", "--pane", "2156", "--kind", "spawn", "--pid", "777", "--now", "t0")
        self._run("append", "--pane", "2156", "--kind", "heartbeat", "--pid", "777",
                  "--session", self.SID, "--writer", "observer", "--now", "t1")
        self._run("append", "--pane", "2156", "--pid", "777", "--rc", "0", "--now", "t2")
        end = self._lines()[-1]
        self.assertIn("kind=exited", end)
        self.assertIn(f"session={self.SID}", end)

    def test_an_earlier_run_never_lends_its_session(self):
        """A reused pane id holds many runs; only lines after this pid's own spawn count."""
        self._run("append", "--pane", "2156", "--kind", "heartbeat", "--pid", "777",
                  "--session", self.SID, "--writer", "observer", "--now", "t0")
        self._run("append", "--pane", "2156", "--kind", "spawn", "--pid", "777", "--now", "t1")
        self._run("append", "--pane", "2156", "--pid", "777", "--rc", "0", "--now", "t2")
        self.assertIn("session=-", self._lines()[-1])


class PaneFromEnvironment(StoreTestCase):
    """The trap passes no `--pane`: the writer reads `$WEZTERM_PANE`, which the pane inherits."""

    def test_env_pane_is_used_when_no_flag_is_given(self):
        result = self._run("append", "--rc", "0", "--now", "t", env={"WEZTERM_PANE": "4242"})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(os.listdir(self.dir), ["pane-4242.log"])


class Daily(StoreTestCase):
    """The standing metric: end kinds per calendar day, and the unexplained share.

    The failure this guards is a metric that counts something else. `spawn` and `heartbeat`
    are markers, and a denominator that includes them answers "how busy was the machine"
    while reading as "how many deaths were unexplained" — so they are asserted absent from
    both the numerator and the denominator, not merely documented as excluded.
    """

    def _write(self, pane, kind, now, rc="0"):
        args = ["append", "--pane", pane, "--now", now, "--rc", rc]
        if kind != "auto":
            args += ["--kind", kind]
        result = self._run(*args)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_counts_each_end_kind_by_day(self):
        self._write("1", "auto", "2026-10-28T01:00:00Z", rc="0")
        self._write("2", "auto", "2026-10-28T02:00:00Z", rc="137")
        self._write("3", "auto", "2026-10-28T03:00:00Z", rc="0")
        self._write("4", "unknown-end", "2026-10-29T03:00:00Z")
        rows = json.loads(self._run("daily", "--json").stdout)["days"]
        self.assertEqual([row["day"] for row in rows], ["2026-10-28", "2026-10-29"])
        self.assertEqual(rows[0][sx.KIND_EXITED], 2)
        self.assertEqual(rows[0][sx.KIND_SIGNALLED], 1)
        self.assertEqual(rows[0][sx.KIND_UNKNOWN], 0)
        self.assertEqual(rows[0]["total"], 3)
        self.assertEqual(rows[1][sx.KIND_UNKNOWN], 1)
        self.assertEqual(rows[1]["total"], 1)

    def test_markers_are_not_counted(self):
        """A spawn or a heartbeat is not a death, and must not enter the denominator."""
        self._write("1", "spawn", "2026-10-28T01:00:00Z")
        self._write("1", "heartbeat", "2026-10-28T01:00:30Z")
        self._write("1", "unknown-end", "2026-10-28T02:00:00Z")
        rows = json.loads(self._run("daily", "--json").stdout)["days"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["total"], 1)
        self.assertEqual(rows[0]["unknown_end_pct"], 100.0)

    def test_the_share_is_over_deaths_not_over_records(self):
        self._write("1", "spawn", "2026-10-28T01:00:00Z")
        self._write("2", "auto", "2026-10-28T02:00:00Z", rc="0")
        self._write("3", "unknown-end", "2026-10-28T03:00:00Z")
        rows = json.loads(self._run("daily", "--json").stdout)["days"]
        self.assertEqual(rows[0]["unknown_end_pct"], 50.0)

    def test_days_is_a_window_ending_today(self):
        """`--days N` is a trailing window, not the last N data rows — a window that
        skipped quiet days could not distinguish them from days with no writer."""
        today = datetime.datetime.now(datetime.timezone.utc).date()
        yesterday = (today - datetime.timedelta(days=1)).isoformat()
        self._write("1", "auto", f"{yesterday}T01:00:00Z")
        rows = json.loads(self._run("daily", "--json", "--days", "2").stdout)["days"]
        self.assertEqual([row["day"] for row in rows], [yesterday, today.isoformat()])
        self.assertEqual(rows[0][sx.KIND_EXITED], 1)
        self.assertEqual(rows[1]["total"], 0)

    def test_line_form_is_one_greppable_line_per_day(self):
        self._write("1", "unknown-end", "2026-10-28T01:00:00Z")
        out = self._run("daily", "--line").stdout.splitlines()
        self.assertEqual(len(out), 1)
        self.assertIn("2026-10-28", out[0])
        self.assertIn("unknown_end=1", out[0])
        self.assertIn("unknown_end_pct=100.0", out[0])

    def test_an_absent_store_is_empty_not_an_error(self):
        result = self._run("daily")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("no end records", result.stdout)

    def _write_raw(self, pane, text, mode="w"):
        with open(os.path.join(self.dir, f"pane-{pane}.log"), mode, encoding="utf-8") as handle:
            handle.write(text)

    def test_a_quiet_day_inside_the_window_is_zero_filled(self):
        """A day with no deaths must print `ends=0`, not vanish — otherwise it is
        indistinguishable from a day the writer never ran."""
        today = datetime.datetime.now(datetime.timezone.utc).date()
        quiet = (today - datetime.timedelta(days=1)).isoformat()
        self._write("1", "unknown-end", f"{today.isoformat()}T01:00:00Z")
        rows = json.loads(self._run("daily", "--json", "--days", "3").stdout)["days"]
        self.assertEqual(len(rows), 3)
        by_day = {row["day"]: row for row in rows}
        self.assertIn(quiet, by_day)
        self.assertEqual(by_day[quiet]["total"], 0)
        self.assertEqual(by_day[quiet]["unknown_end_pct"], 0.0)

    def test_the_window_is_contiguous_even_past_the_data(self):
        self._write("1", "unknown-end", "2026-10-28T01:00:00Z")
        rows = json.loads(self._run("daily", "--json", "--days", "30").stdout)["days"]
        self.assertEqual(len(rows), 30)
        days = [datetime.date.fromisoformat(row["day"]) for row in rows]
        self.assertEqual(days, sorted(days))
        self.assertEqual((days[-1] - days[0]).days, 29)

    def test_single_day_on_an_absent_store_still_prints_a_reading(self):
        """The launchd invocation is `--days 1`; a quiet host must still leave a line."""
        result = self._run("daily", "--line", "--days", "1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(result.stdout.splitlines()), 1)
        self.assertIn("ends=0", result.stdout)

    def test_a_line_without_a_leading_timestamp_is_skipped(self):
        """A torn record must not become a phantom day keyed on `kind=unkno`."""
        self._write_raw("1", "kind=unknown-end\tpane=1\tpid=1\twriter=observer\n")
        self._write("2", "unknown-end", "2026-10-28T01:00:00Z")
        rows = json.loads(self._run("daily", "--json").stdout)["days"]
        self.assertEqual([row["day"] for row in rows], ["2026-10-28"])
        self.assertEqual(rows[0]["total"], 1)

    def test_a_non_pane_file_is_ignored(self):
        with open(os.path.join(self.dir, "notes.txt"), "w", encoding="utf-8") as handle:
            handle.write("2026-10-28T01:00:00Z\tkind=unknown-end\n")
        self._write("1", "unknown-end", "2026-10-28T01:00:00Z")
        rows = json.loads(self._run("daily", "--json").stdout)["days"]
        self.assertEqual(rows[0]["total"], 1)

    def test_an_undecodable_byte_degrades_one_file_not_the_run(self):
        with open(os.path.join(self.dir, "pane-9.log"), "wb") as handle:
            handle.write(b"2026-10-28T01:00:00Z\tkind=unknown-end\tpane=9\t\xff\xfe\n")
        self._write("1", "unknown-end", "2026-10-28T01:00:00Z")
        result = self._run("daily", "--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        rows = json.loads(result.stdout)["days"]
        self.assertEqual(rows[0]["day"], "2026-10-28")
        self.assertEqual(rows[0]["total"], 2)

    def test_json_wins_over_line(self):
        self._write("1", "unknown-end", "2026-10-28T01:00:00Z")
        out = self._run("daily", "--json", "--line").stdout
        self.assertEqual(json.loads(out)["days"][0]["day"], "2026-10-28")


if __name__ == "__main__":
    unittest.main()
