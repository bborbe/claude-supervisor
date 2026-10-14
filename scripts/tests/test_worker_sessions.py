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

⚠️ **The LEDGER moved to the endpoint on 2026-10-14, and this file's fixtures moved with it.**
Until then `ledger_entry` wrote `<sid>.json` into a temp directory and the counter walked it.
`spawn-ledger.py` reads `GET /api/1.0/session-ledger` now, so every case below passes its
ledger through the fixture instead — and the two directory-shaped cases became store-shaped
ones rather than being deleted, because "unreadable" and "nobody recorded" are the two answers
this counter must never collapse and both still have to be reachable.
"""
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest

import endpoint_fixture  # noqa: E402
from endpoint_fixture import FixtureEndpoint, dead_url, ledger_row, row  # noqa: E402

# Side effect only: points the start-time cache at an isolated per-run store, in one shared
# home so five suites cannot each assign the same key and leave only the last standing.
import start_cache_isolation  # noqa: E402,F401

SCRIPTS = pathlib.Path(__file__).resolve().parent.parent
SCRIPT = SCRIPTS / "worker-sessions.py"


class WorkerSessionsTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
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
        """One spawn-ledger row in the store's wire shape — a ROW now, not a file.

        ⚠️ **A file would be read by nobody, and that is the silent-pass shape this file
        exists to avoid.** The reader asks the endpoint; a case that still planted a
        directory would assert against an empty ledger and pass on a counter that saw
        nothing at all.
        """
        return ledger_row(session_id, label, **extra)

    def run_script(self, *args, endpoint=None):
        return subprocess.run(
            [sys.executable, os.path.join(self.bin, "worker-sessions.py"),
             "--endpoint", endpoint or self.endpoint, *args],
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
        ledger = [self.ledger_entry(sid, f"worker {i}") for i, sid in enumerate(ids)]
        with FixtureEndpoint(rows=[row(s) for s in ids], ledger_rows=ledger) as ep:
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
        ledger = [self.ledger_entry("cccccccc-0000-0000-0000-000000000001", "long gone")]
        with FixtureEndpoint(ledger_rows=ledger) as ep:
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
        ledger = [self.ledger_entry(sid, "cluster worker")]
        with FixtureEndpoint(rows=[row(sid, source="cluster")], ledger_rows=ledger) as ep:
            self.endpoint = ep.url
            result = self.run_script("--count")
        self.assertEqual(result.stdout.strip(), "1", result.stderr)

    def test_an_auto_resumed_worker_is_not_counted(self):
        """Auto-resumes answer to the auto-resume gate's own 30-min crash-loop cap, not this
        one: counting them would leave a sweep that revived two dead workers unable to open a
        new one. The marker is the ledger record's `resumed_from`, the same field
        `check-spawn-ledger.py` reads."""
        sid = "ffffffff-0000-0000-0000-000000000004"
        ledger = [self.ledger_entry(sid, "auto-resumed", resumed_from="aaaaaaaa-0000-0000-0000-000000000009")]
        with FixtureEndpoint(rows=[row(sid)], ledger_rows=ledger) as ep:
            self.endpoint = ep.url
            result = self.run_script("--count")
        self.assertEqual(result.stdout.strip(), "0", result.stderr)

    def test_a_manager_record_is_not_counted_against_the_worker_cap(self):
        """`spawn_agent(role="manager")` writes a ledger record exactly like a worker's, so
        presence in the ledger alone counted a manager against the fleet-wide
        `spawn.maxConcurrent` cap and pushed the fleet toward refusing ordinary spawns. The
        record's own `role` is what separates them."""
        manager = "ffffffff-0000-0000-0000-000000000010"
        agent = "ffffffff-0000-0000-0000-000000000011"
        ledger = [
            self.ledger_entry(manager, "topic manager", role="manager"),
            self.ledger_entry(agent, "worker", role="agent"),
        ]
        with FixtureEndpoint(rows=[row(manager), row(agent)], ledger_rows=ledger) as ep:
            self.endpoint = ep.url
            result = self.run_script("--count")
        self.assertEqual(result.stdout.strip(), "1", result.stderr)

    def test_two_agent_records_are_both_counted(self):
        """The control for the manager exclusion: without it the case above passes on a counter
        that returns 0 for everything."""
        ids = [f"ffffffff-0000-0000-0000-00000000001{i}" for i in (2, 3)]
        ledger = [
            self.ledger_entry(sid, label, role="agent")
            for sid, label in zip(ids, ("worker a", "worker b"))
        ]
        with FixtureEndpoint(rows=[row(s) for s in ids], ledger_rows=ledger) as ep:
            self.endpoint = ep.url
            result = self.run_script("--count")
        self.assertEqual(result.stdout.strip(), "2", result.stderr)

    def test_a_role_less_record_still_reads_as_a_worker(self):
        """⚠️ The exclusion is `role == "manager"`, never `role != "agent"`. A record with no
        `role` was written before the field existed, and those are pre-field workers and
        managers alike — so treating absence as "manager" would drop every pre-field worker out
        of the count this cap exists to enforce."""
        sid = "ffffffff-0000-0000-0000-000000000014"
        ledger = [self.ledger_entry(sid, "pre-field worker", role="")]
        with FixtureEndpoint(rows=[row(sid)], ledger_rows=ledger) as ep:
            self.endpoint = ep.url
            result = self.run_script("--count")
        self.assertEqual(result.stdout.strip(), "1", result.stderr)

    def test_list_names_the_source_for_an_endpoint_sourced_worker(self):
        """`status` is the store row's `source` — `cluster` for a mirrored cluster session. It
        is descriptive only, but a reader that dropped it would leave the column empty."""
        sid = "ffffffff-0000-0000-0000-000000000005"
        ledger = [self.ledger_entry(sid, "Some Worker")]
        with FixtureEndpoint(rows=[row(sid, source="cluster")], ledger_rows=ledger) as ep:
            self.endpoint = ep.url
            out = self.run_script("--list").stdout
        line = [ln for ln in out.splitlines() if sid in ln][0]
        self.assertIn("cluster", line)

    def test_a_row_the_endpoint_reports_not_live_is_not_counted(self):
        """The verdict is the store's own `live` flag, never the row's existence — a store
        whose writer was killed keeps its rows, so existence reports the wrong answer for the
        case the store exists to catch."""
        sid = "ffffffff-0000-0000-0000-000000000002"
        ledger = [self.ledger_entry(sid, "finished hours ago")]
        with FixtureEndpoint(rows=[row(sid, live=False, age_seconds=3600)], ledger_rows=ledger) as ep:
            self.endpoint = ep.url
            result = self.run_script("--count")
        self.assertEqual(result.stdout.strip(), "0", result.stderr)

    def test_a_row_missing_its_live_flag_is_not_counted(self):
        """⚠️ A renamed or absent `live` key must never read as liveness. The endpoint is the
        only channel now, so a row that does not positively say `true` is not an answer — and
        a row that is not even an object must be skipped rather than crash the count."""
        sid = "ffffffff-0000-0000-0000-000000000007"
        ledger = [self.ledger_entry(sid, "malformed row")]
        rows = [{"session_id": sid, "source": "mcp-timer"}, "not-a-row", None]
        with FixtureEndpoint(rows=rows, ledger_rows=ledger) as ep:
            self.endpoint = ep.url
            result = self.run_script("--count")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "0")

    def test_an_unreachable_endpoint_exits_two(self):
        """The endpoint is the liveness source, so failing to read it is UNKNOWN for the same
        reason the ledger is: a store that cannot be read may be hiding the very session being
        counted, and folding that into a number under-counts the fleet."""
        ledger = [self.ledger_entry("ffffffff-0000-0000-0000-000000000008", "worker")]
        with FixtureEndpoint(ledger_rows=ledger) as ep:
            self.endpoint = ep.url
        result = self.run_script("--count", endpoint=dead_url())
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")
        self.assertIn("UNKNOWN", result.stderr)

    def test_a_non_200_list_route_exits_two(self):
        """A non-200 that is not a liveness answer must not be read as an empty fleet."""
        ledger = [self.ledger_entry("ffffffff-0000-0000-0000-000000000009", "worker")]
        with FixtureEndpoint(rows=[], list_status=500, ledger_rows=ledger) as ep:
            self.endpoint = ep.url
            result = self.run_script("--count")
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")
        self.assertIn("UNKNOWN", result.stderr)

    def test_the_ignored_flags_warn_instead_of_being_obeyed(self):
        """`--ledger-dir`, `--dir` and `--heartbeat-dir` are kept so existing callers do not
        break, but a caller that passes one must not believe a path or a window is in force
        when neither is."""
        sid = "eeeeeeee-0000-0000-0000-000000000002"
        ledger = [self.ledger_entry(sid, "one")]
        with FixtureEndpoint(rows=[row(sid)], ledger_rows=ledger) as ep:
            self.endpoint = ep.url
            result = self.run_script(
                "--count",
                "--ledger-dir", "/nonexistent",
                "--dir", "/nonexistent",
                "--heartbeat-dir", "/nonexistent",
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "1")
        self.assertIn("IGNORED", result.stderr)

    def test_list_shows_the_label_and_source(self):
        sid = "dddddddd-0000-0000-0000-000000000001"
        ledger = [self.ledger_entry(sid, "Some Worker Task")]
        with FixtureEndpoint(rows=[row(sid, source="mcp-timer")], ledger_rows=ledger) as ep:
            self.endpoint = ep.url
            out = self.run_script("--list").stdout
        self.assertIn(sid, out)
        self.assertIn("Some Worker Task", out)
        self.assertIn("mcp-timer", out)

    def test_count_writes_only_the_integer(self):
        """`$(… --count)` goes straight into a comparison against the target."""
        sid = "eeeeeeee-0000-0000-0000-000000000001"
        ledger = [self.ledger_entry(sid, "one")]
        with FixtureEndpoint(rows=[row(sid)], ledger_rows=ledger) as ep:
            self.endpoint = ep.url
            result = self.run_script("--count")
        self.assertEqual(len(result.stdout.strip().splitlines()), 1)
        self.assertEqual(int(result.stdout.strip()), 1)

    def test_an_unreadable_ledger_exits_two_with_an_empty_stdout(self):
        """'Could not check' must never read as 'zero workers' — an idle fleet and a broken
        instrument must not render the same.

        ⚠️ **A non-200 on the ledger route, not a chmodded directory.** The ledger was a
        directory until 2026-10-14, and the old spelling of this case made a path unreadable;
        the store's equivalent is a route that answers something other than 200, which
        `spawn-ledger.py` turns into `None`. The asymmetry with the case below is the whole
        point: a 500 REFUSES, an empty list PERMITS.
        """
        ledger = [self.ledger_entry("ffffffff-0000-0000-0000-000000000020", "worker")]
        with FixtureEndpoint(ledger_rows=ledger, ledger_status=500) as ep:
            self.endpoint = ep.url
            result = self.run_script("--count")
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")
        self.assertIn("UNKNOWN", result.stderr)

    def test_a_missing_ledger_route_exits_two(self):
        """⚠️ THE DEPLOY-ORDER HAZARD, pinned. A controller older than the session-ledger
        route answers **404**, and a 404 is `None` — which REFUSES a spawn — where an empty
        store is `{}` and permits one. Deploying the supervisor half before the controller
        half is exactly how a fleet-wide spawn outage happens, and this case is what keeps
        the two answers from collapsing into each other.
        """
        with FixtureEndpoint(ledger_status=404) as ep:
            self.endpoint = ep.url
            result = self.run_script("--count")
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")
        self.assertIn("UNKNOWN", result.stderr)

    def test_an_empty_ledger_store_is_an_empty_fleet_not_an_error(self):
        """A store that has never been written means nobody has been spawned — a different
        answer from 'could not read it', and it must not be reported as unknown. `200 []`
        permits a spawn where a 404 must not."""
        with FixtureEndpoint(ledger_rows=[]) as ep:
            self.endpoint = ep.url
            result = self.run_script("--count")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "0")


if __name__ == "__main__":
    unittest.main()
