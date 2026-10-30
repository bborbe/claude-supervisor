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
        self.k.state_path = lambda vault, subject, branch="goal": str(
            self.state / f"{branch}--{self.k.base_slug(subject)}.json"
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
        self.k.cadence = lambda subject, branch="goal": cadence
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


    # --- the topic branch -----------------------------------------------------

    def test_topic_branch_reads_the_topic_directory_and_writes_its_own_cap(self):
        (self.vault / "23 Topics").mkdir(parents=True)
        (self.vault / "23 Topics" / "A Topic.md").write_text(
            GOAL_PAGE.format(sid="sid-2", extra=""), encoding="utf-8"
        )
        self.k.session_verdict = lambda sid: "LIVE"
        self.k.cadence = lambda subject, branch="goal": "OK"
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
        self.k.cadence = lambda subject, branch="goal": "OK"
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
        self.k.cadence = lambda subject, branch="goal": "OK"
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
        self.k.cadence = lambda subject, branch="goal": "STOPPED"
        out = io.StringIO()
        with redirect_stdout(out):
            rc = self.k.main(["--vault", str(self.vault), "--subject", "A Topic",
                              "--branch", "topic"])
        self.assertEqual(0, rc)
        self.assertEqual("leave", out.getvalue().splitlines()[0])

    def test_the_goal_alias_still_works(self):
        self.page()
        rc, out = self.run_keepalive(live="ABSENT")
        self.assertEqual(0, rc)
        self.assertEqual("resume", out[0])


class SessionVerdictTest(unittest.TestCase):
    """`session_verdict` must read the instrument's EXIT CODE, not its text.

    The instrument prints only `LIVE` to stdout; every other verdict goes to stderr. A
    stdout-scanning implementation therefore reports `UNKNOWN` for a dead session, which makes
    the resume branch unreachable — the whole keep-alive inert. These tests pin the contract
    the instrument actually documents.
    """

    def setUp(self):
        self.k = load()
        self._orig = self.k.subprocess
        self.calls = []

    def tearDown(self):
        self.k.subprocess = self._orig

    def _namespace(self, run):
        # ⚠️ Replace the module's `subprocess` REFERENCE, never `subprocess.run` on the stdlib
        # module itself: `import subprocess` binds one shared object, so patching its attribute
        # reaches every other test in the process. The exception types ride along because
        # `session_verdict`'s `except` clause names `subprocess.SubprocessError`, which Python
        # resolves on this very attribute at raise time.
        return types.SimpleNamespace(
            run=run,
            SubprocessError=subprocess.SubprocessError,
            TimeoutExpired=subprocess.TimeoutExpired,
        )

    def _stub(self, rc, stdout="", stderr=""):
        calls = self.calls

        class R:
            returncode = rc

        R.stdout, R.stderr = stdout, stderr

        def run(argv, *a, **kw):
            calls.append(argv)
            return R()

        self.k.subprocess = self._namespace(run)

    def _stub_raising(self, exc):
        def run(*a, **kw):
            raise exc

        self.k.subprocess = self._namespace(run)

    def test_absent_is_read_from_exit_1_even_though_nothing_reaches_stdout(self):
        # The measured shape: stdout empty, the verdict on stderr, exit 1.
        self._stub(1, stdout="", stderr="ABSENT — sid-1\n")
        self.assertEqual("ABSENT", self.k.session_verdict("sid-1"))

    def test_live_is_read_from_exit_0(self):
        self._stub(0, stdout="LIVE — sid-1\n")
        self.assertEqual("LIVE", self.k.session_verdict("sid-1"))

    def test_the_exit_code_wins_over_a_contradictory_stdout(self):
        # The regression this change removes, pinned directly: the old implementation read
        # stdout, so rc 1 with `LIVE` on stdout answered LIVE — a dead session read as alive,
        # the one direction that spawns a second manager on a live one.
        self._stub(1, stdout="LIVE — sid-1\n")
        self.assertEqual("ABSENT", self.k.session_verdict("sid-1"))

    def test_the_probe_is_invoked_with_check_and_the_id(self):
        # A stub that ignores argv cannot tell a correct invocation from a broken one.
        self._stub(0)
        self.k.session_verdict("sid-1")
        self.assertEqual(1, len(self.calls))
        self.assertEqual(sys.executable, self.calls[0][0])
        self.assertEqual(["--check", "sid-1"], self.calls[0][-2:])

    def test_an_unrunnable_probe_is_cannot_tell(self):
        # A missing interpreter or script raises OSError; it must not abort the tick.
        self._stub_raising(OSError("no interpreter"))
        self.assertEqual("UNKNOWN", self.k.session_verdict("sid-1"))

    def test_a_hung_probe_is_cannot_tell(self):
        self._stub_raising(subprocess.TimeoutExpired("probe", 30))
        self.assertEqual("UNKNOWN", self.k.session_verdict("sid-1"))

    def test_every_documented_code_maps(self):
        for rc, want in ((0, "LIVE"), (1, "ABSENT"), (2, "UNKNOWN"),
                         (3, "AMBIGUOUS"), (4, "MALFORMED")):
            self._stub(rc)
            self.assertEqual(want, self.k.session_verdict("sid-1"), rc)

    def test_an_undocumented_exit_is_cannot_tell(self):
        self._stub(7)
        self.assertEqual("UNKNOWN", self.k.session_verdict("sid-1"))


if __name__ == "__main__":
    unittest.main()
