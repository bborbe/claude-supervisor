#!/usr/bin/env python3
"""Tests for scripts/exit-record-coverage.py.

The load-bearing properties:

- a manager pane with no record, or a record with no spawn line, is UNRECORDED;
- a spawn line written with `session=-` (a fresh manager spawn) still counts — the session
  id is not part of the test;
- the pid must be alive: a record whose only spawn line names a dead pid is UNRECORDED, the
  same record with a live pid is not (a reused pane id must not inherit a dead pane's record);
- a pane that cannot be resolved is counted `unknown`, never dropped silently, and an
  unreadable pane map makes every manager `unknown`;
- the title join tolerates the leading status glyph and truncation, and refuses ambiguity.

Run: python3 -m unittest discover -s scripts/tests -v
"""

import importlib.util
import os
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_spec = importlib.util.spec_from_file_location(
    "exit_record_coverage", os.path.join(_HERE, "..", "exit-record-coverage.py"))
erc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(erc)

LIVE, DEAD = 4242, 9999


def alive(pid):
    return pid == LIVE


def spawn(pid, session="-", writer="trap"):
    return (f"2026-11-02T10:00:00Z\tkind=spawn\tpane=7\tsession={session}\tpid={pid}"
            f"\trc=0\tsignal=-\tsender=-\twriter={writer}")


MGR = {"aaaaaaaa-0000": "Fleet Manager"}
PANES = {"7": "✳ Fleet Manager"}


class TestAliveDiscriminator(unittest.TestCase):
    def test_dead_pid_is_unrecorded(self):
        out = erc.coverage(MGR, PANES, {"7": [spawn(DEAD)]}, alive)
        self.assertEqual([r["pane"] for r in out["unrecorded"]], ["7"])

    def test_live_pid_is_recorded(self):
        out = erc.coverage(MGR, PANES, {"7": [spawn(LIVE)]}, alive)
        self.assertEqual(out["unrecorded"], [])

    def test_reused_pane_old_dead_then_new_live(self):
        out = erc.coverage(MGR, PANES, {"7": [spawn(DEAD), spawn(LIVE)]}, alive)
        self.assertEqual(out["unrecorded"], [])


class TestSpawnLineShape(unittest.TestCase):
    def test_no_record_is_unrecorded(self):
        out = erc.coverage(MGR, PANES, {}, alive)
        row = out["unrecorded"][0]
        self.assertEqual((row["pane"], row["session_id"]), ("7", "aaaaaaaa-0000"))
        self.assertIn("UNRECORDED", row["line"])
        self.assertIn("pane 7", row["line"])
        self.assertIn("aaaaaaaa", row["line"])

    def test_session_dash_counts(self):
        out = erc.coverage(MGR, PANES, {"7": [spawn(LIVE, session="-")]}, alive)
        self.assertEqual(out["unrecorded"], [])

    def test_observer_line_is_not_a_spawn(self):
        out = erc.coverage(MGR, PANES, {"7": [spawn(LIVE, writer="observer")]}, alive)
        self.assertEqual(len(out["unrecorded"]), 1)

    def test_heartbeat_only_is_unrecorded(self):
        hb = spawn(LIVE).replace("kind=spawn", "kind=heartbeat")
        out = erc.coverage(MGR, PANES, {"7": [hb]}, alive)
        self.assertEqual(len(out["unrecorded"]), 1)


