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
        self._orig = (self.kp.session_verdict, self.kp.state_path,
                      self.kp.cadence_state_detail)
        self.kp.state_path = lambda vault, subject, branch="goal": str(
            self.state / f"{branch}--{self.kp.base_slug(subject)}.json"
        )
        # ⚠️ The loop probe is injected, never left to read this machine's
        # `~/.claude/state/sweep-gate` tree: a test whose verdict depends on whether a real
        # goal happens to be armed asserts nothing about the code.
        self.kp.cadence_state_detail = lambda subject, branch="goal": ("UNARMED", "")
        self.addCleanup(self._restore)

    def _restore(self):
        (self.kp.session_verdict, self.kp.state_path,
         self.kp.cadence_state_detail) = self._orig

    def goal_loop(self, state):
        """Give every goal page this test reads one loop state."""
        self.kp.cadence_state_detail = lambda subject, branch="goal": (state, "detail")

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

    def test_every_spelling_of_no_id_is_one_fact(self):
        """Absent, empty and literal `null` must be the same fact: nothing recorded.

        The `null` shape is the measured defect (2026-11-08). `scalar()` returns the four
        characters `null`, which is truthy, and `session-liveness.py` admits any lowercase
        prefix as a legal argument — so the probe answered ABSENT and this script printed
        `resume` for a session that was never named. The assertion is on the probe NOT being
        called, which is the state-independent form: a version that folded the id but still
        probed it would pass an output-only check on a day the registry read failed.
        """
        shapes = {
            "absent": TASK_PAGE.format(sid="sid-1").replace(
                "claude_session_id: sid-1\n", ""),
            "empty": TASK_PAGE.format(sid=""),
            "literal null": TASK_PAGE.format(sid="null"),
        }
        for name, body in shapes.items():
            with self.subTest(shape=name):
                self.page(body=body)
                probed = []

                def boom(sid):
                    probed.append(sid)
                    raise AssertionError(f"session_verdict({sid!r}) must not be called")

                self.kp.session_verdict = boom
                out = io.StringIO()
                with redirect_stdout(out):
                    rc = self.k.main(["--vault", str(self.vault), "--task", "A Task"])
                self.assertEqual(1, rc)
                self.assertEqual("cannot-tell", out.getvalue().splitlines()[0])
                self.assertEqual([], probed)

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

    def test_a_todo_task_is_not_resumed(self):
        # `phase: todo` is the operator's approval boundary — the same line the
        # flagged-open leg protects on the other side.
        self.page(body=TASK_PAGE.format(sid="sid-1").replace(
            "phase: execution", "phase: todo"))
        rc, out = self.run_it(live="ABSENT")
        self.assertEqual(0, rc)
        self.assertEqual("jump", out[0])
        self.assertIn("parked", out[1])

    def test_a_done_phase_is_not_resumed(self):
        self.page(body=TASK_PAGE.format(sid="sid-1").replace(
            "phase: execution", "phase: done"))
        rc, out = self.run_it(live="ABSENT")
        self.assertEqual(0, rc)
        self.assertEqual("jump", out[0])
        self.assertIn("parked", out[1])

    def test_a_blocked_status_is_not_resumed(self):
        self.page(body=TASK_PAGE.format(sid="sid-1").replace(
            "status: in_progress", "status: blocked"))
        rc, out = self.run_it(live="ABSENT")
        self.assertEqual(0, rc)
        self.assertEqual("jump", out[0])
        self.assertIn("parked", out[1])

    def test_a_task_owned_by_a_live_goal_manager_is_left_alone(self):
        # The mutual exclusion with goal-drive's orphan gate: the fleet leg must not race it.
        self.page(body=TASK_PAGE.format(sid="sid-1").replace(
            "phase: execution", "phase: execution\ngoals:\n    - '[[A Goal]]'"))
        (self.vault / "24 Goals").mkdir(parents=True)
        (self.vault / "24 Goals" / "A Goal.md").write_text(
            "---\nclaude_session_id: gm-1\n---\n# Success Criteria\n", encoding="utf-8")
        self.kp.session_verdict = lambda sid: "LIVE" if sid == "gm-1" else "ABSENT"
        self.goal_loop("OK")
        out = io.StringIO()
        with redirect_stdout(out):
            rc = self.k.main(["--vault", str(self.vault), "--task", "A Task"])
        self.assertEqual(0, rc)
        lines = out.getvalue().splitlines()
        self.assertEqual("jump", lines[0])
        # ⚠️ The reason must CARRY the evidence, not just the verdict: the round log is the
        # only durable record that this jump rested on a live-and-armed manager.
        self.assertIn("goal manager gm-1 is an owner (session LIVE, loop OK)", lines[1])

    def test_a_goal_manager_stopped_with_its_loop_is_not_an_owner(self):
        """THE BOUNDARY this task was filed for.

        A goal manager that ran `/supervisor:stop` keeps its session and its loop marker says
        `STOPPED`. Reading the session alone made it an owner, the fleet leg deferred to it,
        and the goal round that would resume the worker is exactly what stopped — so the
        worker had no keeper. It must fall through to the fleet leg's own resume.
        """
        self.page(body=TASK_PAGE.format(sid="sid-1").replace(
            "phase: execution", "phase: execution\ngoals:\n    - '[[A Goal]]'"))
        (self.vault / "24 Goals").mkdir(parents=True)
        (self.vault / "24 Goals" / "A Goal.md").write_text(
            "---\nclaude_session_id: gm-1\n---\n", encoding="utf-8")
        self.kp.session_verdict = lambda sid: "LIVE" if sid == "gm-1" else "ABSENT"
        self.goal_loop("STOPPED")
        out = io.StringIO()
        with redirect_stdout(out):
            rc = self.k.main(["--vault", str(self.vault), "--task", "A Task"])
        self.assertEqual(0, rc)
        self.assertEqual("resume", out.getvalue().splitlines()[0])

    def test_an_unreadable_owner_probe_never_hands_the_worker_to_the_fleet(self):
        # `not-owner` is what licenses a resume. A probe that could not answer is
        # `cannot-tell`, and resuming on it is the second writer this whole tier avoids.
        self.page(body=TASK_PAGE.format(sid="sid-1").replace(
            "phase: execution", "phase: execution\ngoals:\n    - '[[A Goal]]'"))
        (self.vault / "24 Goals").mkdir(parents=True)
        (self.vault / "24 Goals" / "A Goal.md").write_text(
            "---\nclaude_session_id: gm-1\n---\n", encoding="utf-8")
        self.kp.session_verdict = lambda sid: "UNKNOWN"
        self.goal_loop("OK")
        out = io.StringIO()
        with redirect_stdout(out):
            rc = self.k.main(["--vault", str(self.vault), "--task", "A Task"])
        self.assertEqual(1, rc)
        self.assertEqual("cannot-tell", out.getvalue().splitlines()[0])

    def test_a_mixed_goal_page_resolves_claude_session_id_first(self):
        # A re-armed legacy page carries both keys. Reading the retired one first
        # would see the stale manager as dead and resume a task a live manager owns.
        self.page(body=TASK_PAGE.format(sid="sid-1").replace(
            "phase: execution", "phase: execution\ngoals:\n    - '[[A Goal]]'"))
        (self.vault / "24 Goals").mkdir(parents=True)
        (self.vault / "24 Goals" / "A Goal.md").write_text(
            "---\nmanager_session_id: gm-old\nclaude_session_id: gm-new\n---\n",
            encoding="utf-8")
        self.kp.session_verdict = lambda sid: "LIVE" if sid == "gm-new" else "ABSENT"
        self.goal_loop("OK")
        out = io.StringIO()
        with redirect_stdout(out):
            rc = self.k.main(["--vault", str(self.vault), "--task", "A Task"])
        self.assertEqual(0, rc)
        lines = out.getvalue().splitlines()
        self.assertEqual("jump", lines[0])
        self.assertIn("goal manager gm-new is an owner", lines[1])

    def test_a_task_whose_goal_manager_is_dead_is_still_resumed(self):
        self.page(body=TASK_PAGE.format(sid="sid-1").replace(
            "phase: execution", "phase: execution\ngoals:\n    - '[[A Goal]]'"))
        (self.vault / "24 Goals").mkdir(parents=True)
        (self.vault / "24 Goals" / "A Goal.md").write_text(
            "---\nclaude_session_id: gm-1\n---\n", encoding="utf-8")
        self.kp.session_verdict = lambda sid: "ABSENT"
        out = io.StringIO()
        with redirect_stdout(out):
            rc = self.k.main(["--vault", str(self.vault), "--task", "A Task"])
        self.assertEqual(0, rc)
        self.assertEqual("resume", out.getvalue().splitlines()[0])

    def test_a_wikilink_outside_goals_does_not_skip_the_worker(self):
        # A `themes:` link to a page with a live manager must not be read as ownership —
        # that would silently skip exactly the dead worker this leg exists to recover.
        self.page(body=TASK_PAGE.format(sid="sid-1").replace(
            "phase: execution", "phase: execution\nthemes:\n    - '[[A Theme]]'"))
        (self.vault / "24 Goals").mkdir(parents=True)
        (self.vault / "24 Goals" / "A Theme.md").write_text(
            "---\nclaude_session_id: gm-1\n---\n", encoding="utf-8")
        self.kp.session_verdict = lambda sid: "LIVE" if sid == "gm-1" else "ABSENT"
        out = io.StringIO()
        with redirect_stdout(out):
            rc = self.k.main(["--vault", str(self.vault), "--task", "A Task"])
        self.assertEqual(0, rc)
        self.assertEqual("resume", out.getvalue().splitlines()[0])

    def test_ambiguous_and_malformed_are_cannot_tell(self):
        self.page()
        for verdict in ("AMBIGUOUS", "MALFORMED"):
            rc, out = self.run_it(live=verdict)
            self.assertEqual(1, rc, verdict)
            self.assertEqual("cannot-tell", out[0], verdict)

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
    # --- a stop is not a death ------------------------------------------------

    def test_a_stopped_task_is_not_resumed(self):
        """SC3(b): the restart half of the crash-vs-stop distinction.

        Paired with the control below, which runs the identical page minus one line. An
        implementation that jumped every row would pass this case alone — and before this
        read existed the marker was on the page and nothing here looked at it, so a worker
        the operator stopped was resumed after its process died.
        """
        self.page(body=TASK_PAGE.format(sid="sid-1").replace(
            "phase: execution", "phase: execution\nstopped: true"))
        rc, out = self.run_it(live="ABSENT")
        self.assertEqual(0, rc)
        self.assertEqual("jump", out[0])
        self.assertIn("stopped", out[1])

    def test_the_same_task_without_the_marker_resumes(self):
        """The control: one line removed, the verdict flips to `resume`."""
        self.page()
        rc, out = self.run_it(live="ABSENT")
        self.assertEqual(0, rc)
        self.assertEqual("resume", out[0])

    def test_a_stop_outranks_a_defer(self):
        """A stopped and deferred row reports the stop, not the defer.

        Same ordering rule `open-queue.guards()` applies: the operator's own act outranks
        a derived state, so the reason names what actually ended the row.
        """
        self.page(body=TASK_PAGE.format(sid="sid-1").replace(
            "phase: execution",
            "phase: execution\nstopped: true\ndefer_date: \"2999-01-01\""))
        rc, out = self.run_it(live="ABSENT")
        self.assertEqual(0, rc)
        self.assertEqual("jump", out[0])
        self.assertIn("stopped", out[1])

    def test_a_false_stop_marker_still_resumes(self):
        """The control for the truthy set: `stopped: false` is not a stop."""
        self.page(body=TASK_PAGE.format(sid="sid-1").replace(
            "phase: execution", "phase: execution\nstopped: false"))
        rc, out = self.run_it(live="ABSENT")
        self.assertEqual(0, rc)
        self.assertEqual("resume", out[0])


if __name__ == "__main__":
    unittest.main()
