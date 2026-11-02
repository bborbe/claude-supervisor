#!/usr/bin/env python3
"""Tests for scripts/defer-date.py and the seven readers that now take it.

`defer_date` is defer-UNTIL, an instant — but every reader used to slice its first ten
characters and compare dates, so a task deferred to `2026-11-02T11:00:00+01:00` surfaced
at midnight, and an offset could shift the day it surfaced on. The properties pinned
here are the ones where a plausible implementation looks right and is wrong:

  * **A full timestamp keeps its time of day.** Truncating it is the defect itself.
  * **A date-only value (and vault-cli's `T00:00:00Z` rewrite of one) still means the
    start of that LOCAL day**, so every existing day-granular defer behaves exactly as
    it did — otherwise this fix would move thousands of rows by a day.
  * **An offset is kept, not normalised away**, so `+01:00` crossing midnight UTC does
    not land the defer on the previous day.
  * **An unparseable value is `invalid`, never "available now".** Reading garbage as due
    opens a parked row; every reader withholds it and says why on stderr.
  * **One per-reader case with a defer ONE HOUR AHEAD LATER THE SAME DAY.** That is the
    shape the old truncation got wrong and no date-only fixture can catch.

Run: python3 -m unittest discover -s scripts/tests -v
"""

import datetime as dt
import importlib.util
import os
import pathlib
import shutil
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO

_HERE = pathlib.Path(__file__).resolve().parent
_SCRIPTS = _HERE.parent

NOW = dt.datetime.fromisoformat("2026-11-02T10:00:00+01:00")
LATER = "2026-11-02T11:00:00+01:00"   # one hour ahead, same local day


