#!/usr/bin/env python3
"""`scripts/fleet-legs.py` and the three shapes it is the single home for.

Rows 5b (keep alive) and 5c (flagged open) restart a crashed manager and open a page the
operator flagged, and both print a count. Until 2026-11-06 that count reached the round's
**tool output** and nothing else: the digest is written at router row 2, before either leg
runs, and `commands/steps/output-shape.md` had no slot for them. Measured on
`~/.claude/state/fleet-handback/2026-11-06T19-10Z.digest` — a real 69-line round over 24
sessions — `grep -c 'unactioned:'` returned **0**.

`test_a_digest_without_the_legs_section_fails_check` is the case this file exists for: it is
the pre-fix digest shape, and it must fail rather than silently omit three readings. The
neighbouring cases pin the two properties that make the lines honest — a not-run leg never
prints as a zero, and the dead-worker count never prints as a bare measurement.

Run: python3 -m unittest discover -s scripts/tests -v
"""

import contextlib
import importlib.util
import io
import os
import tempfile
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(os.path.dirname(_HERE))
_SCRIPT = os.path.join(_ROOT, "scripts", "fleet-legs.py")

_spec = importlib.util.spec_from_file_location("fleet_legs", _SCRIPT)
legs = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(legs)

SHAPE = os.path.join(_ROOT, "commands", "steps", "output-shape.md")
KEEPALIVE = os.path.join(_ROOT, "commands", "steps", "keepalive.md")
FLAGGED = os.path.join(_ROOT, "commands", "steps", "flagged-open.md")
READER = os.path.join(_ROOT, "agents", "fleet-sweep-reader.md")

#: The pre-fix digest, byte-shaped like the real one: header, classification, rows, NOTES —
#: and no LEGS section, which is exactly what the round wrote before this change.
PREFIX_DIGEST = """\
DIGEST 2026-11-06T19:10Z · 24 sessions · snapshot written: 2026-11-06T19:15:03Z
CLASSIFICATION  24 classified · progressing=8 · deferred=3 · parked=3 · lapsed=1
  Development Pipeline Throughput Manager [eab58671] · shell · 6m · lapsed · page.md · 8 · —
LEDGER (open)
  (none)
NOTES   none
"""

SEEDED = """\
LEGS
  keep-alive: not run — no reading recorded this round
  flagged-open: not run — no reading recorded this round
"""


def digest_with_legs():
    return PREFIX_DIGEST + SEEDED


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="fleet-legs-test-")
        self.path = os.path.join(self.dir, "2026-11-06T19-10Z.digest")

    def write(self, text):
        with open(self.path, "w", encoding="utf-8") as fh:
            fh.write(text)

    def read(self):
        with open(self.path, encoding="utf-8") as fh:
            return fh.read()

    def run_legs(self, *argv):
        return legs.main(["--digest", self.path] + list(argv))

    def record_all(self, *, lapsed=0, topicless=0, dead=0, flagged=0):
        """Record a reading for all four keys, the shape a completed round writes."""
        self.run_legs("--leg", "lapsed-managers", "--count", str(lapsed))
        self.run_legs("--leg", "topicless-goal-managers", "--count", str(topicless))
        self.run_legs("--leg", "dead-workers", "--count", str(dead), "--exact")
        self.run_legs("--leg", "operator-flagged", "--count", str(flagged), "--not-invoked")


