#!/usr/bin/env python3
"""`_absent_licensed`'s transcript-recency arm — a recycled pid is not a dead session.

⚠️ **The population, and why it is a separate suite.** `alive: False` is what
`session-identity._pid_identity` answers when the registry record's pid is gone or is held by a
DIFFERENT process. A record that outlived its session, plus a pid number the OS has since
recycled onto another process, therefore reads as death for a session that is still writing.
Measured 2026-11-17 on two live sessions: ABSENT at 11 s and 34 s after a transcript write, each
held `busy` in the registry on a recycled pid. The fix keeps UNKNOWN while the transcript is
recent and leaves ABSENT for a genuinely dead id — the one verdict a reaper reclaims on.

The two directions are asserted independently on purpose:

  * SC1 — `alive: False` plus a transcript written within the window is UNKNOWN, at BOTH measured
    offsets (11 s and 34 s), so a constant shrunk below 34 s fails rather than passing on the
    lucky one;
  * SC2 — `alive: False` plus a transcript idle 600 s still reads ABSENT, on its OWN constant, so
    a blanket "always UNKNOWN for a dead record" fix fails here.

Run: python3 -m unittest discover -s scripts/tests -v
"""

import importlib.util
import io
import json
import os
import tempfile
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout

from endpoint_fixture import FixtureEndpoint, dead_url

_HERE = os.path.dirname(os.path.abspath(__file__))

LIVE, ABSENT, UNKNOWN = 0, 1, 2

# A pid beyond this OS's range, so `session-identity.read_registry` computes `alive: False` from a
# fixture record without a real process — the shape a record whose pid was recycled leaves behind.
DEAD_PID = 4194303

# The two offsets the false ABSENTs were measured at (2026-11-17). Both are asserted.
MEASURED_RECENT_SECONDS = (11, 34)

# ⚠️ **SC2's own constant, deliberately NOT the module's window.** A case that read
# `TRANSCRIPT_RECENT_SECONDS` would move with the fix and assert nothing about the dead case;
# 600 s is five times the window and is not a boundary.
DEAD_TRANSCRIPT_IDLE_SECONDS = 600


