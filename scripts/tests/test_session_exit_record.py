#!/usr/bin/env python3
"""Tests for scripts/session-exit-record.py.

The load-bearing properties are the ones a death would expose:

- a line's field order never moves, because every reader splits on it;
- `rc` past 128 is classified as a **signal** death and named, never folded into `exited`;
- a record path is derived from the pane id and nothing else, and a pane id that could
  escape the store is refused rather than joined into a filename;
- appending never truncates, so the trap and the observer cannot lose each other's line;
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
        line = sx.format_line("2026-10-27T00:00:00Z", sx.KIND_SIGNALLED, "2156", "sid", 137, "SIGKILL")
        self.assertEqual(
            line.split("\t"),
            [
                "2026-10-27T00:00:00Z",
                "kind=signalled kill",
                "pane=2156",
                "session=sid",
                "rc=137",
                "signal=SIGKILL",
            ],
        )

    def test_an_unknown_session_is_a_dash_never_an_empty_field(self):
        line = sx.format_line("t", sx.KIND_EXITED, "1", "", 0, "-")
        self.assertIn("\tsession=-\t", line)

    def test_the_spawn_marker_is_a_start_not_an_end(self):
        """Without it an empty record cannot be told from an unwired writer."""
        line = sx.format_line("t", sx.KIND_SPAWN, "1", "sid", 0, "-")
        self.assertIn("kind=spawn", line)
        self.assertNotIn(sx.KIND_SPAWN, (sx.KIND_EXITED, sx.KIND_SIGNALLED))


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


class AppendAndRead(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.mkdtemp()
        self._saved = os.environ.get("SUPERVISOR_SESSION_EXIT_DIR")
        os.environ["SUPERVISOR_SESSION_EXIT_DIR"] = self._dir

    def tearDown(self):
        if self._saved is None:
            os.environ.pop("SUPERVISOR_SESSION_EXIT_DIR", None)
        else:
            os.environ["SUPERVISOR_SESSION_EXIT_DIR"] = self._saved

    def _run(self, *args):
        return subprocess.run(
            [sys.executable, _SCRIPT, *args],
            capture_output=True,
            text=True,
            env={**os.environ},
        )

    def test_append_creates_the_store_and_writes_one_line(self):
        result = self._run("append", "--pane", "2156", "--rc", "0", "--now", "2026-10-27T00:00:00Z")
        self.assertEqual(result.returncode, 0, result.stderr)
        with open(os.path.join(self._dir, "pane-2156.log"), encoding="utf-8") as handle:
            lines = handle.read().splitlines()
        self.assertEqual(len(lines), 1)
        self.assertIn("kind=exited", lines[0])
        self.assertIn("pane=2156", lines[0])
        self.assertIn("rc=0", lines[0])

    def test_a_second_append_does_not_truncate_the_first(self):
        """Two writers can fire for one pane (trap + observer); neither may lose the other."""
        self._run("append", "--pane", "2156", "--rc", "0", "--now", "t1")
        self._run("append", "--pane", "2156", "--rc", "137", "--now", "t2")
        with open(os.path.join(self._dir, "pane-2156.log"), encoding="utf-8") as handle:
            lines = handle.read().splitlines()
        self.assertEqual(len(lines), 2)
        self.assertIn("rc=0", lines[0])
        self.assertIn("kind=signalled kill", lines[1])
        self.assertIn("signal=SIGKILL", lines[1])

    def test_append_refuses_an_escaping_pane_and_writes_nothing(self):
        result = self._run("append", "--pane", "../evil", "--rc", "0")
        self.assertEqual(result.returncode, 2)
        self.assertIn("bare token", result.stderr)
        self.assertEqual(os.listdir(self._dir), [])

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
        os.environ["SUPERVISOR_SESSION_EXIT_DIR"] = os.path.join(self._dir, "absent")
        result = self._run("list", "--json")
        self.assertEqual(result.returncode, 0)
        self.assertEqual(json.loads(result.stdout)["records"], [])


class PaneFromEnvironment(unittest.TestCase):
    """The trap passes no `--pane`: the writer reads `$WEZTERM_PANE`, which the pane inherits."""

    def test_env_pane_is_used_when_no_flag_is_given(self):
        directory = tempfile.mkdtemp()
        env = {**os.environ, "SUPERVISOR_SESSION_EXIT_DIR": directory, "WEZTERM_PANE": "4242"}
        result = subprocess.run(
            [sys.executable, _SCRIPT, "append", "--rc", "0", "--now", "t"],
            capture_output=True,
            text=True,
            env=env,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(os.listdir(directory), ["pane-4242.log"])


if __name__ == "__main__":
    unittest.main()