class TestCheck(Base):
    def test_a_digest_without_the_legs_section_fails_check(self):
        """The pre-fix shape: the round ran and the digest carries none of the three."""
        self.write(PREFIX_DIGEST)
        self.assertEqual(self.run_legs("--check"), legs.EXIT_INCOMPLETE)

    def test_a_seeded_section_is_not_a_recorded_reading(self):
        """The shape exists, but no leg has spoken — a render must not call that complete."""
        self.write(digest_with_legs())
        self.assertEqual(self.run_legs("--check"), legs.EXIT_INCOMPLETE)

    def test_a_leg_recorded_not_run_passes_check(self):
        """A leg that genuinely did not run writes its own reason, which reads as recorded."""
        self.write(digest_with_legs())
        self.run_legs("--leg", "keep-alive", "--not-run", "debt gate (re-arm, exit 12)")
        self.run_legs("--leg", "flagged-open", "--not-run", "debt gate (exit 12)")
        self.assertEqual(self.run_legs("--check"), 0)

    def test_a_missing_key_fails_check(self):
        self.write(
            PREFIX_DIGEST
            + "LEGS\n"
            + "  keep-alive: unactioned: 0 lapsed-managers\n"
            + "  keep-alive: unactioned: 0 dead-workers\n"
        )
        self.assertEqual(self.run_legs("--check"), legs.EXIT_INCOMPLETE)

    def test_a_duplicated_key_fails_check(self):
        self.write(
            PREFIX_DIGEST
            + "LEGS\n"
            + "  keep-alive: unactioned: 0 lapsed-managers\n"
            + "  keep-alive: unactioned: 1 lapsed-managers\n"
            + "  keep-alive: unactioned: 0 dead-workers\n"
            + "  flagged-open: unactioned: 0 operator-flagged (batch not invoked)\n"
        )
        self.assertEqual(self.run_legs("--check"), legs.EXIT_INCOMPLETE)

    def test_a_key_under_the_wrong_leg_fails_check(self):
        self.write(
            PREFIX_DIGEST
            + "LEGS\n"
            + "  keep-alive: unactioned: 0 lapsed-managers\n"
            + "  keep-alive: unactioned: 0 dead-workers\n"
            + "  keep-alive: unactioned: 0 operator-flagged (batch not invoked)\n"
        )
        self.assertEqual(self.run_legs("--check"), legs.EXIT_INCOMPLETE)

    def test_an_unreadable_digest_is_not_a_clean_read(self):
        self.assertEqual(
            legs.main(["--digest", "/nope/x.digest", "--check"]),
            legs.EXIT_FAILED,
        )


class TestCount(Base):
    def test_count_replaces_the_not_run_seed(self):
        self.write(digest_with_legs())
        self.assertEqual(self.run_legs("--leg", "lapsed-managers", "--count", "2"), 0)
        text = self.read()
        self.assertIn("keep-alive: unactioned: 2 lapsed-managers", text)
        # The leg's OTHER key keeps its not-run reading — one leg, two counts.
        self.assertIn(
            "keep-alive: not run — no reading recorded this round · topicless-goal-managers, dead-workers",
            text,
        )
        self.assertIn("flagged-open: not run — no reading recorded this round", text)
        # The other keep-alive keys and operator-flagged are still seeded, so the shape is not complete.
        self.assertEqual(self.run_legs("--check"), legs.EXIT_INCOMPLETE)
        self.record_all(lapsed=2)
        self.assertEqual(self.run_legs("--check"), 0)

    def test_all_four_counts_render_and_check_clean(self):
        self.write(digest_with_legs())
        self.assertEqual(self.run_legs("--leg", "lapsed-managers", "--count", "0"), 0)
        self.assertEqual(self.run_legs("--leg", "topicless-goal-managers", "--count", "1"), 0)
        self.assertEqual(self.run_legs("--leg", "dead-workers", "--count", "0", "--exact"), 0)
        self.assertEqual(self.run_legs("--leg", "operator-flagged", "--count", "3", "--not-invoked"), 0)
        text = self.read()
        self.assertEqual(text.count("unactioned:"), 4)
        self.assertIn("keep-alive: unactioned: 0 lapsed-managers", text)
        self.assertIn("keep-alive: unactioned: 1 topicless-goal-managers", text)
        self.assertIn("keep-alive: unactioned: 0 dead-workers", text)
        self.assertIn("flagged-open: unactioned: 3 operator-flagged (batch not invoked)", text)
        self.assertEqual(self.run_legs("--check"), 0)

    def test_the_floor_marker_renders_on_the_dead_worker_line(self):
        self.write(digest_with_legs())
        self.assertEqual(
            self.run_legs("--leg", "dead-workers", "--count", "7", "--floor"), 0
        )
        self.assertIn(
            "keep-alive: unactioned: 7 dead-workers (lower bound, scan incomplete)",
            self.read(),
        )

    def test_the_floor_marker_is_refused_on_a_count_that_is_not_a_floor(self):
        self.write(digest_with_legs())
        self.assertEqual(
            self.run_legs("--leg", "lapsed-managers", "--count", "1", "--floor"),
            legs.EXIT_FAILED,
        )
        self.assertIn("no reading recorded this round", self.read())

    def test_an_unlabelled_dead_worker_count_is_refused(self):
        """A bare number reads as a measurement; the author must say which one it is."""
        self.write(digest_with_legs())
        self.assertEqual(
            self.run_legs("--leg", "dead-workers", "--count", "7"), legs.EXIT_FAILED
        )
        self.assertIn("no reading recorded this round", self.read())

    def test_a_count_needs_one_key_not_a_leg(self):
        self.write(digest_with_legs())
        self.assertEqual(
            self.run_legs("--leg", "keep-alive", "--count", "1"), legs.EXIT_FAILED
        )


