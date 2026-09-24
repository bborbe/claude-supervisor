#!/usr/bin/env python3
"""Tests for scripts/stamp-check.py — the rule behind fleet-verify check 3.

The rule (docs/fleet-surface.md § Session stamps): a session may carry many
`claude_session_id` stamps, but **at most one on an open task**, and task
creation never stamps. A session resolves "what am I working on" through its
stamp, so two open tasks behind one stamp is an ambiguous answer; many finished
tasks behind one stamp is history, and it records who did the work.

Measured 2026-09-25 over `25 Tasks/`: 33 stamps spanned 2+ files — 30 were pure
history, 3 were a creating session's stamp left on an open task. Counting "2+
files" called all 33 defects; the rule calls 0 violations and 1 suspect.

The fixtures are `constructed`: hand-written in a temp dir, never the vault.

Run: python3 -m unittest discover -s scripts/tests -v
"""

import importlib.util
import os
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("stamp_check", os.path.join(HERE, "..", "stamp-check.py"))
sc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sc)

A = "aaaaaaaa-0000-0000-0000-000000000001"
B = "bbbbbbbb-0000-0000-0000-000000000002"
C = "cccccccc-0000-0000-0000-000000000003"


def task(stamp, status, worked_by=None, body=""):
    lines = ["---", f"status: {status}"]
    if stamp is not None:
        lines.append(f"claude_session_id: {stamp}")
    lines.append("goals:")
    lines.append("    - '[[Some Goal]]'")
    if worked_by:
        lines.append("metrics_sessions:")
        for s in worked_by:
            lines.append(f"    - session_id: {s}")
    lines += ["---", "Tags: [[Task]]", "", body]
    return "\n".join(lines) + "\n"


class Scratch:
    def __init__(self, files):
        self.files = files

    def __enter__(self):
        self.tmp = tempfile.TemporaryDirectory()
        for name, text in self.files.items():
            with open(os.path.join(self.tmp.name, name + ".md"), "w", encoding="utf-8") as fh:
                fh.write(text)
        return self.tmp.name

    def __exit__(self, *exc):
        self.tmp.cleanup()


class ParseTest(unittest.TestCase):
    def test_empty_stamp_is_no_stamp(self):
        """The measured artifact: `claude_session_id:` with no value, read by a
        newline-crossing `\\s*`, returned the next line — `goals:` — as a stamp
        shared by three fixture files."""
        fm = sc.frontmatter(task("", "next"))
        self.assertIsNone(sc.stamp(fm))

    def test_stamp_in_the_body_is_not_a_declaration(self):
        fm = sc.frontmatter(task(None, "next", body=f"claude_session_id: {A}"))
        self.assertIsNone(sc.stamp(fm))

    def test_quoted_stamp_is_read(self):
        fm = sc.frontmatter(task(f"'{A}'", "next"))
        self.assertEqual(sc.stamp(fm), A)

    def test_worked_by_reads_metrics_sessions(self):
        fm = sc.frontmatter(task(A, "next", worked_by=[B, C]))
        self.assertEqual(sc.worked_by(fm), {B, C})


class ClassifyTest(unittest.TestCase):
    def test_history_is_permitted(self):
        with Scratch({"one": task(A, "completed"), "two": task(A, "aborted"),
                      "three": task(A, "completed")}) as d:
            r = sc.classify(d)
        self.assertEqual(r["violations"], [])
        self.assertEqual([p["stamp"] for p in r["permitted"]], [A])

    def test_one_open_plus_history_is_permitted(self):
        with Scratch({"done": task(A, "completed"), "live": task(A, "in_progress")}) as d:
            r = sc.classify(d)
        self.assertEqual(r["violations"], [])
        self.assertEqual(r["suspects"], [])

    def test_two_open_tasks_behind_one_stamp_is_a_violation(self):
        with Scratch({"x": task(A, "in_progress"), "y": task(A, "next"),
                      "z": task(A, "completed")}) as d:
            r = sc.classify(d)
        self.assertEqual(len(r["violations"]), 1)
        v = r["violations"][0]
        self.assertEqual(v["stamp"], A)
        self.assertEqual(sorted(v["open"]), ["x", "y"])

    def test_open_task_worked_by_someone_else_is_a_suspect(self):
        """The measured creator-stamp: the task names B as its worker in
        `metrics_sessions`, yet carries A — the session that created it — which
        also stamps a finished task."""
        with Scratch({"created": task(A, "next", worked_by=[B]),
                      "finished": task(A, "completed")}) as d:
            r = sc.classify(d)
        self.assertEqual(r["violations"], [])
        self.assertEqual(len(r["suspects"]), 1)
        self.assertEqual(r["suspects"][0]["open"], ["created"])

    def test_open_task_worked_by_its_own_stamp_is_not_a_suspect(self):
        with Scratch({"created": task(A, "next", worked_by=[B, A]),
                      "finished": task(A, "completed")}) as d:
            r = sc.classify(d)
        self.assertEqual(r["suspects"], [])

    def test_single_file_stamps_are_counted_not_listed(self):
        with Scratch({"x": task(A, "next"), "y": task(B, "completed"), "z": task("", "next")}) as d:
            r = sc.classify(d)
        self.assertEqual(r["stamps"], 2)
        self.assertEqual(r["unstamped"], 1)
        self.assertEqual(r["permitted"], [])

    def test_open_status_defaults_to_open(self):
        """A file with no `status:` has not been closed — count it open, so a
        malformed file can raise a violation rather than hide one."""
        with Scratch({"x": task(A, "in_progress"), "y": "---\nclaude_session_id: " + A + "\n---\n"}) as d:
            r = sc.classify(d)
        self.assertEqual(len(r["violations"]), 1)


class ExitCodeTest(unittest.TestCase):
    def test_violation_exits_one(self):
        with Scratch({"x": task(A, "in_progress"), "y": task(A, "next")}) as d:
            self.assertEqual(sc.main([d, "--json"]), 1)

    def test_clean_exits_zero(self):
        with Scratch({"x": task(A, "in_progress"), "y": task(A, "completed")}) as d:
            self.assertEqual(sc.main([d, "--json"]), 0)

    def test_missing_dir_is_not_clean(self):
        """A dir that cannot be read must never render as a vault with no
        violations."""
        self.assertEqual(sc.main(["/nonexistent/stamp-check-fixture", "--json"]), 2)

    def test_empty_dir_is_not_clean(self):
        with Scratch({}) as d:
            self.assertEqual(sc.main([d, "--json"]), 2)


if __name__ == "__main__":
    unittest.main()
