#!/usr/bin/env python3
"""Tests for scripts/manager-coverage-gate.py.

The load-bearing properties, in the order the criteria grade them:

1. **A no-change delta dispatches 0 owners** (SC2) and a changed delta dispatches only the
   rows that moved (SC3) — the saving itself.
2. **The positive control.** Property 1 alone is satisfied by a build that always answers
   "nothing to dispatch", which would save the cost by never producing a coverage map at
   all. So the no-change assertion is paired with a changed-delta assertion that the gate
   DOES emit rows: positive without negative is a probe that cannot fail.
3. **The count is what is asserted, never a clause.** These criteria exist because the
   earlier evidence for them was a presence check on the step's markdown, which passes while
   the step still dispatches every tick. Every assertion here counts stdout lines — the
   caller's own dispatch list — so a gate that returns rows on a no-change delta fails.
4. Fail-open: an unrecognised delta, a CHANGE with no names, and a name the tracked set
   cannot place all dispatch every owner rather than none.
5. stdout carries the dispatch list and nothing else — the no-change marker is on stderr,
   because every caller counts stdout lines.

Run: python3 -m unittest discover -s scripts/tests -v
"""

import os
import subprocess
import sys
import tempfile
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_SCRIPT = os.path.join(os.path.dirname(_HERE), "manager-coverage-gate.py")

TASKS = [
    "/vault/25 Tasks/Alpha Row.md",
    "/vault/25 Tasks/Beta Row.md",
    "/vault/25 Tasks/Gamma Row.md",
]
SUBJECT = "/vault/24 Goals/Subject Page.md"


def run(*args):
    """Run the gate; return (returncode, stdout lines, stderr)."""
    proc = subprocess.run(
        [sys.executable, _SCRIPT, *args],
        capture_output=True,
        text=True,
        check=False,
    )
    return proc.returncode, [ln for ln in proc.stdout.splitlines() if ln.strip()], proc.stderr


class CoverageGateTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tracked = os.path.join(self._tmp.name, "tracked.txt")
        with open(self.tracked, "w", encoding="utf-8") as handle:
            handle.write("# the round's closure-box owners\n")
            handle.write("\n")
            for owner in TASKS + [SUBJECT]:
                handle.write(owner + "\n")

    # --- 1 + 2: the saving, with its positive control -------------------------------

    def test_no_change_dispatches_nothing(self):
        """SC2: a no-change delta emits 0 owners and exits 0."""
        rc, rows, _ = run("--tracked", self.tracked, "--delta", "no change")
        self.assertEqual(rows, [], "a no-change tick must dispatch no owner")
        self.assertEqual(rc, 0)

    def test_no_change_marker_is_on_stderr_not_stdout(self):
        """The line-count contract: a caller counts stdout, so the marker cannot live there."""
        rc, rows, err = run("--tracked", self.tracked, "--delta", "no-change")
        self.assertEqual(rows, [])
        self.assertIn("0 coverage dispatches", err)

    def test_the_rendered_no_change_line_dispatches_nothing(self):
        """The sweep reader's own rendering, passed verbatim as the step instructs.

        Regression: the gate accepted only bare tokens, so the rendered `Delta: no change`
        line failed open and dispatched every owner — the saving gone, silently, on exactly
        the tick this feature exists to make free. Measured 2026-10-16 on a live round.
        """
        rendered = (
            "Delta: no change — all 14 rows' status/phase/met/progress_hash match the "
            "frozen snapshot (recorded_at 2026-10-16T10:47:25+02:00)"
        )
        rc, rows, _ = run("--tracked", self.tracked, "--delta", rendered)
        self.assertEqual(rows, [], "the rendered no-change line must dispatch no owner")
        self.assertEqual(rc, 0)

    def test_the_rendered_change_token_selects_the_changed_rows(self):
        """The same rendering shape on a change tick, so the fix is not a no-change-only branch."""
        rc, rows, _ = run(
            "--tracked", self.tracked,
            "--delta", "Delta: change — 3 rows moved",
            "--changed", "Alpha Row",
        )
        self.assertEqual(rows, [TASKS[0]])
        self.assertEqual(rc, 10)

    def test_a_prose_change_rendering_fails_open_rather_than_narrow(self):
        """⚠️ The boundary this fix deliberately does not cross, pinned so it stays visible.

        The sweep reader renders a *change* as prose — `Delta: <what moved>` — never as a
        token, and it renders `uncomputable` in the same shape. Mapping arbitrary prose to
        CHANGE is therefore a semantics decision, not a normalisation: a mis-read would
        narrow the dispatch to a subset while the gate believed it had read the delta. The
        gate keeps failing open there, so the caller passes the token or pays for every
        owner. Only the no-change rendering is unambiguous enough to decide here.
        """
        rc, rows, _ = run(
            "--tracked", self.tracked,
            "--delta", "Delta: 3 rows moved",
            "--changed", "Alpha Row",
        )
        self.assertEqual(rows, TASKS + [SUBJECT])

    def test_a_change_prose_mentioning_no_change_is_not_a_no_change_verdict(self):
        """⚠️ The unsafe direction, pinned.

        Failing open over-dispatches; reading a change as NO CHANGE skips coverage. So the
        comparison is the leading clause only — prose that says "no change" later in the
        sentence must not collapse the dispatch to nothing.
        """
        rc, rows, _ = run(
            "--tracked", self.tracked,
            "--delta", "Delta: 3 rows moved — no change to the remaining 11",
            "--changed", "Alpha Row",
        )
        self.assertEqual(
            rows, TASKS + [SUBJECT],
            "a change whose prose mentions 'no change' must fail open, never read as no-change",
        )

    def test_an_uncomputable_rendering_still_fails_open(self):
        """The sweep reader's own no-baseline rendering keeps the fail-open contract."""
        rc, rows, _ = run(
            "--tracked", self.tracked,
            "--delta", "uncomputable: no prior snapshot was passed this run",
        )
        self.assertEqual(rows, TASKS + [SUBJECT])

    def test_changed_delta_dispatches_only_the_changed_rows(self):
        """SC3, and the positive control for test 1: the gate CAN emit rows."""
        rc, rows, _ = run(
            "--tracked", self.tracked,
            "--delta", "change",
            "--changed", "Alpha Row, Gamma Row",
        )
        self.assertEqual(rows, [TASKS[0], TASKS[2]])
        self.assertEqual(rc, 10)

    def test_changed_names_match_by_basename_without_suffix(self):
        """The sweep reports names; the tracked set holds paths. The join is the basename."""
        rc, rows, _ = run("--tracked", self.tracked, "--delta", "change", "--changed", "Beta Row")
        self.assertEqual(rows, [TASKS[1]])

    def test_a_long_multi_word_name_resolves_as_one_name(self):
        """⚠️ Regression: this suite caught a whitespace split on its first run.

        Every real task name contains spaces — the one this gate was written for is *"The
        Manager Loop's Coverage Step Dispatches One Subagent per Tracked File Every Tick"* —
        so splitting `--changed` on whitespace shreds it, nothing resolves, and the gate fails
        open on **every** call. The saving disappears while every fail-open assertion in this
        file still passes, which is why the separator is pinned here rather than assumed.
        """
        long_name = (
            "The Manager Loop's Coverage Step Dispatches One Subagent per Tracked File Every Tick"
        )
        owner = f"/vault/25 Tasks/{long_name}.md"
        tracked = os.path.join(self._tmp.name, "long.txt")
        with open(tracked, "w", encoding="utf-8") as handle:
            handle.write(owner + "\n")
            handle.write(SUBJECT + "\n")
        rc, rows, _ = run("--tracked", tracked, "--delta", "change", "--changed", long_name)
        self.assertEqual(rows, [owner], "a multi-word name is one name, not many")
        self.assertEqual(rc, 10)

    def test_changed_names_match_a_full_path_too(self):
        rc, rows, _ = run(
            "--tracked", self.tracked, "--delta", "change", "--changed", TASKS[0],
        )
        self.assertEqual(rows, [TASKS[0]])

    def test_the_coverage_step_invokes_the_gate(self):
        """SC2's second clause: a correct gate the step never calls saves nothing.

        The counted assertion above is primary — it is what makes the criterion fail when the
        *behaviour* is wrong. This clause is what makes it fail when the gate is written and
        left unwired, which the counted assertion alone cannot see: an unwired gate passes
        every test in this file while step 4b still dispatches on every tick.
        """
        step = os.path.normpath(
            os.path.join(_HERE, "..", "..", "commands", "steps", "goal-coverage.md")
        )
        with open(step, encoding="utf-8") as handle:
            body = handle.read()
        self.assertIn(
            "manager-coverage-gate.py", body,
            "step 4b must name the gate it is gated on",
        )
        self.assertIn("--delta", body, "the gate is useless without its delta input")

    # --- the subject page -----------------------------------------------------------

    def test_subject_page_is_emitted_on_a_change_even_when_unnamed(self):
        """Its boxes are the aggregate view, and the sweep names the task that moved."""
        rc, rows, _ = run(
            "--tracked", self.tracked,
            "--delta", "change",
            "--changed", "Alpha Row",
            "--subject", SUBJECT,
        )
        self.assertEqual(rows, [TASKS[0], SUBJECT])

    def test_subject_page_is_not_emitted_on_no_change(self):
        rc, rows, _ = run(
            "--tracked", self.tracked,
            "--delta", "no-change",
            "--subject", SUBJECT,
        )
        self.assertEqual(rows, [])

    # --- 4: fail-open ---------------------------------------------------------------

    def test_unrecognised_delta_fails_open_to_every_owner(self):
        rc, rows, _ = run("--tracked", self.tracked, "--delta", "uncomputable")
        self.assertEqual(rows, TASKS + [SUBJECT])
        self.assertEqual(rc, 10)

    def test_absent_delta_fails_open_to_every_owner(self):
        rc, rows, _ = run("--tracked", self.tracked)
        self.assertEqual(rows, TASKS + [SUBJECT])

    def test_change_with_no_names_fails_open_to_every_owner(self):
        rc, rows, _ = run("--tracked", self.tracked, "--delta", "change")
        self.assertEqual(rows, TASKS + [SUBJECT])

    def test_unresolvable_name_fails_open_whole_set(self):
        """A name the tracked set cannot place puts the whole set's shape in doubt."""
        rc, rows, _ = run(
            "--tracked", self.tracked,
            "--delta", "change",
            "--changed", "Alpha Row, No Such Row",
        )
        self.assertEqual(
            rows, TASKS + [SUBJECT],
            "an unresolvable name must not narrow the dispatch to the names that resolved",
        )

    # --- 5 + usage ------------------------------------------------------------------

    def test_missing_tracked_file_is_a_usage_error(self):
        rc, rows, err = run("--tracked", "/nope/missing.txt", "--delta", "change")
        self.assertEqual(rc, 2)
        self.assertEqual(rows, [])
        self.assertIn("cannot read", err)

    def test_tracked_file_with_no_owners_is_a_usage_error(self):
        empty = os.path.join(self._tmp.name, "empty.txt")
        with open(empty, "w", encoding="utf-8") as handle:
            handle.write("# only a comment\n\n")
        rc, rows, err = run("--tracked", empty, "--delta", "change")
        self.assertEqual(rc, 2)
        self.assertIn("lists no owners", err)

    def test_comments_and_blank_lines_are_skipped(self):
        rc, rows, _ = run("--tracked", self.tracked, "--delta", "uncomputable")
        self.assertNotIn("", rows)
        self.assertFalse([r for r in rows if r.startswith("#")])


if __name__ == "__main__":
    unittest.main()