class TestNotRun(Base):
    def test_not_run_collapses_the_leg_and_names_its_keys(self):
        self.write(digest_with_legs())
        self.assertEqual(
            self.run_legs("--leg", "keep-alive", "--not-run", "debt gate (re-arm, exit 12)"),
            0,
        )
        text = self.read()
        self.assertIn("keep-alive: not run — debt gate (re-arm, exit 12)", text)
        self.assertNotIn("unactioned:", text)
        self.run_legs("--leg", "flagged-open", "--not-run", "debt gate (exit 12)")
        self.assertEqual(self.run_legs("--check"), 0)

    def test_a_partial_not_run_reading_names_the_keys_it_still_covers(self):
        self.write(digest_with_legs())
        self.run_legs("--leg", "lapsed-managers", "--count", "2")
        self.assertIn(
            "keep-alive: not run — no reading recorded this round · topicless-goal-managers, dead-workers",
            self.read(),
        )

    def test_a_not_run_leg_and_a_zero_leg_never_render_the_same(self):
        """The whole point: suppressed and empty must be distinguishable."""
        self.write(digest_with_legs())
        self.run_legs("--leg", "keep-alive", "--not-run", "debt gate (re-arm, exit 12)")
        self.run_legs("--leg", "operator-flagged", "--count", "0", "--not-invoked")
        suppressed = self.read()

        self.write(digest_with_legs())
        self.record_all()
        empty = self.read()

        self.assertNotEqual(suppressed, empty)
        self.assertIn("keep-alive: not run — debt gate (re-arm, exit 12)", suppressed)
        self.assertNotIn("not run —", empty)
        self.assertEqual(empty.count("unactioned:"), 4)

    def test_a_count_after_a_not_run_clears_the_not_run_reading(self):
        """A leg that ran on a later round must not keep its earlier skip."""
        self.write(digest_with_legs())
        self.run_legs("--leg", "keep-alive", "--not-run", "debt gate")
        self.assertEqual(self.run_legs("--leg", "lapsed-managers", "--count", "4"), 0)
        text = self.read()
        self.assertIn("keep-alive: unactioned: 4 lapsed-managers", text)
        self.assertIn("keep-alive: not run — debt gate · topicless-goal-managers, dead-workers", text)
        self.run_legs("--leg", "operator-flagged", "--not-run", "debt gate (exit 12)")
        self.assertEqual(self.run_legs("--check"), 0)


class TestRefusals(Base):
    """The tool refuses the writes that would leave the digest unreadable.

    Argument validation goes through argparse, which exits 2 — the same code a failed
    read/write returns, and distinct from `--check`'s 1.
    """

    def refused(self, *argv):
        with self.assertRaises(SystemExit) as ctx:
            self.run_legs(*argv)
        self.assertEqual(ctx.exception.code, 2)

    def test_a_negative_count_is_refused(self):
        self.write(digest_with_legs())
        self.refused("--leg", "lapsed-managers", "--count", "-5")
        self.assertIn("no reading recorded this round", self.read())

    def test_an_empty_not_run_reason_is_refused(self):
        self.write(digest_with_legs())
        self.refused("--leg", "keep-alive", "--not-run", "   ")
        self.assertIn("no reading recorded this round", self.read())

    def test_a_reason_containing_the_key_separator_is_refused(self):
        self.write(digest_with_legs())
        self.refused("--leg", "keep-alive", "--not-run", "a · b")

    def test_check_refuses_to_run_with_a_write_flag(self):
        self.write(digest_with_legs())
        self.refused("--check", "--leg", "lapsed-managers", "--count", "1")


