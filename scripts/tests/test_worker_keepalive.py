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
        # The crash-loop card is injected, never left to post a real one to the board.
        self.cards = []
        # The stub mirrors the real guard rather than appending unconditionally: a stub that
        # ignored `card_is_noop` would let a crash-loop test read "one card" while the
        # production path posts nothing — the one thing these assertions exist to catch.
        self.k.post_card = self._stub_post_card
        self.kp = self.k._keepalive()
        self.op = self.k._owner()
        self.state = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(self.state, ignore_errors=True))
        self._orig = (self.kp.session_verdict, self.kp.state_path,
                      self.kp.cadence_state_detail, self.op.session_verdict,
                      self.op.loop_state)
        self.kp.state_path = lambda vault, subject, branch="goal": str(
            self.state / f"{branch}--{self.kp.base_slug(subject)}.json"
        )
        # ⚠️ The loop probe is injected, never left to read this machine's
        # `~/.claude/state/sweep-gate` tree: a test whose verdict depends on whether a real
        # goal happens to be armed asserts nothing about the code.
        self.kp.cadence_state_detail = lambda subject, branch="goal": ("UNARMED", "")
        # ⚠️ TWO probes, not one: `main()` reads the keep-alive module's, while `of_subject()`
        # — now behind `goal_owners()` — reads `owner.py`'s own. Routing owner's through the
        # keep-alive seams keeps every per-test patch reaching both; a stub on one alone leaves
        # the other hitting this machine's real registry and cadence tree.
        self.op.session_verdict = lambda sid: self.kp.session_verdict(sid)
        self.op.loop_state = lambda subject, branch="goal", vault="": \
            self.kp.cadence_state_detail(subject, branch)
        self.addCleanup(self._restore)

    def _stub_post_card(self, task, spent, recommend="keep stopped",
                        current="keep stopped"):
        if self.k.card_is_noop(recommend, current):
            return
        self.cards.append((task, spent))

    def _restore(self):
        (self.kp.session_verdict, self.kp.state_path,
         self.kp.cadence_state_detail, self.op.session_verdict,
         self.op.loop_state) = self._orig

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
        # flagged tick protects on the other side.
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

    # --- a dangling `goals:` link is reported, not dropped ----------------------

    def test_a_dangling_goals_link_is_reported_and_still_resumes(self):
        """A declared parent whose page does not exist must not vanish from the owners list.

        `goal_owners()` used to `continue` past the unopenable page, so the list came back
        empty and the leg resumed the row with nothing said. The action is unchanged —
        `not-owner` is exactly what licenses the resume — but the reason must reach the
        output, or a broken link is indistinguishable from a row that declares no goal.

        ⚠️ **`out[:2]`, not `out`.** `agents/fleet-keepalive-actor.md` slices `lines[:2]` into
        one TSV row, so a note printed on line 3 is discarded before the operator sees it —
        the first draft of this change passed a whole-output assertion while being invisible
        in the leg's own report. Asserting the two-line shape is what pins the fix.
        """
        self.page(body=TASK_PAGE.format(sid="sid-1").replace(
            "phase: execution", "phase: execution\ngoals:\n    - '[[No Such Goal]]'"))
        (self.vault / "24 Goals").mkdir(parents=True)
        rc, out = self.run_it(live="ABSENT")
        self.assertEqual(0, rc)
        self.assertEqual("resume", out[0])
        self.assertEqual(
            2, len(out),
            f"the report must stay two lines — the consumer keeps only lines[:2]: {out!r}")
        self.assertIn(
            "No Such Goal", out[1],
            f"the dangling link is not named on the reason line: {out!r}")
        self.assertIn(
            "not-owner", out[1],
            f"the dangling link's verdict is not reported on the reason line: {out!r}")

    def test_a_task_with_no_goals_key_reports_no_goal_verdict(self):
        """The control: a fleet-tier row stays quiet.

        A row with no `goals:` key returns `[]` and this leg resumes it by design. If the
        dangling-link note fired here too, the two shapes would be indistinguishable — which
        is the defect this task exists to remove.
        """
        self.page()
        rc, out = self.run_it(live="ABSENT")
        self.assertEqual(0, rc)
        self.assertEqual("resume", out[0])
        self.assertFalse(
            any("not-owner" in line for line in out),
            f"a goal-less row must not report a goal verdict: {out!r}")

    def test_goal_owners_and_of_subject_agree_on_a_dangling_link(self):
        """The reconciliation: one doctrine, not two.

        `goal_owners()` must answer exactly what `of_subject()` answers for the same page, or
        the module's known-absence rule holds for only one of its two entry points.
        """
        fm = TASK_PAGE.format(sid="sid-1").replace(
            "phase: execution", "phase: execution\ngoals:\n    - '[[No Such Goal]]'")
        (self.vault / "24 Goals").mkdir(parents=True)
        owners = self.k.goal_owners(fm, str(self.vault), "24 Goals")
        direct = self.k._owner().of_subject(
            str(self.vault), "No Such Goal", "goal", "24 Goals")
        self.assertEqual(1, len(owners), owners)
        self.assertEqual(direct.verdict, owners[0].verdict)
        self.assertEqual(direct.reason, owners[0].reason)

    def test_an_unopenable_goal_page_is_cannot_tell_and_never_resumes(self):
        """The side effect the delegation buys: an unreadable page is not an absent one.

        A page that exists but cannot be read may record a live manager; resuming on it is
        the second writer `owner.py` exists to withhold. It used to be `continue`d into `[]`.
        """
        self.page(body=TASK_PAGE.format(sid="sid-1").replace(
            "phase: execution", "phase: execution\ngoals:\n    - '[[A Goal]]'"))
        (self.vault / "24 Goals").mkdir(parents=True)
        # A DIRECTORY named `<subject>.md`: `locate()` matches the entry, the read fails.
        (self.vault / "24 Goals" / "A Goal.md").mkdir()
        rc, out = self.run_it(live="ABSENT")
        self.assertEqual(1, rc)
        self.assertEqual("cannot-tell", out[0])

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

    def test_no_progress_cycles_spaced_beyond_the_old_window_still_stop(self):
        # SC1: for a migrated store the cap is cycles since the last progress, not a time
        # window. Three cycles ~40 min apart with no progress must still stop the loop, and
        # no card is posted — the `crash-loop:` hand-back row is the channel.
        self.page()
        now = 1_800_000_000.0
        sig = self.k.progress_signature(TASK_PAGE.format(sid="sid-1"))
        with open(self.k.state_path(str(self.vault), "A Task"), "w", encoding="utf-8") as fh:
            json.dump({"resumes": [now - 2400, now - 4800, now - 7200],
                       "progress": {"open_boxes": sig[0], "progress_hash": sig[1],
                                    "head_sha": ""}}, fh)
        rc, out = self.run_it(live="ABSENT", argv=("--now", str(now)))
        self.assertEqual(1, rc)
        self.assertEqual("crash-loop", out[0])
        self.assertEqual([], self.cards)

    def test_a_goal_declaring_crash_loop_posts_no_operator_card(self):
        # SC2: a resolvable goal makes the row manager-owned — the `crash-loop:` hand-back
        # row is the channel, so the operator is never asked to decide a resume the manager
        # tier is obliged to make. The card would be a decision the manager already owns.
        self.page(body=TASK_PAGE.format(sid="sid-1").replace(
            "phase: execution",
            "phase: execution\ngoals:\n    - '[[A Goal]]'"))
        (self.vault / "24 Goals").mkdir(parents=True)
        (self.vault / "24 Goals" / "A Goal.md").write_text(
            "---\npage_type: goal\n---\n", encoding="utf-8")
        now = 1_800_000_000.0
        self.seed([now - 60, now - 120, now - 180])
        rc, out = self.run_it(live="ABSENT", argv=("--now", str(now)))
        self.assertEqual(1, rc)
        self.assertEqual("crash-loop", out[0])
        self.assertIn("WORKER CRASH-LOOP", out[1])
        self.assertEqual([], self.cards)

    def test_a_goal_page_that_cannot_be_opened_posts_no_card(self):
        # ⚠️ SUPERSEDES #1056 (v0.362.9), which routed this row to the operator on the
        # reasoning that "the operator is the only decision left". The card recommends
        # `keep stopped` and the row is already stopped, so the operator was asked to ratify
        # the no-op the system performs on its own — 189 such cards in 24 h, none answered.
        # The verdict stays `crash-loop`; the routing moves to the fleet leg's hand-back,
        # which now reports every crash-loop row.
        self.page(body=TASK_PAGE.format(sid="sid-1").replace(
            "phase: execution",
            "phase: execution\ngoals:\n    - '[[A Missing Goal]]'"))
        now = 1_800_000_000.0
        self.seed([now - 60, now - 120, now - 180])
        rc, out = self.run_it(live="ABSENT", argv=("--now", str(now)))
        self.assertEqual(1, rc)
        self.assertEqual("crash-loop", out[0])
        self.assertEqual([], self.cards)

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

    def test_progress_resets_the_cycle_count(self):
        # SC2: three resumes are on record, but the task advanced since the last one
        # (a box ticked / Progress changed). The next record resets the count, so the
        # following probe resumes instead of refusing.
        self.page()
        now = 1_800_000_000.0
        with open(self.k.state_path(str(self.vault), "A Task"), "w", encoding="utf-8") as fh:
            json.dump({"resumes": [now - 60, now - 120, now - 180],
                       "progress": {"open_boxes": 9, "progress_hash": "stale", "head_sha": ""}},
                      fh)
        self.kp.session_verdict = lambda sid: "LIVE"
        out = io.StringIO()
        with redirect_stdout(out):
            rc = self.k.main(["--vault", str(self.vault), "--task", "A Task",
                              "--record-resume", "sid-1", "--now", str(now)])
        self.assertEqual(0, rc)
        rc, out = self.run_it(live="ABSENT", argv=("--now", str(now)))
        self.assertEqual(0, rc)
        self.assertEqual("resume", out[0])
        self.assertIn("attempt 2 of 3", out[1])

    def test_progress_before_the_next_resume_is_not_a_crash_loop(self):
        # A worker that advanced and then died must not be refused for the cycles it ran
        # before it advanced, even though no --record-resume has happened yet.
        self.page()
        now = 1_800_000_000.0
        with open(self.k.state_path(str(self.vault), "A Task"), "w", encoding="utf-8") as fh:
            json.dump({"resumes": [now - 60, now - 120, now - 180],
                       "progress": {"open_boxes": 9, "progress_hash": "stale", "head_sha": ""}},
                      fh)
        rc, out = self.run_it(live="ABSENT", argv=("--now", str(now)))
        self.assertEqual(0, rc)
        self.assertEqual("resume", out[0])

    def test_a_new_commit_counts_as_progress(self):
        # The commit signal: a changed HEAD on the task's repo: branch is progress.
        prev = {"open_boxes": 1, "progress_hash": "h", "head_sha": "aaa"}
        self.assertTrue(self.k.made_progress(prev, (1, "h"), "bbb"))
        self.assertFalse(self.k.made_progress(
            {"open_boxes": 1, "progress_hash": "h", "head_sha": "bbb"}, (1, "h"), "bbb"))

    def test_legacy_store_counts_only_the_old_window_on_the_first_probe(self):
        # A pre-progress store has no baseline. Its old resumes must not read as a live
        # crash loop: the first probe falls back to the retired 30-minute window.
        self.page()
        now = 1_800_000_000.0
        self.seed([now - 3600, now - 7200, now - 10800])
        rc, out = self.run_it(live="ABSENT", argv=("--now", str(now)))
        self.assertEqual(0, rc)
        self.assertEqual("resume", out[0])

    def test_legacy_store_inside_the_window_still_stops(self):
        self.page()
        now = 1_800_000_000.0
        self.seed([now - 60, now - 120, now - 180])
        rc, out = self.run_it(live="ABSENT", argv=("--now", str(now)))
        self.assertEqual(1, rc)
        self.assertEqual("crash-loop", out[0])

    def test_a_record_with_no_task_page_still_counts(self):
        # No page to measure progress against: the resume is recorded with no marker.
        self.kp.session_verdict = lambda sid: "LIVE"
        out = io.StringIO()
        with redirect_stdout(out):
            rc = self.k.main(["--vault", str(self.vault), "--task", "A Task",
                              "--record-resume", "sid-1", "--now", "1800000000"])
        self.assertEqual(0, rc)
        self.assertEqual("recorded", out.getvalue().splitlines()[0])
        with open(self.k.state_path(str(self.vault), "A Task"), encoding="utf-8") as fh:
            self.assertEqual(1, len(json.load(fh)["resumes"]))


