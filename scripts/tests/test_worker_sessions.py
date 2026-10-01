"""`worker-sessions.py` — the fleet worker target's unit, and the join that defines it.

The defect this pins, measured 2026-10-01: the instrument that shipped for the target read
the **headless heartbeat store**, which is stamped only for in-process workers, so it
answered **0 while 11 interactive worker tabs were live**. A target reading 0 on a busy
fleet is inert — managers always see "below target" and always propose, and the cap never
binds — and nothing looks wrong, because 0 is a plausible count for the wrong question.

The load-bearing cases are the two negatives. A session in the registry but **not** the
ledger is a manager or one of the operator's own and must not be counted; a session in the
ledger but **not** the registry has exited and must not be counted either. A check that
only asserted the positive would pass while counting every session on the machine.
"""
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest

SCRIPTS = pathlib.Path(__file__).resolve().parent.parent
SCRIPT = SCRIPTS / "worker-sessions.py"


class WorkerSessionsTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.registry = os.path.join(self.dir, "registry")
        self.ledger = os.path.join(self.dir, "ledger")
        os.makedirs(self.registry)
        os.makedirs(self.ledger)
        # `worker-sessions.py` imports `session-liveness.py` by path from its own directory,
        # so the temp tree needs the real script beside a copy of itself.
        self.bin = os.path.join(self.dir, "scripts")
        os.makedirs(self.bin)
        shutil.copy(SCRIPT, self.bin)
        shutil.copy(SCRIPTS / "session-liveness.py", self.bin)

    def register(self, session_id, status="busy"):
        with open(os.path.join(self.registry, f"{session_id}.json"), "w", encoding="utf-8") as fh:
            json.dump({"sessionId": session_id, "pid": os.getpid(), "status": status}, fh)

    def ledger_entry(self, session_id, label):
        with open(os.path.join(self.ledger, f"{session_id}.json"), "w", encoding="utf-8") as fh:
            json.dump({"session_id": session_id, "label": label, "mode": "interactive"}, fh)

    def run_script(self, *args):
        return subprocess.run(
            [sys.executable, os.path.join(self.bin, "worker-sessions.py"),
             "--dir", self.registry, "--ledger-dir", self.ledger, *args],
            capture_output=True, text=True,
        )

    def test_count_is_zero_when_nothing_is_registered(self):
        result = self.run_script("--count")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "0")

    def test_count_is_the_size_of_the_join(self):
        for i in range(3):
            sid = f"aaaaaaaa-0000-0000-0000-00000000000{i}"
            self.register(sid)
            self.ledger_entry(sid, f"worker {i}")
        self.assertEqual(self.run_script("--count").stdout.strip(), "3")

    def test_a_registry_session_with_no_ledger_record_is_not_a_worker(self):
        """THE MANAGER CASE. A manager is started by hand and holds no ledger record, so it
        must not be counted — counting every registry session is the defect the join fixes."""
        self.register("bbbbbbbb-0000-0000-0000-000000000001")
        self.assertEqual(self.run_script("--count").stdout.strip(), "0")

    def test_a_ledger_record_with_no_registry_entry_has_exited(self):
        """THE LIVENESS CASE. The ledger is the durable half and keeps a record forever; the
        registry is deleted on exit. A dead worker must not be counted."""
        self.ledger_entry("cccccccc-0000-0000-0000-000000000001", "long gone")
        self.assertEqual(self.run_script("--count").stdout.strip(), "0")

    def test_list_shows_the_label_and_status(self):
        sid = "dddddddd-0000-0000-0000-000000000001"
        self.register(sid, status="idle")
        self.ledger_entry(sid, "Some Worker Task")
        out = self.run_script("--list").stdout
        self.assertIn(sid, out)
        self.assertIn("Some Worker Task", out)
        self.assertIn("idle", out)

    def test_count_writes_only_the_integer(self):
        """`$(… --count)` goes straight into a comparison against the target."""
        sid = "eeeeeeee-0000-0000-0000-000000000001"
        self.register(sid)
        self.ledger_entry(sid, "one")
        result = self.run_script("--count")
        self.assertEqual(len(result.stdout.strip().splitlines()), 1)
        self.assertEqual(int(result.stdout.strip()), 1)

    def test_an_unreadable_ledger_exits_two_with_an_empty_stdout(self):
        """'Could not check' must never read as 'zero workers' — an idle fleet and a broken
        instrument must not render the same."""
        os.chmod(self.ledger, 0o000)
        self.addCleanup(os.chmod, self.ledger, 0o755)
        result = self.run_script("--count")
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")
        self.assertIn("UNKNOWN", result.stderr)

    def test_an_absent_ledger_directory_is_an_empty_fleet_not_an_error(self):
        """A directory that was never created means nobody has been spawned — a different
        answer from 'could not read it', and it must not be reported as unknown."""
        shutil.rmtree(self.ledger)
        result = self.run_script("--count")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "0")


if __name__ == "__main__":
    unittest.main()
