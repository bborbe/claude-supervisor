#!/usr/bin/env python3
"""Tests for scripts/topicless-goal-keepalive.py — the fleet tier's topic-less goal leg.

The leg has two halves, and each is pinned in both directions:

  * **inclusion** — a topic-less `execution` goal whose manager is ABSENT reads `resume` and
    carries `/supervisor:open --goal`; a LIVE-but-STALE one reads `restart` and carries
    `/supervisor:restart <id>`.
  * **exclusion** — a topic-declared goal, a `phase: todo` goal and a goal with no
    `claude_session_id` produce no row at all.

⚠️ The liveness verdict is INJECTED, never manufactured: a fabricated uuid answers `UNKNOWN`,
not `ABSENT`, so a test that faked an id and asserted `resume` would assert a state the
instrument never produces (the same rule `test_topic_keepalive.py` documents).
"""

import importlib.util
import io
import json
import os
import pathlib
import shutil
import tempfile
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout

_HERE = pathlib.Path(__file__).resolve().parent


def load():
    spec = importlib.util.spec_from_file_location(
        "topicless_goal_keepalive", str(_HERE.parent / "topicless-goal-keepalive.py")
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


GOAL = """---
page_type: goal
status: {status}
phase: {phase}
claude_session_id: {sid}
---
# Success Criteria
- [ ] one
"""

TOPIC = """---
page_type: topic
claude_session_id: topic-sid
---
## Goals

{body}
"""


class LegTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))
        self.vault = pathlib.Path(self.tmp) / "vault"
        (self.vault / "24 Goals").mkdir(parents=True)
        (self.vault / "23 Topics").mkdir(parents=True)
        self.state = pathlib.Path(self.tmp) / "state"
        self.state.mkdir()
        self.leg = load()
        self.ka = self.leg.keepalive()
        # The cap store and both probes are the instruments; this file tests the decision.
        self.ka.state_path = lambda vault, subject, branch="goal": str(
            self.state / f"{branch}--{self.ka.base_slug(subject)}.json"
        )
        self.verdicts = {}
        self.cadences = {}
        self.ka.session_verdict = lambda sid: self.verdicts.get(sid, "UNKNOWN")
        self.ka.cadence_state_detail = lambda subject, branch="goal", vault="": (
            self.cadences.get(subject, "OK"), "")

    def goal(self, name, sid, status="in_progress", phase="execution"):
        (self.vault / "24 Goals" / f"{name}.md").write_text(
            GOAL.format(status=status, phase=phase, sid=sid), encoding="utf-8")

    def topic(self, name, goals):
        body = "\n".join(f"- [[{g}]]" for g in goals)
        (self.vault / "23 Topics" / f"{name}.md").write_text(
            TOPIC.format(body=body), encoding="utf-8")

    def run_leg(self):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            rc = self.leg.main(["--vault", str(self.vault), "--goals-dir", "24 Goals",
                                  "--topics-dir", "23 Topics"])
        return rc, out.getvalue(), err.getvalue()

    def rows(self, text):
        return {line.split("\t")[0]: line.split("\t") for line in text.splitlines() if "\t" in line}

    def test_both_halves_in_one_run(self):
        # Inclusion: topic-less, execution, manager ABSENT → resume + the open command.
        self.goal("Lonely Goal", "sid-absent")
        self.verdicts["sid-absent"] = "ABSENT"
        self.cadences["Lonely Goal"] = "STALE"
        # Inclusion: topic-less, execution, LIVE with a lapsed loop → restart in place.
        self.goal("Lapsed Goal", "sid-live")
        self.verdicts["sid-live"] = "LIVE"
        self.cadences["Lapsed Goal"] = "STALE"
        # Exclusion: a topic declares it — the topic round owns it, even with the id ABSENT.
        self.goal("Declared Goal", "sid-declared")
        self.verdicts["sid-declared"] = "ABSENT"
        self.topic("Some Topic", ["Declared Goal"])
        # Exclusion: not in execution — no loop runs on it, so it is never resumed.
        self.goal("Backlog Goal", "sid-todo", phase="todo")
        self.verdicts["sid-todo"] = "ABSENT"
        # Exclusion: no recorded id — that is an open, the flagged-open leg's job.
        self.goal("Unopened Goal", "")

        rc, out, _ = self.run_leg()
        self.assertEqual(rc, 0)
        rows = self.rows(out)
        self.assertEqual(sorted(rows), ["Lapsed Goal", "Lonely Goal"])
        self.assertIn("selected: 2", out)
        self.assertIn("actionable: 2", out)

        self.assertEqual(rows["Lonely Goal"][1], "resume")
        self.assertEqual(rows["Lonely Goal"][2], '/supervisor:open --goal "Lonely Goal"')
        self.assertEqual(rows["Lapsed Goal"][1], "restart")
        self.assertEqual(rows["Lapsed Goal"][2], "/supervisor:restart sid-live")

        for absent in ("Declared Goal", "Backlog Goal", "Unopened Goal"):
            self.assertNotIn(absent, out)

    def test_jump_and_leave_carry_no_command(self):
        self.goal("Healthy Goal", "sid-ok")
        self.verdicts["sid-ok"] = "LIVE"
        self.goal("Stopped Goal", "sid-stopped")
        self.cadences["Stopped Goal"] = "STOPPED"
        rc, out, _ = self.run_leg()
        rows = self.rows(out)
        self.assertEqual(rows["Healthy Goal"][1:3], ["jump", "-"])
        self.assertEqual(rows["Stopped Goal"][1:3], ["leave", "-"])
        self.assertIn("actionable: 0", out)

    def test_an_undecidable_probe_never_resumes(self):
        self.goal("Unknown Goal", "sid-unknown")  # no injected verdict → UNKNOWN
        rc, out, _ = self.run_leg()
        self.assertEqual(self.rows(out)["Unknown Goal"][1:3], ["cannot-tell", "-"])

    def test_a_spent_cap_is_a_crash_loop_with_no_command(self):
        self.goal("Looping Goal", "sid-loop")
        self.verdicts["sid-loop"] = "ABSENT"
        now = time.time()
        (self.state / "goal--looping-goal.json").write_text(
            json.dumps({"resumes": [now - 60, now - 120, now - 180]}), encoding="utf-8")
        rc, out, _ = self.run_leg()
        self.assertEqual(self.rows(out)["Looping Goal"][1:3], ["crash-loop", "-"])

    def test_the_run_writes_no_cap(self):
        """Read-only: deciding a resume must not arm the cap against the round's own retry."""
        self.goal("Lonely Goal", "sid-absent")
        self.verdicts["sid-absent"] = "ABSENT"
        self.run_leg()
        self.assertEqual(list(self.state.iterdir()), [])

    def test_an_error_inside_one_verdict_is_that_goals_cannot_tell(self):
        """An OSError from topic-keepalive must not end the run with an undefined exit code."""
        self.goal("Broken Goal", "sid-broken")
        self.goal("Lonely Goal", "sid-absent")
        self.verdicts["sid-absent"] = "ABSENT"

        def boom(subject, branch="goal", vault=""):
            if subject == "Broken Goal":
                raise OSError("state dir unwritable")
            return ("OK", "")
        self.ka.cadence_state_detail = boom
        rc, out, _ = self.run_leg()
        self.assertEqual(rc, 0)
        rows = self.rows(out)
        self.assertEqual(rows["Broken Goal"][1:3], ["cannot-tell", "-"])
        self.assertEqual(rows["Lonely Goal"][1], "resume")

    def test_without_dir_flags_the_folders_come_from_the_vaults_own_config(self):
        self.goal("Lonely Goal", "sid-absent")
        self.verdicts["sid-absent"] = "ABSENT"
        seen = []
        self.leg.folders = lambda vault: (seen.append(vault) or
                                          (str(self.vault / "24 Goals"), str(self.vault / "23 Topics")))
        out = io.StringIO()
        with redirect_stdout(out):
            rc = self.leg.main(["--vault", str(self.vault)])
        self.assertEqual(rc, 0)
        self.assertEqual(seen, [str(self.vault)])
        self.assertIn("Lonely Goal\tresume", out.getvalue())

    def test_an_unresolvable_vault_refuses_with_two(self):
        def refuse(vault):
            raise self.leg.Refused("folders: UNKNOWN vault does not configure topics_dir")
        self.leg.folders = refuse
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            rc = self.leg.main(["--vault", str(self.vault)])
        self.assertEqual(rc, 2)
        self.assertIn("does not configure topics_dir", err.getvalue())
        self.assertNotIn("selected:", out.getvalue())

    def test_an_empty_population_is_zero_not_a_refusal(self):
        rc, out, _ = self.run_leg()
        self.assertEqual(rc, 0)
        self.assertIn("selected: 0", out)

    def test_an_unreadable_goals_dir_refuses_with_two(self):
        shutil.rmtree(self.vault / "24 Goals")
        rc, out, err = self.run_leg()
        self.assertEqual(rc, 2)
        self.assertIn("REFUSED", err)
        self.assertNotIn("selected:", out)


if __name__ == "__main__":
    unittest.main()
