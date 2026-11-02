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

    def test_a_mixed_page_resolves_claude_session_id_first(self):
        """A re-armed legacy page carries both keys; the surviving one is probed.

        The writer sets `claude_session_id` without clearing the retired key, so
        reading the retired key first would probe — and resume — the stale id.
        """
        self.page(sid="sid-old", extra="claude_session_id: sid-new\n")
        seen = []

        def verdict(sid):
            seen.append(sid)
            return "LIVE"

        self.k.session_verdict = verdict
        self.k.cadence = lambda subject, branch="goal": "OK"
        out = io.StringIO()
        with redirect_stdout(out):
            self.k.main(["--vault", str(self.vault), "--goal", "A Goal"])
        self.assertEqual(["sid-new"], seen)

    def test_no_recorded_id_without_a_flag_is_held(self):
        """An absent `manager_session_id` is an OPEN, not a resume, and rule 4 makes an open
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
        self.kwargs = []

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
        calls, kwargs = self.calls, self.kwargs

        class R:
            returncode = rc

        R.stdout, R.stderr = stdout, stderr

        def run(argv, *a, **kw):
            calls.append(argv)
            # ⚠️ The kwargs are recorded, not discarded. A stub that keeps only argv cannot
            # tell a bounded probe from an unbounded one, which is exactly how
            # `timeout=PROBE_TIMEOUT` was deleted with all eight tests still green.
            kwargs.append(kw)
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
        self._stub(1, stdout="LIVE — sid-1\n", stderr="ABSENT — no session id matches sid-1\n")
        self.assertEqual("ABSENT", self.k.session_verdict("sid-1"))

    def test_exit_1_without_the_absent_line_is_cannot_tell(self):
        """⚠️ **Exit 1 is ABSENT's code, and it is also every Python traceback's code.**

        The exit-code read is right, but on its own it turned a crashed instrument into a
        confident `ABSENT` — the one verdict that authorises a resume, so a broken install
        would resume a manager that is already running. `session-liveness.py`'s `--check`
        path prints `ABSENT — <message>` to stderr on every genuine ABSENT, so the exit code
        is corroborated by the word before it is believed. Same rule as `UNKNOWN` for an
        undocumented code: cannot-tell is never death.
        """
        self._stub(1, stdout="", stderr='Traceback (most recent call last):\n  ...\n'
                                       'KeyError: "session_id"\n')
        self.assertEqual("UNKNOWN", self.k.session_verdict("sid-1"))

    def test_exit_1_with_the_absent_line_is_absent_whatever_the_separator(self):
        # The corroboration is the VERDICT WORD, not the em dash: a message-format change
        # must not silently turn every dead session into a cannot-tell.
        for stderr in ("ABSENT — no session id matches sid-1\n",
                       "ABSENT: no session id matches sid-1\n",
                       "\nABSENT — no session id matches sid-1\n"):
            with self.subTest(stderr=stderr):
                self._stub(1, stdout="", stderr=stderr)
                self.assertEqual("ABSENT", self.k.session_verdict("sid-1"))

    def test_a_non_absent_word_on_stderr_does_not_license_absent(self):
        # The word must be the verdict, not a substring of some other line — `ABSENTEE` and
        # a prose mention are both not the instrument's ABSENT.
        for stderr in ("ABSENTEE — sid-1\n", "warning: ABSENT is licensed per id\n"):
            with self.subTest(stderr=stderr):
                self._stub(1, stdout="", stderr=stderr)
                self.assertEqual("UNKNOWN", self.k.session_verdict("sid-1"))

    def test_the_probe_is_bounded_by_probe_timeout(self):
        """⚠️ Deleting `timeout=PROBE_TIMEOUT` must turn this red.

        The hung-child guard is the one thing between a wedged `session-liveness.py` and a
        keep-alive tick that never returns. Before this test the kwargs were never asserted,
        so the bound could be dropped and every other test stayed green — the guard was
        covered by nothing. Asserted on the keyword, not on a `PROBE_TIMEOUT` import, so the
        value is free to change while its use stays pinned.
        """
        self._stub(0)
        self.k.session_verdict("sid-1")
        self.assertEqual(1, len(self.kwargs))
        self.assertIn("timeout", self.kwargs[0])
        self.assertEqual(self.k.PROBE_TIMEOUT, self.kwargs[0]["timeout"])

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
        # ABSENT is the one code that also carries a corroborating stderr line; the other
        # four are believed on the exit code alone.
        for rc, want in ((0, "LIVE"), (1, "ABSENT"), (2, "UNKNOWN"),
                         (3, "AMBIGUOUS"), (4, "MALFORMED")):
            self._stub(rc, stderr="ABSENT — sid-1\n" if rc == 1 else "")
            self.assertEqual(want, self.k.session_verdict("sid-1"), rc)

    def test_an_undocumented_exit_is_cannot_tell(self):
        self._stub(7)
        self.assertEqual("UNKNOWN", self.k.session_verdict("sid-1"))


class VerdictTableTest(unittest.TestCase):
    """`VERDICT_BY_EXIT` must equal the exit codes `session-liveness.py` actually emits.

    The table is a duplicate of the instrument's contract, and it has to be: both scripts run
    standalone out of the plugin cache, so there is no shared module to import at run time.
    A duplicate with no drift check is how the two silently disagree — the instrument gains a
    sixth verdict or renumbers one, this file keeps mapping the old numbers, and a live
    session becomes a resume (or a dead one becomes a cannot-tell) with nothing red. The
    flag-predicate anti-drift test above is the same control for the same reason.
    """

    def setUp(self):
        self.k = load()

    def _instrument(self):
        # ⚠️ `session-liveness.py` is hyphenated, so it cannot be `import`ed by name —
        # `spec_from_file_location` is the only route, and it is the same one `load()` uses.
        spec = importlib.util.spec_from_file_location(
            "session_liveness", str(_HERE.parent / "session-liveness.py")
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_the_table_matches_the_instrument_constants(self):
        instrument = self._instrument()
        self.assertEqual(
            {
                instrument.LIVE: "LIVE",
                instrument.ABSENT: "ABSENT",
                instrument.UNKNOWN: "UNKNOWN",
                instrument.AMBIGUOUS: "AMBIGUOUS",
                instrument.MALFORMED: "MALFORMED",
            },
            self.k.VERDICT_BY_EXIT,
        )

    def test_the_constants_are_distinct_codes(self):
        # A table can match a renumbered instrument only if the codes stay distinct; if two
        # constants ever collided, the dict comparison above would collapse them silently.
        instrument = self._instrument()
        codes = [instrument.LIVE, instrument.ABSENT, instrument.UNKNOWN,
                 instrument.AMBIGUOUS, instrument.MALFORMED]
        self.assertEqual(len(codes), len(set(codes)), codes)


class CadenceTest(unittest.TestCase):
    """`cadence()` is the second probe, and it must be bounded and guarded like the first.

    `PROBE_TIMEOUT`'s comment claims a hung probe cannot wedge the tick, and the tick runs
    BOTH probes — so an unbounded `manager-liveness.py` made that comment false for half its
    own scope. A hung child here blocks the keep-alive forever, which is worse than the
    abort the guard prevents: no verdict is ever printed.
    """

    def setUp(self):
        self.k = load()
        self._orig = self.k.subprocess
        self._orig_verdict = self.k.session_verdict
        self.kwargs = []
        self.addCleanup(self._restore)

    def _restore(self):
        self.k.subprocess = self._orig

    def _namespace(self, run):
        return types.SimpleNamespace(
            run=run,
            SubprocessError=subprocess.SubprocessError,
            TimeoutExpired=subprocess.TimeoutExpired,
        )

    def _stub(self, rc=0, stdout="OK\tslug\tdetail\n", stderr=""):
        kwargs = self.kwargs

        class R:
            returncode = rc

        R.stdout, R.stderr = stdout, stderr

        def run(argv, *a, **kw):
            kwargs.append(kw)
            return R()

        self.k.subprocess = self._namespace(run)

    def _stub_raising(self, exc):
        def run(*a, **kw):
            raise exc

        self.k.subprocess = self._namespace(run)

    def test_the_cadence_probe_is_bounded_by_probe_timeout(self):
        self._stub()
        self.k.cadence("A Goal")
        self.assertEqual(1, len(self.kwargs))
        self.assertIn("timeout", self.kwargs[0])
        self.assertEqual(self.k.PROBE_TIMEOUT, self.kwargs[0]["timeout"])

    def test_an_unrunnable_cadence_probe_does_not_raise(self):
        # Without the guard a missing interpreter aborts the whole tick with a traceback.
        self._stub_raising(OSError("no interpreter"))
        self.assertEqual(self.k.PROBE_FAILED, self.k.cadence("A Goal"))

    def test_a_hung_cadence_probe_does_not_raise(self):
        self._stub_raising(subprocess.TimeoutExpired("probe", 30))
        self.assertEqual(self.k.PROBE_FAILED, self.k.cadence("A Goal"))

    def test_a_crashed_cadence_probe_is_not_read_as_a_corrupt_record(self):
        """⚠️ A probe that could not answer is a fact about the PROBE, not the subject.

        `INVALID` is `manager-liveness.py`'s own verdict for a record that exists and cannot
        be read — and `main` treats `INVALID` on a LIVE session as `restart`. Folding a
        crashed probe into it would restart a live manager on no evidence at all.
        """
        self._stub(rc=1, stdout="", stderr="Traceback (most recent call last):\n")
        self.assertNotEqual("INVALID", self.k.cadence("A Goal"))
        self.assertEqual(self.k.PROBE_FAILED, self.k.cadence("A Goal"))

    def test_every_documented_state_still_maps(self):
        for state in ("OK", "STALE", "STOPPED", "UNARMED", "INVALID"):
            with self.subTest(state=state):
                self._stub(stdout=f"{state}\tslug\tdetail\n")
                self.assertEqual(state, self.k.cadence("A Goal"))

    def test_an_unrecognised_state_is_invalid(self):
        # The pre-existing fallback, unchanged: a well-formed exit with unparseable text is
        # `manager-liveness.py`'s own INVALID, not a probe failure.
        self._stub(stdout="BANANA\tslug\tdetail\n")
        self.assertEqual("INVALID", self.k.cadence("A Goal"))

    def test_an_unreadable_loop_never_licenses_a_restart(self):
        """The consequence, pinned end to end: LIVE + unreadable probe must not restart."""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        vault = pathlib.Path(tmp.name) / "vault"
        (vault / "24 Goals").mkdir(parents=True)
        (vault / "24 Goals" / "A Goal.md").write_text(
            GOAL_PAGE.format(sid="sid-1", extra=""), encoding="utf-8"
        )
        state = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(state, ignore_errors=True))
        original = self.k.state_path
        self.k.state_path = lambda v, subject, branch="goal": str(
            state / f"{branch}--{self.k.base_slug(subject)}.json"
        )
        self.addCleanup(lambda: setattr(self.k, "state_path", original))

        self.k.session_verdict = lambda sid: "LIVE"
        self.addCleanup(lambda: setattr(self.k, "session_verdict", self._orig_verdict))
        self._stub_raising(OSError("no interpreter"))
        out = io.StringIO()
        with redirect_stdout(out):
            self.k.main(["--vault", str(vault), "--goal", "A Goal"])
        self.assertEqual("jump", out.getvalue().splitlines()[0])


if __name__ == "__main__":
    unittest.main()
