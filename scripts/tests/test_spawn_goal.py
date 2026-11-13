"""spawn-goal.py: the three-step arm sequence, and the two ways it must fail closed.

The defect this guards is a spawn path that sends the work command without confirming the
goal armed: the worker then runs with no completion condition and the row idles, which is
exactly what the `goal_loop` feature exists to prevent. Prose cannot enforce the wait, so
the sequence lives in a script whose exit status the call site can check — and both refusals
below are asserted on that status, not on a message a reader might miss.

A fake `wezterm` stands in for the real one: it records every `send-text` and returns
whatever `PANE_TEXT` holds for `get-text`, so the wait and the marker check are exercised
without a terminal.
"""
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest

SCRIPTS = pathlib.Path(__file__).resolve().parent.parent
SCRIPT = SCRIPTS / "spawn-goal.py"

SHORT = """# Success Criteria

- [ ] SC1: The first thing works.
"""

FAKE_WEZTERM = """#!/bin/sh
case "$1 $2" in
  "cli get-text") printf '%s' "${PANE_TEXT:-}" ;;
  "cli send-text") printf '%s\\n' "$*" >> "${SENT_LOG}" ;;
esac
exit 0
"""


class SpawnGoalTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.bin = pathlib.Path(self.dir) / "bin"
        self.bin.mkdir()
        self.task = pathlib.Path(self.dir) / "task.md"
        self.task.write_text(SHORT, encoding="utf-8")
        self.sent = pathlib.Path(self.dir) / "sent.log"
        self.sent.touch()

        wezterm = self.bin / "wezterm"
        wezterm.write_text(FAKE_WEZTERM, encoding="utf-8")
        wezterm.chmod(0o755)

        vault_cli = self.bin / "vault-cli"
        vault_cli.write_text(
            f'#!/bin/sh\nprintf \'{{"file_path": "{self.task}"}}\'\n', encoding="utf-8")
        vault_cli.chmod(0o755)

    def run_script(self, *args, pane_text=""):
        env = {
            **os.environ,
            "PATH": f"{self.bin}:{os.environ.get('PATH', '')}",
            "PANE_TEXT": pane_text,
            "SENT_LOG": str(self.sent),
        }
        return subprocess.run(
            [sys.executable, str(SCRIPT), "--pane", "42", "--vault", "v",
             "--task", "any", "--timeout", "1", *args],
            capture_output=True, text=True, env=env,
        )

    def test_armed_pane_exits_zero_after_sending_condition_and_submit(self):
        result = self.run_script(pane_text="  ⎿  Goal set: Task \"any\" is finished.")
        self.assertEqual(result.returncode, 0, result.stderr)
        # The fake logs one line per send, and the condition itself is multi-line, so the
        # first send is the head of the log and the submit is the last line.
        sent = self.sent.read_text(encoding="utf-8").splitlines()
        self.assertIn("/goal ", sent[0], "the first send must be the condition")
        self.assertTrue(sent[-1].strip().endswith("--no-paste"),
                        f"the last send must be the bare submit: {sent[-1]!r}")

    def test_marker_that_never_appears_fails_closed(self):
        result = self.run_script(pane_text="no marker here")
        self.assertEqual(result.returncode, 2)
        self.assertIn("⛔ goal not set — work command not sent", result.stderr)

    def test_an_emitter_refusal_stops_before_anything_is_sent(self):
        """A task with no Success Criteria makes the emitter exit 3; nothing may reach the pane."""
        self.task.write_text("# Tasks\n\n- [ ] do it\n", encoding="utf-8")
        result = self.run_script(pane_text="  ⎿  Goal set:")
        self.assertEqual(result.returncode, 1)
        self.assertIn("⛔ goal not set — work command not sent", result.stderr)
        self.assertEqual(self.sent.read_text(encoding="utf-8"), "")

    def test_an_unreadable_pane_is_not_treated_as_armed(self):
        """get-text returning nothing must not read as success — absence is not evidence."""
        result = self.run_script(pane_text="")
        self.assertEqual(result.returncode, 2)


if __name__ == "__main__":
    unittest.main()