def load(filename, name=None):
    spec = importlib.util.spec_from_file_location(
        name or filename.replace("-", "_").replace(".py", ""), str(_SCRIPTS / filename)
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


dd = load("defer-date.py")


class TestParseDeferDate(unittest.TestCase):
    def test_empty_and_none_are_no_defer(self):
        self.assertEqual(dd.parse_defer_date(None), (None, None))
        self.assertEqual(dd.parse_defer_date(""), (None, None))
        self.assertEqual(dd.parse_defer_date("   "), (None, None))

    def test_date_only_is_the_start_of_that_local_day(self):
        parsed, reason = dd.parse_defer_date("2026-11-02")
        self.assertIsNone(reason)
        self.assertEqual(parsed, dt.datetime(2026, 11, 2).astimezone())
        self.assertIsNotNone(parsed.tzinfo)

    def test_quoted_date_only_is_the_same_day(self):
        parsed, _ = dd.parse_defer_date('"2026-11-02"')
        self.assertEqual(parsed.date(), dt.date(2026, 11, 2))
        self.assertEqual((parsed.hour, parsed.minute), (0, 0))

    def test_vault_cli_midnight_utc_is_the_start_of_the_local_day(self):
        """vault-cli rewrites an unquoted date scalar to RFC3339 midnight UTC."""
        parsed, reason = dd.parse_defer_date("2026-11-02T00:00:00Z")
        self.assertIsNone(reason)
        self.assertEqual(parsed, dt.datetime(2026, 11, 2).astimezone())

    def test_full_timestamp_keeps_time_and_offset(self):
        parsed, reason = dd.parse_defer_date(LATER)
        self.assertIsNone(reason)
        self.assertEqual(parsed, NOW + dt.timedelta(hours=1))
        self.assertEqual(parsed.utcoffset(), dt.timedelta(hours=1))

    def test_offset_is_kept_across_midnight_utc(self):
        """00:30+01:00 is 23:30Z the day before — the local day is what must survive."""
        parsed, _ = dd.parse_defer_date("2026-11-02T00:30:00+01:00")
        self.assertEqual(parsed.date(), dt.date(2026, 11, 2))
        self.assertEqual(parsed.hour, 0)
        self.assertEqual(parsed.utcoffset(), dt.timedelta(hours=1))
        self.assertEqual(parsed.astimezone(dt.timezone.utc).date(), dt.date(2026, 11, 1))

    def test_z_suffix_is_utc(self):
        parsed, _ = dd.parse_defer_date("2026-11-02T11:00:00Z")
        self.assertEqual(parsed.utcoffset(), dt.timedelta(0))
        self.assertEqual(parsed.hour, 11)

    def test_naive_datetime_is_local(self):
        parsed, reason = dd.parse_defer_date("2026-11-02T11:00:00")
        self.assertIsNone(reason)
        # The offset of the PARSED wall clock, not of the current one: comparing against
        # `now` goes red every summer in a DST zone.
        self.assertEqual(parsed.utcoffset(),
                         dt.datetime(2026, 11, 2, 11, 0).astimezone().utcoffset())
        self.assertEqual((parsed.hour, parsed.minute), (11, 0))

    def test_garbage_is_invalid_with_a_reason(self):
        parsed, reason = dd.parse_defer_date("next tuesday")
        self.assertIsNone(parsed, "garbage must never parse as an instant")
        self.assertIn("next tuesday", reason)

    def test_impossible_date_is_invalid(self):
        parsed, reason = dd.parse_defer_date("2026-02-31")
        self.assertIsNone(parsed)
        self.assertTrue(reason)


class TestDeferState(unittest.TestCase):
    def test_one_hour_ahead_is_deferred(self):
        self.assertEqual(dd.defer_state(LATER, NOW)[0], "deferred")

    def test_one_hour_behind_is_due(self):
        self.assertEqual(dd.defer_state("2026-11-02T09:00:00+01:00", NOW)[0], "due")

    def test_a_date_only_today_is_due(self):
        """The old day-granular rule: a defer naming today makes the row available."""
        self.assertEqual(dd.defer_state("2026-11-02", NOW)[0], "due")

    def test_no_defer_is_none(self):
        self.assertEqual(dd.defer_state("", NOW), ("none", None))

    def test_garbage_is_invalid_not_due(self):
        state, reason = dd.defer_state("soon", NOW)
        self.assertEqual(state, "invalid")
        self.assertTrue(reason)

    def test_now_accepts_a_date_and_a_tuple(self):
        self.assertEqual(dd.as_now(dt.date(2026, 11, 2)), dt.datetime(2026, 11, 2).astimezone())
        self.assertEqual(dd.as_now((2026, 11, 2)), dt.datetime(2026, 11, 2).astimezone())


# ---------------------------------------------------------------- the readers


def frontmatter_block(**fields):
    return "".join(f'{k}: "{v}"\n' for k, v in fields.items())


class TestOpenQueue(unittest.TestCase):
    """scripts/open-queue.py — `guards()` decides whether a row may open."""

    def setUp(self):
        self.oq = load("open-queue.py")

    def test_a_defer_later_today_does_not_open(self):
        block = frontmatter_block(defer_date=LATER)
        self.assertEqual("deferred", self.oq.guards(block, NOW.isoformat()))

    def test_a_date_only_defer_today_still_opens(self):
        block = frontmatter_block(defer_date="2026-11-02")
        self.assertIsNone(self.oq.guards(block, "2026-11-02"))


class TestFlaggedRows(unittest.TestCase):
    """scripts/flagged-rows.py — `eligible()` gates the flagged-row scan."""

    def setUp(self):
        self.fr = load("flagged-rows.py")

    def page(self, defer):
        return f'flag: true\nstatus: in_progress\ndefer_date: "{defer}"\n'

    def test_a_defer_later_today_is_not_eligible(self):
        self.assertEqual(
            "deferred", self.fr.eligible(self.page(LATER), "task", NOW.isoformat())
        )

    def test_a_date_only_defer_today_is_eligible(self):
        self.assertIsNone(self.fr.eligible(self.page("2026-11-02"), "task", "2026-11-02"))


class TestNextFocus(unittest.TestCase):
    """scripts/next-focus.py — `due_today()` ranks what the day surfaces."""

    def setUp(self):
        self.nf = load("next-focus.py")
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.tasks = pathlib.Path(self.dir.name)

    def task(self, name, defer):
        (self.tasks / f"{name}.md").write_text(
            f'---\nstatus: in_progress\nphase: execution\ndefer_date: "{defer}"\n---\n',
            encoding="utf-8",
        )

    def names(self, **kwargs):
        rows = self.nf.due_today(None, str(self.tasks), NOW.date(), **kwargs)
        return [row["name"] for row in rows or []]

    def test_a_defer_later_today_does_not_surface(self):
        self.task("Later", LATER)
        self.assertEqual([], self.names(now=NOW))

    def test_a_date_only_defer_today_still_surfaces(self):
        self.task("Today", "2026-11-02")
        self.assertEqual(["Today"], self.names(now=NOW))


class TestOrphanCandidates(unittest.TestCase):
    """scripts/orphan-candidates.py — `is_parked()` spares scheduled rows."""

    def setUp(self):
        self.oc = load("orphan-candidates.py")

    def test_a_defer_later_today_is_parked(self):
        block = frontmatter_block(defer_date=LATER)
        self.assertEqual("defer_date", self.oc.is_parked(block, NOW.isoformat()))

    def test_a_date_only_defer_today_is_due_not_parked(self):
        """A defer naming TODAY is due: `<=` now, so only a later instant parks it."""
        block = frontmatter_block(defer_date="2026-11-02")
        self.assertIsNone(self.oc.is_parked(block, "2026-11-02"))


class TestWorkerKeepalive(unittest.TestCase):
    """scripts/worker-keepalive.py — a snoozed task is not a dead worker."""

    def setUp(self):
        self.wk = load("worker-keepalive.py")
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.vault = pathlib.Path(self.tmp.name) / "vault"
        (self.vault / "25 Tasks").mkdir(parents=True)
        self.kp = self.wk._keepalive()
        self.state = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(self.state, ignore_errors=True))
        self._orig = (self.kp.session_verdict, self.kp.state_path)
        self.kp.state_path = lambda vault, subject, branch="goal": str(
            self.state / f"{branch}--{self.kp.base_slug(subject)}.json"
        )
        self.kp.session_verdict = lambda sid: "ABSENT"
        self.addCleanup(self._restore)

    def _restore(self):
        self.kp.session_verdict, self.kp.state_path = self._orig

    def run_it(self, defer):
        (self.vault / "25 Tasks" / "A Task.md").write_text(
            "---\nstatus: in_progress\nphase: execution\n"
            f'claude_session_id: "sid-1"\ndefer_date: "{defer}"\n---\n\n# Tasks\n\n- [ ] one\n',
            encoding="utf-8",
        )
        out = StringIO()
        with redirect_stdout(out):
            rc = self.wk.main(["--vault", str(self.vault), "--task", "A Task",
                               "--now", str(NOW.timestamp())])
        return rc, out.getvalue().splitlines()

    def test_a_defer_later_today_is_not_resumed(self):
        rc, out = self.run_it(LATER)
        self.assertEqual(0, rc)
        self.assertEqual("jump", out[0])
        self.assertIn("deferred", out[1])

    def test_a_date_only_defer_today_still_resumes(self):
        rc, out = self.run_it("2026-11-02")
        self.assertEqual("resume", out[0])


