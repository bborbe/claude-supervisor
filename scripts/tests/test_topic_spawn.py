#!/usr/bin/env python3
"""Tests for scripts/topic-spawn.py — the topic tier's spawn gate.

Every refusal is a named token on the **first line**, so each test asserts the token
rather than prose, and each refusal is paired with a positive control: a gate that
refused everything would satisfy every refusal case on its own.
"""

import io
import pathlib
import tempfile
import unittest
from contextlib import redirect_stdout

_HERE = pathlib.Path(__file__).resolve().parent

import importlib.util

_spec = importlib.util.spec_from_file_location("topic_spawn", str(_HERE.parent / "topic-spawn.py"))
ts = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ts)

TOPIC = """---
page_type: topic
status: in_progress
---
## Goals
- [[Alpha]]
- [[Beta]]
- [[Gamma]]
- [[Delta]]
"""

GOAL = """---
page_type: goal
status: in_progress
phase: {phase}
priority: {priority}
manager_session_id: {sid}
---
# Success Criteria
- [ ] one
"""


class GoalsOfShapesTest(unittest.TestCase):
    """The three shapes the vault writes, plus the two that name nothing.

    ⚠️ The block form is the one that bit: `m.end()` lands before the newline after the
    `goals:` key, so `splitlines()` opens with an empty line, and a parser that treats
    that as the end of the block returns `[]` for every block-form task — which is most
    of the vault. A goal then reads as having no ready work, and the ranking silently
    falls through to `priority`.
    """

    def test_inline_list(self):
        self.assertEqual(["A", "B"], ts.goals_of("goals: ['[[A]]', '[[B]]']"))

    def test_plain_scalar(self):
        self.assertEqual(["Solo Goal"], ts.goals_of("goals: Solo Goal"))

    def test_block_list(self):
        self.assertEqual(
            ["A2A Workers Are Prod-Ready", "Second"],
            ts.goals_of("goals:\n    - '[[A2A Workers Are Prod-Ready]]'\n    - '[[Second]]'\nstatus: x"),
        )

    def test_nested_list_of_list(self):
        self.assertEqual(
            ["Nested One", "Nested Two"],
            ts.goals_of("goals:\n    - - Nested One\n    - - Nested Two"),
        )

    def test_an_empty_declaration_names_nothing(self):
        self.assertEqual([], ts.goals_of("goals: []"))

    def test_an_absent_key_names_nothing(self):
        self.assertEqual([], ts.goals_of("status: in_progress"))


class DeclaredGoalsTest(unittest.TestCase):
    def test_wikilinks_and_a_trailing_note(self):
        self.assertEqual(
            ["Prod-Ready Thing", "Other"],
            ts.declared_goals("## Goals\n- [[Prod-Ready Thing]]\n- [[Other]] — a note\n"),
        )

    def test_a_hyphenated_name_is_not_split(self):
        # The separator rule requires whitespace around the dash, so an intra-word
        # hyphen survives — the vault has many (`Prod-Ready`, `Non-Goal`).
        self.assertEqual(["A Non-Goal Thing"], ts.declared_goals("## Goals\n- [[A Non-Goal Thing]]\n"))


class TopicSpawnTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.vault = pathlib.Path(self.tmp.name)
        (self.vault / "23 Topics").mkdir()
        (self.vault / "24 Goals").mkdir()
        (self.vault / "25 Tasks").mkdir()
        (self.vault / "23 Topics" / "A Topic.md").write_text(TOPIC, encoding="utf-8")
        self.goal("Alpha", "planning", 2)
        self.goal("Beta", "execution", 1)
        self.goal("Gamma", "planning", 3)
        self.goal("Delta", "execution", 4)
        # The live probes are stubbed: this suite tests the GATE, and a real
        # `session-liveness` probe would make the result depend on the machine.
        self._orig = (ts.live_goal_managers, ts.fleet_manager_count, ts.fleet_manager_ceiling)
        ts.live_goal_managers = lambda vault, topics_dir, goals: 0
        ts.fleet_manager_count = lambda: 0
        ts.fleet_manager_ceiling = lambda: 10
        self.addCleanup(self._restore)

    def _restore(self):
        ts.live_goal_managers, ts.fleet_manager_count, ts.fleet_manager_ceiling = self._orig

    def goal(self, name, phase, priority, sid=""):
        (self.vault / "24 Goals" / f"{name}.md").write_text(
            GOAL.format(phase=phase, priority=priority, sid=sid), encoding="utf-8"
        )

    def run_gate(self, *argv):
        out = io.StringIO()
        with redirect_stdout(out):
            rc = ts.main(["--vault", str(self.vault), "--topic", "A Topic", *argv])
        return rc, out.getvalue().splitlines()

    # --- the four refusals, each with its control -----------------------------

    def test_a_worker_request_is_refused(self):
        rc, out = self.run_gate("--worker", "Some Task")
        self.assertEqual(1, rc)
        self.assertEqual("workers are not this tier's to start", out[0])

    def test_an_undeclared_goal_is_refused(self):
        self.goal("Orphan", "planning", 1)
        rc, out = self.run_gate("--goal", "Orphan")
        self.assertEqual(1, rc)
        self.assertEqual("goal-not-in-scope", out[0])

    def test_a_declared_goal_at_phase_planning_passes(self):
        rc, out = self.run_gate("--goal", "Alpha")
        self.assertEqual(0, rc)
        self.assertEqual("spawn-ok: Alpha", out[0])

    def test_a_goal_at_phase_todo_is_refused(self):
        self.goal("Delta", "todo", 4)
        rc, out = self.run_gate("--goal", "Delta")
        self.assertEqual(1, rc)
        self.assertEqual("goal-not-startable", out[0])

    def test_the_same_goal_flipped_to_planning_passes(self):
        self.goal("Delta", "planning", 4)
        rc, out = self.run_gate("--goal", "Delta")
        self.assertEqual(0, rc)
        self.assertEqual("spawn-ok: Delta", out[0])

    def test_the_per_topic_cap_refuses_and_names_goal_cap(self):
        ts.live_goal_managers = lambda vault, topics_dir, goals: 3
        rc, out = self.run_gate("--goal", "Alpha")
        self.assertEqual(1, rc)
        self.assertEqual("manager-cap", out[0])
        self.assertEqual("goal-cap", out[1])

    def test_one_slot_below_the_cap_starts(self):
        ts.live_goal_managers = lambda vault, topics_dir, goals: 2
        rc, out = self.run_gate("--goal", "Alpha")
        self.assertEqual(0, rc)
        self.assertEqual("spawn-ok: Alpha", out[0])

    def test_the_fleet_ceiling_refuses_and_names_manager_cap(self):
        ts.fleet_manager_count = lambda: 10
        ts.fleet_manager_ceiling = lambda: 10
        rc, out = self.run_gate("--goal", "Alpha")
        self.assertEqual(1, rc)
        self.assertEqual("manager-cap", out[0])
        self.assertEqual("manager-cap", out[1])

    def test_one_manager_below_the_ceiling_starts(self):
        ts.fleet_manager_count = lambda: 9
        ts.fleet_manager_ceiling = lambda: 10
        rc, out = self.run_gate("--goal", "Alpha")
        self.assertEqual(0, rc)

    # --- the ranking ----------------------------------------------------------

    def test_the_ranking_fills_exactly_n_slots(self):
        rc, out = self.run_gate("--rank")
        self.assertEqual(0, rc)
        self.assertEqual("rank: 4 startable of 4 declared · N = 3", out[0])
        self.assertEqual(3, sum(1 for line in out[1:] if " start " in line))
        self.assertEqual(1, sum(1 for line in out[1:] if " held " in line))

    def test_the_ranking_orders_by_priority_ascending(self):
        rc, out = self.run_gate("--rank")
        self.assertIn("Beta", out[1])   # priority 1
        self.assertIn("Alpha", out[2])  # priority 2
        self.assertIn("Gamma", out[3])  # priority 3
        self.assertIn("Delta", out[4])  # priority 4, held

    def test_two_startable_goals_fill_both_slots(self):
        # The control that keeps the ranker from being a constant-3 stub.
        (self.vault / "23 Topics" / "A Topic.md").write_text(
            TOPIC.replace("- [[Gamma]]\n", "").replace("- [[Delta]]\n", ""), encoding="utf-8"
        )
        rc, out = self.run_gate("--rank")
        self.assertEqual("rank: 2 startable of 2 declared · N = 3", out[0])
        self.assertEqual(2, sum(1 for line in out[1:] if " start " in line))


if __name__ == "__main__":
    unittest.main()