class TestDigestPreservation(Base):
    def test_a_pre_fix_digest_gains_a_complete_section_before_notes(self):
        """The header AND its lines must land together, or the next --check reads an empty body."""
        self.write(PREFIX_DIGEST)  # no LEGS section, NOTES present
        self.run_legs("--leg", "lapsed-managers", "--count", "1")
        text = self.read()
        self.assertLess(text.index("LEGS"), text.index("NOTES"))
        self.assertLess(text.index("keep-alive: unactioned: 1 lapsed-managers"), text.index("NOTES"))
        self.record_all(lapsed=1)
        self.assertEqual(self.run_legs("--check"), 0)

    def test_the_write_preserves_the_digest_mtime(self):
        """`fleet-digest.py` ages some rounds by this file's mtime — a leg must not reset it."""
        self.write(digest_with_legs())
        os.utime(self.path, (1700000000, 1700000000))
        self.run_legs("--leg", "lapsed-managers", "--count", "1")
        self.assertEqual(int(os.stat(self.path).st_mtime), 1700000000)
    def test_the_rest_of_the_digest_is_untouched(self):
        self.write(digest_with_legs())
        self.run_legs("--leg", "operator-flagged", "--count", "1", "--not-invoked")
        text = self.read()
        self.assertTrue(text.startswith("DIGEST 2026-11-06T19:10Z"))
        self.assertIn("CLASSIFICATION  24 classified", text)
        self.assertIn("NOTES   none", text)


class TestPerKind(Base):
    """The flagged-open reading, per kind.

    A single `unactioned: <n>` cannot say whether the operator's flag reached tasks, goals or
    topics: a round that opened three topics and a round that opened nothing printed the same
    line, so the flag could be silently unhonoured and the digest still read clean. The
    per-kind shape carries `opened/declined/eligible` for each kind, which makes
    `opened + declined == eligible` checkable rather than asserted once for all three.
    """

    GOOD = "tasks=1/2/3 goals=0/1/1 topics=0/0/0"

    def test_the_per_kind_line_renders_all_three_kinds(self):
        self.write(digest_with_legs())
        self.assertEqual(
            self.run_legs("--per-kind", self.GOOD, "--invoked"), 0
        )
        self.assertIn("flagged-open: tasks=1/2/3 goals=0/1/1 topics=0/0/0 (batch invoked)", self.read())

    def test_the_per_kind_line_covers_the_flagged_key_so_check_passes(self):
        self.write(digest_with_legs())
        self.run_legs("--leg", "keep-alive", "--not-run", "debt gate (exit 12)")
        self.assertEqual(self.run_legs("--per-kind", self.GOOD, "--invoked"), 0)
        self.assertEqual(self.run_legs("--check"), 0)

    def test_the_per_kind_line_supersedes_the_single_count(self):
        """Both cover operator-flagged, and two lines for one key is the duplicate --check refuses."""
        self.write(digest_with_legs())
        self.run_legs("--leg", "operator-flagged", "--count", "3", "--invoked")
        self.assertEqual(self.run_legs("--per-kind", self.GOOD, "--invoked"), 0)
        text = self.read()
        self.assertNotIn("unactioned:", text)
        self.assertEqual(text.count("flagged-open:"), 1)

    def refused(self, *argv):
        """A refused write returns EXIT_FAILED and leaves the digest untouched."""
        self.assertEqual(self.run_legs(*argv), legs.EXIT_FAILED)

    def test_a_triple_that_does_not_add_up_is_refused(self):
        """The assertion the three numbers exist for: opened + declined == eligible."""
        self.write(digest_with_legs())
        self.refused(
            "--per-kind", "tasks=1/2/4 goals=0/1/1 topics=0/0/0", "--invoked"
        )
        self.assertIn("no reading recorded this round", self.read())

    def test_a_per_kind_line_without_a_batch_marker_is_refused(self):
        """Without the marker an unactioned count reads as a batch that ran and found nothing."""
        self.write(digest_with_legs())
        self.refused("--per-kind", self.GOOD)
        self.assertIn("no reading recorded this round", self.read())

    def test_the_kinds_must_appear_once_each_in_order(self):
        """A transposed pair would write a line whose labels are a lie."""
        self.write(digest_with_legs())
        self.refused(
            "--per-kind", "goals=0/1/1 tasks=1/2/3 topics=0/0/0", "--invoked"
        )
        self.assertIn("no reading recorded this round", self.read())

    def test_a_missing_kind_is_refused(self):
        self.write(digest_with_legs())
        self.refused("--per-kind", "tasks=1/2/3 goals=0/1/1", "--invoked")

    def test_a_hand_written_per_kind_line_without_a_marker_fails_check(self):
        """The reader's own guard, reached only by a line the writer did not produce.

        The writer refuses a marker-less `--per-kind`, so a test that goes through it proves
        the writer and never the reader — a hand-edited digest is the case the reader exists
        for, and it must not read as a recorded reading.
        """
        self.write(
            PREFIX_DIGEST
            + "LEGS\n"
            + "  keep-alive: not run — debt gate (exit 12)\n"
            + "  flagged-open: tasks=1/2/3 goals=0/1/1 topics=0/0/0\n"
        )
        self.assertEqual(self.run_legs("--check"), legs.EXIT_INCOMPLETE)

    def test_a_hand_written_per_kind_line_that_does_not_add_up_fails_check(self):
        self.write(
            PREFIX_DIGEST
            + "LEGS\n"
            + "  keep-alive: not run — debt gate (exit 12)\n"
            + "  flagged-open: tasks=1/2/4 goals=0/1/1 topics=0/0/0 (batch invoked)\n"
        )
        self.assertEqual(self.run_legs("--check"), legs.EXIT_INCOMPLETE)

    def test_a_per_kind_line_on_another_leg_is_unparseable(self):
        """The shape belongs to the leg whose single count it replaces."""
        self.write(
            PREFIX_DIGEST
            + "LEGS\n"
            + "  keep-alive: tasks=1/2/3 goals=0/1/1 topics=0/0/0 (batch invoked)\n"
            + "  flagged-open: not run — debt gate (exit 12)\n"
        )
        self.assertEqual(self.run_legs("--check"), legs.EXIT_INCOMPLETE)

    def test_per_kind_takes_no_leg_flag(self):
        """Mutual exclusion is an argument-shape error, so argparse owns it and exits 2."""
        self.write(digest_with_legs())
        with self.assertRaises(SystemExit) as ctx:
            self.run_legs(
                "--leg", "operator-flagged", "--per-kind", self.GOOD, "--invoked"
            )
        self.assertEqual(ctx.exception.code, 2)


