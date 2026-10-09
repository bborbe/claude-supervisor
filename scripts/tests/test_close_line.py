#!/usr/bin/env python3
"""Tests for scripts/close-line.py — the sweep's one close-line producer.

Load-bearing properties, from the 2026-09-27 measurement (the line was true at
emission and the session was gone ~10 minutes later):

  * a runnable line carries BOTH its verification moment and its re-check command,
    so a reader can settle a stale line without trusting the render;
  * the re-check embeds the FULL session id — a prefix is the measured trap on the
    sibling read (it returns UNREADABLE, the branch that says "act as before");
  * `absent` and `unknown` render the candidate, which carries no command;
  * a `closed` newest closer suppresses the runnable line even on a LIVE verdict —
    the 2026-10-09 measurement, where the line was void at BIRTH rather than
    decaying: the session had closed clean four minutes BEFORE the line was
    emitted, so a live verdict alone is not a sufficient condition;
  * the stamp is the emission moment, not a constant — two emissions differ, and
    `--now` pins it for a fixture.

Run: python3 -m unittest discover -s scripts/tests -v
"""

import importlib.util
import os
import unittest
from datetime import datetime, timezone

_HERE = os.path.dirname(os.path.abspath(__file__))


def load():
    spec = importlib.util.spec_from_file_location(
        "close_line", os.path.join(os.path.dirname(_HERE), "close-line.py")
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


M = load()
SID = "5384b63b-07b5-45d7-ad60-d78d0e4d3b87"
TASK = "A Reap Notice Goes Stale Between Emission and Read"
ROOT = "/opt/plugin"


class CloseLineTest(unittest.TestCase):
    def line(self, liveness, **kw):
        return M.close_line(TASK, SID, liveness, root=ROOT, **kw)

    # --- the runnable line -------------------------------------------------

    def test_runnable_line_carries_all_three_parts(self):
        out = self.line(M.LIVENESS_LIVE)
        self.assertIn("✅ DONE — CLOSE:", out)
        self.assertIn(f"[{SID[:8]}]", out)
        self.assertIn("— verified ", out)
        self.assertIn("· re-check: ", out)
        self.assertIn(M.CLOSE_ACTION, out)

    def test_parked_is_runnable_too(self):
        # A headless worker holding a gate is live for this purpose — the shipped
        # gate emits the line for LIVENESS_PARKED as well as LIVENESS_LIVE.
        self.assertIn("✅ DONE — CLOSE:", self.line(M.LIVENESS_PARKED))

    def test_recheck_embeds_the_full_id_never_the_prefix(self):
        out = self.line(M.LIVENESS_LIVE)
        self.assertIn(f"--check {SID}", out)
        self.assertNotIn(f"--check {SID[:8]} ", out)

    def test_recheck_names_the_resolved_plugin_root(self):
        self.assertIn(f"{ROOT}/scripts/session-liveness.py", self.line(M.LIVENESS_LIVE))

    # --- the candidate -----------------------------------------------------

    def test_absent_renders_the_candidate_with_no_command(self):
        out = self.line(M.LIVENESS_ABSENT)
        self.assertIn("🔧 CLOSE-ME CANDIDATE:", out)
        self.assertNotIn("re-check", out)
        self.assertNotIn(M.CLOSE_ACTION, out)

    def test_unknown_renders_the_candidate_too(self):
        # UNKNOWN is not ABSENT, but both withhold the command; the caller holds
        # the distinction and is the one that acts on it.
        self.assertIn("🔧 CLOSE-ME CANDIDATE:", self.line(M.LIVENESS_UNKNOWN))

    # --- the second input: the caller's newest-closer read ------------------

    def test_a_closed_closer_suppresses_the_runnable_line(self):
        # THE FIX. The registry answers "is the process alive"; a session that has
        # run its own close is live for as long as its process holds the socket, so
        # a live verdict alone cannot see that the instruction is already void.
        # Measured 2026-10-09: the session closed clean at 07:39:39Z and the line
        # was emitted at 07:43:21Z — void at birth, not decayed.
        out = self.line(M.LIVENESS_LIVE, closer=M.CLOSER_CLOSED)
        self.assertIn("🔧 CLOSE-ME CANDIDATE:", out)
        self.assertNotIn("✅ DONE — CLOSE:", out)
        self.assertNotIn("re-check", out)
        self.assertNotIn(M.CLOSE_ACTION, out)

    def test_the_closed_candidate_does_not_claim_liveness_is_unverified(self):
        # The two candidate reasons must not collapse: here the liveness IS
        # confirmed, so "UNVERIFIED" would be a false statement about the probe.
        out = self.line(M.LIVENESS_LIVE, closer=M.CLOSER_CLOSED)
        self.assertNotIn(M.CANDIDATE_SUFFIX, out)
        self.assertIn(M.CLOSED_SUFFIX, out)

    def test_an_open_closer_leaves_the_liveness_verdict_deciding(self):
        # A real gate: the row still owes its close, so the line stays runnable.
        self.assertIn("✅ DONE — CLOSE:",
                      self.line(M.LIVENESS_LIVE, closer=M.CLOSER_OPEN))

    def test_an_unreadable_closer_never_suppresses(self):
        # `agents/manager-drive.md`: a failed read never suppresses a notice.
        self.assertIn("✅ DONE — CLOSE:",
                      self.line(M.LIVENESS_LIVE, closer=M.CLOSER_UNREADABLE))

    def test_omitting_the_closer_behaves_exactly_as_before(self):
        # Absent is not a silent default to `open` — it is the pre-flag contract,
        # so a caller that has not adopted the second read is unaffected.
        self.assertEqual(self.line(M.LIVENESS_LIVE),
                         self.line(M.LIVENESS_LIVE, closer=None))
        self.assertIn("✅ DONE — CLOSE:", self.line(M.LIVENESS_LIVE, closer=None))

    # --- the stamp ---------------------------------------------------------

    def test_stamp_is_the_emission_moment_not_a_constant(self):
        pinned = datetime(2026, 10, 7, 9, 30, 0, tzinfo=timezone.utc)
        self.assertIn("verified 2026-10-07T09:30:00Z", self.line(M.LIVENESS_LIVE, now=pinned))
        self.assertNotIn(
            "verified 2026-10-07T09:30:00Z",
            self.line(M.LIVENESS_LIVE, now=datetime(2026, 10, 7, 9, 31, 0, tzinfo=timezone.utc)),
        )

    def test_stamp_is_utc_and_z_suffixed(self):
        out = M.stamp(datetime(2026, 10, 7, 11, 30, 0, tzinfo=timezone.utc))
        self.assertEqual(out, "2026-10-07T11:30:00Z")

    # --- no bare line ------------------------------------------------------

    def test_no_liveness_value_yields_a_bare_close_instruction(self):
        # The defect this script exists to remove: a close instruction a reader
        # cannot settle. Every value either carries the stamp + command, or
        # carries no close verb at all.
        #
        # ⚠️ Over BOTH axes. The closer is a second way into the runnable branch,
        # and an invariant checked on one axis is exactly how a new input
        # reintroduces the defect it was written to remove.
        for word in M.LIVENESS_WORDS:
            for closer in (None,) + M.CLOSER_WORDS:
                out = self.line(word, closer=closer)
                if "✅ DONE — CLOSE:" in out:
                    self.assertIn("— verified ", out)
                    self.assertIn("· re-check: ", out)

    # --- CLI contract ------------------------------------------------------

    def test_exit_codes_track_the_line_shape(self):
        self.assertEqual(M.main(["--task", TASK, "--session", SID, "--liveness", "live"]), 0)
        self.assertEqual(M.main(["--task", TASK, "--session", SID, "--liveness", "parked"]), 0)
        self.assertEqual(M.main(["--task", TASK, "--session", SID, "--liveness", "absent"]), 1)
        self.assertEqual(M.main(["--task", TASK, "--session", SID, "--liveness", "unknown"]), 1)
        # The closer axis: a live verdict on a closed-clean session renders the
        # candidate, so a caller branching on 0/1 sees the same split as the reader.
        self.assertEqual(
            M.main(["--task", TASK, "--session", SID, "--liveness", "live",
                    "--closer", "closed"]),
            1,
        )
        self.assertEqual(
            M.main(["--task", TASK, "--session", SID, "--liveness", "live",
                    "--closer", "open"]),
            0,
        )

    def test_bad_now_is_a_usage_error_not_a_silent_stamp(self):
        self.assertEqual(
            M.main(["--task", TASK, "--session", SID, "--liveness", "live", "--now", "yesterday"]),
            2,
        )


if __name__ == "__main__":
    unittest.main()