class TestManagerQuiet(unittest.TestCase):
    """scripts/manager-quiet.py — `classify()` is what stands a manager down."""

    def setUp(self):
        # Restored on teardown: a leaked env var and temp dir would follow every later
        # test in this process.
        state_dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, state_dir, True)
        previous = os.environ.get("MANAGER_LIVENESS_STATE_DIR")
        os.environ["MANAGER_LIVENESS_STATE_DIR"] = state_dir
        self.addCleanup(self._restore_state_dir, previous)
        self.mq = load("manager-quiet.py")

    @staticmethod
    def _restore_state_dir(previous):
        if previous is None:
            os.environ.pop("MANAGER_LIVENESS_STATE_DIR", None)
        else:
            os.environ["MANAGER_LIVENESS_STATE_DIR"] = previous

    def classify(self, defer, today):
        fm = f"---\nstatus: in_progress\nphase: execution\ndefer_date: {defer}\n---\n"
        return self.mq.classify(fm, "/nonexistent", None, self.mq._date_key(today))

    def test_a_defer_later_today_is_quiet(self):
        verdict = self.classify(LATER, NOW.isoformat())
        self.assertEqual("quiet", verdict["kind"])
        self.assertTrue(verdict["defer"].startswith("2026-11-02T11:00"))

    def test_a_date_only_defer_today_is_runnable(self):
        verdict = self.classify("2026-11-02", "2026-11-02")
        self.assertEqual("runnable", verdict["kind"])

    def test_a_bad_today_refuses_instead_of_reading_as_now(self):
        """`--today garbage` must exit 2, not silently become "now" — falling through
        would read future defers as quiet and stand the manager down."""
        err = StringIO()
        with redirect_stderr(err):
            code = self.mq.main(["--subject", "S", "--vault", "/nonexistent",
                                 "--today", "garbage"])
        self.assertEqual(2, code)
        self.assertIn("REFUSED", err.getvalue())

    def test_the_earliest_future_defer_is_reported(self):
        # Cross-offset on purpose: A sorts FIRST as a string but is the LATER instant
        # (23:30Z vs 23:15Z), so a plain string sort names the wrong trigger and this
        # case fails against it.
        seen = {"defer": {"A": "2026-11-02T23:30:00Z", "B": "2026-11-03T00:15:00+01:00"},
                "blocked": {}}
        self.assertEqual("2026-11-03T00:15:00+01:00", self.mq.reopen_trigger(seen))


