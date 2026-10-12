#!/usr/bin/env python3
"""Tests for scripts/propose-managers.py.

Each case guards a defect the card has actually shipped or was filed against:

  * the threshold is asserted in BOTH directions — a goal with exactly the threshold
    qualifies and one below it does not, because a one-sided fixture passes on a script
    that proposes every goal.
  * the slot gate emits at most `free` cards and emits ZERO at the cap — the operator's
    own acceptance criterion, and the half a "it posted something" test cannot see.
  * ranking is priority then approved-count, asserted against a fixture where the two
    disagree, so a script that sorts on either alone fails.
  * the dedup key carries no timestamp — a key that moves on a re-post of an unchanged
    goal defeats the store's open-scoped suppression and asks the operator twice
    (measured 2026-10-02 on the sibling under-target card).
  * an unreadable board is UNKNOWN, never zero — the `None`-not-`0` rule from
    `docs/pane-reads.md`. An empty fleet and a failed read must not render the same, and
    only one of them posts past a limit.
  * frontmatter-only reading — a whole-file scan matches keys quoted in task-body prose
    and qualifies a goal on the strength of a sentence *about* approval.

Run: python3 -m unittest discover -s scripts/tests -v
"""

import importlib.util
import os
import tempfile
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_SCRIPT = os.path.join(os.path.dirname(_HERE), "propose-managers.py")

_spec = importlib.util.spec_from_file_location("propose_managers", _SCRIPT)
pm = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pm)

VAULT = {"name": "private-personal", "tasks_dir": "/nonexistent/tasks", "goals_dir": None}
OTHER = {"name": "seibert-brogrammers", "tasks_dir": "/nonexistent/tasks", "goals_dir": None}


def row(goal, vault=VAULT, title="a task"):
    return {"goal": goal, "vault": vault, "title": title}


def task(phase="planning", status="in_progress", approved="2026-10-12T09:00:00+02:00",
         goals=("[[Some Goal]]",), body=""):
    """One task file's text, in the vault's frontmatter shape."""
    lines = ["---"]
    if approved:
        lines.append(f"approved_at: {approved}")
    lines.append(f"phase: {phase}")
    lines.append(f"status: {status}")
    lines.append("goals:")
    for goal in goals:
        lines.append(f"    - '{goal}'")
    lines.append("---")
    lines.append("Tags: [[Task]]")
    lines.append("")
    lines.append(body)
    return "\n".join(lines)


class QualifyTest(unittest.TestCase):
    def test_goal_at_threshold_qualifies(self):
        rows = [row("G", title="t1"), row("G", title="t2")]
        self.assertEqual([c["goal"] for c in pm.qualify(rows, [])], ["G"])

    def test_goal_below_threshold_does_not(self):
        """The other direction: one approved task is a row to open, not a subject."""
        self.assertEqual(pm.qualify([row("G", title="t1")], []), [])

    def test_goal_with_a_live_manager_does_not_qualify(self):
        rows = [row("G", title="t1"), row("G", title="t2")]
        self.assertEqual(pm.qualify(rows, ["G"]), [])

    def test_manager_suffix_and_case_both_match(self):
        rows = [row("Some Goal", title="t1"), row("Some Goal", title="t2")]
        self.assertEqual(pm.qualify(rows, ["some goal manager"]), [])

    def test_same_title_in_two_vaults_is_two_goals(self):
        """Goal titles are not namespaced across vaults — they must never be summed."""
        rows = [row("G", vault=VAULT, title="t1"), row("G", vault=OTHER, title="t1")]
        self.assertEqual(pm.qualify(rows, []), [])

    def test_ranking_is_priority_then_count(self):
        """Priority wins over count, so a script sorting on count alone fails here."""
        rows = [
            # priority 3, four approved tasks — ranks last
            row("Low", title="a"), row("Low", title="b"),
            row("Low", title="c"), row("Low", title="d"),
            # priority 1, two approved tasks — ranks first
            row("High", title="e"), row("High", title="f"),
        ]
        priorities = {"Low": 3, "High": 1}
        original = pm.goal_priority
        pm.goal_priority = lambda _dir, name: priorities.get(name)
        try:
            self.assertEqual([c["goal"] for c in pm.qualify(rows, [])], ["High", "Low"])
        finally:
            pm.goal_priority = original

    def test_equal_priority_ranks_the_most_approved_work_first(self):
        rows = [row("Few", title="a"), row("Few", title="b"),
                row("Many", title="c"), row("Many", title="d"), row("Many", title="e")]
        original = pm.goal_priority
        pm.goal_priority = lambda _dir, _name: 2
        try:
            self.assertEqual([c["goal"] for c in pm.qualify(rows, [])], ["Many", "Few"])
        finally:
            pm.goal_priority = original

    def test_unset_priority_ranks_last(self):
        rows = [row("Unset", title="a"), row("Unset", title="b"),
                row("Set", title="c"), row("Set", title="d")]
        original = pm.goal_priority
        pm.goal_priority = lambda _dir, name: 3 if name == "Set" else None
        try:
            self.assertEqual([c["goal"] for c in pm.qualify(rows, [])], ["Set", "Unset"])
        finally:
            pm.goal_priority = original


