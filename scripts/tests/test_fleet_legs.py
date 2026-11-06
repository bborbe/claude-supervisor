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

import importlib.util
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


class TestCheck(Base):
    def test_a_digest_without_the_legs_section_fails_check(self):
        """The pre-fix shape: the round ran and the digest carries none of the three."""
        self.write(PREFIX_DIGEST)
        self.assertEqual(self.run_legs("--check"), legs.EXIT_INCOMPLETE)

    def test_the_seeded_section_passes_check(self):
        """Every leg not run is a complete shape — the lines exist, none claims a count."""
        self.write(digest_with_legs())
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
            + "  flagged-open: unactioned: 0 operator-flagged\n"
        )
        self.assertEqual(self.run_legs("--check"), legs.EXIT_INCOMPLETE)

    def test_a_key_under_the_wrong_leg_fails_check(self):
        self.write(
            PREFIX_DIGEST
            + "LEGS\n"
            + "  keep-alive: unactioned: 0 lapsed-managers\n"
            + "  keep-alive: unactioned: 0 dead-workers\n"
            + "  keep-alive: unactioned: 0 operator-flagged\n"
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
            "keep-alive: not run — no reading recorded this round · dead-workers", text
        )
        self.assertIn("flagged-open: not run — no reading recorded this round", text)
        self.assertEqual(self.run_legs("--check"), 0)

    def test_all_three_counts_render_and_check_clean(self):
        self.write(digest_with_legs())
        self.assertEqual(self.run_legs("--leg", "lapsed-managers", "--count", "0"), 0)
        self.assertEqual(self.run_legs("--leg", "dead-workers", "--count", "0", "--exact"), 0)
        self.assertEqual(self.run_legs("--leg", "operator-flagged", "--count", "3"), 0)
        text = self.read()
        self.assertEqual(text.count("unactioned:"), 3)
        self.assertIn("keep-alive: unactioned: 0 lapsed-managers", text)
        self.assertIn("keep-alive: unactioned: 0 dead-workers", text)
        self.assertIn("flagged-open: unactioned: 3 operator-flagged", text)
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
        self.assertEqual(self.run_legs("--check"), 0)

    def test_a_partial_not_run_reading_names_the_keys_it_still_covers(self):
        self.write(digest_with_legs())
        self.run_legs("--leg", "lapsed-managers", "--count", "2")
        self.assertIn(
            "keep-alive: not run — no reading recorded this round · dead-workers",
            self.read(),
        )

    def test_a_not_run_leg_and_a_zero_leg_never_render_the_same(self):
        """The whole point: suppressed and empty must be distinguishable."""
        self.write(digest_with_legs())
        self.run_legs("--leg", "keep-alive", "--not-run", "debt gate (re-arm, exit 12)")
        self.run_legs("--leg", "operator-flagged", "--count", "0")
        suppressed = self.read()

        self.write(digest_with_legs())
        self.run_legs("--leg", "lapsed-managers", "--count", "0")
        self.run_legs("--leg", "dead-workers", "--count", "0", "--exact")
        self.run_legs("--leg", "operator-flagged", "--count", "0")
        empty = self.read()

        self.assertNotEqual(suppressed, empty)
        self.assertIn("keep-alive: not run — debt gate (re-arm, exit 12)", suppressed)
        self.assertNotIn("not run —", empty)
        self.assertEqual(empty.count("unactioned:"), 3)

    def test_a_count_after_a_not_run_clears_the_not_run_reading(self):
        """A leg that ran on a later round must not keep its earlier skip."""
        self.write(digest_with_legs())
        self.run_legs("--leg", "keep-alive", "--not-run", "debt gate")
        self.assertEqual(self.run_legs("--leg", "lapsed-managers", "--count", "4"), 0)
        text = self.read()
        self.assertIn("keep-alive: unactioned: 4 lapsed-managers", text)
        self.assertIn("keep-alive: not run — debt gate · dead-workers", text)
        self.assertEqual(self.run_legs("--check"), 0)


class TestDigestPreservation(Base):
    def test_the_rest_of_the_digest_is_untouched(self):
        self.write(digest_with_legs())
        self.run_legs("--leg", "operator-flagged", "--count", "1")
        text = self.read()
        self.assertTrue(text.startswith("DIGEST 2026-11-06T19:10Z"))
        self.assertIn("CLASSIFICATION  24 classified", text)
        self.assertIn("NOTES   none", text)


class TestSingleHome(unittest.TestCase):
    """The formats live in output-shape.md item 14, and nowhere else is a second copy."""

    def test_output_shape_defines_the_three_formats(self):
        with open(SHAPE, encoding="utf-8") as fh:
            text = fh.read()
        self.assertIn("**`Legs`**", text)
        self.assertIn("keep-alive: unactioned: <n> lapsed-managers", text)
        self.assertIn("keep-alive: unactioned: <n> dead-workers", text)
        self.assertIn("flagged-open: unactioned: <n> operator-flagged", text)
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


if __name__ == "__main__":
    unittest.main()
