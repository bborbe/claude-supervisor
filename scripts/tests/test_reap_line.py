#!/usr/bin/env python3
"""Tests for scripts/reap-line.py — the reap hand-over's one producer.

Load-bearing properties, from the 2026-10-03 tick-86 measurement (a run reported
`reaped 2` over two sessions the messaging layer could not reach):

  * only a `live` verdict yields a `SendMessage` line; every other verdict — and
    in particular `parked`, which `close-line.py` treats as runnable — renders
    `reap moot — no live recipient`;
  * the deliverable line embeds the FULL session id, never a prefix, so the
    reader's re-check is as strong as the caller's probe;
  * the tick-86 replay (`agent_1` / `99058101…` and `agent_10` / `4cd2c43c…`,
    both absent) yields no delivery, while a live control still does — so a
    producer that simply drops every reap fails here;
  * the stamp is the emission moment, not a constant.

Run: python3 -m unittest discover -s scripts/tests -v
"""

import importlib.util
import os
import unittest
from datetime import datetime, timezone

_HERE = os.path.dirname(os.path.abspath(__file__))


def load():
    spec = importlib.util.spec_from_file_location(
        "reap_line", os.path.join(os.path.dirname(_HERE), "reap-line.py")
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


M = load()

# Tick 86's two targets. Both were in the supervisor registry; neither was on the
# harness peer list, and `SendMessage` refused every spelling of either.
AGENT_1 = "99058101-0000-4000-8000-000000000001"
AGENT_10 = "4cd2c43c-0000-4000-8000-000000000010"
LIVE = "5384b63b-07b5-45d7-ad60-d78d0e4d3b87"
TASK = "The Reap Remedy Has No Recipient When the Target Session Has Already Ended"
ROOT = "/opt/plugin"


class ReapLineTest(unittest.TestCase):
    def line(self, liveness, session=LIVE, **kw):
        return M.reap_line(TASK, session, liveness, root=ROOT, **kw)

    # --- the moot render ---------------------------------------------------

    def test_absent_renders_moot(self):
        self.assertEqual(self.line(M.LIVENESS_ABSENT, session=AGENT_1), M.MOOT)

    def test_unknown_renders_moot(self):
        self.assertEqual(self.line(M.LIVENESS_UNKNOWN, session=AGENT_1), M.MOOT)

    def test_parked_renders_moot(self):
        # A parked session is live-but-gated; the reap notice is not what it
        # needs, and `close-line.py`'s runnable treatment does not carry over.
        self.assertEqual(self.line(M.LIVENESS_PARKED), M.MOOT)

    def test_moot_carries_no_sendmessage(self):
        for word in M.LIVENESS_WORDS:
            if word == M.LIVENESS_LIVE:
                continue
            self.assertNotIn("SendMessage", self.line(word), word)

    def test_moot_is_the_exact_line(self):
        self.assertEqual(M.MOOT, "reap moot — no live recipient")

    # --- the deliverable render --------------------------------------------

    def test_live_renders_exactly_one_sendmessage_line(self):
        out = self.line(M.LIVENESS_LIVE)
        self.assertEqual(sum("SendMessage" in ln for ln in out.splitlines()), 1)
        self.assertIn("✅ REAP:", out)

    def test_live_carries_the_full_session_id(self):
        self.assertIn(LIVE, self.line(M.LIVENESS_LIVE))

    def test_live_recheck_embeds_the_full_id_never_the_prefix(self):
        out = self.line(M.LIVENESS_LIVE)
        self.assertIn(f"--check {LIVE}", out)
        self.assertNotIn(f"--check {LIVE[:8]} ", out)

    def test_live_recheck_names_the_resolved_plugin_root(self):
        self.assertIn(f"{ROOT}/scripts/session-liveness.py", self.line(M.LIVENESS_LIVE))

    def test_live_carries_a_stamp_and_the_non_authorising_wording(self):
        out = self.line(M.LIVENESS_LIVE)
        self.assertIn("— verified ", out)
        self.assertIn("non-authorising", out)

    # --- the tick-86 replay ------------------------------------------------

    def test_tick86_agent_1_absent_yields_no_delivery(self):
        out = self.line(M.LIVENESS_ABSENT, session=AGENT_1)
        self.assertNotIn("SendMessage", out)
        self.assertEqual(out, M.MOOT)

    def test_tick86_agent_10_absent_yields_no_delivery(self):
        out = self.line(M.LIVENESS_ABSENT, session=AGENT_10)
        self.assertNotIn("SendMessage", out)
        self.assertEqual(out, M.MOOT)

    def test_tick86_live_control_still_delivers(self):
        # The control: a producer that dropped EVERY reap would pass the two
        # replay assertions above, so this one pins the other direction.
        out = self.line(M.LIVENESS_LIVE, session=LIVE)
        self.assertIn("SendMessage", out)
        self.assertIn(LIVE, out)

    # --- the stamp ---------------------------------------------------------

    def test_stamp_is_the_emission_moment_not_a_constant(self):
        pinned = datetime(2026, 10, 20, 7, 30, 0, tzinfo=timezone.utc)
        self.assertIn("verified 2026-10-20T07:30:00Z", self.line(M.LIVENESS_LIVE, now=pinned))
        self.assertNotIn(
            "verified 2026-10-20T07:30:00Z",
            self.line(M.LIVENESS_LIVE,
                      now=datetime(2026, 10, 20, 7, 31, 0, tzinfo=timezone.utc)),
        )

    def test_stamp_is_utc_and_z_suffixed(self):
        self.assertEqual(
            M.stamp(datetime(2026, 10, 20, 11, 30, 0, tzinfo=timezone.utc)),
            "2026-10-20T11:30:00Z",
        )

    # --- no bare reap ------------------------------------------------------

    def test_only_live_yields_a_sendmessage_across_the_vocabulary(self):
        for word in M.LIVENESS_WORDS:
            out = self.line(word)
            self.assertEqual("SendMessage" in out, word == M.LIVENESS_LIVE, word)

    # --- CLI contract ------------------------------------------------------

    def test_exit_codes(self):
        # Both renders are normal outcomes; only a usage error is non-zero.
        for word in ("absent", "unknown", "parked"):
            self.assertEqual(
                M.main(["--task", TASK, "--session", AGENT_1, "--liveness", word]), 0, word
            )
        self.assertEqual(
            M.main(["--task", TASK, "--session", LIVE, "--liveness", "live"]), 0
        )

    def test_bad_now_is_a_usage_error_not_a_silent_stamp(self):
        self.assertEqual(
            M.main(["--task", TASK, "--session", LIVE, "--liveness", "live",
                    "--now", "yesterday"]),
            2,
        )


if __name__ == "__main__":
    unittest.main()
