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
import sys
import tempfile
import time
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
manager_session_id: {sid}
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
        self._orig = (self.k.session_verdict, self.k.cadence, self.k.state_path)
        self.k.state_path = lambda vault, goal: str(
            self.state / f"goal--{self.k.base_slug(goal)}.json"
        )
        self.addCleanup(self._restore)

    def _restore(self):
        self.k.session_verdict, self.k.cadence, self.k.state_path = self._orig

    def page(self, sid="sid-1", extra=""):
        (self.vault / "24 Goals" / "A Goal.md").write_text(
            GOAL_PAGE.format(sid=sid, extra=extra), encoding="utf-8"
        )

    def run_keepalive(self, live="ABSENT", cadence="OK", argv=()):
        self.k.session_verdict = lambda sid: live
        self.k.cadence = lambda goal: cadence
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

    def test_no_recorded_id_without_a_flag_is_held(self):
        """An absent `manager_session_id` is an OPEN, not a resume, and rule 4 makes an open
        the operator's act. The two arms must not share a verdict — `resume` here handed the
        agent a `/supervisor:open --goal` line for a goal it may not open, which is the whole
        defect this split removes."""
        self.page(sid="")
        rc, out = self.run_keepalive(live="LIVE")
        self.assertEqual(0, rc)
        self.assertEqual("open-unflagged", out[0])
        self.assertIn("not flagged by the operator", out[1])

    def test_no_recorded_id_with_an_operator_flag_opens(self):
        self.page(sid="", extra="flag: true\nflag_set_by: operator\n")
        rc, out = self.run_keepalive(live="LIVE")
        self.assertEqual(0, rc)
        self.assertEqual("open", out[0])

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
                self.assertEqual(spawn.operator_flagged(fm), self.k.operator_flagged(fm))

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


if __name__ == "__main__":
    unittest.main()