def load():
    spec = importlib.util.spec_from_file_location(
        "session_liveness", os.path.join(os.path.dirname(_HERE), "session-liveness.py")
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class AbsentLicensedTranscriptRecency(unittest.TestCase):
    def setUp(self):
        self.m = load()
        self.tmp = tempfile.TemporaryDirectory()
        # The identity registry is the input this arm reads, so it is isolated: the machine's real
        # registry would otherwise answer for whatever sessions are running here.
        self.registry = os.path.join(self.tmp.name, "registry")
        os.makedirs(self.registry)
        # The start-time cache is keyed by pid, so it is isolated for the same reason.
        self._prior_cache_env = os.environ.get("SUPERVISOR_START_CACHE")
        os.environ["SUPERVISOR_START_CACHE"] = os.path.join(self.tmp.name, "starts.json")
        # Transcripts are found under `$CLAUDE_CONFIG_DIR/projects`, isolated so a real transcript
        # on this host cannot decide a fixture's verdict.
        self.config = os.path.join(self.tmp.name, "config")
        os.makedirs(os.path.join(self.config, "projects", "-some-project"))
        self._prior_config_env = os.environ.get("CLAUDE_CONFIG_DIR")
        os.environ["CLAUDE_CONFIG_DIR"] = self.config

    def tearDown(self):
        if self._prior_config_env is None:
            os.environ.pop("CLAUDE_CONFIG_DIR", None)
        else:
            os.environ["CLAUDE_CONFIG_DIR"] = self._prior_config_env
        if self._prior_cache_env is None:
            os.environ.pop("SUPERVISOR_START_CACHE", None)
        else:
            os.environ["SUPERVISOR_START_CACHE"] = self._prior_cache_env
        self.tmp.cleanup()

    def plant(self, session_id, pid=DEAD_PID, **extra):
        """A registry record the reader computes `alive: False` from — the recycled-pid shape."""
        rec = {"sessionId": session_id, "pid": pid, "name": "synth", "status": "busy"}
        rec.update(extra)
        with open(os.path.join(self.registry, "%s.json" % session_id), "w", encoding="utf-8") as fh:
            json.dump(rec, fh)

    def transcript(self, session_id, idle_seconds, project="-some-project"):
        """A transcript for `session_id`, last written `idle_seconds` ago."""
        directory = os.path.join(self.config, "projects", project)
        os.makedirs(directory, exist_ok=True)
        path = os.path.join(directory, "%s.jsonl" % session_id)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("{}\n")
        then = time.time() - idle_seconds
        os.utime(path, (then, then))
        return path

    def check(self, session_id):
        """`--check` against an endpoint holding NO row for the id — the 404 path into the arm."""
        out, err = io.StringIO(), io.StringIO()
        with FixtureEndpoint([]) as fx, redirect_stdout(out), redirect_stderr(err):
            rc = self.m.main(["--check", session_id, "--endpoint", fx.url, "--dir", self.registry])
        return rc, out.getvalue() + err.getvalue()

    # ---- SC1: a recent transcript disproves the record's death -------------------------

    def test_alive_false_with_a_transcript_at_either_measured_offset_is_unknown(self):
        """Both measured false ABSENTs (11 s and 34 s) must answer UNKNOWN, not ABSENT.

        ⚠️ **Both offsets, not one.** The 11 s case alone passes on a constant of 30 s, which
        would still publish ABSENT for the 34 s reading — the second measured failure.
        """
        for idle in MEASURED_RECENT_SECONDS:
            with self.subTest(idle_seconds=idle):
                sid = "a1c0ffee-1111-2222-3333-4444%08d" % idle
                self.plant(sid)
                self.transcript(sid, idle)
                rc, out = self.check(sid)
                self.assertEqual(rc, UNKNOWN, out)
                self.assertIn("UNKNOWN", out)

    def test_alive_false_with_a_transcript_just_inside_the_window_is_unknown(self):
        """The window's near edge: 119 s is inside it, so the record's death is not believed."""
        sid = "b2c0ffee-1111-2222-3333-444455556666"
        self.plant(sid)
        self.transcript(sid, 119)
        rc, out = self.check(sid)
        self.assertEqual(rc, UNKNOWN, out)

    # ---- SC2: the reaper keeps its ABSENT --------------------------------------------

    def test_alive_false_with_a_dead_transcript_still_reads_absent(self):
        """A genuinely dead session — transcript idle 600 s — still licenses ABSENT.

        ⚠️ **The laziest fix is a blanket UNKNOWN for `alive: False`, and this is what refuses
        it.** The constant here is the suite's own 600 s, never the module's window.
        """
        sid = "c3c0ffee-1111-2222-3333-444455556666"
        self.plant(sid)
        self.transcript(sid, DEAD_TRANSCRIPT_IDLE_SECONDS)
        rc, out = self.check(sid)
        self.assertEqual(rc, ABSENT, out)
        self.assertIn("ABSENT", out)

    def test_alive_false_with_no_transcript_still_reads_absent(self):
        """No transcript is no evidence either way, so today's ABSENT stands.

        ⚠️ **The fallback is load-bearing.** A transcript that cannot be resolved (cleaned up, or
        written under another config dir) must not turn every dead id into UNKNOWN — that would
        trade one false verdict for another.
        """
        sid = "d4c0ffee-1111-2222-3333-444455556666"
        self.plant(sid)
        rc, out = self.check(sid)
        self.assertEqual(rc, ABSENT, out)

    def test_a_live_registry_entry_keeps_its_unknown_whatever_the_transcript_says(self):
        """The `non_dead` guard is unchanged: a record that is NOT `alive: False` is UNKNOWN.

        This case exists so the new arm cannot be read as replacing the older one — a record
        whose pid is occupied but unprovable (`alive: None`) reaches UNKNOWN before the
        transcript is consulted at all.
        """
        sid = "e5c0ffee-1111-2222-3333-444455556666"
        self.plant(sid, pid=os.getpid())  # alive: None — the pid is held, identity unprovable
        self.transcript(sid, DEAD_TRANSCRIPT_IDLE_SECONDS)
        rc, out = self.check(sid)
        self.assertEqual(rc, UNKNOWN, out)


class LocalVerdictTranscriptRecency(unittest.TestCase):
    """The SECOND ABSENT licence — `_local_verdict`, the endpoint-down disk fallback.

    ⚠️ **A separate population, not a second spelling of the suite above.** `_local_verdict`
    is the endpoint-down disk fallback, and it licenses ABSENT on `alive is False` — an arm
    the five cases above never reach, because they drive `_absent_licensed` through the
    per-id 404 path. So those cases cannot fail if this arm regresses, and this class cannot
    fail if theirs does.

    Every case here forces the fallback (an unreachable endpoint), patches the registry to a
    record it HOLDS and computes dead, and patches the headless store to a readable, empty
    set — so ABSENT is licensable on every other axis and the transcript is the only variable.
    """

    def setUp(self):
        self.m = load()
        self.tmp = tempfile.TemporaryDirectory()
        self.registry = os.path.join(self.tmp.name, "registry")
        os.makedirs(self.registry)
        self._prior_cache_env = os.environ.get("SUPERVISOR_START_CACHE")
        os.environ["SUPERVISOR_START_CACHE"] = os.path.join(self.tmp.name, "starts.json")
        self.config = os.path.join(self.tmp.name, "config")
        os.makedirs(os.path.join(self.config, "projects", "-some-project"))
        self._prior_config_env = os.environ.get("CLAUDE_CONFIG_DIR")
        os.environ["CLAUDE_CONFIG_DIR"] = self.config

    def tearDown(self):
        if self._prior_config_env is None:
            os.environ.pop("CLAUDE_CONFIG_DIR", None)
        else:
            os.environ["CLAUDE_CONFIG_DIR"] = self._prior_config_env
        if self._prior_cache_env is None:
            os.environ.pop("SUPERVISOR_START_CACHE", None)
        else:
            os.environ["SUPERVISOR_START_CACHE"] = self._prior_cache_env
        self.tmp.cleanup()

    def transcript(self, session_id, idle_seconds, project="-some-project"):
        directory = os.path.join(self.config, "projects", project)
        os.makedirs(directory, exist_ok=True)
        path = os.path.join(directory, "%s.jsonl" % session_id)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("{}\n")
        then = time.time() - idle_seconds
        os.utime(path, (then, then))
        return path

    def verdict(self, sid):
        """`--check` against an UNREACHABLE endpoint, with the registry holding `sid` dead."""

        class _RegistryHoldsItDead:
            @staticmethod
            def read_registry(directory=None):
                return {sid: {"alive": False, "pid": 1234}}

        prior_id, prior_headless = self.m._IDENTITY, self.m._headless_live_ids
        self.m._IDENTITY = _RegistryHoldsItDead()
        # A readable, EMPTY headless store: `None` would answer UNKNOWN on its own axis and
        # mask whether the transcript arm fired at all.
        self.m._headless_live_ids = lambda: set()
        out, err = io.StringIO(), io.StringIO()
        try:
            with redirect_stdout(out), redirect_stderr(err):
                rc = self.m.main(
                    ["--check", sid, "--endpoint", dead_url(), "--dir", self.registry]
                )
        finally:
            self.m._IDENTITY, self.m._headless_live_ids = prior_id, prior_headless
        return rc, out.getvalue() + err.getvalue()

    def test_a_dead_registry_row_with_a_recent_transcript_is_unknown(self):
        """SC1 on the fallback arm: 11 s after a transcript write, the record's death is refused.

        ⚠️ **Red before the fix.** This function returned ABSENT unconditionally on
        `alive is False`, so this case read exit 1 where exit 2 is required.
        """
        sid = "f6c0ffee-1111-2222-3333-444455556666"
        self.transcript(sid, 11)
        rc, out = self.verdict(sid)
        self.assertEqual(rc, UNKNOWN, out)
        self.assertIn("UNKNOWN", out)

    def test_a_dead_registry_row_with_a_dead_transcript_still_reads_absent(self):
        """SC2 on the fallback arm: a genuinely dead session keeps its ABSENT.

        Green before and after — the constant here is the suite's own 600 s, never the
        module's window, so a blanket UNKNOWN on this arm fails.
        """
        sid = "07c0ffee-1111-2222-3333-444455556666"
        self.transcript(sid, DEAD_TRANSCRIPT_IDLE_SECONDS)
        rc, out = self.verdict(sid)
        self.assertEqual(rc, ABSENT, out)

    def test_a_dead_registry_row_with_no_transcript_still_reads_absent(self):
        """No transcript is no evidence either way, so the fallback's ABSENT stands."""
        sid = "18c0ffee-1111-2222-3333-444455556666"
        rc, out = self.verdict(sid)
        self.assertEqual(rc, ABSENT, out)


if __name__ == "__main__":
    unittest.main()
