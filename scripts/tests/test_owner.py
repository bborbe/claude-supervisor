#!/usr/bin/env python3
"""Tests for scripts/owner.py — the one ownership definition.

Two boundaries carry the task this file exists for, and both are asserted here rather than in a
consumer, because a consumer that gets it wrong is exactly the defect the resolver removes:

  * a `LIVE` session whose loop is `STOPPED` is **not** an owner (its worker falls to the fleet);
  * a `LIVE` session whose loop is `OK` (or `STALE`) **is** an owner (its worker is deferred).

The third is the fail direction: an unreadable probe is `cannot-tell`, never `not-owner` — the
resolver must never license a second writer on a read that failed.

⚠️ The probes are injected, never manufactured — a fabricated uuid answers `UNKNOWN`, so a test
that faked one and asserted a verdict would be asserting a state the instrument never produces.
"""

import importlib.util
import io
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import types
import unittest
from contextlib import redirect_stdout

_HERE = pathlib.Path(__file__).resolve().parent


def load():
    spec = importlib.util.spec_from_file_location("owner", str(_HERE.parent / "owner.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class VerdictTableTest(unittest.TestCase):
    """`verdict_of` — the definition, with no probe in the way."""

    def setUp(self):
        self.o = load()

    def test_live_with_an_armed_loop_is_an_owner(self):
        for loop in ("OK", "STALE"):
            self.assertEqual(("owner", "live-armed"),
                             self.o.verdict_of("LIVE", loop), loop)

    def test_a_live_session_with_a_stopped_loop_is_not_an_owner(self):
        # THE BOUNDARY. A `/supervisor:stop`ped manager keeps its session; the round that would
        # keep its workers alive is exactly what stopped.
        self.assertEqual(("not-owner", "loop-stopped"), self.o.verdict_of("LIVE", "STOPPED"))

    def test_stopped_outranks_the_session(self):
        # Whatever the session reads, a deliberate stand-down owns nothing.
        for session in ("LIVE", "ABSENT", "UNKNOWN", "MALFORMED", "NONE", ""):
            self.assertEqual(("not-owner", "loop-stopped"),
                             self.o.verdict_of(session, "STOPPED"), session)

    def test_the_unmeasurable_loop_states_are_cannot_tell(self):
        for loop, reason in (("SLEEPING", "loop-sleeping"),
                             ("UNARMED", "loop-unarmed"),
                             ("INVALID", "loop-invalid"),
                             ("UNREADABLE", "loop-unreadable")):
            self.assertEqual(("cannot-tell", reason),
                             self.o.verdict_of("LIVE", loop), loop)

    def test_an_unreadable_session_probe_is_cannot_tell(self):
        for session in ("UNKNOWN", "AMBIGUOUS", "MALFORMED"):
            self.assertEqual(("cannot-tell", "session-undecidable"),
                             self.o.verdict_of(session, "OK"), session)

    def test_an_absent_session_is_not_an_owner(self):
        # The only session verdict that licenses a resume, so it must be a definite answer.
        for loop in ("OK", "STALE", "UNARMED", "INVALID"):
            self.assertEqual(("not-owner", "session-absent"),
                             self.o.verdict_of("ABSENT", loop), loop)

    def test_no_recorded_session_is_not_an_owner(self):
        # Nothing to protect from a second writer: the first was never recorded.
        for loop in ("OK", "UNARMED", "INVALID", "NONE"):
            self.assertEqual(("not-owner", "no-session"),
                             self.o.verdict_of("NONE", loop), loop)

    def test_a_live_session_with_no_loop_read_is_cannot_tell(self):
        self.assertEqual(("cannot-tell", "loop-unread"), self.o.verdict_of("LIVE", "NONE"))

    def test_no_state_other_than_live_and_armed_earns_owner(self):
        # The exhaustive statement of the definition: over every (session, loop) pair the two
        # probes can produce, `owner` appears exactly for LIVE x {OK, STALE} and nowhere else.
        sessions = ("LIVE", "ABSENT", "UNKNOWN", "AMBIGUOUS", "MALFORMED", "NONE")
        loops = ("OK", "STALE", "STOPPED", "SLEEPING", "UNARMED", "INVALID", "UNREADABLE", "NONE")
        owners = {(s, l) for s in sessions for l in loops
                  if self.o.verdict_of(s, l)[0] == "owner"}
        self.assertEqual({("LIVE", "OK"), ("LIVE", "STALE")}, owners)


class PageAndSessionTest(unittest.TestCase):
    """`of_page` / `of_session` with both probes injected."""

    def setUp(self):
        self.o = load()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.manager_dir = pathlib.Path(self.tmp.name) / "worker-manager"
        self.manager_dir.mkdir()
        self._orig = (self.o.session_verdict, self.o.loop_state, self.o.MANAGER_DIR)
        self.addCleanup(self._restore)
        self.o.MANAGER_DIR = str(self.manager_dir)
        self.sessions = {}
        self.loops = {}
        self.o.session_verdict = lambda sid: self.sessions.get(sid, "ABSENT")
        self.o.loop_state = lambda subject, branch="goal": self.loops.get(
            (subject, branch), ("UNARMED", ""))

    def _restore(self):
        self.o.session_verdict, self.o.loop_state, self.o.MANAGER_DIR = self._orig

    def register(self, sid, subject, branch="goal"):
        (self.manager_dir / f"{sid}.json").write_text(
            json.dumps({"subject": subject, "branch": branch, "vault": "/v"}), encoding="utf-8")

    def test_a_page_whose_manager_is_live_and_armed_is_owned(self):
        self.sessions["gm-1"] = "LIVE"
        self.loops[("A Goal", "goal")] = ("OK", "age=10s limit=1000s")
        o = self.o.of_page("claude_session_id: gm-1\n", "A Goal", "goal")
        self.assertEqual("owner", o.verdict)
        self.assertEqual("gm-1", o.session_id)
        self.assertEqual("session LIVE, loop OK", o.describe())

    def test_a_page_whose_manager_is_stopped_is_not_owned(self):
        self.sessions["gm-1"] = "LIVE"
        self.loops[("A Goal", "goal")] = ("STOPPED", "marker present")
        o = self.o.of_page("claude_session_id: gm-1\n", "A Goal", "goal")
        self.assertEqual("not-owner", o.verdict)
        self.assertEqual("loop-stopped", o.reason)

    def test_a_page_with_no_session_is_not_owned_and_its_loop_is_still_read(self):
        self.loops[("A Goal", "goal")] = ("UNARMED", "")
        o = self.o.of_page("status: in_progress\n", "A Goal", "goal")
        self.assertEqual("not-owner", o.verdict)
        self.assertEqual("no-session", o.reason)
        self.assertEqual("NONE", o.session)

    def test_a_failed_loop_probe_is_cannot_tell(self):
        self.sessions["gm-1"] = "LIVE"
        self.loops[("A Goal", "goal")] = ("UNREADABLE", "")
        o = self.o.of_page("claude_session_id: gm-1\n", "A Goal", "goal")
        self.assertEqual("cannot-tell", o.verdict)
        self.assertEqual("loop-unreadable", o.reason)

    def test_a_failed_session_probe_is_cannot_tell(self):
        self.sessions["gm-1"] = "UNKNOWN"
        self.loops[("A Goal", "goal")] = ("OK", "")
        o = self.o.of_page("claude_session_id: gm-1\n", "A Goal", "goal")
        self.assertEqual("cannot-tell", o.verdict)
        self.assertEqual("session-undecidable", o.reason)

    def test_a_session_resolves_its_loop_from_its_own_registration(self):
        self.register("gm-1", "A Goal")
        self.sessions["gm-1"] = "LIVE"
        self.loops[("A Goal", "goal")] = ("OK", "")
        o = self.o.of_session("gm-1")
        self.assertEqual("owner", o.verdict)

    def test_a_session_with_no_registration_is_cannot_tell_not_no_owner(self):
        # The registration is the only source of the subject, so an absent one is a read that
        # could not happen — never a licence to resume over a live manager.
        self.sessions["gm-9"] = "LIVE"
        o = self.o.of_session("gm-9")
        self.assertEqual("cannot-tell", o.verdict)
        self.assertEqual("loop-unread", o.reason)
        self.assertIn("no manager registration", o.detail)

    def test_a_session_id_that_is_not_a_bare_filename_is_refused(self):
        self.assertIsNone(self.o.registration("../../etc/passwd")[0])

    def test_a_registration_naming_an_unknown_branch_is_refused(self):
        (self.manager_dir / "gm-1.json").write_text(
            json.dumps({"subject": "A Goal", "branch": "nonsense"}), encoding="utf-8")
        self.assertIsNone(self.o.registration("gm-1")[0])


class CliTest(unittest.TestCase):
    def test_the_cli_prints_the_verdict_line(self):
        o = load()
        o.session_verdict = lambda sid: "LIVE"
        o.loop_state = lambda subject, branch="goal": ("STOPPED", "marker present")
        import io
        from contextlib import redirect_stdout
        out = io.StringIO()
        with redirect_stdout(out):
            rc = o.main(["--session", "gm-1", "--subject", "A Goal"])
        self.assertEqual(0, rc)
        self.assertEqual("not-owner\tloop-stopped\tLIVE\tSTOPPED\tmarker present",
                         out.getvalue().strip())

    def test_the_cli_refuses_when_it_has_nothing_to_resolve(self):
        o = load()
        self.assertEqual(2, o.main([]))


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
        self.k.loop_state("A Goal")[0]
        self.assertEqual(1, len(self.kwargs))
        self.assertIn("timeout", self.kwargs[0])
        self.assertEqual(self.k.PROBE_TIMEOUT, self.kwargs[0]["timeout"])

    def test_an_unrunnable_cadence_probe_does_not_raise(self):
        # Without the guard a missing interpreter aborts the whole tick with a traceback.
        self._stub_raising(OSError("no interpreter"))
        self.assertEqual(self.k.PROBE_FAILED, self.k.loop_state("A Goal")[0])

    def test_a_hung_cadence_probe_does_not_raise(self):
        self._stub_raising(subprocess.TimeoutExpired("probe", 30))
        self.assertEqual(self.k.PROBE_FAILED, self.k.loop_state("A Goal")[0])

    def test_a_crashed_cadence_probe_is_not_read_as_a_corrupt_record(self):
        """⚠️ A probe that could not answer is a fact about the PROBE, not the subject.

        `INVALID` is `manager-liveness.py`'s own verdict for a record that exists and cannot
        be read — and `main` treats `INVALID` on a LIVE session as `restart`. Folding a
        crashed probe into it would restart a live manager on no evidence at all.
        """
        self._stub(rc=1, stdout="", stderr="Traceback (most recent call last):\n")
        self.assertNotEqual("INVALID", self.k.loop_state("A Goal")[0])
        self.assertEqual(self.k.PROBE_FAILED, self.k.loop_state("A Goal")[0])

    def test_every_documented_state_still_maps(self):
        for state in ("OK", "STALE", "STOPPED", "UNARMED", "INVALID"):
            with self.subTest(state=state):
                self._stub(stdout=f"{state}\tslug\tdetail\n")
                self.assertEqual(state, self.k.loop_state("A Goal")[0])

    def test_an_unrecognised_state_is_invalid(self):
        # The pre-existing fallback, unchanged: a well-formed exit with unparseable text is
        # `manager-liveness.py`'s own INVALID, not a probe failure.
        self._stub(stdout="BANANA\tslug\tdetail\n")
        self.assertEqual("INVALID", self.k.loop_state("A Goal")[0])

    def test_the_sleeping_detail_is_returned_with_its_state(self):
        """A sleep is only actionable if the line names the instant, not just the state."""
        self._stub(
            stdout="SLEEPING\tslug\tuntil 2026-11-09T08:00:00+01:00 (70856s remaining)\n"
        )
        self.assertEqual(
            ("SLEEPING", "until 2026-11-09T08:00:00+01:00 (70856s remaining)"),
            self.k.loop_state("A Goal"),
        )

    def test_the_state_alone_is_still_available(self):
        self._stub(stdout="SLEEPING\tslug\tuntil 2026-11-09T08:00:00+01:00\n")
        self.assertEqual("SLEEPING", self.k.loop_state("A Goal")[0])

    def test_a_state_line_with_no_detail_yields_an_empty_detail(self):
        self._stub(stdout="OK\n")
        self.assertEqual(("OK", ""), self.k.loop_state("A Goal"))


if __name__ == "__main__":
    unittest.main()