class TestStatus(Base):
    """The round's verdict as ONE string the render copies, not a sentence to remember.

    `--check`'s answer arrives as stderr lines plus an exit code; a render told to "render it
    as INCOMPLETE" is a sentence a model is asked to recall, and a softened transcription is
    exactly how a failed round reads as a clean one. `--status` returns the line itself.
    """

    def status_of(self, *argv):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = self.run_legs(*argv)
        return buf.getvalue().strip(), code

    def test_a_complete_reading_is_ok(self):
        self.write(digest_with_legs())
        self.record_all(lapsed=0, topicless=1, dead=0, flagged=2)
        line, code = self.status_of("--status")
        self.assertEqual(code, 0)
        self.assertTrue(line.startswith("legs: OK"))

    def test_the_sweep_seed_is_incomplete_and_names_why(self):
        self.write(digest_with_legs())
        line, code = self.status_of("--status")
        self.assertEqual(code, legs.EXIT_INCOMPLETE)
        self.assertTrue(line.startswith("legs: INCOMPLETE — "))
        self.assertIn("no reading recorded", line)

    def test_a_missing_section_is_incomplete(self):
        self.write(PREFIX_DIGEST)
        line, code = self.status_of("--status")
        self.assertEqual(code, legs.EXIT_INCOMPLETE)
        self.assertIn("no LEGS section", line)

    def test_an_unreadable_digest_is_unknown_not_incomplete(self):
        """A failed read and a suppressed leg must not print the same verdict."""
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = legs.main(["--digest", "/nope/x.digest", "--status"])
        self.assertEqual(code, legs.EXIT_FAILED)

    def test_status_takes_no_write_flag(self):
        self.write(digest_with_legs())
        with self.assertRaises(SystemExit) as ctx:
            self.run_legs("--status", "--leg", "lapsed-managers", "--count", "1")
        self.assertEqual(ctx.exception.code, 2)