class CrashLoopCardPayloadTest(unittest.TestCase):
    """The guard, and the payload of the one card shape that still survives it.

    The crash-loop tests above stub `post_card` out, so they prove the verdict and whether a
    card is posted, never what it says. Two things are pinned here against the real
    `post_card`: that a no-op recommendation posts nothing, and that a card whose
    recommendation differs from the current state still posts a real question — the control
    without which a blanket `return` would pass every other test in this file.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.k = load()
        self.calls = []
        self._orig_run = self.k.subprocess.run
        self.k.subprocess.run = self._capture

    def tearDown(self):
        self.k.subprocess.run = self._orig_run

    def _capture(self, argv, **kwargs):
        self.calls.append(argv)

    def test_a_noop_recommendation_posts_nothing(self):
        # The defect: the crash-loop card recommends `keep stopped` for a row that is
        # already stopped, so it asks the operator to ratify what the system does anyway.
        self.k.post_card("A Task", 3)
        self.assertEqual([], self.calls)

    def test_a_recommendation_that_differs_from_the_current_state_still_posts(self):
        # The control that makes the guard a filter and not a blanket suppression. Nothing
        # in this script asks a non-no-op question today, so the parameters are the only way
        # to exercise the surviving branch.
        self.k.post_card("A Task", 3, recommend="resume anyway", current="keep stopped")
        self.assertEqual(1, len(self.calls))
        argv = self.calls[0]
        self.assertEqual("resume anyway", argv[argv.index("--recommend") + 1])

    def test_the_surviving_card_carries_a_question_naming_the_decision(self):
        self.k.post_card("A Task", 3, recommend="resume anyway", current="keep stopped")
        self.assertEqual(1, len(self.calls))
        argv = self.calls[0]
        payload = argv[argv.index("--payload") + 1]
        self.assertIn("Resume anyway?", payload)
        self.assertIn("A Task", payload)
        self.assertIn("3 resumes", payload)
        # A question with no options is not answerable, and the dedup key is the join back
        # to the mechanism — one card per task, never one per crash loop.
        self.assertEqual(["keep stopped", "resume anyway"],
                         [argv[i + 1] for i, a in enumerate(argv) if a == "--option"])
        self.assertEqual("crash-loop:a-task", argv[argv.index("--dedup-key") + 1])


if __name__ == "__main__":
    unittest.main()
