"""`worker-sessions.py` — the fleet worker target's unit, and the join that defines it.

The defect this pins, measured 2026-10-01: the instrument that shipped for the target read
the **headless heartbeat store**, which is stamped only for in-process workers, so it
answered **0 while 11 interactive worker tabs were live**. A target reading 0 on a busy
fleet is inert — managers always see "below target" and always propose, and the cap never
binds — and nothing looks wrong, because 0 is a plausible count for the wrong question.

The load-bearing cases are the two negatives. A session live at the endpoint but **not** in
the ledger is a manager or one of the operator's own and must not be counted; a session in
the ledger that is **not** live at the endpoint has exited and must not be counted either.
A check that only asserted the positive would pass while counting every session on the
machine.

⚠️ **The liveness source moved to the endpoint on 2026-10-09 and the join did NOT move with
it.** Until then this file planted registry files and heartbeat stamps and pointed the counter
at both directories. The endpoint holds both populations in one store, so the fixtures are now
store rows — but the unit stays *ledger ∩ live*, which is what makes this a count of workers
rather than of every session on the machine. Operator ruling 2026-10-09: keep the join, move
only the liveness source. The old `--dir` / `--heartbeat-dir` flags are still accepted and
ignored, so a caller that passes one is warned rather than silently obeyed; that is pinned
below too.
"""
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest

import endpoint_fixture  # noqa: E402
from endpoint_fixture import FixtureEndpoint, dead_url, row  # noqa: E402

# Side effect only: points the start-time cache at an isolated per-run store, in one shared
# home so five suites cannot each assign the same key and leave only the last standing.
import start_cache_isolation  # noqa: E402,F401

SCRIPTS = pathlib.Path(__file__).resolve().parent.parent
SCRIPT = SCRIPTS / "worker-sessions.py"


class WorkerSessionsTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.ledger = os.path.join(self.dir, "ledger")
        os.makedirs(self.ledger)
        # `worker-sessions.py` imports its siblings by path from its own directory, so the temp
        # tree needs the real scripts beside a copy of itself. `session-liveness.py` is where
        # the endpoint read lives now; `spawn-ledger.py` is where the ledger reader lives since
        # SC11 extracted it out of this file. A fixture missing either makes the counter exit 2,
        # which the negatives below would read as "not counted" and pass on a broken instrument.
        self.bin = os.path.join(self.dir, "scripts")
        os.makedirs(self.bin)
        shutil.copy(SCRIPT, self.bin)
        shutil.copy(SCRIPTS / "session-liveness.py", self.bin)
        shutil.copy(SCRIPTS / "spawn-ledger.py", self.bin)

    def ledger_entry(self, session_id, label, **extra):
        record = {"session_id": session_id, "label": label, "mode": "interactive"}
        record.update(extra)
        with open(os.path.join(self.ledger, f"{session_id}.json"), "w", encoding="utf-8") as fh:
            json.dump(record, fh)

    def run_script(self, *args, endpoint=None):
        return subprocess.run(
            [sys.executable, os.path.join(self.bin, "worker-sessions.py"),
             "--endpoint", endpoint or self.endpoint,
             "--ledger-dir", self.ledger, *args],
            capture_output=True, text=True,
        )

    def test_count_is_zero_when_nothing_is_registered(self):
        with FixtureEndpoint() as ep:
            self.endpoint = ep.url
            result = self.run_script("--count")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "0")

    def test_count_is_the_size_of_the_join(self):
        ids = [f"aaaaaaaa-0000-0000-0000-00000000000{i}" for i in range(3)]
        for i, sid in enumerate(ids):
            self.ledger_entry(sid, f"worker {i}")
        with FixtureEndpoint(rows=[row(s) for s in ids]) as ep:
            self.endpoint = ep.url
            result = self.run_script("--count")
        self.assertEqual(result.stdout.strip(), "3", result.stderr)

    def test_a_live_session_with_no_ledger_record_is_not_a_worker(self):
        """THE MANAGER CASE. A manager is started by hand and holds no ledger record, so it
        must not be counted — counting every live session is the defect the join fixes."""
        with FixtureEndpoint(rows=[row("bbbbbbbb-0000-0000-0000-000000000001")]) as ep:
            self.endpoint = ep.url
            result = self.run_script("--count")
        self.assertEqual(result.stdout.strip(), "0", result.stderr)

    def test_a_ledger_record_with_no_endpoint_row_has_exited(self):
        """THE LIVENESS CASE. The ledger is the durable half and keeps a record forever; a
        session with no row at the endpoint has exited and must not be counted."""
        self.ledger_entry("cccccccc-0000-0000-0000-000000000001", "long gone")
        with FixtureEndpoint() as ep:
            self.endpoint = ep.url
            result = self.run_script("--count")
        self.assertEqual(result.stdout.strip(), "0", result.stderr)

    def test_a_ledger_record_live_at_the_endpoint_is_a_worker(self):
        """THE FIX, and the case this file used to pin wrongly. A worker with no registry entry
        is not thereby dead: a headless worker is an in-process `query()` and a cluster worker
        runs on another machine, so neither can hold a pid-keyed registry file. Their liveness
        is the endpoint's, and the count must include them or `spawn.maxConcurrent` bounds a
        population it cannot see."""
        sid = "ffffffff-0000-0000-0000-000000000001"
        self.ledger_entry(sid, "cluster worker")
        with FixtureEndpoint(rows=[row(sid, source="cluster")]) as ep:
            self.endpoint = ep.url
            result = self.run_script("--count")
        self.assertEqual(result.stdout.strip(), "1", result.stderr)

    def test_an_auto_resumed_worker_is_not_counted(self):
        """Auto-resumes answer to the auto-resume gate's own 30-min crash-loop cap, not this
        one: counting them would leave a sweep that revived two dead workers unable to open a
        new one. The marker is the ledger record's `resumed_from`, the same field
        `check-spawn-ledger.py` reads."""
        sid = "ffffffff-0000-0000-0000-000000000004"
        self.ledger_entry(sid, "auto-resumed", resumed_from="aaaaaaaa-0000-0000-0000-000000000009")
        with FixtureEndpoint(rows=[row(sid)]) as ep:
            self.endpoint = ep.url
            result = self.run_script("--count")
        self.assertEqual(result.stdout.strip(), "0", result.stderr)

    def test_list_names_the_source_for_an_endpoint_sourced_worker(self):
        """`status` is the store row's `source` — `cluster` for a mirrored cluster session. It
        is descriptive only, but a reader that dropped it would leave the column empty."""
        sid = "ffffffff-0000-0000-0000-000000000005"
        self.ledger_entry(sid, "Some Worker")
        with FixtureEndpoint(rows=[row(sid, source="cluster")]) as ep:
            self.endpoint = ep.url
            out = self.run_script("--list").stdout
        line = [ln for ln in out.splitlines() if sid in ln][0]
        self.assertIn("cluster", line)

    def test_a_row_the_endpoint_reports_not_live_is_not_counted(self):
        """The verdict is the store's own `live` flag, never the row's existence — a store
        whose writer was killed keeps its rows, so existence reports the wrong answer for the
        case the store exists to catch."""
        sid = "ffffffff-0000-0000-0000-000000000002"
        self.ledger_entry(sid, "finished hours ago")
        with FixtureEndpoint(rows=[row(sid, live=False, age_seconds=3600)]) as ep:
            self.endpoint = ep.url
            result = self.run_script("--count")
        self.assertEqual(result.stdout.strip(), "0", result.stderr)

    def test_a_row_missing_its_live_flag_is_not_counted(self):
        """⚠️ A renamed or absent `live` key must never read as liveness. The endpoint is the
        only channel now, so a row that does not positively say `true` is not an answer — and
        a row that is not even an object must be skipped rather than crash the count."""
        sid = "ffffffff-0000-0000-0000-000000000007"
        self.ledger_entry(sid, "malformed row")
        rows = [{"session_id": sid, "source": "mcp-timer"}, "not-a-row", None]
        with FixtureEndpoint(rows=rows) as ep:
            self.endpoint = ep.url
            result = self.run_script("--count")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "0")

    def test_an_unreachable_endpoint_exits_two(self):
        """The endpoint is the liveness source, so failing to read it is UNKNOWN for the same
        reason the ledger is: a store that cannot be read may be hiding the very session being
        counted, and folding that into a number under-counts the fleet."""
        self.ledger_entry("ffffffff-0000-0000-0000-000000000008", "worker")
        result = self.run_script("--count", endpoint=dead_url())
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")
        self.assertIn("UNKNOWN", result.stderr)

    def test_a_non_200_list_route_exits_two(self):
        """A non-200 that is not a liveness answer must not be read as an empty fleet."""
        self.ledger_entry("ffffffff-0000-0000-0000-000000000009", "worker")
        with FixtureEndpoint(rows=[], list_status=500) as ep:
            self.endpoint = ep.url
            result = self.run_script("--count")
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")
        self.assertIn("UNKNOWN", result.stderr)

    def test_the_ignored_flags_warn_instead_of_being_obeyed(self):
        """`--dir` and `--heartbeat-dir` are kept so existing callers do not break, but a caller
        that passes one must not believe a path or a window is in force when neither is."""
        sid = "eeeeeeee-0000-0000-0000-000000000002"
        self.ledger_entry(sid, "one")
        with FixtureEndpoint(rows=[row(sid)]) as ep:
            self.endpoint = ep.url
            result = self.run_script("--count", "--dir", "/nonexistent", "--heartbeat-dir", "/nonexistent")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "1")
        self.assertIn("IGNORED", result.stderr)

    def test_list_shows_the_label_and_source(self):
        sid = "dddddddd-0000-0000-0000-000000000001"
        self.ledger_entry(sid, "Some Worker Task")
        with FixtureEndpoint(rows=[row(sid, source="mcp-timer")]) as ep:
            self.endpoint = ep.url
            out = self.run_script("--list").stdout
        self.assertIn(sid, out)
        self.assertIn("Some Worker Task", out)
        self.assertIn("mcp-timer", out)

    def test_count_writes_only_the_integer(self):
        """`$(… --count)` goes straight into a comparison against the target."""
        sid = "eeeeeeee-0000-0000-0000-000000000001"
        self.ledger_entry(sid, "one")
        with FixtureEndpoint(rows=[row(sid)]) as ep:
            self.endpoint = ep.url
            result = self.run_script("--count")
        self.assertEqual(len(result.stdout.strip().splitlines()), 1)
        self.assertEqual(int(result.stdout.strip()), 1)

    def test_an_unreadable_ledger_exits_two_with_an_empty_stdout(self):
        """'Could not check' must never read as 'zero workers' — an idle fleet and a broken
        instrument must not render the same.

        A regular file, not `chmod 0o000`: a 0o000 directory is still readable by root
        (`CAP_DAC_OVERRIDE`), so the chmod spelling of this test passes or fails on the euid of
        whoever runs it — green on CI, red under root. `os.listdir` on a regular file raises
        `NotADirectoryError`, which is an `OSError` but not a `FileNotFoundError`.
        """
        shutil.rmtree(self.ledger)
        with open(self.ledger, "w", encoding="utf-8") as fh:
            fh.write("")
        with FixtureEndpoint() as ep:
            self.endpoint = ep.url
            result = self.run_script("--count")
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")
        self.assertIn("UNKNOWN", result.stderr)

    def test_an_absent_ledger_directory_is_an_empty_fleet_not_an_error(self):
        """A directory that was never created means nobody has been spawned — a different
        answer from 'could not read it', and it must not be reported as unknown."""
        shutil.rmtree(self.ledger)
        with FixtureEndpoint() as ep:
            self.endpoint = ep.url
            result = self.run_script("--count")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "0")


if __name__ == "__main__":
    unittest.main()
