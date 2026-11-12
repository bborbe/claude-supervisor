#!/usr/bin/env python3
"""Tests for scripts/topic-keepalive.py.

The two verdicts the live environment cannot fabricate — `resume` on a genuinely
`ABSENT` id, and the `crash-loop` cap — are pinned here by injecting the probes, so
the branch that decides *whether a second manager is started* has a regression test
rather than only a runbook.

⚠️ `UNKNOWN` is not `ABSENT`. A fabricated uuid answers `UNKNOWN` (the liveness
instrument withholds `ABSENT` for an id the identity registry still holds as non-dead),
so a test that faked an id and asserted `resume` would be asserting a state the
instrument never produces — the constructed-probe defect. These tests inject the
verdict instead of manufacturing one.
"""

import importlib.util
import io
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import time
import types
import unittest
from contextlib import redirect_stdout

_HERE = pathlib.Path(__file__).resolve().parent


def load():
    spec = importlib.util.spec_from_file_location(
        "topic_keepalive", str(_HERE.parent / "topic-keepalive.py")
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


GOAL_PAGE = """---
page_type: goal
status: in_progress
phase: execution
claude_session_id: {sid}
{extra}---
# Success Criteria
- [ ] one
"""


class KeepAliveTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.vault = pathlib.Path(self.tmp.name) / "vault"
        (self.vault / "24 Goals").mkdir(parents=True)
        self.k = load()
        self.state = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(self.state, ignore_errors=True))
        self._orig = (self.k.session_verdict, self.k.cadence, self.k.state_path,
                      self.k.flagged_path)
        self.k.state_path = lambda vault, subject, branch="goal": str(
            self.state / f"{branch}--{self.k.base_slug(subject)}.json"
        )
        # ⚠️ The stand-down marker must never reach the REAL store: an unpatched
        # `flagged_path` writes under the live state dir, and a marker left there makes
        # an unrelated later run stand a live manager down.
        self.k.flagged_path = lambda vault, subject, branch="goal": str(
            self.state / f"{branch}--{self.k.base_slug(subject)}.flagged"
        )
        self.addCleanup(self._restore)

    def _restore(self):
        (self.k.session_verdict, self.k.cadence, self.k.state_path,
         self.k.flagged_path) = self._orig

    def page(self, sid="sid-1", extra=""):
        (self.vault / "24 Goals" / "A Goal.md").write_text(
            GOAL_PAGE.format(sid=sid, extra=extra), encoding="utf-8"
        )

    def run_keepalive(self, live="ABSENT", cadence="OK", argv=()):
        self.k.session_verdict = lambda sid: live
        self.k.cadence = lambda subject, branch="goal", vault="": cadence
        self.k.cadence_state_detail = lambda subject, branch="goal", vault="": (cadence, "")
        out = io.StringIO()
        with redirect_stdout(out):
            rc = self.k.main(["--vault", str(self.vault), "--goal", "A Goal", *argv])
        return rc, out.getvalue().splitlines()

    def seed(self, stamps):
        path = self.k.state_path(str(self.vault), "A Goal")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"resumes": stamps}, fh)

    # --- the two signals, and the verdict each licenses -----------------------

    def test_absent_session_resumes(self):
        self.page()
        rc, out = self.run_keepalive(live="ABSENT")
        self.assertEqual(0, rc)
        self.assertEqual("resume", out[0])

    def test_unknown_is_cannot_tell_and_never_resumes(self):
        # The measured direction: a fabricated id answers UNKNOWN, and UNKNOWN must
        # never be read as death — a second manager on a live one is the double writer.
        self.page()
        rc, out = self.run_keepalive(live="UNKNOWN")
        self.assertEqual(1, rc)
        self.assertEqual("cannot-tell", out[0])

    def test_live_session_with_a_stale_loop_restarts_in_place(self):
        self.page()
        rc, out = self.run_keepalive(live="LIVE", cadence="STALE")
        self.assertEqual(0, rc)
        self.assertEqual("restart", out[0])
        self.assertIn("/supervisor:restart", out[1])

    def test_live_session_with_an_armed_loop_jumps(self):
        self.page()
        rc, out = self.run_keepalive(live="LIVE", cadence="OK")
        self.assertEqual(0, rc)
        self.assertEqual("jump", out[0])

    def test_a_deliberate_stand_down_is_left_alone(self):
        self.page()
        rc, out = self.run_keepalive(live="LIVE", cadence="STOPPED")
        self.assertEqual(0, rc)
        self.assertEqual("leave", out[0])

    def test_a_mixed_page_resolves_claude_session_id_first(self):
        """A page left over from the tolerance window carries both keys; the retired one is ignored.

        `claude_session_id` is the only key read, so a stale retired id on the same page
        can never be probed — and resumed — in its place.
        """
        self.page(sid="sid-new", extra="manager_session_id: sid-old\n")
        seen = []

        def verdict(sid):
            seen.append(sid)
            return "LIVE"

        self.k.session_verdict = verdict
        self.k.cadence = lambda subject, branch="goal", vault="": "OK"
        self.k.cadence_state_detail = lambda subject, branch="goal", vault="": ("OK", "")
        out = io.StringIO()
        with redirect_stdout(out):
            self.k.main(["--vault", str(self.vault), "--goal", "A Goal"])
        self.assertEqual(["sid-new"], seen)

    def test_no_recorded_id_without_a_flag_is_held(self):
        """An absent `claude_session_id` is an OPEN, not a resume, and rule 4 makes an open
        the operator's act. The two arms must not share a verdict — `resume` here handed the
        agent a `/supervisor:open --goal` line for a goal it may not open, which is the whole
        defect this split removes."""
        self.page(sid="")
        rc, out = self.run_keepalive(live="LIVE")
        self.assertEqual(0, rc)
        self.assertEqual("open-unflagged", out[0])
        self.assertIn("does not carry the flag", out[1])

    def test_no_recorded_id_with_an_operator_flag_opens(self):
        self.page(sid="", extra="flag: true\nflag_set_by: operator\n")
        rc, out = self.run_keepalive(live="LIVE")
        self.assertEqual(0, rc)
        self.assertEqual("open", out[0])

    # ------------------------------------------------ unflag → stand-down (M1)

    def _marker(self):
        return self.state / "goal--a-goal.flagged"

    def test_an_admitted_flag_writes_the_stand_down_marker(self):
        """The marker is what makes "cleared" distinguishable from "never flagged"."""
        self.page(sid="sid-1", extra="flag: true\nflag_set_by: operator\n")
        rc, out = self.run_keepalive(live="LIVE")
        self.assertEqual(0, rc)
        self.assertEqual("jump", out[0])
        self.assertTrue(self._marker().exists())

    def test_clearing_the_flag_stands_the_manager_down(self):
        """M1 — unflagging retires the live manager at the next round."""
        self._marker().write_text("2026-11-12T00:00:00+0100\n", encoding="utf-8")
        self.page(sid="sid-1")  # the flag was cleared
        rc, out = self.run_keepalive(live="LIVE")
        self.assertEqual(0, rc)
        self.assertEqual("stand-down", out[0])
        self.assertIn("--stop", out[1])
        self.assertTrue(self._marker().exists(),
                        "the marker is kept, so a round that skips the stop retries")

    def test_a_stopped_loop_leaves_and_does_not_re_stand_down(self):
        """The retry terminates by itself: once `--stop` lands the loop reads STOPPED, and
        the `loop-stopped` arm returns before the stand-down branch is reached."""
        self._marker().write_text("2026-11-12T00:00:00+0100\n", encoding="utf-8")
        self.page(sid="sid-1")
        rc, out = self.run_keepalive(live="LIVE", cadence="STOPPED")
        self.assertEqual(0, rc)
        self.assertEqual("leave", out[0])

    def test_a_never_flagged_subject_is_not_stood_down(self):
        """⚠️ The regression guard, measured 2026-11-12: of 84 live goal pages carrying a
        `claude_session_id`, **64 carry no flag at all**. An absolute "unflagged + session
        id" rule would stand down 83 of 84 live managers; only a flag that was SET and then
        cleared may retire one."""
        self.page(sid="sid-1")
        rc, out = self.run_keepalive(live="LIVE")
        self.assertEqual(0, rc)
        self.assertEqual("jump", out[0])

    def test_the_flag_predicate_agrees_with_topic_spawn(self):
        """⚠️ The predicate is duplicated because both scripts run standalone out of the
        plugin cache. This is the anti-drift control: a split verdict between the two would
        open exactly the goal the spawn gate refuses."""
        spec = importlib.util.spec_from_file_location(
            "topic_spawn", str(_HERE.parent / "topic-spawn.py")
        )
        spawn = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(spawn)
        for fm in (
            "flag: true\nflag_set_by: operator\n",
            "flag: true\nflag_set_by: legacy\n",
            "flag: yes\nflag_set_by: operator\n",
            "flag: on\nflag_set_by: operator\n",
            "flag: true\nflag_set_by: agent\n",
            "flag: true\n",
            "flag: false\nflag_set_by: operator\n",
            "",
        ):
            with self.subTest(fm=fm):
                self.assertEqual(spawn.flagged(fm), self.k.flagged(fm))

    def test_the_session_id_fold_agrees_with_topic_spawn(self):
        """⚠️ `session_id` is duplicated for the same reason `flagged` is, and needs the
        same anti-drift control: a split verdict would let one script treat a `null` id as
        "nothing recorded" while the other still probes it as an id."""
        spec = importlib.util.spec_from_file_location(
            "topic_spawn", str(_HERE.parent / "topic-spawn.py")
        )
        spawn = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(spawn)
        for fm in (
            "claude_session_id: 3716bdff-a825-433a-bd73-c96d64970d29\n",
            "claude_session_id: fa2dc5f3-8dd\n",
            "claude_session_id: null\n",
            "claude_session_id: NULL\n",
            "claude_session_id: ~\n",
            "claude_session_id: []\n",
            "claude_session_id:\n",
            "claude_session_id: \"null\"\n",
            "",
        ):
            with self.subTest(fm=fm):
                self.assertEqual(spawn.session_id(fm), self.k.session_id(fm))

    # --- the crash-loop cap ---------------------------------------------------

    def test_fourth_resume_inside_the_window_is_a_crash_loop(self):
        self.page()
        now = time.time()
        self.seed([now - 60, now - 120, now - 180])
        rc, out = self.run_keepalive(live="ABSENT", argv=("--now", str(now)))
        self.assertEqual(1, rc)
        self.assertEqual("crash-loop", out[0])
        self.assertIn("MANAGER CRASH-LOOP", out[1])

    def test_two_spent_resumes_still_resume(self):
        self.page()
        now = time.time()
        self.seed([now - 60, now - 120])
        rc, out = self.run_keepalive(live="ABSENT", argv=("--now", str(now)))
        self.assertEqual(0, rc)
        self.assertEqual("resume", out[0])
        self.assertIn("attempt 3 of 3", out[1])

    def test_a_resume_outside_the_window_does_not_count(self):
        # The cap is per 30 minutes, not per goal lifetime: three resumes an hour ago
        # must not arm it against a fresh attempt.
        self.page()
        now = time.time()
        self.seed([now - 4000, now - 4100, now - 4200])
        rc, out = self.run_keepalive(live="ABSENT", argv=("--now", str(now)))
        self.assertEqual(0, rc)
        self.assertEqual("resume", out[0])

    # --- the write half -------------------------------------------------------

    def test_a_resume_that_did_not_take_does_not_arm_the_cap(self):
        self.page()
        self.k.session_verdict = lambda sid: "ABSENT"
        out = io.StringIO()
        with redirect_stdout(out):
            rc = self.k.main(["--vault", str(self.vault), "--goal", "A Goal",
                              "--record-resume", "sid-1"])
        self.assertEqual(1, rc)
        self.assertEqual("cannot-tell", out.getvalue().splitlines()[0])
        self.assertFalse(os.path.exists(self.k.state_path(str(self.vault), "A Goal")))


    # --- the topic branch -----------------------------------------------------

    def test_topic_branch_reads_the_topic_directory_and_writes_its_own_cap(self):
        (self.vault / "23 Topics").mkdir(parents=True)
        (self.vault / "23 Topics" / "A Topic.md").write_text(
            GOAL_PAGE.format(sid="sid-2", extra=""), encoding="utf-8"
        )
        self.k.session_verdict = lambda sid: "LIVE"
        self.k.cadence = lambda subject, branch="goal", vault="": "OK"
        self.k.cadence_state_detail = lambda subject, branch="goal", vault="": ("OK", "")
        out = io.StringIO()
        with redirect_stdout(out):
            rc = self.k.main(["--vault", str(self.vault), "--subject", "A Topic",
                              "--branch", "topic", "--record-resume", "sid-2"])
        self.assertEqual(0, rc)
        self.assertEqual("recorded", out.getvalue().splitlines()[0])
        self.assertTrue(os.path.exists(self.k.state_path(str(self.vault), "A Topic", "topic")))
        # The goal branch's cap is a different file — a topic resume must not arm it.
        self.assertFalse(os.path.exists(self.k.state_path(str(self.vault), "A Topic", "goal")))

    def test_topic_branch_never_reads_a_goal_page(self):
        # The goal page exists and the topic page does not: a topic read must not
        # fall back to it, or a lapsed goal manager would be reported as a topic one.
        self.page()
        out = io.StringIO()
        with redirect_stdout(out):
            rc = self.k.main(["--vault", str(self.vault), "--subject", "A Goal",
                              "--branch", "topic"])
        self.assertEqual(1, rc)
        self.assertEqual("cannot-tell", out.getvalue().splitlines()[0])

    def test_topic_branch_names_the_topic_opener_on_an_open(self):
        # Master's `open`/`open-unflagged` split applies to the topic branch too: a topic
        # page with no recorded id is an OPEN, and rule 4 makes that the operator's act.
        (self.vault / "23 Topics").mkdir(parents=True)
        (self.vault / "23 Topics" / "A Topic.md").write_text(
            GOAL_PAGE.format(sid="", extra="flag: true\nflag_set_by: operator\n"),
            encoding="utf-8"
        )
        self.k.cadence = lambda subject, branch="goal", vault="": "OK"
        self.k.cadence_state_detail = lambda subject, branch="goal", vault="": ("OK", "")
        out = io.StringIO()
        with redirect_stdout(out):
            self.k.main(["--vault", str(self.vault), "--subject", "A Topic",
                         "--branch", "topic"])
        lines = out.getvalue().splitlines()
        self.assertEqual("open", lines[0])
        self.assertIn("/supervisor:open --topic", lines[1])

    def test_topic_branch_refuses_an_unflagged_open(self):
        (self.vault / "23 Topics").mkdir(parents=True)
        (self.vault / "23 Topics" / "A Topic.md").write_text(
            GOAL_PAGE.format(sid="", extra=""), encoding="utf-8")
        self.k.cadence = lambda subject, branch="goal", vault="": "OK"
        self.k.cadence_state_detail = lambda subject, branch="goal", vault="": ("OK", "")
        out = io.StringIO()
        with redirect_stdout(out):
            self.k.main(["--vault", str(self.vault), "--subject", "A Topic",
                         "--branch", "topic"])
        self.assertEqual("open-unflagged", out.getvalue().splitlines()[0])

    def test_a_topic_stand_down_is_left_alone(self):
        (self.vault / "23 Topics").mkdir(parents=True)
        (self.vault / "23 Topics" / "A Topic.md").write_text(
            GOAL_PAGE.format(sid="sid-2", extra=""), encoding="utf-8"
        )
        self.k.session_verdict = lambda sid: "LIVE"
        self.k.cadence = lambda subject, branch="goal", vault="": "STOPPED"
        self.k.cadence_state_detail = lambda subject, branch="goal", vault="": ("STOPPED", "")
        out = io.StringIO()
        with redirect_stdout(out):
            rc = self.k.main(["--vault", str(self.vault), "--subject", "A Topic",
                              "--branch", "topic"])
        self.assertEqual(0, rc)
        self.assertEqual("leave", out.getvalue().splitlines()[0])

    def test_an_unreadable_loop_never_licenses_a_restart(self):
        """A probe that could not answer is a fact about the PROBE, not about the subject.

        `INVALID` is `manager-liveness.py`'s own verdict for a record that exists and cannot be
        read, and this tier restarts a LIVE session on it. Folding a crashed probe into
        `INVALID` would restart a live manager on no evidence at all, so `UNREADABLE` jumps.
        """
        self.page()
        rc, out = self.run_keepalive(live="LIVE", cadence="UNREADABLE")
        self.assertEqual(0, rc)
        self.assertEqual("jump", out[0])

    def test_the_goal_alias_still_works(self):
        self.page()
        rc, out = self.run_keepalive(live="ABSENT")
        self.assertEqual(0, rc)
        self.assertEqual("resume", out[0])


if __name__ == "__main__":
    unittest.main()
