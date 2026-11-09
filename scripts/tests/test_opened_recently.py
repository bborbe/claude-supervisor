"""opened-recently.py: the batch's per-run memory, read from the spawn ledger.

The defect this answers, measured 2026-11-07: two `/supervisor:open --flagged` commands
minutes apart re-opened the same 21 rows, because the batch carried no record of what it
had just opened. The ledger already holds that record, so the fix is a read — never a
second store.

Three cases are load-bearing, and each is a way this script could be plausibly wrong:

- `test_an_unreadable_ledger_is_exit_2` — the fold that matters. `read_ledger()` returns
  `None` for "could not read" and `{}` for "nobody has ever spawned"; collapsing the two
  turns a failed read into a confident "not opened", which *authorises a duplicate spawn*.
  Exit 2 is the only honest answer there.
- `test_the_glyph_and_the_suffix_name_the_same_subject` — a tab spawn writes `⚙ <subject>`
  and `uniqueTabName` suffixes `(2)` on a collision, so a raw equality test would never
  match a real row and the dedupe would silently do nothing.
- `test_a_resume_row_counts_as_an_open` — a resume is still the batch opening that row;
  excluding it would re-open every resumed row on the next run.
"""
import datetime
import pathlib
import subprocess
import sys
import unittest

import endpoint_fixture  # noqa: E402
from endpoint_fixture import FixtureEndpoint, dead_url, ledger_row  # noqa: E402

SCRIPT = pathlib.Path(__file__).resolve().parent.parent / "opened-recently.py"

#: A fixed "now", so the window assertions never depend on the wall clock.
NOW = "2026-11-07T12:00:00Z"

#: Distinctive, because one case asserts the row's id is NAMED on stdout.
SID = "cccccccc-3333-4333-8333-cccccccccccc"


def at(minutes_ago):
    return (
        datetime.datetime.fromisoformat(NOW.replace("Z", "+00:00"))
        - datetime.timedelta(minutes=minutes_ago)
    ).isoformat()


def run(endpoint, subject, minutes=60):
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--endpoint", endpoint,
         "--subject", subject, "--minutes", str(minutes), "--now", NOW],
        capture_output=True, text=True, check=False,
    )


class OpenedRecentlyTest(unittest.TestCase):
    def test_opened_inside_the_window_is_found(self):
        with FixtureEndpoint(ledger_rows=[
            ledger_row(SID, label="a task", spawned_at=at(10)),
        ]) as fixture:
            result = run(fixture.url, "a task")
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn(SID, result.stdout)

    def test_row_outside_the_window_is_not_found(self):
        with FixtureEndpoint(ledger_rows=[
            ledger_row(SID, label="a task", spawned_at=at(120)),
        ]) as fixture:
            result = run(fixture.url, "a task", minutes=60)
        self.assertEqual(1, result.returncode, result.stderr)
        self.assertEqual("", result.stdout.strip())

    def test_a_different_subject_is_not_found(self):
        with FixtureEndpoint(ledger_rows=[
            ledger_row(SID, label="another task", spawned_at=at(5)),
        ]) as fixture:
            result = run(fixture.url, "a task")
        self.assertEqual(1, result.returncode, result.stderr)

    def test_the_glyph_and_the_suffix_name_the_same_subject(self):
        with FixtureEndpoint(ledger_rows=[
            ledger_row(SID, label="⚙ a task (2)", spawned_at=at(5)),
        ]) as fixture:
            result = run(fixture.url, "a task")
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn(SID, result.stdout)

    def test_a_resume_row_counts_as_an_open(self):
        with FixtureEndpoint(ledger_rows=[
            ledger_row(SID, label="a task", spawned_at=at(5),
                       resumed_from="dddddddd-4444-4444-8444-dddddddddddd"),
        ]) as fixture:
            result = run(fixture.url, "a task")
        self.assertEqual(0, result.returncode, result.stderr)

    def test_an_unreadable_ledger_is_exit_2(self):
        """Never 1 — an unreadable ledger is not an empty one."""
        result = run(dead_url(), "a task")
        self.assertEqual(2, result.returncode, result.stdout + result.stderr)

    def test_an_empty_ledger_is_not_opened_not_unreadable(self):
        """`{}` is a fact: nobody has spawned here, so the row was not opened."""
        with FixtureEndpoint(ledger_rows=[]) as fixture:
            result = run(fixture.url, "a task")
        self.assertEqual(1, result.returncode, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