class TestSingleHome(unittest.TestCase):
    """The formats live in output-shape.md item 14, and nowhere else is a second copy."""

    def test_output_shape_defines_the_four_formats(self):
        with open(SHAPE, encoding="utf-8") as fh:
            text = fh.read()
        self.assertIn("**`Legs`**", text)
        self.assertIn("keep-alive: unactioned: <n> lapsed-managers", text)
        self.assertIn("keep-alive: unactioned: <n> topicless-goal-managers", text)
        self.assertIn("keep-alive: unactioned: <n> dead-workers", text)
        self.assertIn("flagged-open: unactioned: <n> operator-flagged (batch invoked|batch not invoked)", text)
        self.assertIn("(lower bound, scan incomplete)", text)
        self.assertIn("not run — <reason>", text)
        self.assertIn("fleet-legs.py", text)

    def test_the_acting_legs_point_at_that_home_and_do_not_restate_it(self):
        for path in (KEEPALIVE, FLAGGED):
            with open(path, encoding="utf-8") as fh:
                text = fh.read()
            with self.subTest(path=os.path.basename(path)):
                self.assertIn("output-shape.md` item 14", text)
                self.assertIn("fleet-legs.py", text)
                # The pre-fix instruction, verbatim, is what the change removes.
                self.assertNotIn("Print `unactioned: <n>", text)

    def test_the_sweep_seeds_the_section_so_the_shape_always_exists(self):
        with open(READER, encoding="utf-8") as fh:
            text = fh.read()
        self.assertIn("LEGS", text)
        self.assertIn("keep-alive: not run — no reading recorded this round", text)
        self.assertIn("flagged-open: not run — no reading recorded this round", text)


class TestBatchMarker(Base):
    """The flagged-open count must say whether the batch ran.

    One bare number meant both "the leg ran and left N" and "the leg never ran, N
    are waiting" — the second is the failure the leg exists to remove.
    """

    def test_an_unlabelled_operator_flagged_count_is_refused(self):
        self.write(digest_with_legs())
        self.assertNotEqual(self.run_legs("--leg", "operator-flagged", "--count", "3"), 0)

    def test_an_unlabelled_other_count_is_unaffected(self):
        self.write(digest_with_legs())
        self.assertEqual(self.run_legs("--leg", "lapsed-managers", "--count", "3"), 0)

    def test_the_marker_distinguishes_invoked_from_not_invoked(self):
        self.write(digest_with_legs())
        self.run_legs("--leg", "operator-flagged", "--count", "3", "--invoked")
        self.assertIn(
            "flagged-open: unactioned: 3 operator-flagged (batch invoked)", self.read()
        )
        self.run_legs("--leg", "operator-flagged", "--count", "3", "--not-invoked")
        self.assertIn(
            "flagged-open: unactioned: 3 operator-flagged (batch not invoked)", self.read()
        )

    LEGS_ALL = (
        "DIGEST 2026-11-06T19:10Z\nLEGS\n"
        "  keep-alive: unactioned: 0 lapsed-managers\n"
        "  keep-alive: unactioned: 0 topicless-goal-managers\n"
        "  keep-alive: unactioned: 0 dead-workers\n"
    )

    def test_the_marker_is_refused_on_a_key_with_no_batch(self):
        self.write(digest_with_legs())
        self.assertNotEqual(
            self.run_legs("--leg", "lapsed-managers", "--count", "3", "--invoked"), 0
        )

    def test_check_accepts_a_labelled_flagged_line(self):
        self.write(
            self.LEGS_ALL
            + "  flagged-open: unactioned: 3 operator-flagged (batch not invoked)\n"
        )
        self.assertEqual(self.run_legs("--check"), 0)

    def test_check_rejects_a_bare_operator_flagged_line(self):
        self.write(self.LEGS_ALL + "  flagged-open: unactioned: 3 operator-flagged\n")
        self.assertEqual(self.run_legs("--check"), 1)

    def test_check_rejects_a_marker_on_a_key_with_no_batch(self):
        self.write(
            "DIGEST 2026-11-06T19:10Z\nLEGS\n"
            "  keep-alive: unactioned: 0 lapsed-managers (batch invoked)\n"
            "  keep-alive: unactioned: 0 topicless-goal-managers\n"
            "  keep-alive: unactioned: 0 dead-workers\n"
            "  flagged-open: unactioned: 3 operator-flagged (batch invoked)\n"
        )
        self.assertEqual(self.run_legs("--check"), 1)


if __name__ == "__main__":
    unittest.main()
