#!/usr/bin/env python3
"""Tests for scripts/closer-daily-reading.py — the SC4 daily reading.

The reading replaces a vacuous `exit 0`: a bare `closer-gate-lint.py` run has no
closer on stdin, so it lints nothing and exits 0. Every property below exists to
keep the replacement from certifying something it did not look at:

  * **`closers=` excludes the records that judged nothing.** `not-a-gate` and
    `no-closer` carry `closer: null`; counting them would inflate the population
    with the very lines the hook declined to lint.
  * **`session-bound=` counts sessions, not records.** A re-blocked closer
    re-emits its original `class` under `blocked-retry`, so a record count
    reports one session twice (measured 2026-11-24: 3 records, 2 sessions).
  * **Every non-clean outcome is a non-zero exit.** An empty day, a day with a
    session-bound observable, and a day whose population cannot be checked all
    have to fail — a reading that prints a warning and exits 0 is the defect
    this script replaces.
  * **`--root` makes two copies comparable.** The live directory accrues during
    the day, so the only honest comparison of two builds is over one frozen
    snapshot; the flag is what makes that possible.

Run: python3 -m unittest discover -s scripts/tests
"""

import contextlib
import importlib.util
import io
import json
import os
import tempfile
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_SCRIPT = os.path.join(os.path.dirname(_HERE), "closer-daily-reading.py")

_spec = importlib.util.spec_from_file_location("closer_daily_reading", _SCRIPT)
cdr = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cdr)

DAY = "2026-11-24"
#: Mid-day UTC, so the local calendar date is the same in every zone CI runs in.
MIDDAY = f"{DAY}T12:00:00+00:00"


def _record(session_id, reason, closer=None, cls=None, ts=MIDDAY):
    return {
        "ts": ts,
        "session_id": session_id,
        "decision": "block" if cls or reason not in ("not-a-gate", "no-closer") else "allow",
        "reason": reason,
        "verb": None,
        "matched": None,
        "closer": closer,
        "ends": None,
        "class": cls,
        "phrase": None,
    }


