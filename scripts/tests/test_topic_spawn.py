#!/usr/bin/env python3
"""Tests for scripts/topic-spawn.py — the topic tier's spawn gate.

Every refusal is a named token on the **first line**, so each test asserts the token
rather than prose, and each refusal is paired with a positive control: a gate that
refused everything would satisfy every refusal case on its own.
"""

import io
import json
import pathlib
import tempfile
import types
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

    def test_a_prose_bullet_is_not_a_declared_goal(self):
        """F2 — a note bullet's bold label parsed as a goal name.

        Measured 2026-10-20: `- **Binding now (2026-10-19 22:04):** …` produced the
        name `**Binding now`, inflating one topic's declared count 26 against 25
        real goals. With the goals dir passed, an entry must resolve to a page.
        """
        with tempfile.TemporaryDirectory() as tmp:
            goals = pathlib.Path(tmp)
            (goals / "Alpha.md").write_text("---\n---\n", encoding="utf-8")
            page = ("## Goals\n- [[Alpha]]\n"
                    "- **Binding now (2026-10-19 22:04):** a note, not a member\n")
            self.assertEqual(["Alpha"], ts.declared_goals(page, str(goals)))

    def test_without_a_goals_dir_the_parse_stays_syntactic(self):
        """The control: the directory is what filters an unresolvable name.

        The bold-label test above passes on the markup alone, so this one uses a
        plain name that resolves to nothing — kept by the syntactic parse, dropped
        once the goals dir is supplied.
        """
        page = "## Goals\n- [[Alpha]]\n- Missing Goal\n"
        self.assertEqual(["Alpha", "Missing Goal"], ts.declared_goals(page))
        with tempfile.TemporaryDirectory() as tmp:
            goals = pathlib.Path(tmp)
            (goals / "Alpha.md").write_text("---\n---\n", encoding="utf-8")
            self.assertEqual(["Alpha"], ts.declared_goals(page, str(goals)))


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
        self.assertEqual("workers are not this verb's to start", out[0])

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

    def test_a_goal_with_no_phase_is_exempt_and_starts(self):
        """F1 — an ABSENT phase is exempt, not refused.

        The accepted design leaves the unphased goals on today's behaviour, and the
        topic tier is their only start path: refusing them made 22 of one live
        topic's 25 goals unstartable the moment the split shipped.
        """
        (self.vault / "24 Goals" / "Delta.md").write_text(
            "---\npage_type: goal\nstatus: in_progress\npriority: 4\n---\n",
            encoding="utf-8",
        )
        rc, out = self.run_gate("--goal", "Delta")
        self.assertEqual(0, rc)
        self.assertEqual("spawn-ok: Delta", out[0])

    def test_a_goal_with_no_phase_is_ranked_not_dropped(self):
        (self.vault / "24 Goals" / "Delta.md").write_text(
            "---\npage_type: goal\nstatus: in_progress\npriority: 4\n---\n",
            encoding="utf-8",
        )
        rc, out = self.run_gate("--rank")
        self.assertEqual("rank: 4 startable of 4 declared · N = 3", out[0])

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


class FleetGoalManagersTest(unittest.TestCase):
    """F5 — the N-slot count must see a goal manager the page never recorded."""

    def _with_probe(self, doc):
        fake = types.SimpleNamespace(returncode=0, stdout=json.dumps(doc))
        original = ts.subprocess.run
        ts.subprocess.run = lambda *a, **k: fake
        self.addCleanup(lambda: setattr(ts.subprocess, "run", original))

    def test_a_registry_manager_on_a_goal_counts(self):
        self._with_probe({"sessions": [
            {"session_id": "s1", "role": "manager", "subject": "Alpha"},
            {"session_id": "s2", "role": "manager", "subject": "Beta"},
            {"session_id": "s3", "role": "worker", "subject": "Alpha"},
        ]})
        self.assertEqual({"s1"}, ts.fleet_goal_managers(["Alpha"]))

    def test_a_worker_on_a_goal_does_not_count(self):
        self._with_probe({"sessions": [
            {"session_id": "s3", "role": "worker", "subject": "Alpha"},
        ]})
        self.assertEqual(set(), ts.fleet_goal_managers(["Alpha"]))

    def test_the_page_id_and_the_registry_id_are_counted_once_each(self):
        with tempfile.TemporaryDirectory() as tmp:
            vault = pathlib.Path(tmp)
            (vault / "24 Goals").mkdir()
            (vault / "24 Goals" / "Alpha.md").write_text(
                "---\nmanager_session_id: page-sid\n---\n", encoding="utf-8"
            )
            self._with_probe({"sessions": [
                {"session_id": "page-sid", "role": "manager", "subject": "Alpha"},
                {"session_id": "registry-sid", "role": "manager", "subject": "Alpha"},
            ]})
            self.assertEqual(2, ts.live_goal_managers(str(vault), "", ["Alpha"]))


if __name__ == "__main__":
    unittest.main()