class TestCloseWorker(unittest.TestCase):
    """scripts/close-worker.py — a future defer is an accepted end state."""

    def setUp(self):
        self.cw = load("close-worker.py")

    def test_a_defer_later_today_permits_the_close(self):
        task = ("/tmp/A Task.md",
                {"status": "in_progress", "defer_date": LATER}, False)
        self.assertIsNone(self.cw.task_verdict(task, NOW))

    def test_a_defer_already_passed_refuses_the_close(self):
        task = ("/tmp/A Task.md",
                {"status": "in_progress", "defer_date": "2026-11-02T09:00:00+01:00"}, False)
        self.assertIsNotNone(self.cw.task_verdict(task, NOW))


class TestNoReaderTruncatesOrDefaultsToADate(unittest.TestCase):
    """The two regressions a per-reader unit test cannot see.

    A reader can call the shared parser and still be wrong twice over:

      * **`[:10]` on the raw value** — the truncation this change removes.
      * **a date-only clock.** `defer_state(value, date.today().isoformat())` reads
        "today" as local midnight, so a task deferred to 00:17 is still parked at
        00:37 — the same defect, moved from the value to the clock. The default must
        be the current instant.

    Both are structural, so they are pinned structurally, like the repo's `check-*`
    guards.
    """

    READERS = ["open-queue.py", "flagged-rows.py", "next-focus.py",
               "orphan-candidates.py", "worker-keepalive.py",
               "manager-quiet.py", "close-worker.py"]

    def source(self, name):
        return (_SCRIPTS / name).read_text(encoding="utf-8")

    def test_no_reader_slices_a_defer_date(self):
        for name in self.READERS:
            with self.subTest(reader=name):
                self.assertNotRegex(
                    self.source(name), r"defer[^\n]*\[:10\]",
                    f"{name} still truncates a defer_date to ten characters",
                )

    def test_no_reader_defaults_its_clock_to_a_date(self):
        for name in self.READERS:
            with self.subTest(reader=name):
                src = self.source(name)
                for pattern in (r"date\.today\(\)", r"localtime\(\)\[:3\]"):
                    self.assertNotRegex(
                        src, pattern,
                        f"{name} defaults its defer clock to a date, not an instant",
                    )


if __name__ == "__main__":
    unittest.main()
