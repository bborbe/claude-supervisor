"""Tests for scripts/crashed-managers.py — the uncovered live-but-lapsed card.

The predicate has three conditions and every one of them is a way the card must NOT fire, so
each gets a case. `cadence_state` and `session_verdict` are stubbed: both shell out to another
script, and this file is about the *decision*, not about re-testing those two instruments.
"""

import importlib.util
import os
import tempfile
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_spec = importlib.util.spec_from_file_location(
    "crashed_managers", os.path.join(REPO, "scripts", "crashed-managers.py")
)
cm = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cm)


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


class EvaluateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.vault = os.path.join(self.tmp.name, "vault")
        self.goals = os.path.join(self.vault, "24 Goals")
        self.topics = os.path.join(self.vault, "23 Topics")
        os.makedirs(self.goals)
        os.makedirs(self.topics)
        self._cadence = cm.cadence_state
        self._verdict = cm.session_verdict

    def tearDown(self):
        cm.cadence_state = self._cadence
        cm.session_verdict = self._verdict
        self.tmp.cleanup()

    def goal(self, name, sid="aaaa1111-0000-0000-0000-000000000000"):
        write(os.path.join(self.goals, f"{name}.md"),
              f"---\nmanager_session_id: {sid}\npage_type: goal\n---\n\n# Impact\n")

    def topic(self, name, goals, sid="bbbb2222-0000-0000-0000-000000000000"):
        body = "\n".join(f"- [ ] [[{g}]]" for g in goals)
        write(os.path.join(self.topics, f"{name}.md"),
              f"---\nmanager_session_id: {sid}\npage_type: topic\n---\n\n# Goals\n\n{body}\n")

    def stub(self, state, verdict):
        cm.cadence_state = lambda name, branch: state
        cm.session_verdict = lambda sid: verdict

    def test_live_but_lapsed_and_uncovered_cards(self):
        self.goal("Lonely Goal")
        self.stub("STALE", "LIVE")
        row = cm.evaluate(self.vault, "goal", "lonely-goal")
        self.assertTrue(row["card"], row["reason"])
        self.assertEqual(row["subject"], "Lonely Goal")

    def test_covered_by_a_live_topic_manager_does_not_card(self):
        self.goal("Covered Goal")
        self.topic("Some Topic", ["Covered Goal"])
        self.stub("STALE", "LIVE")
        row = cm.evaluate(self.vault, "goal", "covered-goal")
        self.assertFalse(row["card"])
        self.assertIn("covered", row["reason"])

    def test_a_dead_topic_manager_leaves_the_goal_uncovered(self):
        self.goal("Orphan Goal")            # sid aaaa1111-… (LIVE)
        self.topic("Dead Topic", ["Orphan Goal"])   # sid bbbb2222-… (ABSENT)
        cm.cadence_state = lambda name, branch: "STALE"
        cm.session_verdict = lambda sid: "LIVE" if sid.startswith("aaaa") else "ABSENT"
        row = cm.evaluate(self.vault, "goal", "orphan-goal")
        self.assertTrue(row["card"], row["reason"])

    def test_stopped_is_a_stand_down_never_a_crash(self):
        self.goal("Stood Down")
        self.stub("STOPPED", "LIVE")
        row = cm.evaluate(self.vault, "goal", "stood-down")
        self.assertFalse(row["card"])
        self.assertIn("STOPPED", row["reason"])

    def test_an_absent_session_is_an_abandoned_record(self):
        self.goal("Abandoned")
        self.stub("STALE", "ABSENT")
        row = cm.evaluate(self.vault, "goal", "abandoned")
        self.assertFalse(row["card"])
        self.assertIn("ABSENT", row["reason"])

    def test_an_undecidable_probe_is_never_a_card(self):
        self.goal("Undecidable")
        self.stub("STALE", "UNKNOWN")
        row = cm.evaluate(self.vault, "goal", "undecidable")
        self.assertFalse(row["card"])
        self.assertIn("UNKNOWN", row["reason"])

    def test_a_page_without_a_session_id_is_unverifiable(self):
        write(os.path.join(self.goals, "No Sid.md"), "---\npage_type: goal\n---\n")
        self.stub("STALE", "LIVE")
        row = cm.evaluate(self.vault, "goal", "no-sid")
        self.assertFalse(row["card"])
        self.assertIn("manager_session_id", row["reason"])

    def test_a_record_with_no_page_is_abandoned(self):
        self.stub("STALE", "LIVE")
        row = cm.evaluate(self.vault, "goal", "no-such-goal")
        self.assertFalse(row["card"])
        self.assertIn("abandoned record", row["reason"])

    def test_a_topic_manager_is_never_covered(self):
        write(os.path.join(self.topics, "Lapsed Topic.md"),
              "---\nmanager_session_id: cccc3333-0000-0000-0000-000000000000\n---\n\n# Goals\n")
        self.stub("STALE", "LIVE")
        row = cm.evaluate(self.vault, "topic", "lapsed-topic")
        self.assertTrue(row["card"], row["reason"])


class SubjectsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def test_legacy_records_are_counted_and_never_attributed(self):
        for name in ("goal--a-goal.cadence", "topic--a-topic.cadence", "flat-legacy.cadence"):
            write(os.path.join(self.dir, name), "900\n")
        pairs, legacy = cm.subjects(self.dir)
        self.assertEqual(pairs, [("goal", "a-goal"), ("topic", "a-topic")])
        self.assertEqual(legacy, 1)

    def test_an_unreadable_dir_refuses_rather_than_reads_empty(self):
        with self.assertRaises(cm.Refused):
            cm.subjects(os.path.join(self.dir, "does-not-exist"))


if __name__ == "__main__":
    unittest.main()
