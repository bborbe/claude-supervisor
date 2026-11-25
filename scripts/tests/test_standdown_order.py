#!/usr/bin/env python3
"""Tests for scripts/standdown-order.py and the two readers that delegate to it.

The marker is honoured by ORDERING, not presence. `/supervisor:stop` writes
`stopped: true` + `stop_date`; a later `flag_set_at` is the operator's newest word
and reopens the row. Both readers — `open-queue.is_stopped()` and the keepalive's
resume decision — used to test truthiness alone, so a re-flagged row stayed shut.

Measured 2026-11-24 on `Classify the Heat-Pump Penetration Seal`: flag cleared
17:10, stopped 18:05:33, re-flagged 19:36:58.408271.

What this suite pins:

  * the ordering itself — a stop strictly newer than the flag skips, a flag newer
    than the stop opens, and the tie opens (a stop must be *newer* to win);
  * the flag instant is the raw `flag_set_at`, never `approval_instant()`'s
    `approved_at`-first fallback — a page carrying BOTH must order on the flag;
  * the provenance gate — an agent-set or unattributed flag never reopens a stop,
    because `flag_set_at` is written for those too and they open nothing here;
  * the no-date fallback — `stopped: true` with `stop_date` absent, or with an
    unparseable `flag_set_at`, keeps the presence-only skip, because absent
    evidence never reopens a retired row;
  * each reader's own call path, not just the shared module: `open-queue`'s
    `is_stopped(frontmatter(text))` and `worker-keepalive`'s `main()`.

Run: python3 -m unittest discover -s scripts/tests -v
"""

import importlib.util
import io
import os
import re
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timezone
from unittest import mock

_HERE = os.path.dirname(os.path.abspath(__file__))
_SCRIPTS_DIR = os.path.dirname(_HERE)


def _load(name, filename):
    spec = importlib.util.spec_from_file_location(name, os.path.join(_SCRIPTS_DIR, filename))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


order = _load("standdown_order", "standdown-order.py")
open_queue = _load("open_queue", "open-queue.py")
keepalive = _load("worker_keepalive", "worker-keepalive.py")

#: The measured 2026-11-24 pair. They differ ONLY in which instant is later.
STOPPED_AFTER_FLAG = ("2026-11-24T18:05:33+01:00", "2026-11-24T17:10:00+01:00")
FLAG_AFTER_STOP = ("2026-11-24T18:05:33+01:00", "2026-11-24T19:36:58.408271+01:00")


def page(stop_date=None, flag_set_at=None, stopped="true", approved_at=None,
         flag="true", flag_set_by="operator"):
    """A task page's frontmatter, shaped the way the vault writes it."""
    extra = f"stopped: {stopped}\n"
    if stop_date is not None:
        extra += f"stop_date: {stop_date}\n"
    if flag_set_at is not None:
        extra += f"flag_set_at: {flag_set_at}\n"
    if flag is not None:
        extra += f"flag: {flag}\n"
    if flag_set_by is not None:
        extra += f"flag_set_by: {flag_set_by}\n"
    if approved_at is not None:
        extra += f"approved_at: {approved_at}\n"
    return (
        "---\n"
        "status: in_progress\n"
        "phase: execution\n"
        "priority: 2\n"
        f"{extra}"
        "---\n"
        "Tags: [[Task]]\n"
    )


def text_scalar(block, key):
    """A frontmatter-text accessor — the shape both real readers take."""
    match = re.search(rf"^{re.escape(key)}:[ \t]*(.*)$", block, re.MULTILINE)
    return match.group(1).strip().strip("\"'") if match else ""


class ParseInstantTest(unittest.TestCase):
    def test_offset_and_z_and_naive_are_all_aware(self):
        for value in ("2026-11-24T18:05:33+01:00", "2026-11-24T17:05:33Z",
                      "2026-11-24T18:05:33"):
            parsed = order.parse_instant(value)
            self.assertIsNotNone(parsed, value)
            self.assertIsNotNone(parsed.tzinfo, value)

    def test_z_is_utc_and_orders_against_an_offset(self):
        self.assertEqual(order.parse_instant("2026-11-24T17:05:33Z"),
                         order.parse_instant("2026-11-24T18:05:33+01:00"))

    def test_unreadable_values_are_none_never_now(self):
        for value in ("", None, "   ", "not-a-date", "yesterday"):
            self.assertIsNone(order.parse_instant(value), repr(value))


