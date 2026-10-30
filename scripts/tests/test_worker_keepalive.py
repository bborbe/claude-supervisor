#!/usr/bin/env python3
"""Tests for scripts/worker-keepalive.py.

The fleet tier reported a dead worker and never resumed it (`agents/fleet-drive.md`:
*"escalate, never nudge"*), so the resume branch and its cap have no live environment that
can exercise them cheaply. Both are pinned here by injecting the probes.

⚠️ The verdicts are injected, never manufactured — the same reason `test_topic_keepalive.py`
gives: a fabricated uuid answers `UNKNOWN`, so a test that faked one and asserted `resume`
would be asserting a state the instrument never produces.
"""

import importlib.util
import io
import json
import os
import pathlib
import tempfile
import unittest
from contextlib import redirect_stdout

_HERE = pathlib.Path(__file__).resolve().parent

TASK_PAGE = """---
page_type: task
status: in_progress
phase: execution
claude_session_id: {sid}
---
# Tasks

- [ ] one
- [x] two
"""


def load():
    spec = importlib.util.spec_from_file_location(
        "worker_keepalive", str(_HERE.parent / "worker-keepalive.py")
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class WorkerKeepAliveTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.vault = pathlib.Path(self.tmp.name) / "vault"
        (self.vault / "25 Tasks").mkdir(parents=True)
        self.k = load()
        self.kp = self.k._keepalive()
        self.state = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(self.state, ignore_errors=True))
        self._orig = (self.kp.session_verdict, self.kp.state_path)
        self.kp.state_path = lambda vault, subject, branch="goal": str(
            self.state / f"{branch}--{self.kp.base_slug(subject)}.json"
        )
        self.addCleanup(self._restore)

    def _restore(self):
        self.kp.session_verdict, self.kp.state_path = self._orig

    def page(self, sid="sid-1", body=None):
        (self.vault / "25 Tasks" / "A Task.md").write_text(
            body if body is not None else TASK_PAGE.format(sid=sid), encoding="utf-8"
        )

    def run_it(self, live="ABSENT", argv=()):
        self.kp.session_verdict = lambda sid: live
        out = io.StringIO()
        with redirect_stdout(out):
            rc = self.k.main(["--vault", str(self.vault), "--task", "A Task", *argv])
        return rc, out.getvalue().splitlines()

    def seed(self, stamps):
        with open(self.k.state_path(str(self.vault), "A Task"), "w",
                  encoding="utf-8") as fh:
            json.dump({"resumes": stamps}, fh)

    # --- the verdicts ---------------------------------------------------------

    def test_an_absent_session_resumes(self):
        self.page()
        rc, out = self.run_it(live="ABSENT")
        self.assertEqual(0, rc)
        self.assertEqual("resume", out[0])
        self.assertIn("/supervisor:open --task", out[1])

    def test_a_live_session_jumps(self):
        self.page()
        rc, out = self.run_it(live="LIVE")
        self.assertEqual(0, rc)
        self.assertEqual("jump", out[0])

    def test_unknown_is_cannot_tell_and_never_resumes(self):
        self.page()
        rc, out = self.run_it(live="UNKNOWN")
        self.assertEqual(1, rc)
        self.assertEqual("cannot-tell", out[0])

    def test_a_task_with_no_open_boxes_is_not_resumed(self):
        self.page(body=TASK_PAGE.format(sid="sid-1").replace("- [ ] one", "- [x] one"))
        rc, out = self.run_it(live="ABSENT")
        self.assertEqual(0, rc)
        self.assertEqual("jump", out[0])

    def test_a_terminal_task_is_not_resumed(self):
        self.page(body=TASK_PAGE.format(sid="sid-1").replace(
            "status: in_progress", "status: completed"))
        rc, out = self.run_it(live="ABSENT")
        self.assertEqual(0, rc)
        self.assertEqual("jump", out[0])

    def test_no_recorded_id_is_cannot_tell(self):
        self.page(body=TASK_PAGE.format(sid=""))
        rc, out = self.run_it(live="ABSENT")
        self.assertEqual(1, rc)
        self.assertEqual("cannot-tell", out[0])

    # --- a park is not a death ------------------------------------------------

    def test_a_held_task_is_not_resumed(self):
        self.page(body=TASK_PAGE.format(sid="sid-1").replace(
            "status: in_progress", "status: hold"))
        rc, out = self.run_it(live="ABSENT")
        self.assertEqual(0, rc)
        self.assertEqual("jump", out[0])
        self.assertIn("parked", out[1])

    def test_a_task_in_human_review_is_not_resumed(self):
        self.page(body=TASK_PAGE.format(sid="sid-1").replace(
            "phase: execution", "phase: human_review"))
        rc, out = self.run_it(live="ABSENT")
        self.assertEqual(0, rc)
        self.assertEqual("jump", out[0])
        self.assertIn("parked", out[1])

    def test_a_deferred_task_is_not_resumed(self):
        self.page(body=TASK_PAGE.format(sid="sid-1").replace(
            "phase: execution", "phase: execution\ndefer_date: \"2999-01-01\""))
        rc, out = self.run_it(live="ABSENT")
        self.assertEqual(0, rc)
        self.assertEqual("jump", out[0])
        self.assertIn("deferred", out[1])

    # --- the cap --------------------------------------------------------------

    def test_fourth_resume_inside_the_window_is_a_crash_loop(self):
        self.page()
        now = 1_800_000_000.0
        self.seed([now - 60, now - 120, now - 180])
        rc, out = self.run_it(live="ABSENT", argv=("--now", str(now)))
        self.assertEqual(1, rc)
        self.assertEqual("crash-loop", out[0])
        self.assertIn("WORKER CRASH-LOOP", out[1])

    def test_two_spent_resumes_still_resume(self):
        self.page()
        now = 1_800_000_000.0
        self.seed([now - 60, now - 120])
        rc, out = self.run_it(live="ABSENT", argv=("--now", str(now)))
        self.assertEqual(0, rc)
        self.assertEqual("resume", out[0])
        self.assertIn("attempt 3 of 3", out[1])

    def test_a_resume_outside_the_window_does_not_count(self):
        self.page()
        now = 1_800_000_000.0
        self.seed([now - 3600, now - 3700, now - 3800])
        rc, out = self.run_it(live="ABSENT", argv=("--now", str(now)))
        self.assertEqual(0, rc)
        self.assertEqual("resume", out[0])

    def test_a_resume_that_did_not_take_does_not_arm_the_cap(self):
        self.page()
        self.kp.session_verdict = lambda sid: "ABSENT"
        out = io.StringIO()
        with redirect_stdout(out):
            rc = self.k.main(["--vault", str(self.vault), "--task", "A Task",
                              "--record-resume", "sid-1"])
        self.assertEqual(1, rc)
        self.assertEqual("cannot-tell", out.getvalue().splitlines()[0])
        self.assertFalse(os.path.exists(
            self.k.state_path(str(self.vault), "A Task")))

    def test_a_confirmed_resume_is_recorded(self):
        self.page()
        self.kp.session_verdict = lambda sid: "LIVE"
        out = io.StringIO()
        with redirect_stdout(out):
            rc = self.k.main(["--vault", str(self.vault), "--task", "A Task",
                              "--record-resume", "sid-1"])
        self.assertEqual(0, rc)
        self.assertEqual("recorded", out.getvalue().splitlines()[0])
        self.assertTrue(os.path.exists(
            self.k.state_path(str(self.vault), "A Task")))


if __name__ == "__main__":
    unittest.main()
