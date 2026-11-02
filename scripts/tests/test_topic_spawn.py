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
flag: {flag}
flag_set_by: {setter}
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

    def goal(self, name, phase, priority, sid="", flag="true", setter="operator"):
        """Write a goal page. Flagged by the operator by default — opening a manager is
        the operator's act (rule 4), so an unflagged goal is the exception under test."""
        (self.vault / "24 Goals" / f"{name}.md").write_text(
            GOAL.format(phase=phase, priority=priority, sid=sid, flag=flag, setter=setter),
            encoding="utf-8",
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

    # --- rule 4: opening a goal manager is the operator's act ---------------------

    def test_an_unflagged_goal_is_refused(self):
        """Operator ruling 2026-10-30: "we dont want to auto open topic-manager,
        goal-manager ... but flag are fine". Without this the gate lives only in
        `commands/topic-drive.md`, and a documented mandate is not an executed one."""
        self.goal("Delta", "planning", 4, flag="false")
        rc, out = self.run_gate("--goal", "Delta")
        self.assertEqual(1, rc)
        self.assertEqual("goal-unflagged", out[0])

    def test_a_goal_flagged_by_a_non_operator_still_passes(self):
        """Provenance is NOT read. Operator ruling 2026-10-30: "in the end it should not
        matter how set the flag … if flag, should be open if claude session limit allow it".
        Reading `flag_set_by` here again would silently reinstate the gate it removed."""
        self.goal("Delta", "planning", 4, setter="agent")
        rc, out = self.run_gate("--goal", "Delta")
        self.assertEqual(0, rc)
        self.assertEqual("spawn-ok: Delta", out[0])

    def test_a_goal_flagged_with_no_setter_still_passes(self):
        """A bare `flag: true` is a flag. The setter is a note, not a condition."""
        page = self.vault / "24 Goals" / "Delta.md"
        page.write_text(
            page.read_text(encoding="utf-8").replace("flag_set_by: operator\n", ""),
            encoding="utf-8",
        )
        rc, out = self.run_gate("--goal", "Delta")
        self.assertEqual(0, rc)
        self.assertEqual("spawn-ok: Delta", out[0])

    def test_a_legacy_flag_still_passes(self):
        """193 rows were flagged before the field was written; they keep working."""
        self.goal("Delta", "planning", 4, setter="legacy")
        rc, out = self.run_gate("--goal", "Delta")
        self.assertEqual(0, rc)
        self.assertEqual("spawn-ok: Delta", out[0])

    def test_an_unflagged_goal_is_ranked_but_never_takes_a_slot(self):
        """The rank path is the second door into the same open, so it carries the gate too.

        ⚠️ Ranked, NOT dropped: `commands/topic-drive.md` § Start promises the operator the
        row *with its rank*, and a row that vanishes from a sweep is indistinguishable from
        a row that got fixed. The `unflagged` slot keeps it reported and unstartable.
        """
        self.goal("Beta", "execution", 1, flag="false")
        rows = ts.rank(str(self.vault), TOPIC, ["Alpha", "Beta", "Gamma", "Delta"], 3)
        by_goal = {r["goal"]: r for r in rows}
        self.assertIn("Beta", by_goal)
        self.assertEqual("unflagged", by_goal["Beta"]["slot"])
        self.assertEqual(3, sum(1 for r in rows if r["slot"] == "start"))

    def test_a_yes_flag_is_accepted(self):
        """YAML spells a boolean three ways, and `operator_pin` beside this gate already
        accepts `yes` — refusing it here would hold a goal the operator did flag."""
        self.goal("Delta", "planning", 4, flag="yes")
        rc, out = self.run_gate("--goal", "Delta")
        self.assertEqual(0, rc)
        self.assertEqual("spawn-ok: Delta", out[0])

    # --- the topic tier's own gate (--topic-open) ------------------------------

    def _flag_topic(self, setter="operator"):
        page = self.vault / "23 Topics" / "A Topic.md"
        page.write_text(
            page.read_text(encoding="utf-8").replace(
                "status: in_progress",
                f"status: in_progress\nflag: true\nflag_set_by: {setter}",
            ),
            encoding="utf-8",
        )

    def test_an_unflagged_topic_is_refused(self):
        """Without this gate a flag on a topic page is read by nothing — `open.md`'s topic
        branch gates on `phase: todo` alone, so rule 4 had no leg one tier up."""
        rc, out = self.run_gate("--topic-open")
        self.assertEqual(1, rc)
        self.assertEqual("topic-unflagged", out[0])

    def test_a_flagged_topic_opens(self):
        self._flag_topic()
        rc, out = self.run_gate("--topic-open")
        self.assertEqual(0, rc)
        self.assertEqual("open-ok: A Topic", out[0])

    def test_a_topic_flagged_by_an_agent_still_opens(self):
        self._flag_topic(setter="agent")
        rc, out = self.run_gate("--topic-open")
        self.assertEqual(0, rc)
        self.assertEqual("open-ok: A Topic", out[0])

    def test_a_flagged_topic_is_refused_at_the_fleet_ceiling(self):
        """The cap is the ONLY bound once the flag is present — that is the ruling."""
        self._flag_topic()
        ts.fleet_manager_count = lambda: 10
        ts.fleet_manager_ceiling = lambda: 10
        rc, out = self.run_gate("--topic-open")
        self.assertEqual(1, rc)
        self.assertEqual("manager-cap", out[0])

    # --- the flag gate's position among the other gates --------------------------

    def test_an_unflagged_todo_goal_names_the_flag_not_the_phase(self):
        """5b fires before the phase gate, so the refusal names the lever the operator has
        to pull first. Both are true of this row; only one is the next act."""
        self.goal("Delta", "todo", 4, flag="false")
        rc, out = self.run_gate("--goal", "Delta")
        self.assertEqual(1, rc)
        self.assertEqual("goal-unflagged", out[0])

    def test_a_terminal_goal_is_refused_as_terminal_even_when_unflagged(self):
        """5a fires before 5b: a closed goal is not awaiting a flag, and telling the
        operator to flag it would invite reopening finished work."""
        self.goal("Beta", "execution", 1, flag="false")
        page = self.vault / "24 Goals" / "Beta.md"
        page.write_text(
            page.read_text(encoding="utf-8").replace("status: in_progress", "status: completed"),
            encoding="utf-8",
        )
        rc, out = self.run_gate("--goal", "Beta")
        self.assertEqual(1, rc)
        self.assertEqual("goal-terminal", out[0])

    def test_a_held_goal_still_passes(self):
        """`hold` is a pause, not a closure — `TERMINAL_STATUSES` is `(completed, aborted)`,
        and the module docstring says so. Without this positive control, adding `hold` to
        that tuple would silently contradict the documentation with no failing test."""
        self.goal("Beta", "execution", 1)
        page = self.vault / "24 Goals" / "Beta.md"
        page.write_text(
            page.read_text(encoding="utf-8").replace("status: in_progress", "status: hold"),
            encoding="utf-8",
        )
        rc, out = self.run_gate("--goal", "Beta")
        self.assertEqual(0, rc)
        self.assertEqual("spawn-ok: Beta", out[0])

    def test_the_flag_gate_precedes_the_cap(self):
        """At the cap an unflagged goal must be told to wait for the flag, not for a slot —
        otherwise the operator frees a slot and gets the identical refusal back."""
        ts.live_goal_managers = lambda vault, topics_dir, goals: 3
        self.goal("Alpha", "planning", 2, flag="false")
        rc, out = self.run_gate("--goal", "Alpha")
        self.assertEqual(1, rc)
        self.assertEqual("goal-unflagged", out[0])

    def test_the_absent_phase_exemption_does_not_rescue_an_unflagged_goal(self):
        """The exemption is about `phase` alone, and must not read as a blanket pass — an
        unflagged goal with no phase is still an open rule 4 forbids."""
        (self.vault / "24 Goals" / "Delta.md").write_text(
            "---\npage_type: goal\nstatus: in_progress\npriority: 4\n"
            "flag: false\nflag_set_by: operator\n---\n",
            encoding="utf-8",
        )
        rc, out = self.run_gate("--goal", "Delta")
        self.assertEqual(1, rc)
        self.assertEqual("goal-unflagged", out[0])

    def test_a_goal_with_no_phase_is_exempt_and_starts(self):
        """F1 — an ABSENT phase is exempt, not refused.

        The accepted design leaves the unphased goals on today's behaviour, and the
        topic tier is their only start path: refusing them made 22 of one live
        topic's 25 goals unstartable the moment the split shipped.
        """
        (self.vault / "24 Goals" / "Delta.md").write_text(
            "---\npage_type: goal\nstatus: in_progress\npriority: 4\nflag: true\nflag_set_by: operator\n---\n",
            encoding="utf-8",
        )
        rc, out = self.run_gate("--goal", "Delta")
        self.assertEqual(0, rc)
        self.assertEqual("spawn-ok: Delta", out[0])

    def test_a_terminal_goal_is_refused(self):
        """Closure leaves `phase: execution` in place, so a phase-only gate keeps
        offering a goal the same tick has just closed — the "finished goal silently
        restarts" failure. The refusal names the real cause."""
        self.goal("Beta", "execution", 1)
        page = self.vault / "24 Goals" / "Beta.md"
        page.write_text(
            page.read_text(encoding="utf-8").replace("status: in_progress", "status: completed"),
            encoding="utf-8",
        )
        rc, out = self.run_gate("--goal", "Beta")
        self.assertEqual(1, rc)
        self.assertEqual("goal-terminal", out[0])

    def test_a_terminal_goal_is_not_ranked(self):
        page = self.vault / "24 Goals" / "Beta.md"
        page.write_text(
            page.read_text(encoding="utf-8").replace("status: in_progress", "status: completed"),
            encoding="utf-8",
        )
        rows = ts.rank(str(self.vault), TOPIC, ["Alpha", "Beta", "Gamma", "Delta"], 3)
        self.assertNotIn("Beta", [r["goal"] for r in rows])

    def test_a_goal_with_no_phase_is_ranked_not_dropped(self):
        (self.vault / "24 Goals" / "Delta.md").write_text(
            "---\npage_type: goal\nstatus: in_progress\npriority: 4\nflag: true\nflag_set_by: operator\n---\n",
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

    def test_the_rank_header_counts_only_the_flagged_rows(self):
        """`<n>` is the flagged count, never `len(rows)` — an unflagged row is listed but can
        never take a slot, so counting it would report work the topic cannot start."""
        self.goal("Delta", "execution", 4, flag="false")
        rc, out = self.run_gate("--rank")
        self.assertEqual(0, rc)
        self.assertEqual("rank: 3 startable of 4 declared · N = 3", out[0])

    def test_the_rank_output_prints_the_unflagged_slot(self):
        """The dict is asserted elsewhere; this pins the PRINTED line, which is what the
        agent renders — a slot the report cannot show is a row the operator never sees."""
        self.goal("Delta", "execution", 4, flag="false")
        rc, out = self.run_gate("--rank")
        line = next((l for l in out[1:] if "Delta" in l), None)
        self.assertIsNotNone(line, f"no Delta row in the ranking output: {out}")
        self.assertIn("unflagged", line)

    def test_a_yes_flag_is_ranked(self):
        """The truthy set is shared by both paths, so the rank path must accept it too."""
        self.goal("Delta", "execution", 4, flag="yes")
        rc, out = self.run_gate("--rank")
        self.assertEqual("rank: 4 startable of 4 declared · N = 3", out[0])

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

    def test_a_mixed_goal_page_resolves_claude_session_id_first(self):
        # A re-armed legacy page carries both keys; the surviving one is the live
        # manager. Reading the retired key first would count zero live managers.
        with tempfile.TemporaryDirectory() as tmp:
            vault = pathlib.Path(tmp)
            (vault / "24 Goals").mkdir()
            (vault / "24 Goals" / "Alpha.md").write_text(
                "---\nmanager_session_id: old-sid\nclaude_session_id: new-sid\n---\n",
                encoding="utf-8",
            )
            self._with_probe({"sessions": [
                {"session_id": "new-sid", "role": "manager", "subject": "Alpha"},
            ]})
            self.assertEqual(1, ts.live_goal_managers(str(vault), "", ["Alpha"]))

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
