#!/usr/bin/env python3
"""Fixtures for `scripts/a2a-liveness.py` — one per row, each ASSERTING the rendered verdict.

The criterion is the assertion, not the label: a fixture file carrying named cases with no
`assertEqual` would satisfy a weaker reading of the task and pin nothing, so every case below
asserts the verdict the row is named for.

Run: python3 -m unittest discover -s scripts/tests -v
"""

import importlib.util
import json
import os
import tempfile
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_SCRIPT = os.path.join(os.path.dirname(_HERE), "a2a-liveness.py")

_spec = importlib.util.spec_from_file_location("a2a_liveness", _SCRIPT)
a2a = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(a2a)

# Two ids, the shape the sweep actually probes: a local spawner's and the remote worker's own,
# both read off one task's `claude_session_id` + `metrics_sessions` (measured 2026-10-17 on the
# task that spawned the k8s worker `37ded6f4ca94e293`).
LOCAL = "7c8c0602-f390-4199-a133-27b91407deb1"
REMOTE = "6fb25641-843e-477a-b968-d4908e8ff278"
IDS = [LOCAL, REMOTE]
WORKER = "37ded6f4ca94e293"

# What `session-liveness.py` returns for each, as exit codes.
ABSENT = a2a.LIVENESS_ABSENT
LIVE = a2a.LIVENESS_LIVE
UNKNOWN = 2

# No stop recorded anywhere — the ordinary case, and the one every non-stop fixture passes.
NO_STOPS = set()


def row(**over):
    """One ledger row, shaped as `recordToWire` serves it."""
    base = {"session_id": REMOTE, "target": "k8s", "worker_id": WORKER}
    base.update(over)
    return base


def codes(local=ABSENT, remote=ABSENT):
    return {LOCAL: local, REMOTE: remote}


class Fixtures(unittest.TestCase):
    """F1-F6 — the named rows the task's SC1 and SC3 declare."""

    def test_f1_live_id_is_live(self):
        """F1: a live id in the set -> LIVE (the regression guard; the heartbeat already does this)."""
        self.assertEqual(a2a.verdict([row()], IDS, codes(remote=LIVE), NO_STOPS), a2a.LIVE)

    def test_f2_recorded_stop_is_finished(self):
        """F2: a recorded stop, no live id -> FINISHED."""
        rows = [row(stopped_at="2026-10-17T09:00:00Z")]
        self.assertEqual(a2a.verdict(rows, IDS, codes(), NO_STOPS), a2a.FINISHED)

    def test_f3_no_stop_and_no_live_id_is_orphan(self):
        """F3: no recorded stop, no live id -> ORPHAN."""
        self.assertEqual(a2a.verdict([row()], IDS, codes(), NO_STOPS), a2a.ORPHAN)

    def test_f4_unreadable_ledger_is_unverified(self):
        """F4: the ledger could not be read -> UNVERIFIED, never ORPHAN."""
        self.assertEqual(a2a.verdict(None, IDS, codes(), NO_STOPS), a2a.UNVERIFIED)

    def test_f5_malformed_stop_is_unverified(self):
        """F5: a stop field present but malformed -> UNVERIFIED, never FINISHED."""
        for bad in (True, 0, {}, [], "not-a-date"):
            with self.subTest(stopped_at=bad):
                self.assertEqual(
                    a2a.verdict([row(stopped_at=bad)], IDS, codes(), NO_STOPS), a2a.UNVERIFIED
                )

    def test_f6_non_remote_target_is_not_remote(self):
        """F6: a row whose target is not docker/k8s -> NOT-REMOTE, the local verdict stands."""
        self.assertEqual(
            a2a.verdict([row(target="interactive")], IDS, codes(), NO_STOPS), a2a.NOT_REMOTE
        )


class StopFile(unittest.TestCase):
    """The stop file's half — the rows the ledger cannot carry.

    `writeLedger` refuses a rehydrated agent, so a stop on a worker this server did NOT spawn
    never reaches the ledger row. The file is the only record of it, and the resolver must read
    it or that worker keeps reading as an orphan candidate.
    """

    def test_f7_a_stop_on_a_rehydrated_row_is_finished_not_orphan(self):
        """F7 — the manager's required case: the ledger row carries no stop, the file does."""
        rows = [row()]  # no `stopped_at`: this server never wrote the row
        stops = {WORKER}
        self.assertEqual(a2a.verdict(rows, IDS, codes(), stops), a2a.FINISHED)

    def test_f8_an_unreadable_stop_file_is_unverified_not_orphan(self):
        """An unreadable file proves nothing: it exists, so something stopped."""
        self.assertEqual(a2a.verdict([row()], IDS, codes(), None), a2a.UNVERIFIED)

    def test_a_missing_stop_file_is_empty_not_unreadable(self):
        """Nothing writes the file until the first stop on this host, so absence is ordinary."""
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(a2a.read_stops(os.path.join(tmp, "nope.jsonl")), set())

    def test_a_present_unreadable_file_is_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "stops.jsonl")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("{}\n")
            os.chmod(path, 0o000)
            try:
                self.assertIsNone(a2a.read_stops(path))
            finally:
                os.chmod(path, 0o600)

    def test_the_join_is_on_worker_id_never_the_task(self):
        """One task can be spawned again; a task-keyed join would read the successor as stopped."""
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "stops.jsonl")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(json.dumps({"task": "some task", "stopped_at": "2026-10-17"}) + "\n")
                handle.write(json.dumps({"worker_id": WORKER, "task": "other"}) + "\n")
            stops = a2a.read_stops(path)
            self.assertEqual(stops, {WORKER})

    def test_a_malformed_line_is_skipped_not_fatal(self):
        """One bad append must not blind the resolver to every stop beside it."""
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "stops.jsonl")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("not json\n")
                handle.write("\n")
                handle.write(json.dumps({"worker_id": WORKER}) + "\n")
            self.assertEqual(a2a.read_stops(path), {WORKER})

    def test_the_writer_shape_is_read(self):
        """The shape `appendStop` writes — pinned here so a change on the JS side fails this."""
        written = {
            "worker_id": WORKER,
            "task": "Some Task",
            "stopped_at": "2026-10-17T09:00:00.000Z",
            "stopping_session": "aaaa1111-0000-0000-0000-000000000000",
        }
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "stops.jsonl")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(json.dumps(written) + "\n")
            self.assertEqual(a2a.read_stops(path), {WORKER})


