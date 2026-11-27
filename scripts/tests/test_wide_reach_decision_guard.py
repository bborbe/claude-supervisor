#!/usr/bin/env python3
"""Tests for `wide-reach-decision-guard.py`.

Each case names what must stay green as well as what must go red: a guard that
refused everything would satisfy every refusal assertion below and censor every
ordinary card, so the ALLOW cases are the load-bearing half.
"""

from __future__ import annotations

import importlib.util
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
MODULE = os.path.join(os.path.dirname(HERE), "wide-reach-decision-guard.py")


def _load():
    spec = importlib.util.spec_from_file_location("wide_reach_guard", MODULE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


guard = _load()


class ClassifyCard(unittest.TestCase):
    def test_no_options_is_announcement(self):
        self.assertEqual(guard.classify_card([]), "announcement")
        self.assertEqual(guard.classify_card(None), "announcement")

    def test_pure_ack_options_are_announcement(self):
        options = [{"label": "ok"}, {"label": "later"}, {"label": "noted."}]
        self.assertEqual(guard.classify_card(options), "announcement")

    def test_one_real_option_makes_it_deciding(self):
        options = [{"label": "ok"}, {"label": "weekly candles"}]
        self.assertEqual(guard.classify_card(options), "deciding")

    def test_yes_no_is_deciding(self):
        # A yes/no answer to a real question IS the decision. If this read as a
        # pure ack, the guard would wave through exactly the card it exists for.
        self.assertEqual(guard.classify_card([{"label": "yes"}]), "deciding")
        self.assertEqual(guard.classify_card([{"label": "no"}]), "deciding")

    def test_bare_string_options_accepted(self):
        self.assertEqual(guard.classify_card(["ok"]), "announcement")
        self.assertEqual(guard.classify_card(["ship it"]), "deciding")


class PendingMarks(unittest.TestCase):
    def test_frontmatter_flag(self):
        marks = guard.pending_decision_marks({"wide_reach_decision": "pending"}, "")
        self.assertIn("wide_reach_decision: pending", marks)

    def test_decision_block(self):
        marks = guard.pending_decision_marks({}, "intro\n\n# Decision\n\nstuff")
        self.assertIn("# Decision block", marks)

    def test_decision_block_requires_exact_heading(self):
        # `# Decisions` (plural) and `# Decision log` must not match.
        self.assertEqual(guard.pending_decision_marks({}, "# Decisions\n"), [])
        self.assertEqual(guard.pending_decision_marks({}, "# Decision log\n"), [])

    def test_pending_records(self):
        marks = guard.pending_decision_marks({"_pending_records": ["TDR x"]}, "")
        self.assertEqual(marks, ["Decision Record TDR x (proposed)"])

    def test_no_marks(self):
        self.assertEqual(guard.pending_decision_marks({}, "just prose"), [])


class RecordPending(unittest.TestCase):
    def test_proposed_is_pending(self):
        self.assertTrue(guard.decision_record_is_pending({}))

    def test_reviewed_is_not_pending(self):
        self.assertFalse(guard.decision_record_is_pending({"reviewed": "true"}))

    def test_accepted_is_not_pending(self):
        self.assertFalse(guard.decision_record_is_pending({"status": "accepted"}))


class Refusal(unittest.TestCase):
    def test_deciding_card_on_pending_task_is_refused(self):
        out = guard.refusal_for(
            [{"label": "weekly candles"}],
            {"wide_reach_decision": "pending"},
            "")
        self.assertIsNotNone(out)
        self.assertIn("wide-reach decision pending", out)

    def test_deciding_card_on_unrelated_task_is_allowed(self):
        # The guard is not a general card censor: an ordinary question stands.
        self.assertIsNone(
            guard.refusal_for([{"label": "weekly candles"}], {}, "prose"))

    def test_announcement_card_on_pending_task_is_allowed(self):
        # The announcement card is the sanctioned channel — never refused.
        self.assertIsNone(
            guard.refusal_for([{"label": "ok"}], {"wide_reach_decision": "pending"}, ""))

    def test_announcement_card_with_no_options_is_allowed(self):
        self.assertIsNone(
            guard.refusal_for([], {"wide_reach_decision": "pending"}, ""))


class LoadSessionTask(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.vault = os.path.join(self.root, "private-personal")
        os.makedirs(os.path.join(self.vault, "25 Tasks"))
        os.makedirs(os.path.join(self.vault, "40 Decisions"))

    def _task(self, name, front, body=""):
        path = os.path.join(self.vault, "25 Tasks", f"{name}.md")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("---\n" + front + "\n---\n" + body)

    def _decision(self, name, front):
        path = os.path.join(self.vault, "40 Decisions", f"{name}.md")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("---\n" + front + "\n---\n")

    def test_resolves_task_by_session_id(self):
        self._task("A Task",
                   "claude_session_id: abc123\nstatus: in_progress\nwide_reach_decision: pending")
        front, _ = guard.load_session_task("abc123", self.root)
        self.assertIsNotNone(front)
        self.assertEqual(front["_name"], "A Task")

    def test_cross_vault_record_join(self):
        # The join is `related_task`, never a wikilink: the record lives in a
        # different vault from the task and still resolves.
        self._task("A Task", "claude_session_id: abc123\nstatus: in_progress")
        self._decision("TDR 1", "related_task: '[[A Task]]'")
        front, _ = guard.load_session_task("abc123", self.root)
        self.assertEqual(front["_pending_records"], ["TDR 1"])

    def test_accepted_record_does_not_join(self):
        self._task("A Task", "claude_session_id: abc123\nstatus: in_progress")
        self._decision("TDR 1", "related_task: '[[A Task]]'\nstatus: accepted")
        front, _ = guard.load_session_task("abc123", self.root)
        self.assertEqual(front["_pending_records"], [])

    def test_unknown_session_fails_open(self):
        self._task("A Task", "claude_session_id: other\nstatus: in_progress")
        front, body = guard.load_session_task("abc123", self.root)
        self.assertIsNone(front)
        self.assertIsNone(body)

    def test_closed_task_is_ignored(self):
        self._task("A Task", "claude_session_id: abc123\nstatus: completed")
        front, _ = guard.load_session_task("abc123", self.root)
        self.assertIsNone(front)


if __name__ == "__main__":
    unittest.main()
