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


if __name__ == "__main__":
    unittest.main()