class OrderingTest(unittest.TestCase):
    def verdict(self, stop_date=None, flag_set_at=None, stopped="true",
                flag="true", flag_set_by="operator"):
        return order.is_stopped(
            page(stop_date=stop_date, flag_set_at=flag_set_at, stopped=stopped,
                 flag=flag, flag_set_by=flag_set_by),
            text_scalar,
        )

    def test_stop_after_flag_skips(self):
        self.assertTrue(self.verdict(*STOPPED_AFTER_FLAG))

    def test_flag_after_stop_opens(self):
        self.assertFalse(self.verdict(*FLAG_AFTER_STOP))

    def test_tie_opens(self):
        same = "2026-11-24T18:05:33+01:00"
        self.assertFalse(self.verdict(same, same))

    def test_not_stopped_is_never_skipped(self):
        for stopped in ("false", "no", "on-but-not", "", None):
            self.assertFalse(self.verdict(stopped=stopped, flag_set_at="2026-11-24T19:00:00+01:00"))

    def test_no_stop_date_keeps_the_presence_skip(self):
        self.assertTrue(self.verdict(stop_date=None, flag_set_at="2026-11-24T19:00:00+01:00"))

    def test_unparseable_flag_keeps_the_presence_skip(self):
        self.assertTrue(self.verdict(stop_date="2026-11-24T18:05:33+01:00",
                                     flag_set_at="not-a-date"))

    def test_agent_set_flag_never_reopens_a_stop(self):
        """`flag_set_at` is written for an agent flag too — ordering alone would reopen."""
        self.assertTrue(self.verdict(flag_set_by="agent", *FLAG_AFTER_STOP))
        self.assertTrue(self.verdict(flag_set_by="manager", *FLAG_AFTER_STOP))

    def test_unattributed_flag_never_reopens_a_stop(self):
        self.assertTrue(self.verdict(flag_set_by=None, *FLAG_AFTER_STOP))

    def test_unflagged_row_never_reopens_a_stop(self):
        self.assertTrue(self.verdict(flag="false", *FLAG_AFTER_STOP))
        self.assertTrue(self.verdict(flag=None, *FLAG_AFTER_STOP))

    def test_legacy_operator_flag_reopens(self):
        self.assertFalse(self.verdict(flag_set_by="legacy", *FLAG_AFTER_STOP))

    def test_ordering_uses_the_raw_flag_instant_not_approved_at(self):
        """`approval_instant()` returns `approved_at` first — the wrong instant here."""
        self.assertFalse(order.is_stopped(
            page(stop_date="2026-11-24T18:05:33+01:00",
                 flag_set_at="2026-11-24T19:36:58.408271+01:00",
                 approved_at="2026-11-20T09:00:00+01:00"),
            text_scalar,
        ))


class OpenQueueReaderTest(unittest.TestCase):
    """SC1 — through the reader's own `is_stopped(frontmatter(text))` path."""

    def verdict(self, stop_date=None, flag_set_at=None, **kwargs):
        return open_queue.is_stopped(
            open_queue.frontmatter(page(stop_date=stop_date, flag_set_at=flag_set_at, **kwargs)))

    def test_both_orderings_disagree(self):
        self.assertTrue(self.verdict(*STOPPED_AFTER_FLAG))
        self.assertFalse(self.verdict(*FLAG_AFTER_STOP))

    def test_no_date_fallbacks(self):
        self.assertTrue(self.verdict(stop_date=None, flag_set_at="2026-11-24T19:00:00+01:00"))
        self.assertTrue(self.verdict(stop_date="2026-11-24T18:05:33+01:00",
                                     flag_set_at="not-a-date"))

    def test_unflagged_row_is_never_stopped(self):
        self.assertFalse(self.verdict(stopped="false", stop_date="2026-11-24T18:05:33+01:00",
                                      flag_set_at="2026-11-24T17:10:00+01:00"))


