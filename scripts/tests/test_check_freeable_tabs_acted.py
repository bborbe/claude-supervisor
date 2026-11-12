"""The guard on a drive round's freeable tabs: green on a quiet round, red on the 2026-11-12 miss.

The load-bearing test is the deficit one. A check that only ever runs on a round that closed
everything is indistinguishable from one that can never fail, so the payload fixture is the
MEASURED round -- `2026-11-12T10-08-16Z`, one row under `FREEABLE TABS` carrying a resolved
pane-46 link, queued as a reap and never closed -- and the clean reading is the same payload
with a `closed` entry in the action record. One payload, two verdicts, differing only in
whether the record covers the row.

The second deficit test is the reason the denominator is scoped to rows with a resolved link:
a row rendering `no pane — <reason>` is a headless or unresolvable row, and counting it would
report a deficit on every quiet round -- which is how a guard stops being read.

The static half is tripped by a copy of the tree with exactly one clause removed each time,
because every one of those clauses is a sentence in a markdown file that a later edit can
delete without anything else noticing.
"""
import importlib.util
import io
import pathlib
import shutil
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout

SCRIPTS = pathlib.Path(__file__).resolve().parent.parent
CHECK = SCRIPTS / "check-freeable-tabs-acted.py"

_spec = importlib.util.spec_from_file_location("check_freeable_tabs_acted", CHECK)
check = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(check)

#: The measured round, trimmed to the blocks the parse reads. The row is verbatim from
#: `2026-11-12T10-08-16Z.drive`, including the em-dash-less `·` separators.
DEFECT_PAYLOAD = """\
fleet-drive — 17 candidates (296 retained from the ledger) · 3 finished (reaped) · 0 closed · headless=12

FREEABLE TABS (a slot the operator can free without a diagnosis)
  private-personal-c9 · Cleanup Email Inbox (Work) - 2026W46-thu · finished · no pane — live but no pane resolves
  Update All Service Repos Go Modules 2026-11 · Update All Service Repos' Go Modules for Go Mod Update 2026-11 · finished · http://127.0.0.1:1337/jump?pane=46&t=abc
  Start Day - 2026-11-12 · finished · no pane — not in this round's digest (session absent from the registry)

Waiting on your keystroke — re-read 2026-11-12T10:08:19Z
  1. **Answer** the question — Some Session · http://127.0.0.1:1337/jump?pane=99&t=zzz
"""

#: A quiet round: the block exists, nothing carries a resolved link.
QUIET_PAYLOAD = """\
fleet-drive — 4 candidates (303 retained from the ledger) · 0 finished (reaped) · 0 closed

FREEABLE TABS (a slot the operator can free without a diagnosis)
  Every Session Records Its State and What It Waits On · deferred 2026-11-12T09:00 · no pane — digest carried —

Waiting on your keystroke — re-read 2026-11-12T11:01:30Z
"""


def run(argv):
    """(exit code, stdout, stderr) for one invocation."""
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = check.main(argv)
    return code, out.getvalue(), err.getvalue()


class FreeableTabsArtifactTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write(self, name, text):
        path = pathlib.Path(self.tmp) / name
        path.write_text(text, encoding="utf-8")
        return str(path)

    def test_the_measured_round_fails_and_names_the_row(self):
        """The 2026-11-12 round listed a freeable row with a link and closed nothing."""
        drive = self._write("2026-11-12T10-08-16Z.drive", DEFECT_PAYLOAD)
        code, out, err = run(["--drive", drive])
        self.assertEqual(code, 1, out + err)
        self.assertIn("freeable-tabs: rows 3 · linkable 1 · acted 0 · missing 1", out)
        self.assertIn("Update All Service Repos Go Modules 2026-11", err)

    def test_the_same_payload_passes_once_the_record_carries_a_close(self):
        """The same payload, one `closed` entry: the gate discriminates on the record."""
        drive = self._write("2026-11-12T10-08-16Z.drive", DEFECT_PAYLOAD)
        self._write(
            "2026-11-12T10-08-16Z.sent",
            "closed\tUpdate All Service Repos Go Modules 2026-11\n"
            "closed\tprivate-personal-c9\n",
        )
        code, out, err = run(["--drive", drive])
        self.assertEqual(code, 0, out + err)
        self.assertIn("freeable-tabs: rows 3 · linkable 1 · acted 1 · missing 0", out)

    def test_a_quiet_round_is_clean(self):
        """No resolved link anywhere: `rows` still counts, `missing` is 0."""
        drive = self._write("2026-11-12T11-01-24Z.drive", QUIET_PAYLOAD)
        code, out, err = run(["--drive", drive])
        self.assertEqual(code, 0, out + err)
        self.assertIn("freeable-tabs: rows 1 · linkable 0 · acted 0 · missing 0", out)

    def test_a_long_wait_row_with_a_link_is_never_a_deficit(self):
        """`long-wait <age>` rows are excluded by contract: closing one destroys a live wait.

        A resolved link on such a row is a CORRECT non-close. Counting it would report a
        deficit on every round that has one, which is how a guard stops being read.
        """
        payload = QUIET_PAYLOAD.replace(
            "  Every Session Records Its State and What It Waits On · deferred 2026-11-12T09:00 · no pane — digest carried —",
            "  A Parked Worker Holding A Wait · long-wait 14h · http://127.0.0.1:1337/jump?pane=12&t=q",
        )
        drive = self._write("lw.drive", payload)
        code, out, err = run(["--drive", drive])
        self.assertEqual(code, 0, out + err)
        self.assertIn("freeable-tabs: rows 1 · linkable 0 · acted 0 · missing 0", out)

    def test_a_long_wait_row_with_a_task_column_is_still_excluded(self):
        """The payload renders an optional task column between the name and the class.

        Reading the class by position returns the TASK name on every such row, which silently
        disabled the long-wait exclusion it was meant to apply. The class is located by value.
        """
        payload = QUIET_PAYLOAD.replace(
            "  Every Session Records Its State and What It Waits On · deferred 2026-11-12T09:00 · no pane — digest carried —",
            "  A Parked Worker · The Task It Holds · long-wait 14h · http://127.0.0.1:1337/jump?pane=12&t=q",
        )
        drive = self._write("lw4.drive", payload)
        code, out, err = run(["--drive", drive])
        self.assertEqual(code, 0, out + err)
        self.assertIn("freeable-tabs: rows 1 · linkable 0 · acted 0 · missing 0", out)

    def test_a_finished_row_with_a_task_column_is_still_actionable(self):
        """The same 4-field shape, class `finished`: it must still be counted."""
        payload = QUIET_PAYLOAD.replace(
            "  Every Session Records Its State and What It Waits On · deferred 2026-11-12T09:00 · no pane — digest carried —",
            "  A Worker · Its Task · finished · http://127.0.0.1:1337/jump?pane=12&t=q",
        )
        drive = self._write("fin4.drive", payload)
        code, out, err = run(["--drive", drive])
        self.assertEqual(code, 1, out + err)
        self.assertIn("freeable-tabs: rows 1 · linkable 1 · acted 0 · missing 1", out)

    def test_a_skipped_entry_counts_as_acted(self):
        """A refusal token is a legitimate outcome — the row is left for the next round."""
        drive = self._write("2026-11-12T10-08-16Z.drive", DEFECT_PAYLOAD)
        self._write(
            "2026-11-12T10-08-16Z.sent",
            "skipped\tUpdate All Service Repos Go Modules 2026-11\n",
        )
        code, out, err = run(["--drive", drive])
        self.assertEqual(code, 0, out + err)
        self.assertIn("missing 0", out)

    def test_a_bare_sent_entry_is_not_an_act_on_a_freeable_row(self):
        """A plain name is a `sent` entry — messaging a freeable row is the defect itself."""
        drive = self._write("2026-11-12T10-08-16Z.drive", DEFECT_PAYLOAD)
        self._write(
            "2026-11-12T10-08-16Z.sent",
            "Update All Service Repos Go Modules 2026-11\n",
        )
        code, out, err = run(["--drive", drive])
        self.assertEqual(code, 1, out + err)
        self.assertIn("missing 1", out)

    def test_a_no_pane_row_is_never_a_deficit(self):
        """The denominator is resolved links only, so an unlinked row cannot read as a miss."""
        drive = self._write("2026-11-12T11-01-24Z.drive", QUIET_PAYLOAD)
        _, out, _ = run(["--drive", drive])
        self.assertIn("missing 0", out)
        self.assertIn("rows 1", out)

    def test_the_join_ignores_a_decoration_prefix(self):
        """A registry label carries `⚙ `; the record may not. An exact join reports a miss."""
        payload = QUIET_PAYLOAD.replace(
            "Every Session Records Its State and What It Waits On",
            "⚙ Every Session Records Its State and What It Waits On",
        ).replace("no pane — digest carried —", "http://127.0.0.1:1337/jump?pane=5&t=q")
        drive = self._write("r.drive", payload)
        self._write("r.sent", "closed\tEvery Session Records Its State and What It Waits On\n")
        code, out, err = run(["--drive", drive])
        self.assertEqual(code, 0, out + err)
        self.assertIn("missing 0", out)

    def test_an_unreadable_payload_is_refused_not_a_deficit(self):
        """A broken read says nothing about the rows, so it must not render as a miss."""
        code, out, err = run(["--drive", str(pathlib.Path(self.tmp) / "absent.drive")])
        self.assertEqual(code, 2, out + err)

    def test_sent_without_drive_is_refused(self):
        """`--sent` alone would fall through to the static run and exit 0 unmeasured."""
        code, _, err = run(["--sent", str(pathlib.Path(self.tmp) / "x.sent")])
        self.assertEqual(code, 2, err)

    def test_an_unknown_flag_is_refused(self):
        code, _, err = run(["--payload", "x"])
        self.assertEqual(code, 2, err)


