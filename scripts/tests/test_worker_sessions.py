"""`worker-sessions.py` — the fleet worker target's unit, and the join that defines it.

The defect this pins, measured 2026-10-01: the instrument that shipped for the target read
the **headless heartbeat store**, which is stamped only for in-process workers, so it
answered **0 while 11 interactive worker tabs were live**. A target reading 0 on a busy
fleet is inert — managers always see "below target" and always propose, and the cap never
binds — and nothing looks wrong, because 0 is a plausible count for the wrong question.

The load-bearing cases are the two negatives. A session in the registry but **not** the
ledger is a manager or one of the operator's own and must not be counted; a session in the
ledger with **neither** liveness channel behind it has exited and must not be counted either.
A check that only asserted the positive would pass while counting every session on the
machine.

⚠️ **The second negative was too strong, and this file was pinning the defect.** Until
2026-10-05 a session in the ledger but not the registry read as *exited* — true for a tab
whose process is gone, and false for every worker that never had a registry entry to begin
with. A headless worker is an in-process SDK `query()` inside the server, and a cluster worker
is a process on another machine; both hold a ledger record and a heartbeat stamp and no
registry entry, so both were counted as dead. The registry is one liveness channel, not the
liveness authority, and `test_a_ledger_record_with_a_fresh_heartbeat_is_a_worker` is the case
that separates the two readings.
"""
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

# Side effect only: points the start-time cache at an isolated per-run store, in one shared
# home so five suites cannot each assign the same key and leave only the last standing.
import start_cache_isolation  # noqa: E402,F401

SCRIPTS = pathlib.Path(__file__).resolve().parent.parent
SCRIPT = SCRIPTS / "worker-sessions.py"


class WorkerSessionsTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.registry = os.path.join(self.dir, "registry")
        self.ledger = os.path.join(self.dir, "ledger")
        self.beats = os.path.join(self.dir, "heartbeats")
        os.makedirs(self.registry)
        os.makedirs(self.ledger)
        os.makedirs(self.beats)
        # `worker-sessions.py` imports `session-liveness.py` by path from its own directory, so
        # the temp tree needs the real script beside a copy of itself — and `session-liveness.py`
        # in turn loads `live-workers.py`, which is where the heartbeat store is read. All three
        # travel together; a fixture missing the third makes the counter exit 2, which the
        # negatives below would read as "not counted" and pass on a broken instrument.
        self.bin = os.path.join(self.dir, "scripts")
        os.makedirs(self.bin)
        shutil.copy(SCRIPT, self.bin)
        shutil.copy(SCRIPTS / "session-liveness.py", self.bin)
        shutil.copy(SCRIPTS / "live-workers.py", self.bin)

    def heartbeat_stamp(self, session_id, mode="headless", source=None, age_seconds=0.0):
        """A heartbeat stamp, with its AGE set by mtime — the field the verdict rests on.

        `source: "cluster"` is what marks a mirrored cluster session, and it is load-bearing
        rather than descriptive: a STALE cluster stamp is `unknown` (the cluster could not be
        read), never dead.
        """
        path = os.path.join(self.beats, f"{session_id}.json")
        record = {"sessionId": session_id, "mode": mode}
        if source is not None:
            record["source"] = source
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(record, fh)
        if age_seconds:
            when = time.time() - age_seconds
            os.utime(path, (when, when))

    def register(self, session_id, status="busy"):
        with open(os.path.join(self.registry, f"{session_id}.json"), "w", encoding="utf-8") as fh:
            json.dump({"sessionId": session_id, "pid": os.getpid(), "status": status}, fh)

    def ledger_entry(self, session_id, label, **extra):
        record = {"session_id": session_id, "label": label, "mode": "interactive"}
        record.update(extra)
        with open(os.path.join(self.ledger, f"{session_id}.json"), "w", encoding="utf-8") as fh:
            json.dump(record, fh)

    def run_script(self, *args):
        return subprocess.run(
            [sys.executable, os.path.join(self.bin, "worker-sessions.py"),
             "--dir", self.registry, "--ledger-dir", self.ledger,
             "--heartbeat-dir", self.beats, *args],
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

    def test_a_ledger_record_with_neither_liveness_channel_has_exited(self):
        """THE LIVENESS CASE. The ledger is the durable half and keeps a record forever; the
        registry entry is deleted on exit and the heartbeat stamp goes stale. A worker with
        neither behind it must not be counted."""
        self.ledger_entry("cccccccc-0000-0000-0000-000000000001", "long gone")
        self.assertEqual(self.run_script("--count").stdout.strip(), "0")

    def test_a_ledger_record_with_a_fresh_heartbeat_is_a_worker(self):
        """THE FIX, and the case this file used to pin wrongly. A worker with no registry entry
        is not thereby dead: a headless worker is an in-process `query()` and a cluster worker
        runs on another machine, so neither can hold a pid-keyed registry file. Their liveness
        is the heartbeat stamp, and the count must include them or `spawn.maxConcurrent` bounds
        a population it cannot see."""
        sid = "ffffffff-0000-0000-0000-000000000001"
        self.ledger_entry(sid, "cluster worker")
        self.heartbeat_stamp(sid, mode="cluster", source="cluster")
        self.assertEqual(self.run_script("--count").stdout.strip(), "1")

    def test_an_auto_resumed_worker_is_not_counted(self):
        """Auto-resumes answer to the auto-resume gate's own 30-min crash-loop cap, not this
        one: counting them would leave a sweep that revived two dead workers unable to open a
        new one. The marker is the ledger record's `resumed_from`, the same field
        `check-spawn-ledger.py` reads."""
        sid = "ffffffff-0000-0000-0000-000000000004"
        self.ledger_entry(sid, "auto-resumed", resumed_from="aaaaaaaa-0000-0000-0000-000000000009")
        self.heartbeat_stamp(sid)
        self.assertEqual(self.run_script("--count").stdout.strip(), "0")

    def test_list_names_the_mode_for_a_heartbeat_sourced_worker(self):
        """`status` names whichever channel answered. For a worker with no registry entry that
        is the stamp's `mode` — and `read_live` dropping the field left this column permanently
        `None` while the mjs twin read the real value off the stamp file."""
        sid = "ffffffff-0000-0000-0000-000000000005"
        self.ledger_entry(sid, "Some Worker")
        self.heartbeat_stamp(sid, mode="cluster", source="cluster")
        line = [ln for ln in self.run_script("--list").stdout.splitlines() if sid in ln][0]
        self.assertIn("cluster", line)

    def test_a_stale_heartbeat_is_not_counted(self):
        """The verdict is the stamp's AGE against the TTL, never the file's existence — a store
        whose writer was killed keeps its files, so existence reports the wrong answer for the
        case the store exists to catch."""
        sid = "ffffffff-0000-0000-0000-000000000002"
        self.ledger_entry(sid, "finished hours ago")
        self.heartbeat_stamp(sid, mode="headless", age_seconds=3600)
        self.assertEqual(self.run_script("--count").stdout.strip(), "0")

    def test_a_stale_cluster_stamp_with_no_reachability_marker_is_not_counted(self):
        """'Cannot tell' is not 'live'. A stale cluster stamp with the mirror's reachability
        marker also stale means the cluster could not be read, so this stamp's staleness proves
        nothing about the worker — `read_live` reports it `unknown`, and counting it would put
        a possibly-dead session on the fleet's books."""
        sid = "ffffffff-0000-0000-0000-000000000003"
        self.ledger_entry(sid, "cluster worker behind an outage")
        self.heartbeat_stamp(sid, mode="cluster", source="cluster", age_seconds=3600)
        self.assertEqual(self.run_script("--count").stdout.strip(), "0")

    def test_an_unreadable_heartbeat_store_exits_two(self):
        """The heartbeat store is a liveness channel now, so failing to read it is UNKNOWN for
        the same reason the registry is: a channel that cannot be read may be hiding the very
        session being counted, and folding that into a number under-counts the fleet."""
        os.chmod(self.beats, 0o000)
        self.addCleanup(os.chmod, self.beats, 0o755)
        result = self.run_script("--count")
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")
        self.assertIn("UNKNOWN", result.stderr)

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