class KeepaliveReaderTest(unittest.TestCase):
    """SC2 — through `worker-keepalive.py`'s own resume decision (`main`)."""

    def run_keepalive(self, stop_date=None, flag_set_at=None, **kwargs):
        with tempfile.TemporaryDirectory() as vault, \
                tempfile.TemporaryDirectory() as state:
            tasks = os.path.join(vault, "25 Tasks")
            os.makedirs(tasks)
            with open(os.path.join(tasks, "Fixture Row.md"), "w", encoding="utf-8") as fh:
                fh.write(page(stop_date=stop_date, flag_set_at=flag_set_at, **kwargs))
            out = io.StringIO()
            with mock.patch.dict(os.environ, {"MANAGER_RESUMES_STATE_DIR": state}), \
                    redirect_stdout(out):
                keepalive.main(["--vault", vault, "--task", "Fixture Row",
                                "--dir", "25 Tasks", "--now", "1795000000"])
            return out.getvalue()

    STOP_LINE = "task is stopped by the operator; a stop is not a death"

    def test_stop_after_flag_is_skipped(self):
        self.assertIn(self.STOP_LINE, self.run_keepalive(*STOPPED_AFTER_FLAG))

    def test_flag_after_stop_falls_through(self):
        self.assertNotIn(self.STOP_LINE, self.run_keepalive(*FLAG_AFTER_STOP))

    def test_no_date_fallbacks_are_skipped(self):
        self.assertIn(self.STOP_LINE,
                      self.run_keepalive(stop_date=None,
                                         flag_set_at="2026-11-24T19:00:00+01:00"))
        self.assertIn(self.STOP_LINE,
                      self.run_keepalive(stop_date="2026-11-24T18:05:33+01:00",
                                         flag_set_at="not-a-date"))


_FIXTURES = os.path.join(_HERE, "fixtures", "standdown-order")


class FixturePairTest(unittest.TestCase):
    """SC3 — the two purpose-built pages, read from disk by each reader.

    They differ ONLY in which of `flag_set_at` / `stop_date` is later, and they are
    repo files rather than vault rows because `flag: true` + `flag_set_by: operator`
    is approval at every reader — a live row in that state would be opened by the
    running fleet.
    """

    def read(self, name):
        with open(os.path.join(_FIXTURES, name + ".md"), encoding="utf-8") as fh:
            return fh.read()

    def keepalive_output(self, name):
        out = io.StringIO()
        with tempfile.TemporaryDirectory() as state, \
                mock.patch.dict(os.environ, {"MANAGER_RESUMES_STATE_DIR": state}), \
                redirect_stdout(out):
            keepalive.main(["--vault", os.path.dirname(_FIXTURES), "--task", name,
                            "--dir", "standdown-order", "--now", "1795000000"])
        return out.getvalue()

    STOP_LINE = "task is stopped by the operator; a stop is not a death"

    def test_open_queue_skips_the_stop_and_opens_the_reflag(self):
        self.assertTrue(open_queue.is_stopped(
            open_queue.frontmatter(self.read("stopped-after-flag"))))
        self.assertFalse(open_queue.is_stopped(
            open_queue.frontmatter(self.read("flag-after-stop"))))

    def test_keepalive_skips_the_stop_and_falls_through_the_reflag(self):
        self.assertIn(self.STOP_LINE, self.keepalive_output("stopped-after-flag"))
        self.assertNotIn(self.STOP_LINE, self.keepalive_output("flag-after-stop"))

    def test_guards_report_stopped_only_for_the_stop_after_flag_row(self):
        """The reopen must reach the SELECTION surface, not only the predicate."""
        today = datetime(2026, 11, 25, 12, 0, tzinfo=timezone.utc)
        self.assertEqual(
            open_queue.guards(open_queue.frontmatter(self.read("stopped-after-flag")), today),
            "stopped")
        self.assertIsNone(
            open_queue.guards(open_queue.frontmatter(self.read("flag-after-stop")), today))


if __name__ == "__main__":
    unittest.main()
