"""`live-workers.py --count` — the counting mode, and why `--list | wc -l` cannot be it.

The defect this pins, measured 2026-10-01 against the real store: an empty heartbeat dir
makes `--list` print `no live headless workers in <dir>` — to **stdout**, like every other
line the script emits — so `--list | wc -l` answers **1** for an idle fleet. A caller
comparing that against a worker target reads "one worker live" on a machine with none, and
nothing looks wrong, because 1 is a plausible count.

`test_list_line_count_is_wrong_for_an_empty_store` asserts that miscount *deliberately*, so
the reason `--count` exists cannot be quietly forgotten and the two modes cannot drift into
agreeing by accident.
"""
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

SCRIPT = pathlib.Path(__file__).resolve().parent.parent / "live-workers.py"


class LiveWorkersCountTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)

    def stamp(self, session_id, age_seconds=0.0):
        """Write one heartbeat stamp, optionally aged past the TTL."""
        path = os.path.join(self.dir, f"{session_id}.json")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write('{"pid": 1234}')
        if age_seconds:
            old = time.time() - age_seconds
            os.utime(path, (old, old))

    def run_script(self, *args):
        return subprocess.run(
            [sys.executable, str(SCRIPT), "--dir", self.dir, *args],
            capture_output=True, text=True,
        )

    def test_count_on_an_empty_store_is_zero(self):
        result = self.run_script("--count")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "0")

    def test_count_matches_the_number_of_fresh_stamps(self):
        for i in range(3):
            self.stamp(f"aaaaaaaa-0000-0000-0000-00000000000{i}")
        result = self.run_script("--count")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "3")

    def test_a_stale_stamp_is_not_counted(self):
        """The verdict is the stamp's age, never the file's existence — so a worker killed
        with `kill -9`, whose stamp was never cleared, must not be counted."""
        self.stamp("bbbbbbbb-0000-0000-0000-000000000001", age_seconds=120)
        result = self.run_script("--count")
        self.assertEqual(result.stdout.strip(), "0")

    def test_count_writes_only_the_integer(self):
        """`$(... --count)` goes straight into a comparison, so any prose on stdout would be
        captured as the value. One line, and it parses as an int."""
        self.stamp("cccccccc-0000-0000-0000-000000000001")
        result = self.run_script("--count")
        self.assertEqual(len(result.stdout.strip().splitlines()), 1)
        self.assertEqual(int(result.stdout.strip()), 1)

    def test_an_unreadable_store_exits_two_with_an_empty_stdout(self):
        """'Could not check' must never read as 'zero workers'. The message goes to stderr,
        which `$(...)` cannot capture, and the exit code is the only signal."""
        unreadable = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, unreadable, ignore_errors=True)
        os.chmod(unreadable, 0o000)
        self.addCleanup(os.chmod, unreadable, 0o755)
        result = subprocess.run(
            [sys.executable, str(SCRIPT), "--count", "--dir", unreadable],
            capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")
        self.assertIn("UNKNOWN", result.stderr)

    def test_list_line_count_is_wrong_for_an_empty_store(self):
        """THE LOAD-BEARING CASE. This asserts the miscount on purpose: it is the reason
        `--count` exists. If someone later makes `--list` silent when empty, this test fails
        and the comment above it is the explanation for why that is a deliberate change."""
        result = self.run_script("--list")
        self.assertEqual(result.returncode, 0)
        self.assertEqual(len(result.stdout.strip().splitlines()), 1)
        self.assertIn("no live headless workers", result.stdout)
        # ... while --count, over the same store, answers the truthful 0.
        self.assertEqual(self.run_script("--count").stdout.strip(), "0")


if __name__ == "__main__":
    unittest.main()