class SafetyOrder(unittest.TestCase):
    """The directions that must not be reachable — each one is a defect, not a preference."""

    def test_live_outranks_a_stale_stop(self):
        """A re-spawned worker leaves the old stop behind; the live heartbeat must win."""
        rows = [row(stopped_at="2026-10-17T09:00:00Z")]
        self.assertEqual(a2a.verdict(rows, IDS, codes(remote=LIVE), NO_STOPS), a2a.LIVE)

    def test_stop_outranks_orphan(self):
        """The one dangerous direction: a stopped worker must never read as ORPHAN."""
        rows = [row(stopped_at="2026-10-17")]
        self.assertNotEqual(a2a.verdict(rows, IDS, codes(), NO_STOPS), a2a.ORPHAN)
        self.assertEqual(a2a.verdict(rows, IDS, codes(), NO_STOPS), a2a.FINISHED)

    def test_unknown_liveness_is_unverified_not_orphan(self):
        """An unreadable source cannot prove a worker dead."""
        self.assertEqual(a2a.verdict([row()], IDS, codes(remote=UNKNOWN), NO_STOPS), a2a.UNVERIFIED)

    def test_a_probe_that_could_not_run_is_unverified(self):
        self.assertEqual(
            a2a.verdict([row()], IDS, {LOCAL: ABSENT, REMOTE: None}, NO_STOPS), a2a.UNVERIFIED
        )

    def test_no_ids_is_unverified(self):
        self.assertEqual(a2a.verdict([row()], [], {}, NO_STOPS), a2a.UNVERIFIED)

    def test_empty_ledger_is_not_remote_not_unverified(self):
        """A readable ledger with no matching row is a positive answer: not this rule's row."""
        self.assertEqual(a2a.verdict([], IDS, codes(), NO_STOPS), a2a.NOT_REMOTE)

    def test_exit_codes_are_distinct(self):
        """The caller distinguishes the verdicts by exit code, so no two may collide."""
        values = list(a2a.EXIT.values())
        self.assertEqual(len(values), len(set(values)))

    def test_absent_matches_session_liveness_codes(self):
        """The liveness codes mirrored here must be the instrument's own, not a re-invention."""
        self.assertEqual(a2a.LIVENESS_LIVE, 0)
        self.assertEqual(a2a.LIVENESS_ABSENT, 1)


class LedgerParsing(unittest.TestCase):
    def test_non_array_body_is_unreadable(self):
        """A body that is not an array is the store answering a different question."""
        for body in ({"rows": []}, "[]", 0, None, True):
            with self.subTest(body=body):
                self.assertIsNone(a2a.parse_ledger_rows(body))

    def test_non_dict_rows_are_dropped_not_fatal(self):
        self.assertEqual(a2a.parse_ledger_rows([{"a": 1}, "junk", 3]), [{"a": 1}])

    def test_empty_array_is_readable_and_empty(self):
        """`[]` is a positive claim, distinct from the `None` an outage yields."""
        self.assertEqual(a2a.parse_ledger_rows([]), [])

    def test_stop_state_reads_presence_not_truthiness(self):
        self.assertEqual(a2a.stop_state({}, NO_STOPS), "none")
        self.assertEqual(a2a.stop_state({"stopped_at": None}, NO_STOPS), "none")
        self.assertEqual(a2a.stop_state({"stopped_at": ""}, NO_STOPS), "none")
        self.assertEqual(a2a.stop_state({"stopped_at": "2026-10-17T09:00:00Z"}, NO_STOPS), "stopped")
        self.assertEqual(a2a.stop_state({"stopped_at": "2026-10-17"}, NO_STOPS), "stopped")
        self.assertEqual(a2a.stop_state({"stopped_at": False}, NO_STOPS), "malformed")

    def test_stop_state_reads_the_file_by_worker_id(self):
        self.assertEqual(a2a.stop_state({"worker_id": WORKER}, {WORKER}), "stopped")
        self.assertEqual(a2a.stop_state({"worker_id": WORKER}, set()), "none")
        # A row with no worker id cannot be joined to the file, and must not be read as stopped.
        self.assertEqual(a2a.stop_state({}, {WORKER}), "none")


if __name__ == "__main__":
    unittest.main()