class TestUnknown(unittest.TestCase):
    def test_pane_absent_from_list_is_unknown(self):
        out = erc.coverage(MGR, {"3": "✳ Something Else Entirely"}, {}, alive)
        self.assertEqual((len(out["unknown"]), out["unrecorded"]), (1, []))
        self.assertIn("unknown: 1", erc.unknown_line(out))

    def test_unreadable_pane_map_makes_all_unknown(self):
        out = erc.live_coverage(MGR, None)
        self.assertEqual((len(out["unknown"]), out["unrecorded"]), (1, []))

    def test_no_unknown_no_line(self):
        out = erc.coverage(MGR, PANES, {"7": [spawn(LIVE)]}, alive)
        self.assertIsNone(erc.unknown_line(out))

    def test_several_unknowns_are_counted_and_all_named(self):
        """The `", ".join(...)` over N>1 unknowns — the branch every earlier test missed.

        A single-unknown fixture (`test_pane_absent_from_list_is_unknown`) pins the
        count but never the join: an implementation that emitted only the first name
        would still print `unknown: 1` there. This one carries three, and asserts the
        count AND that each name reaches the line — reverting the join to
        `result["unknown"][0]` fails it.
        """
        managers = {"aaaaaaaa-0000": "Fleet Manager", "bbbbbbbb-0000": "Trading Manager",
                    "cccccccc-0000": "Docs Manager"}
        out = erc.coverage(managers, {"3": "✳ Something Else Entirely"}, {}, alive)
        self.assertEqual((len(out["unknown"]), out["unrecorded"]), (3, []))
        line = erc.unknown_line(out)
        self.assertIn("unknown: 3", line)
        for name in ("Fleet Manager", "Trading Manager", "Docs Manager"):
            self.assertIn(name, line)


class _TempStore:
    """`session-exit-record.py`'s own two hooks, pointed at a temp store.

    `live_coverage` reaches the real module for `state_dir()` and `alive()`; this
    stands in for both, so the wrapper's own wiring — pane map to titles, store to
    records, records to `has_live_spawn` — runs end to end without the live store.
    """

    def __init__(self, directory):
        self._directory = directory

    def state_dir(self):
        return self._directory

    @staticmethod
    def alive(pid):
        return pid == LIVE


class TestLiveCoverage(unittest.TestCase):
    """`live_coverage`'s happy path — the branch that runs on every clean fleet.

    Every earlier test called `coverage()` directly or `live_coverage(MGR, None)`;
    the arm that turns a pane map into titles, reads the store and consults `alive`
    (`exit-record-coverage.py:222-224`) had never run. These three pin it, over a
    real temp store rather than a stubbed `read_records`, so the store read is part
    of what is exercised.
    """

    PANE_MAP = {"7": {"title": "✳ Fleet Manager"}}

    def _run(self, lines_by_pane):
        import tempfile
        saved = erc._record_module
        with tempfile.TemporaryDirectory() as directory:
            for pane, lines in lines_by_pane.items():
                with open(os.path.join(directory, f"pane-{pane}.log"), "w", encoding="utf-8") as handle:
                    handle.write("".join(line + "\n" for line in lines))
            try:
                erc._record_module = lambda: _TempStore(directory)
                return erc.live_coverage(MGR, self.PANE_MAP)
            finally:
                erc._record_module = saved

    def test_live_spawn_in_the_store_reads_as_recorded(self):
        out = self._run({"7": [spawn(LIVE)]})
        self.assertEqual((out["unrecorded"], out["unknown"]), ([], []))

    def test_empty_store_reads_as_unrecorded(self):
        out = self._run({})
        self.assertEqual(len(out["unrecorded"]), 1)
        self.assertEqual(out["unknown"], [])

    def test_dead_pid_in_the_store_reads_as_unrecorded(self):
        out = self._run({"7": [spawn(DEAD)]})
        self.assertEqual(len(out["unrecorded"]), 1)


