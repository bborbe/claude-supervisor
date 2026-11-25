#!/usr/bin/env python3
"""Tests for scripts/closer-daily-reading.py — the SC4 daily reading.

The reading replaces a vacuous `exit 0`: a bare `closer-gate-lint.py` run has no
closer on stdin, so it lints nothing and exits 0. Every property below exists to
keep the replacement from certifying something it did not look at:

  * **`closers=` counts every line the hook found a `👤 You:` on.** Only
    `no-closer` leaves `closer` null. `not-a-gate` carries the text and is
    counted — the session-bound check runs before the gate checks, so its
    `⏰ Ends:` was judged too, and dropping it would hide most of the population
    (2 672 of 3 324 records on 2026-11-24).
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
        try:
            code = cdr.main(argv)
        except SystemExit as exc:  # argparse raises on a usage error and on --help
            code = exc.code if isinstance(exc.code, int) else cdr.EXIT_USAGE
    return code, out.getvalue(), err.getvalue()


class ReadTest(unittest.TestCase):
    def test_closers_counts_a_not_a_gate_line_and_drops_a_null_closer(self):
        # `not-a-gate` means "a closer line, but not an ask" — the session-bound
        # check already ran on it, so it belongs in the population. `no-closer` is
        # the only reason with a null `closer`, and it is the one that drops out.
        records = [
            _record("a", "not-a-gate", closer="👤 You: nothing"),
            _record("b", "no-closer"),
            _record("c", "allow-listed", closer="👤 You: approve: x"),
        ]
        self.assertEqual(cdr.read(records), (2, 0, 2))

    def test_session_bound_counts_distinct_sessions_not_records(self):
        records = [
            _record("a", "session-bound-ends", closer="👤 You: nothing", cls="monitor"),
            _record("a", "session-bound-ends", closer="👤 You: nothing", cls="monitor"),
            _record("a", "blocked-retry", closer="👤 You: nothing", cls="monitor"),
            _record("b", "session-bound-ends", closer="👤 You: nothing", cls="cron-create"),
            _record("c", "allow-listed", closer="👤 You: approve: y"),
        ]
        self.assertEqual(cdr.read(records), (5, 2, 3))

    def test_a_class_with_no_session_id_still_counts(self):
        # `verdict()` takes the id from the payload, so a record can carry a class
        # with no id. Dropping it would undercount the session-bound population —
        # the one direction that certifies a day clean.
        records = [_record(None, "session-bound-ends", closer="x", cls="monitor")]
        _, bound, sessions = cdr.read(records)
        self.assertEqual(bound, 1)
        self.assertEqual(sessions, 0)

    def test_many_null_session_ids_count_as_one_unknown_session(self):
        # The sentinel is shared, so an id-less population is one unknown session
        # rather than N — the bound count stays a count of sessions.
        records = [
            _record(None, "session-bound-ends", closer="x", cls="monitor"),
            _record(None, "session-bound-ends", closer="y", cls="cron-create"),
        ]
        _, bound, _ = cdr.read(records)
        self.assertEqual(bound, 1)

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

    def test_unparsable_and_unreadable_inputs_are_reported_not_silent(self):
        with tempfile.TemporaryDirectory() as root:
            with open(os.path.join(root, "a.jsonl"), "w", encoding="utf-8") as handle:
                handle.write("{not json\n")
                handle.write(json.dumps(_record("a", "allow-listed", closer="x")) + "\n")
            # A directory named like a record file: open() raises, and the read
            # must say so rather than reporting a clean day.
            os.makedirs(os.path.join(root, "b.jsonl"))
            code, out, err = _run(["--date", DAY, "--root", root, "--census-sessions", "1"])
        self.assertEqual(code, cdr.EXIT_CLEAN)
        self.assertIn("closers=1", out)
        self.assertIn("1 unreadable", err)
        self.assertIn("1 unparsable", err)

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

    def test_a_zero_census_cannot_certify_a_day(self):
        # `sessions < 0.9 * 0` is `sessions < 0`, never true — so a zero denominator
        # would pass every day clean with zero coverage. The vacuous exit 0 this
        # reading exists to replace, reintroduced through the denominator.
        with tempfile.TemporaryDirectory() as root:
            _write(root, "a", [_record("a", "allow-listed", closer="x")])
            code, out, err = _run(["--date", DAY, "--root", root, "--census-sessions", "0"])
        self.assertEqual(code, cdr.EXIT_COVERAGE)
        self.assertIn("not a population", err)

    def test_a_negative_census_cannot_certify_a_day(self):
        with tempfile.TemporaryDirectory() as root:
            _write(root, "a", [_record("a", "allow-listed", closer="x")])
            code, _, _ = _run(["--date", DAY, "--root", root, "--census-sessions", "-5"])
        self.assertEqual(code, cdr.EXIT_COVERAGE)

    def test_a_usage_error_is_not_exit_2(self):
        # argparse's own error exit is 2, which is this script's session-bound code;
        # a typo'd flag must not read as "the day is NOT clean".
        with tempfile.TemporaryDirectory() as root:
            code, _, _ = _run(["--date", DAY, "--root", root, "--no-such-flag"])
        self.assertEqual(code, cdr.EXIT_USAGE)
        self.assertNotEqual(code, cdr.EXIT_SESSION_BOUND)

    def test_missing_root_is_a_usage_error(self):
        code, _, err = _run(["--date", DAY, "--root", "/nonexistent/closer-state"])
        self.assertEqual(code, cdr.EXIT_USAGE)
        self.assertIn("no such directory", err)

    def test_bad_date_is_a_usage_error(self):
        with tempfile.TemporaryDirectory() as root:
            code, _, err = _run(["--date", "24.11.2026", "--root", root])
        self.assertEqual(code, cdr.EXIT_USAGE)
        self.assertIn("YYYY-MM-DD", err)


class DefaultRootTest(unittest.TestCase):
    def test_the_hooks_own_override_is_honoured(self):
        # closer-gate-lint.py resolves its directory from this variable first, so a
        # reading that ignored it would read an empty default and exit 3 every day.
        self.assertEqual(
            cdr.default_root({cdr.DEFAULT_ROOT_ENV: "/somewhere/else"}), "/somewhere/else"
        )

    def test_the_default_applies_when_the_override_is_unset_or_empty(self):
        self.assertEqual(cdr.default_root({}), cdr.DEFAULT_ROOT)
        self.assertEqual(cdr.default_root({cdr.DEFAULT_ROOT_ENV: ""}), cdr.DEFAULT_ROOT)

    def test_an_explicit_root_wins_over_the_override(self):
        with tempfile.TemporaryDirectory() as root:
            _write(root, "a", [_record("a", "allow-listed", closer="x")])
            previous = os.environ.get(cdr.DEFAULT_ROOT_ENV)
            os.environ[cdr.DEFAULT_ROOT_ENV] = "/nonexistent/override"
            try:
                code, _, _ = _run(["--date", DAY, "--root", root, "--census-sessions", "1"])
            finally:
                if previous is None:
                    del os.environ[cdr.DEFAULT_ROOT_ENV]
                else:
                    os.environ[cdr.DEFAULT_ROOT_ENV] = previous
        self.assertEqual(code, cdr.EXIT_CLEAN)


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

    def test_the_sessions_column_is_found_by_header_not_position(self):
        # A table that gains a column must not silently return the wrong number.
        with tempfile.TemporaryDirectory() as root:
            path = os.path.join(root, "goal.md")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("| Date | census unkept | sessions | note |\n")
                handle.write("|---|---|---|---|\n")
                handle.write(f"| {DAY} | 9 | 44 | extra |\n")
            self.assertEqual(
                cdr.census_from_goal_page(path, cdr.datetime.date.fromisoformat(DAY)), 44
            )

    def test_a_row_under_a_second_header_uses_that_header(self):
        # The column index is re-read per table, so a page whose first table has no
        # row for the day still resolves the second table's row correctly.
        with tempfile.TemporaryDirectory() as root:
            path = os.path.join(root, "goal.md")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("| Date | sessions |\n|---|---|\n")
                handle.write("| 2026-11-20 | 7 |\n\n")
                handle.write("| Date | census unkept | sessions |\n|---|---|---|\n")
                handle.write(f"| {DAY} | 9 | 44 |\n")
            self.assertEqual(
                cdr.census_from_goal_page(path, cdr.datetime.date.fromisoformat(DAY)), 44
            )

    def test_a_bad_byte_in_the_goal_page_does_not_crash(self):
        with tempfile.TemporaryDirectory() as root:
            path = os.path.join(root, "goal.md")
            with open(path, "wb") as handle:
                handle.write(b"| Date | sessions |\n|---|---|\n")
                handle.write(b"| " + DAY.encode() + b" | 44 |\n")
                handle.write(b"\xff\xfe broken prose\n")
            self.assertEqual(
                cdr.census_from_goal_page(path, cdr.datetime.date.fromisoformat(DAY)), 44
            )

    def test_goal_page_supplies_the_denominator(self):
        with tempfile.TemporaryDirectory() as root:
            _write(root, "a", [_record("a", "allow-listed", closer="x")])
            path = self._page(root, [f"| {DAY} | 0 | 0 | 1 | |"])
            code, out, _ = _run(["--date", DAY, "--root", root, "--goal-page", path])
        self.assertEqual(code, cdr.EXIT_CLEAN)
        self.assertIn("coverage=1/1", out)


if __name__ == "__main__":
    unittest.main()
