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
- the observer records a disappearance as `unknown-end` — an inference, never
  `signalled kill` — and stays silent when the parent already reported an end;
- an absent record reads as *nothing*, not as an error — absence is the state this tool
  exists to make visible.

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

    def test_the_observer_never_claims_a_signal(self):
        """A signal is a fact only the parent's `waitpid` holds; the observer infers."""
        self._run("observe", "--pane", "2156", "--pid", str(self._gone_pid()), "--foreground")
        line = self._lines()[0]
        self.assertNotIn("signalled kill", line)
        self.assertIn("rc=unavailable", line)
        self.assertIn("signal=unavailable", line)

    def test_the_observers_end_carries_an_unavailable_sender(self):
        self._run("observe", "--pane", "2156", "--pid", str(self._gone_pid()), "--foreground")
        self.assertIn("sender=unavailable(", self._lines()[0])

    def test_the_parents_report_wins_over_the_observers_inference(self):
        """The trap runs before the shell exits, so its line is already on disk."""
        self._run("append", "--pane", "2156", "--rc", "137", "--now", "t1")
        self._run("observe", "--pane", "2156", "--pid", str(self._gone_pid()), "--foreground")
        lines = self._lines()
        self.assertEqual(len(lines), 1)
        self.assertIn("kind=signalled kill", lines[0])

    def test_a_nonpositive_pid_is_refused(self):
        result = self._run("observe", "--pane", "2156", "--pid", "0", "--foreground")
        self.assertEqual(result.returncode, 2)
        self.assertEqual(os.listdir(self.dir), [])


class PaneFromEnvironment(StoreTestCase):
    """The trap passes no `--pane`: the writer reads `$WEZTERM_PANE`, which the pane inherits."""

    def test_env_pane_is_used_when_no_flag_is_given(self):
        result = self._run("append", "--rc", "0", "--now", "t", env={"WEZTERM_PANE": "4242"})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(os.listdir(self.dir), ["pane-4242.log"])


if __name__ == "__main__":
    unittest.main()