def _write(root, name, records):
    with open(os.path.join(root, f"{name}.jsonl"), "w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record) + "\n")


def _run(argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = cdr.main(argv)
    return code, out.getvalue(), err.getvalue()


class ReadTest(unittest.TestCase):
    def test_closers_excludes_records_that_judged_nothing(self):
        records = [
            _record("a", "not-a-gate"),
            _record("b", "no-closer"),
            _record("c", "allow-listed", closer="👤 You: approve: x"),
        ]
        self.assertEqual(cdr.read(records), (1, 0, 1))

    def test_session_bound_counts_distinct_sessions_not_records(self):
        records = [
            _record("a", "session-bound-ends", closer="👤 You: nothing", cls="monitor"),
            _record("a", "session-bound-ends", closer="👤 You: nothing", cls="monitor"),
            _record("a", "blocked-retry", closer="👤 You: nothing", cls="monitor"),
            _record("b", "session-bound-ends", closer="👤 You: nothing", cls="cron-create"),
            _record("c", "allow-listed", closer="👤 You: approve: y"),
        ]
        self.assertEqual(cdr.read(records), (5, 2, 3))

    def test_blocked_retry_alone_does_not_make_a_session_bound_session(self):
        # A retry record re-emits the original class; without the session dedupe a
        # single unclean session reads as two.
        records = [
            _record("a", "session-bound-ends", closer="x", cls="monitor"),
            _record("a", "blocked-retry", closer="x", cls="monitor"),
        ]
        _, bound, _ = cdr.read(records)
        self.assertEqual(bound, 1)


class DayFilterTest(unittest.TestCase):
    def test_other_days_are_not_counted(self):
        with tempfile.TemporaryDirectory() as root:
            _write(root, "a", [_record("a", "allow-listed", closer="x")])
            _write(
                root,
                "b",
                [_record("b", "allow-listed", closer="x", ts="2026-11-23T12:00:00+00:00")],
            )
            code, out, _ = _run(["--date", DAY, "--root", root, "--census-sessions", "1"])
        self.assertEqual(code, cdr.EXIT_CLEAN)
        self.assertIn("closers=1 ", out)

    def test_missing_and_unparsable_ts_are_skipped_not_counted(self):
        with tempfile.TemporaryDirectory() as root:
            _write(
                root,
                "a",
                [
                    _record("a", "allow-listed", closer="x", ts=None),
                    _record("a", "allow-listed", closer="x", ts="not-a-date"),
                ],
            )
            code, out, err = _run(["--date", DAY, "--root", root])
        self.assertEqual(code, cdr.EXIT_EMPTY)
        self.assertIn("closers=0", out)
        self.assertIn("undated", err)


class ExitCodeTest(unittest.TestCase):
    def test_clean_day_exits_zero_and_prints_one_line(self):
        with tempfile.TemporaryDirectory() as root:
            _write(root, "a", [_record("a", "allow-listed", closer="x")])
            code, out, _ = _run(["--date", DAY, "--root", root, "--census-sessions", "1"])
        self.assertEqual(code, cdr.EXIT_CLEAN)
        self.assertEqual(len(out.strip().splitlines()), 1)
        self.assertEqual(
            out.strip(), f"{DAY} closers=1 session-bound=0 sessions=1 coverage=1/1"
        )

    def test_session_bound_observable_fails_the_day(self):
        with tempfile.TemporaryDirectory() as root:
            _write(
                root,
                "a",
                [_record("a", "session-bound-ends", closer="x", cls="monitor")],
            )
            code, out, err = _run(["--date", DAY, "--root", root, "--census-sessions", "1"])
        self.assertEqual(code, cdr.EXIT_SESSION_BOUND)
        self.assertIn("session-bound=1", out)
        self.assertIn("NOT clean", err)

    def test_empty_day_exits_non_zero(self):
        with tempfile.TemporaryDirectory() as root:
            code, out, _ = _run(["--date", DAY, "--root", root, "--census-sessions", "10"])
        self.assertEqual(code, cdr.EXIT_EMPTY)
        self.assertIn("closers=0", out)

    def test_no_denominator_exits_non_zero(self):
        with tempfile.TemporaryDirectory() as root:
            _write(root, "a", [_record("a", "allow-listed", closer="x")])
            code, out, _ = _run(["--date", DAY, "--root", root])
        self.assertEqual(code, cdr.EXIT_COVERAGE)
        self.assertIn("coverage=1/unknown", out)

    def test_coverage_below_the_floor_exits_non_zero(self):
        with tempfile.TemporaryDirectory() as root:
            _write(root, "a", [_record("a", "allow-listed", closer="x")])
            code, out, _ = _run(["--date", DAY, "--root", root, "--census-sessions", "2"])
        self.assertEqual(code, cdr.EXIT_COVERAGE)
        self.assertIn("coverage=1/2", out)

    def test_coverage_at_the_floor_passes(self):
        # 9 of 10 is exactly the floor; the comparison must not reject it.
        records = [_record(f"s{i}", "allow-listed", closer="x") for i in range(9)]
        with tempfile.TemporaryDirectory() as root:
            _write(root, "a", records)
            code, _, _ = _run(["--date", DAY, "--root", root, "--census-sessions", "10"])
        self.assertEqual(code, cdr.EXIT_CLEAN)

    def test_session_bound_outranks_a_thin_population(self):
        # The substantive finding wins: a day can be both unclean and thin.
        with tempfile.TemporaryDirectory() as root:
            _write(
                root,
                "a",
                [_record("a", "session-bound-ends", closer="x", cls="monitor")],
            )
            code, _, _ = _run(["--date", DAY, "--root", root, "--census-sessions", "100"])
        self.assertEqual(code, cdr.EXIT_SESSION_BOUND)

    def test_missing_root_is_a_usage_error(self):
        code, _, err = _run(["--date", DAY, "--root", "/nonexistent/closer-state"])
        self.assertEqual(code, cdr.EXIT_USAGE)
        self.assertIn("no such directory", err)

    def test_bad_date_is_a_usage_error(self):
        with tempfile.TemporaryDirectory() as root:
            code, _, err = _run(["--date", "24.11.2026", "--root", root])
        self.assertEqual(code, cdr.EXIT_USAGE)
        self.assertIn("YYYY-MM-DD", err)


class SnapshotTest(unittest.TestCase):
    def test_two_roots_with_the_same_content_read_identically(self):
        records = [
            _record("a", "session-bound-ends", closer="x", cls="monitor"),
            _record("b", "allow-listed", closer="y"),
        ]
        with tempfile.TemporaryDirectory() as one, tempfile.TemporaryDirectory() as two:
            _write(one, "a", records)
            _write(two, "a", records)
            argv = ["--date", DAY, "--census-sessions", "2"]
            first = _run(argv + ["--root", one])
            second = _run(argv + ["--root", two])
        self.assertEqual(first, second)
        self.assertIn(f"{DAY} closers=2 session-bound=1 sessions=2 coverage=2/2", first[1])

    def test_live_accrual_between_runs_is_why_the_snapshot_exists(self):
        # Pinning the reason for `--root`: one extra record changes the line, so a
        # comparison against a live directory can never distinguish a mismatch
        # from new records.
        with tempfile.TemporaryDirectory() as root:
            _write(root, "a", [_record("a", "allow-listed", closer="x")])
            before = _run(["--date", DAY, "--root", root, "--census-sessions", "1"])[1]
            _write(root, "b", [_record("b", "allow-listed", closer="y")])
            after = _run(["--date", DAY, "--root", root, "--census-sessions", "1"])[1]
        self.assertNotEqual(before, after)


class GoalPageTest(unittest.TestCase):
    def _page(self, root, rows):
        path = os.path.join(root, "goal.md")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("# Readings\n\n| Date | census unkept | census double-kept | sessions | closer-lint |\n")
            handle.write("|---|---|---|---|---|\n")
            for row in rows:
                handle.write(row + "\n")
        return path

    def test_reads_the_sessions_cell_for_the_day(self):
        with tempfile.TemporaryDirectory() as root:
            path = self._page(
                root,
                [
                    "| 2026-11-23 | 1 | 0 | 23 | exit 0 |",
                    f"| {DAY} | 9 | 0 | 30 | vacuous |",
                ],
            )
            self.assertEqual(cdr.census_from_goal_page(path, cdr.datetime.date.fromisoformat(DAY)), 30)

    def test_absent_day_and_non_numeric_cell_yield_none(self):
        with tempfile.TemporaryDirectory() as root:
            path = self._page(root, ["| 2026-11-23 | 1 | 0 | 23 | exit 0 |"])
            day = cdr.datetime.date.fromisoformat(DAY)
            self.assertIsNone(cdr.census_from_goal_page(path, day))
            path2 = self._page(root, [f"| {DAY} | 9 | 0 | not run | x |"])
            self.assertIsNone(cdr.census_from_goal_page(path2, day))

    def test_goal_page_supplies_the_denominator(self):
        with tempfile.TemporaryDirectory() as root:
            _write(root, "a", [_record("a", "allow-listed", closer="x")])
            path = self._page(root, [f"| {DAY} | 0 | 0 | 1 | |"])
            code, out, _ = _run(["--date", DAY, "--root", root, "--goal-page", path])
        self.assertEqual(code, cdr.EXIT_CLEAN)
        self.assertIn("coverage=1/1", out)


if __name__ == "__main__":
    unittest.main()
