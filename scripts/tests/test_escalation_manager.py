#!/usr/bin/env python3
"""Tests for scripts/escalation-manager.py — the manager that owns one task.

Load-bearing properties:

  * a task whose goal has a live manager resolves to that manager (`chain`);
  * a task with no live ancestor renders `fleet` — tier 4's answer, not an error;
  * ⚠️ an UNREADABLE registry raises and exits non-zero. It must never render `fleet`:
    `fleet` claims the Fleet Manager owns the row, and a board that could not be read
    cannot support that claim. `cannot-tell` and `nobody` must not render the same.

Run: python3 -m unittest discover -s scripts/tests -v
"""

import importlib.util
import os
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_SCRIPT = os.path.join(os.path.dirname(_HERE), "escalation-manager.py")


def load():
    spec = importlib.util.spec_from_file_location("escalation_manager", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeIndex:
    def goals_of(self, _title):
        return ["Some Goal"]

    def managed_by_of(self, _title):
        return None


class FakeGrouping:
    def __init__(self, roles=None, subjects=None):
        self.roles = roles or {}
        self.subjects = subjects or {}


class FakeBoard:
    """The slice of `fleet-board.py` this script calls."""

    def __init__(self, registry, manager=None):
        self._registry = registry
        self._manager = manager

    def registry_records(self):
        return self._registry

    def vault_index(self):
        return FakeIndex()

    def colour_census(self):
        return {}

    def loop_slugs(self):
        return set()

    def build_grouping(self, *_a, **_k):
        return FakeGrouping()

    def manager_on_chain(self, *_a, **_k):
        return self._manager


class EscalationManagerTest(unittest.TestCase):
    def setUp(self):
        self.mod = load()
        self._orig_sessions = self.mod._fleet_sessions
        self.mod._fleet_sessions = lambda: type(
            "FS", (), {"build_task_index": staticmethod(lambda: ({}, {}))})()

    def tearDown(self):
        self.mod._fleet_sessions = self._orig_sessions

    def test_a_live_manager_resolves_through_the_chain(self):
        board = FakeBoard({"abc123": {"name": "Some Goal Manager"}}, manager="abc123")
        self.assertEqual(("Some Goal Manager", "chain"), self.mod.resolve("T", board=board))

    def test_no_live_ancestor_renders_fleet(self):
        board = FakeBoard({"abc123": {"name": "x"}}, manager=None)
        self.assertEqual(("fleet", "fleet"), self.mod.resolve("T", board=board))

    def test_an_unreadable_registry_raises_rather_than_rendering_fleet(self):
        board = FakeBoard(None)
        with self.assertRaises(RuntimeError):
            self.mod.resolve("T", board=board)

    def test_a_manager_session_with_no_name_raises(self):
        board = FakeBoard({"abc123": {}}, manager="abc123")
        with self.assertRaises(RuntimeError):
            self.mod.resolve("T", board=board)

    def test_resolve_many_builds_the_grouping_once_for_every_title(self):
        builds = []

        class CountingBoard(FakeBoard):
            def build_grouping(self, *_a, **_k):
                builds.append(1)
                return FakeGrouping()

        board = CountingBoard({"abc123": {"name": "Some Goal Manager"}}, manager="abc123")
        out = self.mod.resolve_many(["A", "B", "A"], board=board)
        self.assertEqual(1, len(builds), "one build for N titles, not one per title")
        self.assertEqual({"A", "B"}, set(out))
        self.assertEqual(("Some Goal Manager", "chain"), out["A"])

    def test_resolve_many_renders_fleet_per_title(self):
        board = FakeBoard({"abc123": {"name": "x"}}, manager=None)
        out = self.mod.resolve_many(["A", "B"], board=board)
        self.assertEqual(("fleet", "fleet"), out["A"])
        self.assertEqual(("fleet", "fleet"), out["B"])

    def test_main_exits_non_zero_on_a_failed_read(self):
        original = self.mod.resolve

        def boom(_title):
            raise RuntimeError("session registry unreadable")

        self.mod.resolve = boom
        try:
            self.assertEqual(2, self.mod.main(["T"]))
        finally:
            self.mod.resolve = original


if __name__ == "__main__":
    unittest.main()
