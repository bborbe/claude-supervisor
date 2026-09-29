#!/usr/bin/env python3
"""Tests for scripts/orphan-candidates.py.

Covers the logic that decides what counts as an orphan candidate. Each case here
guards a defect the check has actually shipped:

  * frontmatter-only reading -- a whole-file scan matches keys quoted in task-body
    prose and read the task documenting the bug as a routine.
  * the park union -- a parked routine and an orphan are both genuinely dead, so
    the park signal is the only discriminator. Neither signal alone is sufficient.
  * no lower age bound -- the old >=4h cut excluded the recently-died orphans the
    check exists to find.

Run: python3 -m unittest discover -s scripts/tests -v
"""

import importlib.util
import os
import tempfile
import time
import unittest
from datetime import date, timedelta

_HERE = os.path.dirname(os.path.abspath(__file__))
_SCRIPT = os.path.join(os.path.dirname(_HERE), "orphan-candidates.py")

_spec = importlib.util.spec_from_file_location("orphan_candidates", _SCRIPT)
oc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(oc)


def task_file(directory, name, frontmatter, body="", age_hours=1.0):
    """Write a task file and backdate its mtime by `age_hours`."""
    path = os.path.join(directory, name)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(f"---\n{frontmatter}\n---\n{body}\n")
    stamp = time.time() - age_hours * 3600
    os.utime(path, (stamp, stamp))
    return path


class Frontmatter(unittest.TestCase):
    def test_extracts_only_the_frontmatter_block(self):
        text = "---\nstatus: in_progress\n---\n\nstatus: completed\n"
        self.assertEqual(oc.frontmatter(text), "status: in_progress")

    def test_body_prose_does_not_leak_in(self):
        """The bug this guards: a body line quoting a key read as a real claim."""
        text = (
            "---\nstatus: in_progress\n---\n\n"
            "Note: `created_by: recurring-task-creator` would miss a routine task.\n"
        )
        self.assertIsNone(oc.field(oc.frontmatter(text), "created_by"))

    def test_missing_frontmatter_is_empty(self):
        self.assertEqual(oc.frontmatter("# No frontmatter here\n"), "")

    def test_field_reads_and_unquotes(self):
        block = 'defer_date: "2026-09-19"\nstatus: in_progress'
        self.assertEqual(oc.field(block, "defer_date"), "2026-09-19")
        self.assertEqual(oc.field(block, "status"), "in_progress")

    def test_field_absent_is_none(self):
        self.assertIsNone(oc.field("status: in_progress", "defer_date"))


class ParkFilter(unittest.TestCase):
    """The union of two signals -- neither alone is sufficient."""

    TODAY = date(2026, 9, 18).isoformat()

    def test_future_defer_date_is_parked(self):
        block = 'defer_date: "2026-09-19"\nstatus: in_progress'
        self.assertEqual(oc.is_parked(block, self.TODAY), "defer_date")

    def test_same_day_defer_date_is_parked(self):
        block = 'defer_date: "2026-09-18"'
        self.assertEqual(oc.is_parked(block, self.TODAY), "defer_date")

    def test_past_defer_date_is_not_parked(self):
        block = 'defer_date: "2026-09-01"'
        self.assertIsNone(oc.is_parked(block, self.TODAY))

    def test_recurring_creator_is_parked_without_defer_date(self):
        """The Start Day family: recurring, but carries no defer_date."""
        block = "created_by: recurring-task-creator"
        self.assertEqual(oc.is_parked(block, self.TODAY), "created_by")

    def test_neither_signal_is_not_parked(self):
        """The real orphans: no defer_date, no created_by."""
        self.assertIsNone(oc.is_parked("status: in_progress", self.TODAY))

    def test_past_defer_date_with_recurring_creator_still_parked(self):
        block = 'defer_date: "2026-09-01"\ncreated_by: recurring-task-creator'
        self.assertEqual(oc.is_parked(block, self.TODAY), "created_by")


class Candidates(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = self._tmp.name
        self.addCleanup(self._tmp.cleanup)
        self.today = date.today().isoformat()

    def names(self, live=(), max_age_days=7):
        found = oc.candidates(self.dir, self.today, max_age_days, set(live))
        return [name for name, _, _ in found]

    def test_open_task_with_dead_session_is_a_candidate(self):
        task_file(self.dir, "Orphan.md", "status: in_progress\nclaude_session_id: 32d5e57c-aaaa", age_hours=3)
        self.assertEqual(self.names(live=[]), ["Orphan"])

    def test_no_lower_bound(self):
        """A 3h-old file is still a candidate -- the old >=4h cut dropped these."""
        task_file(self.dir, "Recent.md", "status: in_progress\nclaude_session_id: aaaa1111", age_hours=3)
        self.assertEqual(self.names(), ["Recent"])

    def test_upper_bound_excludes_backlog(self):
        task_file(self.dir, "Ancient.md", "status: in_progress\nclaude_session_id: bbbb2222", age_hours=24 * 10)
        self.assertEqual(self.names(), [])

    def test_live_session_is_not_a_candidate(self):
        task_file(self.dir, "Owned.md", "status: in_progress\nclaude_session_id: cccc3333-aaaa")
        self.assertEqual(self.names(live=["cccc3333"]), [])

    def test_non_open_status_is_skipped(self):
        task_file(self.dir, "Done.md", "status: completed\nclaude_session_id: dddd4444")
        self.assertEqual(self.names(), [])

    def test_parked_routines_are_skipped(self):
        task_file(self.dir, "Routine.md", "status: in_progress\nclaude_session_id: eeee5555\ncreated_by: recurring-task-creator")
        task_file(self.dir, "Scheduled.md", f'status: in_progress\nclaude_session_id: ffff6666\ndefer_date: "{self.today}"')
        self.assertEqual(self.names(), [])

    def test_missing_session_id_is_skipped(self):
        task_file(self.dir, "Unstamped.md", "status: in_progress")
        self.assertEqual(self.names(), [])

    def test_non_markdown_files_are_ignored(self):
        with open(os.path.join(self.dir, "notes.txt"), "w", encoding="utf-8") as handle:
            handle.write("status: in_progress\nclaude_session_id: 9999aaaa\n")
        self.assertEqual(self.names(), [])


class ResolveFleetSessions(unittest.TestCase):
    def test_explicit_path_wins(self):
        self.assertEqual(oc.resolve_fleet_sessions("/explicit/fleet-sessions.py"), "/explicit/fleet-sessions.py")

    def test_sibling_is_preferred(self):
        """The sibling ships in the same directory, so the two move together."""
        found = oc.resolve_fleet_sessions()
        self.assertIsNotNone(found, "fleet-sessions.py not resolvable from the sibling path")
        self.assertEqual(os.path.dirname(found), os.path.dirname(_SCRIPT))


if __name__ == "__main__":
    unittest.main()