class TestNarrowedStoreRead(unittest.TestCase):
    """`read_records()` opens only the panes that resolved — not the whole store.

    The store is keyed per pane and grows with every spawn the fleet has ever made
    (443 spawn lines measured 2026-11-02), while a render consults a handful of
    manager panes. Both counts are quoted in `# Results`.
    """

    def test_read_records_narrows_to_the_named_panes(self):
        import tempfile
        with tempfile.TemporaryDirectory() as directory:
            for pane in ("1", "2", "3", "7", "9"):
                with open(os.path.join(directory, f"pane-{pane}.log"), "w", encoding="utf-8") as handle:
                    handle.write(spawn(LIVE) + "\n")
            self.assertEqual(sorted(erc.read_records(directory)), ["1", "2", "3", "7", "9"])
            self.assertEqual(sorted(erc.read_records(directory, panes={"7"})), ["7"])
            self.assertEqual(sorted(erc.read_records(directory, panes={"7", "9"})), ["7", "9"])

    def test_live_coverage_reads_only_the_resolved_panes(self):
        """The narrowing is applied at the wrapper, where the resolution already happened."""
        seen = {}
        saved_read, saved_module = erc.read_records, erc._record_module
        try:
            def fake_read(directory=None, panes=None):
                seen["panes"] = panes
                return {}

            erc.read_records = fake_read
            erc._record_module = lambda: _TempStore("/nonexistent")
            erc.live_coverage(MGR, {"7": {"title": "✳ Fleet Manager"},
                                    "9": {"title": "✳ Some Other Manager"}})
        finally:
            erc.read_records, erc._record_module = saved_read, saved_module
        self.assertEqual(seen["panes"], {"7"}, "only the pane that resolved may be read")


class TestReadRecords(unittest.TestCase):
    def test_undecodable_record_does_not_blind_the_rest(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "pane-1.log"), "wb") as h:
                h.write(b"\xff\xfe torn\n")
            with open(os.path.join(d, "pane-2.log"), "w", encoding="utf-8") as h:
                h.write(spawn(LIVE) + "\n")
            with open(os.path.join(d, "other.txt"), "w") as h:
                h.write("ignored\n")
            out = erc.read_records(d)
        self.assertEqual(sorted(out), ["1", "2"])
        self.assertEqual(out["2"], [spawn(LIVE)])

    def test_absent_store_is_empty(self):
        self.assertEqual(erc.read_records("/nonexistent/session-exit-dir"), {})


class TestEdgeNames(unittest.TestCase):
    def test_empty_name_is_unknown(self):
        out = erc.coverage({"bbbbbbbb-0000": ""}, PANES, {}, alive)
        self.assertEqual((len(out["unknown"]), out["unrecorded"]), (1, []))

    def test_two_exact_titles_are_unknown(self):
        out = erc.coverage(MGR, {"1": "✳ Fleet Manager", "2": "⠿ Fleet Manager"}, {}, alive)
        self.assertEqual((len(out["unknown"]), out["unrecorded"]), (1, []))

    def test_all_unknown_shape(self):
        out = erc.all_unknown(MGR)
        self.assertEqual(out["unrecorded"], [])
        self.assertEqual(out["unknown"][0]["session_id"], "aaaaaaaa-0000")