class SlotGateTest(unittest.TestCase):
    """The operator's own acceptance criterion: at most `free` cards, zero at the cap."""

    def candidates(self, count):
        return [{"goal": f"G{n}", "vault": "v", "approved": 2, "tasks": [],
                 "priority": n} for n in range(count)]

    def test_three_qualifying_goals_and_two_free_slots_emit_exactly_two(self):
        cards = pm.cap_cards(self.candidates(3), 2)
        self.assertEqual([c["goal"] for c in cards], ["G0", "G1"])

    def test_rank_order_is_preserved_by_the_cap(self):
        """The cap is a slice, never a re-rank — the ranking is `qualify`'s alone."""
        self.assertEqual([c["goal"] for c in pm.cap_cards(self.candidates(5), 3)],
                         ["G0", "G1", "G2"])

    def test_at_the_cap_zero_cards(self):
        self.assertEqual(pm.cap_cards(self.candidates(4), 0), [])

    def test_negative_free_is_zero_cards_not_a_negative_slice(self):
        """A cap read below the live count must not slice from the end."""
        self.assertEqual(pm.cap_cards(self.candidates(4), -2), [])

    def test_more_slots_than_goals_emits_every_goal(self):
        self.assertEqual(len(pm.cap_cards(self.candidates(2), 9)), 2)


class LiveManagersTest(unittest.TestCase):
    def board(self, sessions):
        import json
        return json.dumps({"sessions": sessions})

    def test_manager_role_counts_and_fleet_manager_is_excluded(self):
        labels = pm.live_manager_labels(self.board([
            {"label": "Fleet Manager", "role": "manager"},
            {"label": "Cluster Workers Manager", "role": "manager"},
            {"label": "some worker", "role": "worker"},
        ]))
        self.assertEqual(labels, ["Cluster Workers Manager"])

    def test_unreadable_board_is_unknown_not_empty(self):
        """`None`, never `[]` — an empty fleet is the answer that posts past a limit."""
        for bad in ("", "not json", "{}", '{"sessions": null}'):
            self.assertIsNone(pm.live_manager_labels(bad), bad)

    def test_a_readable_board_with_no_managers_is_empty_not_unknown(self):
        self.assertEqual(pm.live_manager_labels(self.board([])), [])


class DedupKeyTest(unittest.TestCase):
    def test_key_is_the_goal_name_alone(self):
        self.assertEqual(pm.dedup_key("Some Goal"), "fleet-propose-manager:Some Goal")

    def test_key_carries_no_timestamp(self):
        """A key that moves on an unchanged goal re-asks the operator (2026-10-02)."""
        self.assertEqual(pm.dedup_key("Some Goal"), pm.dedup_key("Some Goal"))
        self.assertNotRegex(pm.dedup_key("Some Goal"), r"\d{4}-\d{2}-\d{2}")


class ScanTest(unittest.TestCase):
    def scan_dir(self, files):
        """Write `{filename: text}` into a temp tasks dir and scan it."""
        with tempfile.TemporaryDirectory() as tmp:
            for name, text in files.items():
                with open(os.path.join(tmp, name), "w", encoding="utf-8") as handle:
                    handle.write(text)
            return pm.scan([{"name": "v", "tasks_dir": tmp, "goals_dir": None}])

    def test_approved_live_phase_task_is_a_row(self):
        rows = self.scan_dir({"t.md": task()})
        self.assertEqual([r["goal"] for r in rows], ["Some Goal"])

    def test_phase_todo_is_not_approved_work(self):
        self.assertEqual(self.scan_dir({"t.md": task(phase="todo")}), [])

    def test_terminal_status_is_not_approved_work(self):
        for status in ("completed", "aborted", "hold", "backlog"):
            self.assertEqual(self.scan_dir({"t.md": task(status=status)}), [], status)

    def test_missing_approved_at_is_not_approved(self):
        self.assertEqual(self.scan_dir({"t.md": task(approved=None)}), [])

    def test_prose_about_approval_does_not_approve_a_task(self):
        """Frontmatter only: a sentence *about* approval is not an approval record."""
        body = "This task was approved_at some point but the field is absent."
        self.assertEqual(self.scan_dir({"t.md": task(approved=None, body=body)}), [])

    def test_a_task_naming_two_goals_counts_for_both(self):
        rows = self.scan_dir({"t.md": task(goals=("[[A]]", "[[B]]"))})
        self.assertEqual(sorted(r["goal"] for r in rows), ["A", "B"])

    def test_goal_link_is_unwrapped(self):
        rows = self.scan_dir({"t.md": task(goals=("[[Some Goal]]",))})
        self.assertEqual(rows[0]["goal"], "Some Goal")


class CardTest(unittest.TestCase):
    def test_card_carries_both_option_labels_and_the_recommendation(self):
        candidate = {"goal": "G", "vault": "v", "approved": 2, "tasks": ["a", "b"], "priority": 1}
        card = pm.card_for(candidate, free=2, cap=5)
        self.assertEqual(card["options"], ["start manager", "not now"])
        self.assertEqual(card["recommend"], "start manager")
        self.assertEqual(card["dedup_key"], "fleet-propose-manager:G")

    def test_payload_names_the_goal_the_count_and_the_absence_of_a_manager(self):
        candidate = {"goal": "G", "vault": "v", "approved": 4, "tasks": [], "priority": None}
        payload = pm.card_for(candidate, free=1, cap=5)["payload"]
        self.assertIn('"G"', payload)
        self.assertIn("4 approved", payload)
        self.assertIn("no live manager", payload)


if __name__ == "__main__":
    unittest.main()
