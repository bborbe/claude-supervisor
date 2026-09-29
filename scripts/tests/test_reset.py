#!/usr/bin/env python3
"""Tests for scripts/reset.py.

The load-bearing property is that reset never discards: the ledger's entry id set
is identical before and after, a completed task closes its entry with a cited
path, a missing task flags rather than removes, and an asked-of-you entry is never
touched by disk evidence. The goals-shape cases guard the tracked set, where a
shape-blind parser renders a smaller, complete-looking set with no error.

Run: python3 -m unittest discover -s scripts/tests -v
"""

import importlib.util
import json
import os
import tempfile
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_spec = importlib.util.spec_from_file_location("reset", os.path.join(os.path.dirname(_HERE), "reset.py"))
reset = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(reset)


def task(path, status, goals="goals:\n    - '[[G]]'"):
    with open(path, "w") as fh:
        fh.write(f"---\nstatus: {status}\n{goals}\n---\nbody\n")


class GoalsShapes(unittest.TestCase):
    def test_every_declaration_shape_resolves(self):
        shapes = [
            "---\ngoals:\n    - '[[G]]'\n---\n",
            "---\ngoals: G\n---\n",
            "---\ngoals: ['[[G]]']\n---\n",
            "---\ngoals:\n    - - G\n---\n",
            "---\ngoals:\n- \"[[G|alias]]\"\n---\n",
        ]
        for text in shapes:
            self.assertEqual(reset.goals_of(text), {"G"}, text)

    def test_goals_after_long_frontmatter(self):
        pad = "metrics_sessions:\n" + "    - session_id: x\n" * 400
        self.assertEqual(reset.goals_of("---\n" + pad + "goals: G\n---\n"), {"G"})

    def test_empty_and_absent(self):
        self.assertEqual(reset.goals_of("---\ngoals: []\n---\n"), set())
        self.assertEqual(reset.goals_of("---\nstatus: next\n---\n"), set())


class Ledger(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.oi = reset.load_open_items()
        self.oi.ROOT = os.path.join(self.tmp, "state")
        tasks = os.path.join(self.tmp, "25 Tasks")
        os.makedirs(tasks)
        self.oi._TASK_DIRS = [tasks]
        task(os.path.join(tasks, "Done.md"), "completed")
        task(os.path.join(tasks, "Busy.md"), "in_progress")
        task(os.path.join(tasks, "Dropped.md"), "aborted")
        self.items = [
            {"id": "a1", "kind": "pushed", "task": "Done", "state": "open"},
            {"id": "a2", "kind": "pushed", "task": "Busy", "state": "open"},
            {"id": "a3", "kind": "asked-of-me", "task": "Missing", "state": "open"},
            {"id": "a4", "kind": "asked-of-you", "task": "Done", "state": "open"},
            {"id": "a5", "kind": "pushed", "task": "Dropped", "state": "open"},
            {"id": "a6", "kind": "asked-of-me", "task": None, "state": "open"},
            {"id": "a7", "kind": "pushed", "task": "Done", "state": "closed"},
        ]
        os.makedirs(self.oi.ROOT)
        with open(self.oi.path_for("s"), "w") as fh:
            json.dump({"session_id": "s", "items": self.items}, fh)

    def after(self):
        with open(self.oi.path_for("s")) as fh:
            return {i["id"]: i for i in json.load(fh)["items"]}

    def test_never_discards_and_classifies(self):
        reset.revalidate(self.oi, "s", dry=False)
        got = self.after()
        self.assertEqual(sorted(got), sorted(i["id"] for i in self.items))
        self.assertEqual(got["a1"]["state"], "closed")
        self.assertIn("Done.md reads status: completed", got["a1"]["closed_evidence"])
        self.assertEqual(got["a2"]["state"], "open")
        self.assertNotIn("reset_flag", got["a2"])
        self.assertEqual(got["a3"]["state"], "open")
        self.assertIn("task file not found", got["a3"]["reset_flag"])
        self.assertEqual(got["a4"]["state"], "open")  # only the operator closes it
        self.assertIn("aborted", got["a5"]["reset_flag"])
        self.assertEqual(got["a6"]["state"], "open")

    def test_dry_run_writes_nothing(self):
        # open-items' own load() may persist its schema heal; reset's changes must not land.
        reset.revalidate(self.oi, "s", dry=True)
        for item in self.after().values():
            self.assertNotIn("reset_flag", item)
            self.assertNotIn("reset:", item.get("closed_evidence") or "")
        self.assertEqual(self.after()["a1"]["state"], "open")


class Digest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._state = reset.STATE
        reset.STATE = self.tmp
        os.makedirs(os.path.join(self.tmp, "sweep-gate-loop", "myvault"))
        self.path = os.path.join(self.tmp, "sweep-gate-loop", "myvault", "my-topic.json")

    def tearDown(self):
        reset.STATE = self._state

    def test_rewrites_digest_keeps_busy_since(self):
        with open(self.path, "w") as fh:
            json.dump({"digest": "abc", "busy_since": {"x": "t"}}, fh)
        reset.reset_digest("My Topic", "myvault", dry=False)
        data = json.load(open(self.path))
        self.assertNotEqual(data["digest"], "abc")
        self.assertTrue(data["digest"].startswith("reset-"))
        self.assertEqual(data["busy_since"], {"x": "t"})

    def test_lowercases_a_display_case_vault(self):
        """Records written before 2026-09-23 carry `MyVault`; the directory is
        lowercase. A strict join rewrites a digest the gate never reads."""
        with open(self.path, "w") as fh:
            json.dump({"digest": "abc"}, fh)
        reset.reset_digest("My Topic", "MyVault", dry=False)
        self.assertNotEqual(json.load(open(self.path))["digest"], "abc")

    def test_absent_stays_absent(self):
        lines = reset.reset_digest("My Topic", "myvault", dry=False)
        self.assertFalse(os.path.exists(self.path))
        self.assertIn("absent", lines[0])


if __name__ == "__main__":
    unittest.main()