class TestControlCharacters(unittest.TestCase):
    """A session name is operator-controlled text that lands on the operator's stderr.

    The fixture is pinned to the two bytes a whitespace fold does NOT remove —
    `\\x07` (BEL) and `\\x1b[31m` (ANSI SGR). A tab-only fixture would pass on the
    unmodified code, because `fleet-board.py`'s `cell()` already folds whitespace;
    these two survive it, so the assertion below can actually fail.
    """

    def test_bell_never_reaches_the_unrecorded_line(self):
        # The BEL sits BEFORE the name: `normalize()` strips a leading non-word run,
        # so the pane still resolves and the line is built from the same name the
        # operator's terminal would receive.
        out = erc.coverage({"aaaaaaaa-0000": "\x07Fleet Manager"}, PANES, {}, alive)
        self.assertEqual(len(out["unrecorded"]), 1, "the BEL prefix must not stop the pane resolving")
        line = out["unrecorded"][0]["line"]
        self.assertNotIn("\x07", line)
        self.assertIn("Fleet Manager", line)

    def test_sgr_sequence_never_reaches_the_unknown_line(self):
        out = erc.coverage(
            {"bbbbbbbb-0000": "\x07\x1b[31mGhost Manager"}, {"3": "✳ Something Else"}, {}, alive)
        self.assertEqual(len(out["unknown"]), 1)
        line = erc.unknown_line(out)
        self.assertNotIn("\x07", line)
        self.assertNotIn("\x1b", line)
        self.assertIn("Ghost Manager", line)

    def test_a_name_that_is_only_control_characters_falls_back_to_the_session_id(self):
        out = erc.coverage({"bbbbbbbb-0000": "\x1b[31m"}, {"3": "✳ Something Else"}, {}, alive)
        self.assertIn("bbbbbbbb", erc.unknown_line(out))

    def test_a_name_of_invisible_format_characters_falls_back_to_the_session_id(self):
        """Zero-width and bidi-override characters render as nothing but stay truthy.

        Without stripping them, `printable(name) or session_id[:8]` keeps the invisible
        name and the line names no session at all — the operator sees a gap. The
        right-to-left override is the sharper case: it would visually reorder the rest
        of the line rather than merely vanish.
        """
        out = erc.coverage({"bbbbbbbb-0000": "​‮"}, {"3": "✳ Something Else"}, {}, alive)
        self.assertIn("bbbbbbbb", erc.unknown_line(out))

    def test_a_whitespace_only_name_falls_back_to_the_session_id(self):
        """Whitespace renders as a gap, so the line would name no session at all."""
        out = erc.coverage({"bbbbbbbb-0000": "   "}, {"3": "✳ Something Else"}, {}, alive)
        self.assertIn("bbbbbbbb", erc.unknown_line(out))


class TestManagersOf(unittest.TestCase):
    def test_only_manager_role(self):
        spec = importlib.util.spec_from_file_location("fleet_board", os.path.join(_HERE, "..", "fleet-board.py"))
        fb = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(fb)
        registry = {"m": {"name": "X Manager"}, "w": {"name": "a worker"}}
        grouping = fb.Grouping(roles={"m": fb.ROLE_MANAGER})
        self.assertEqual(fb.managers_of(registry, grouping), {"m": "X Manager"})


class TestResolvePane(unittest.TestCase):
    def test_gear_and_glyph_stripped(self):
        self.assertEqual(erc.resolve_pane("⚙ Add a Thing Manager", {"5": "✳ ⚙ Add a Thing Manager"}), "5")

    def test_truncated_title_prefix(self):
        name = "A Create-Task Is Refused Because the Create Allowlist Is Missing Five Keys"
        self.assertEqual(erc.resolve_pane(name, {"9": "✳ A Create-Task Is Refused Because the Create Allowlist Is Missing Fi"}), "9")

    def test_short_prefix_refused(self):
        self.assertIsNone(erc.resolve_pane("Fleet", {"1": "Fleet Manager Extra"}))

    def test_ambiguous_refused(self):
        panes = {"1": "✳ Fleet Manager", "2": "◐ Fleet Manager"}
        self.assertIsNone(erc.resolve_pane("Fleet Manager", panes))

    def test_prefix_at_the_floor_resolves(self):
        """The boundary itself: `min(len(t), len(key)) >= PREFIX_FLOOR` at exactly 20.

        The neighbours either side of the boundary were both untested — the short
        case used 5 characters and the truncated case ran far above the floor — so
        `<` and `<=` were indistinguishable. Reverting the comparison to `>` fails
        this test and nothing else.
        """
        key = "a" * erc.PREFIX_FLOOR
        self.assertEqual(erc.resolve_pane(key, {"9": "✳ " + key + " Manager"}), "9")

    def test_one_below_the_floor_is_refused(self):
        """The other side of the same boundary — 19 characters is a coincidence."""
        key = "a" * (erc.PREFIX_FLOOR - 1)
        self.assertIsNone(erc.resolve_pane(key, {"9": "✳ " + key + " Manager"}))


if __name__ == "__main__":
    unittest.main()