class FreeableTabsStaticTest(unittest.TestCase):
    """Each clause is one sentence in a markdown file; removing it must fail the gate."""

    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        self._save(check.ROUND, check.OUTPUT_SHAPE)

    def tearDown(self):
        check.ROUND, check.OUTPUT_SHAPE = self._originals
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _save(self, *paths):
        self._originals = (check.ROUND, check.OUTPUT_SHAPE)
        for path in paths:
            dest = self.tmp / path.name
            dest.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
        self._copies = {path.name: self.tmp / path.name for path in paths}

    def _trip(self, path, needle):
        text = path.read_text(encoding="utf-8")
        self.assertIn(needle, text, "the clause under test is not in the file")
        path.write_text(text.replace(needle, ""), encoding="utf-8")

    def test_the_tree_passes_static(self):
        self.assertEqual(check.static_failures(), ([], False))

    def test_a_round_step_without_the_required_line_fails(self):
        round_copy = self._copies[check.ROUND.name]
        self._trip(round_copy, check.ROUND_LINE_MARKER)
        check.ROUND = round_copy
        self.assertTrue(check.static_failures()[0])

    def test_a_round_step_that_stops_running_the_check_fails(self):
        round_copy = self._copies[check.ROUND.name]
        self._trip(round_copy, check.CHECK_NAME)
        check.ROUND = round_copy
        self.assertTrue(check.static_failures()[0])

    def test_an_output_shape_without_the_line_fails(self):
        shape_copy = self._copies[check.OUTPUT_SHAPE.name]
        self._trip(shape_copy, check.OUTPUT_SHAPE_MARKER)
        check.OUTPUT_SHAPE = shape_copy
        self.assertTrue(check.static_failures()[0])


if __name__ == "__main__":
    unittest.main()
