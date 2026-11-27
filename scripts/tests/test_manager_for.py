#!/usr/bin/env python3
"""Tests for scripts/manager-for.py and fleet-board.py:manager_for_goal.

The rule is the board's, not a second matcher: these fixtures build a `VaultIndex`
and a registry by hand, exactly as `test_fleet_board.py` does, so a divergence
between the approval gate and the board's `manager:` column is observable.

Run: python3 -m unittest discover -s scripts/tests -v
"""

import contextlib
import importlib.util
import io
import json
import os
import sys
import tempfile
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_SCRIPTS = os.path.dirname(_HERE)


def _load(name, filename):
    spec = importlib.util.spec_from_file_location(name, os.path.join(_SCRIPTS, filename))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


fb = _load("fleet_board", "fleet-board.py")
mf = _load("manager_for", "manager-for.py")

CWD = "/Users/x/Documents/Obsidian/my-vault"


def _sid(n):
    return "aaaabbbb-0000-0000-0000-00000000000%d" % n


def _registry(*entries):
    return {
        _sid(i): {"status": "idle", "name": name, "cwd": CWD}
        for i, name in entries
    }


class TestManagerForGoal(unittest.TestCase):
    def test_a_live_goal_manager_answers_goal(self):
        index = fb.VaultIndex(goals=["Sample Goal"])
        registry = _registry((1, "Sample Goal Manager"))
        name, source = fb.manager_for_goal("Sample Goal", index, registry, {}, set(), {})
        self.assertEqual((name, source), ("Sample Goal Manager", "goal"))

    def test_a_topic_manager_declaring_the_goal_answers_topic(self):
        index = fb.VaultIndex(
            goals=["Sample Goal"],
            topics=["Sample Topic"],
            goal_topics={"sample goal": ["Sample Topic"]},
        )
        registry = _registry((1, "Sample Topic Manager"))
        name, source = fb.manager_for_goal("Sample Goal", index, registry, {}, set(), {})
        self.assertEqual((name, source), ("Sample Topic Manager", "topic"))

    def test_a_direct_goal_manager_beats_the_topic_manager(self):
        index = fb.VaultIndex(
            goals=["Sample Goal"],
            topics=["Sample Topic"],
            goal_topics={"sample goal": ["Sample Topic"]},
        )
        registry = _registry((1, "Sample Topic Manager"), (2, "Sample Goal Manager"))
        name, source = fb.manager_for_goal("Sample Goal", index, registry, {}, set(), {})
        self.assertEqual((name, source), ("Sample Goal Manager", "goal"))

    def test_a_goal_with_no_live_manager_answers_none(self):
        index = fb.VaultIndex(goals=["Sample Goal"])
        registry = _registry((1, "Fleet Manager"), (2, "Some Other Manager"))
        self.assertEqual(
            fb.manager_for_goal("Sample Goal", index, registry, {}, set(), {}),
            (None, None),
        )

    def test_a_goal_that_names_no_page_answers_none(self):
        index = fb.VaultIndex(goals=["Sample Goal"])
        registry = _registry((1, "Sample Goal Manager"))
        self.assertEqual(
            fb.manager_for_goal("No Such Goal", index, registry, {}, set(), {}),
            (None, None),
        )


class TestManagerForCLI(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.vault = os.path.join(self.tmp.name, "vault")
        os.makedirs(os.path.join(self.vault, "24 Goals"))
        os.makedirs(os.path.join(self.vault, "23 Topics"))
        with open(os.path.join(self.vault, "24 Goals", "Sample Goal.md"), "w", encoding="utf-8") as fh:
            fh.write("---\nstatus: in_progress\n---\n")
        with open(os.path.join(self.vault, "23 Topics", "Sample Topic.md"), "w", encoding="utf-8") as fh:
            fh.write("---\nstatus: in_progress\n---\n## Goals\n- [[Sample Goal]]\n")

    def tearDown(self):
        self.tmp.cleanup()

    def _registry_dir(self, name):
        d = os.path.join(self.tmp.name, "sessions")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "1.json"), "w", encoding="utf-8") as fh:
            json.dump(
                {
                    "sessionId": _sid(1),
                    "pid": os.getpid(),
                    "name": name,
                    "status": "idle",
                    "cwd": self.vault,
                },
                fh,
            )
        return d

    def test_cli_answers_the_topic_manager(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = mf.main(
                [
                    "--goal", "Sample Goal",
                    "--vault", self.vault,
                    "--registry-dir", self._registry_dir("Sample Topic Manager"),
                    "--json",
                ]
            )
        self.assertEqual(rc, 0)
        self.assertEqual(
            json.loads(out.getvalue()),
            {"goal": "Sample Goal", "manager": "Sample Topic Manager", "source": "topic"},
        )

    def test_cli_refuses_a_missing_vault(self):
        rc = mf.main(["--goal", "Sample Goal", "--vault", os.path.join(self.tmp.name, "nope"), "--json"])
        self.assertEqual(rc, 2)

    def test_cli_refuses_an_unreadable_registry(self):
        rc = mf.main(
            [
                "--goal", "Sample Goal",
                "--vault", self.vault,
                "--registry-dir", os.path.join(self.tmp.name, "missing"),
                "--json",
            ]
        )
        self.assertEqual(rc, 2)


if __name__ == "__main__":
    unittest.main()
