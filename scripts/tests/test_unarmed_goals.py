#!/usr/bin/env python3
"""Tests for scripts/unarmed-goals.py.

The load-bearing properties, each of which is a way the report could lie:

  * a goal with a live session and NO record is reported (the gap this closes);
  * a goal with a record is not reported, even a lapsed one — that is `STALE`'s job,
    and folding it in is the defect this script exists to end;
  * a goal with no live session is never reported — 318 goals in the primary vault
    and a handful carry live sessions, so a report that ignored liveness would bury
    the signal under noise;
  * a `themes:`-only task is reported as `NO-SUBJECT`, never silently dropped;
  * an unreadable registry is `unknown` + non-zero, never `0`.

Run: python3 -m unittest discover -s scripts/tests -v
"""
import importlib.util
import io
import os
import re
import tempfile
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout

_HERE = os.path.dirname(os.path.abspath(__file__))


def load():
    spec = importlib.util.spec_from_file_location(
        "unarmed_goals", os.path.join(os.path.dirname(_HERE), "unarmed-goals.py")
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


TASK = """---
status: in_progress
claude_session_id: {sid}
{extra}---

body
"""


class UnarmedGoals(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = tempfile.TemporaryDirectory()
        os.environ["MANAGER_LIVENESS_STATE_DIR"] = self.state.name
        self.m = load()
        self.tasks = os.path.join(self.tmp.name, "25 Tasks")
        os.makedirs(self.tasks)
        os.makedirs(os.path.join(self.tmp.name, "24 Goals"))

    def tearDown(self):
        self.tmp.cleanup()
        self.state.cleanup()
        os.environ.pop("MANAGER_LIVENESS_STATE_DIR", None)

    def task(self, name, sid, goals=None, themes=None, status="in_progress"):
        extra = ""
        if goals:
            extra += "goals:\n" + "".join("    - '[[%s]]'\n" % g for g in goals)
        if themes:
            extra += "themes:\n" + "".join("    - '[[%s]]'\n" % t for t in themes)
        text = TASK.format(sid=sid, extra=extra).replace("status: in_progress", "status: " + status)
        with open(os.path.join(self.tasks, name + ".md"), "w", encoding="utf-8") as fh:
            fh.write(text)

    def arm(self, goal, age=10):
        slug = re.sub(r"[^a-z0-9]+", "-", goal.lower()).strip("-")
        p = os.path.join(self.state.name, "goal--%s.cadence" % slug)
        with open(p, "w", encoding="utf-8") as fh:
            fh.write("300\n")
        t = time.time() - age
        os.utime(p, (t, t))

    def live(self, *sids):
        return [{"sessionId": s, "alive": True} for s in sids]

    def collect(self, *sids):
        return self.m.collect(self.tasks, self.live(*sids))

    def test_a_goal_with_a_live_session_and_no_record_is_reported(self):
        self.task("T", "aaaaaaaa-1111", goals=["Gap Goal"])
        unarmed, _ = self.collect("aaaaaaaa-1111")
        self.assertEqual(["Gap Goal"], [r["goal"] for r in unarmed])
        self.assertEqual(["aaaaaaaa-1111"], unarmed[0]["sessions"])

    def test_a_goal_with_a_record_is_not_reported(self):
        # Lapsed is `STALE`'s class, not this report's — folding it in is the defect.
        self.task("T", "bbbbbbbb-2222", goals=["Armed Goal"])
        self.arm("Armed Goal", age=100000)
        unarmed, _ = self.collect("bbbbbbbb-2222")
        self.assertEqual([], unarmed)

    def test_a_goal_with_no_live_session_is_never_reported(self):
        self.task("T", "cccccccc-3333", goals=["Quiet Goal"])
        unarmed, _ = self.collect("dddddddd-4444")
        self.assertEqual([], unarmed)

    def test_a_terminal_task_is_not_a_live_session(self):
        self.task("T", "eeeeeeee-5555", goals=["Done Goal"], status="completed")
        unarmed, _ = self.collect("eeeeeeee-5555")
        self.assertEqual([], unarmed)

    def test_a_themes_only_task_is_reported_as_no_subject(self):
        self.task("Loose", "ffffffff-6666", themes=["Some Theme"])
        unarmed, no_subject = self.collect("ffffffff-6666")
        self.assertEqual([], unarmed)
        self.assertEqual(["Loose"], [r["task"] for r in no_subject])

    def test_a_topics_only_task_is_not_no_subject(self):
        # A topic IS a derivable manager subject, so it is not the no-subject case.
        self.task("Topic Task", "11111111-7777", themes=["T"])
        path = os.path.join(self.tasks, "Topic Task.md")
        with open(path, encoding="utf-8") as fh:
            text = fh.read().replace(
                "themes:", "topics:\n    - '[[Some Topic]]'\nthemes:")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
        unarmed, no_subject = self.collect("11111111-7777")
        self.assertEqual([], unarmed)
        self.assertEqual([], no_subject)

    def test_the_render_names_the_goal_and_its_session_count(self):
        self.task("T", "aaaaaaaa-1111", goals=["Gap Goal"])
        unarmed, no_subject = self.collect("aaaaaaaa-1111")
        out = self.m.render(unarmed, no_subject)
        self.assertIn("unarmed goals: 1 unarmed goal", out)
        self.assertIn("Gap Goal — 1 live session", out)

    def test_an_empty_population_renders_zero(self):
        self.assertEqual("unarmed goals: 0", self.m.render([], []))

    def test_an_unreadable_registry_is_unknown_and_non_zero(self):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            rc = self.m.main([
                "--vault", self.tmp.name,
                "--tasks-dir", self.tasks,
                "--registry-dir", "/nonexistent",
            ])
        self.assertEqual(2, rc)
        self.assertIn("unarmed goals: unknown", out.getvalue())
        self.assertNotIn("unarmed goals: 0", out.getvalue())

    def test_a_missing_tasks_dir_refuses(self):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            rc = self.m.main([
                "--vault", self.tmp.name,
                "--tasks-dir", os.path.join(self.tmp.name, "nope"),
            ])
        self.assertEqual(1, rc)

    def test_the_row_set_changes_with_the_registry(self):
        # The falsifier: a hardcoded report prints the same rows against any registry.
        self.task("A", "aaaaaaaa-1111", goals=["Gap Goal"])
        self.task("B", "bbbbbbbb-2222", goals=["Other Gap"])
        first, _ = self.collect("aaaaaaaa-1111")
        second, _ = self.collect("aaaaaaaa-1111", "bbbbbbbb-2222")
        self.assertEqual(["Gap Goal"], [r["goal"] for r in first])
        self.assertEqual(["Gap Goal", "Other Gap"], [r["goal"] for r in second])


if __name__ == "__main__":
    unittest.main()
