#!/usr/bin/env python3
"""Tests for `task-decision-resolver.py`.

The ALLOW cases are load-bearing: a resolver that returned `pending` for
everything would satisfy every refuse assertion and block every task close.
"""

from __future__ import annotations

import importlib.util
import os
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
MODULE = os.path.join(os.path.dirname(HERE), "task-decision-resolver.py")


def _load():
    spec = importlib.util.spec_from_file_location("tdr", MODULE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


tdr = _load()


class Fixture(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.vault = os.path.join(self.root, "private-personal")
        os.makedirs(os.path.join(self.vault, "25 Tasks"))
        os.makedirs(os.path.join(self.vault, "40 Decisions"))
        self.task = os.path.join(self.vault, "25 Tasks", "A Task.md")

    def task_file(self, front="", body="", name="A Task"):
        path = os.path.join(self.vault, "25 Tasks", f"{name}.md")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("---\n" + front + "\n---\n" + body)
        return path

    def record(self, front="", body="decision body", name="TDR 1"):
        path = os.path.join(self.vault, "40 Decisions", f"{name}.md")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("---\n" + front + "\n---\n" + body)
        return path


class BodyHash(unittest.TestCase):
    def test_ignores_frontmatter(self):
        a = "---\nstatus: proposed\n---\nthe decision"
        b = "---\nstatus: accepted\n---\nthe decision"
        self.assertEqual(tdr.body_hash(a), tdr.body_hash(b))

    def test_changes_on_body_edit(self):
        a = "---\nx: 1\n---\nthe decision"
        b = "---\nx: 1\n---\nthe decision, edited"
        self.assertNotEqual(tdr.body_hash(a), tdr.body_hash(b))


class Resolve(Fixture):
    def test_pending_when_record_has_no_ack(self):
        task = self.task_file(front="task_identifier: uuid-1")
        self.record(front="related_task: '[[A Task]]'")
        self.assertEqual(tdr.resolve(task, self.root)["status"], "pending")

    def test_accepted_when_acked_revision_matches(self):
        task = self.task_file(front="task_identifier: uuid-1")
        path = self.record(front="related_task: '[[A Task]]'")
        with open(path, encoding="utf-8") as handle:
            digest = tdr.body_hash(handle.read())
        self.record(front=f"related_task: '[[A Task]]'\nacked_revision: {digest}")
        self.assertEqual(tdr.resolve(task, self.root)["status"], "accepted")

    def test_stale_ack_after_body_edit(self):
        task = self.task_file(front="task_identifier: uuid-1")
        path = self.record(front="related_task: '[[A Task]]'")
        with open(path, encoding="utf-8") as handle:
            digest = tdr.body_hash(handle.read())
        # Ack written, then the body edited: the digest no longer matches.
        self.record(front=f"related_task: '[[A Task]]'\nacked_revision: {digest}",
                    body="decision body, edited after the ack")
        self.assertEqual(tdr.resolve(task, self.root)["status"], "pending")

    def test_none_when_no_record(self):
        task = self.task_file(front="task_identifier: uuid-1")
        self.assertEqual(tdr.resolve(task, self.root)["status"], "none")

    def test_uuid_join_beats_title(self):
        # The record names the UUID, not the title — still resolves.
        task = self.task_file(front="task_identifier: uuid-9")
        self.record(front="related_task: 'uuid-9'")
        self.assertEqual(tdr.resolve(task, self.root)["status"], "pending")

    def test_duplicate_title_does_not_match(self):
        task = self.task_file(front="task_identifier: uuid-1")
        # A second vault holding the same task title — the title link is now
        # ambiguous, so it must not be used as a join.
        other = os.path.join(self.root, "other-vault", "25 Tasks")
        os.makedirs(other)
        with open(os.path.join(other, "A Task.md"), "w", encoding="utf-8") as handle:
            handle.write("---\ntask_identifier: uuid-2\n---\n")
        self.record(front="related_task: '[[A Task]]'")
        self.assertEqual(tdr.resolve(task, self.root)["status"], "none")

    def test_required_decision_naming_missing_record_is_unresolvable(self):
        task = self.task_file(front="required_decision: TDR 404")
        self.record(front="related_task: '[[A Task]]'", name="TDR 1")
        self.assertEqual(tdr.resolve(task, self.root)["status"], "unresolvable")

    def test_missing_task_file_is_unresolvable(self):
        self.assertEqual(tdr.resolve("/nonexistent/x.md", self.root)["status"],
                         "unresolvable")

    def test_reviewed_true_alone_is_not_accepted(self):
        # A hand edit writes reviewed: true; without acked_revision it is not evidence.
        task = self.task_file(front="task_identifier: uuid-1")
        self.record(front="related_task: '[[A Task]]'\nreviewed: true\nstatus: accepted")
        self.assertEqual(tdr.resolve(task, self.root)["status"], "pending")


class IncompleteScan(unittest.TestCase):
    def test_no_task_dirs_is_unresolvable(self):
        root = tempfile.mkdtemp()
        os.makedirs(os.path.join(root, "empty-vault"))
        task = os.path.join(root, "t.md")
        with open(task, "w", encoding="utf-8") as handle:
            handle.write("---\ntask_identifier: x\n---\n")
        self.assertEqual(tdr.resolve(task, root)["status"], "unresolvable")


if __name__ == "__main__":
    unittest.main()
