#!/usr/bin/env python3
"""Tests for scripts/session-holds.py.

Covers the store's two load-bearing properties -- a hold is DURABLE, and a hold
is only ever removed by `release` -- plus the one criterion that cannot be
checked in one direction alone: a `--list` that flags EVERY hold as a release
candidate satisfies "a dead session is flagged" exactly as a correct one does.

Both directions are therefore asserted by name, per the rule that a suppression
guard needs both-direction tests: the too-tight case (`list` flags a dead
session) and the too-loose case (`list` does NOT flag a live one). They fail in
opposite directions and only one of them looks like a bug -- a build that flags
everything reads as maximally helpful.

The negative controls are written against a REALISTIC store, not an empty one:
near-miss session ids are present alongside the held one, because a guard tested
against an empty file and a guard tested against a populated one with near-misses
are different tests, and only the second exercises the boundary.

Run: python3 -m unittest discover -s scripts/tests -v
"""

import contextlib
import importlib.util
import io
import json
import os
import sys
import tempfile
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_SCRIPT = os.path.join(os.path.dirname(_HERE), "session-holds.py")

_spec = importlib.util.spec_from_file_location("session_holds", _SCRIPT)
sh = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sh)

HELD = "aaaaaaaa-1111-2222-3333-444444444444"
NEAR_MISS = "aaaaaaab-1111-2222-3333-444444444444"
OTHER = "bbbbbbbb-1111-2222-3333-444444444444"
LIVE = "cccccccc-1111-2222-3333-444444444444"
DEAD = "dddddddd-1111-2222-3333-444444444444"


class SessionHoldsTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        sh.HOLDS = os.path.join(self.dir, "session-holds.json")
        sh.LOCK = sh.HOLDS + ".lock"
        self.live = {LIVE}

    def run_cmd(self, argv):
        """Run a subcommand, returning (exit_code, stdout)."""
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = sh.main(argv)
        return rc, out.getvalue()

    def store(self):
        with open(sh.HOLDS, encoding="utf-8") as handle:
            return json.load(handle)

    def seed(self, *sids):
        for sid in sids:
            self.run_cmd(["hold", sid, "--reason", "seeded", "--by", "operator"])

    # -- the store's core property ------------------------------------------

    def test_fires_on_a_held_session(self):
        """The too-tight direction: a held session reads as held."""
        self.run_cmd(["hold", HELD, "--reason", "operator: do not work on this"])
        rc, out = self.run_cmd(["is-held", HELD])
        self.assertEqual(0, rc)
        self.assertIn("do not work on this", out)

    def test_does_not_fire_on_a_clean_session(self):
        """The too-loose direction: an UNHELD session does not read as held.

        Written against a populated store carrying a near-miss id -- `aaaaaaab-`
        against the held `aaaaaaaa-` -- so a prefix match, a substring match or a
        flag-everything build all fail here. An empty store would let all three
        pass.
        """
        self.seed(HELD, OTHER)
        rc, out = self.run_cmd(["is-held", NEAR_MISS])
        self.assertEqual(1, rc)
        self.assertEqual("", out)

    def test_a_hold_is_only_removed_by_release(self):
        """Reading the store never prunes it -- a dead session's hold persists."""
        self.seed(DEAD)
        self.run_cmd(["list"])
        self.run_cmd(["is-held", DEAD])
        self.assertIn(DEAD, self.store()["holds"])

    def test_release_removes_the_hold(self):
        self.seed(HELD)
        rc, _ = self.run_cmd(["release", HELD])
        self.assertEqual(0, rc)
        self.assertNotIn(HELD, self.store()["holds"])

    def test_release_of_an_unheld_session_is_refused(self):
        self.seed(HELD)
        rc, _ = self.run_cmd(["release", OTHER])
        self.assertEqual(sh.NOT_HELD, rc)
        self.assertIn(HELD, self.store()["holds"])

    def test_re_hold_keeps_the_original_held_at(self):
        """Age is how long the operator's policy has stood, not when it was edited."""
        self.run_cmd(["hold", HELD, "--reason", "first"])
        first = self.store()["holds"][HELD]["held_at"]
        self.run_cmd(["hold", HELD, "--reason", "second"])
        entry = self.store()["holds"][HELD]
        self.assertEqual(first, entry["held_at"])
        self.assertEqual("second", entry["reason"])

    # -- the release-candidate criterion, BOTH directions --------------------

    def test_list_flags_a_dead_session_as_a_release_candidate(self):
        """The too-tight direction: a dead session's hold surfaces for release."""
        sh.live_sessions = lambda: set()
        self.seed(DEAD)
        _, out = self.run_cmd(["list"])
        self.assertIn("RELEASE CANDIDATE", out)
        self.assertIn(DEAD, out)
        self.assertIn("1 release candidate(s)", out)

    def test_list_does_not_flag_a_live_session(self):
        """The too-loose direction: a live session's hold is NOT a candidate.

        Without this half a `list` that flags every hold passes the criterion
        above while being useless -- the operator would be told to release every
        hold they have. Both sessions are held in one run, so the two clauses are
        read off the SAME output.
        """
        sh.live_sessions = lambda: {LIVE}
        self.seed(LIVE, DEAD)
        _, out = self.run_cmd(["list"])
        lines = [ln for ln in out.splitlines() if LIVE in ln or DEAD in ln]
        self.assertTrue(any(ln.startswith("⏸️ HELD") for ln in lines), out)
        self.assertTrue(
            any("RELEASE CANDIDATE" in ln for ln in lines), out
        )
        self.assertIn("2 hold(s); 1 release candidate(s)", out)

    def test_list_renders_a_held_row_rather_than_hiding_it(self):
        """A hold suppresses the MESSAGE, never the ROW."""
        sh.live_sessions = lambda: {LIVE}
        self.seed(LIVE)
        _, out = self.run_cmd(["list"])
        self.assertIn("⏸️ HELD", out)
        self.assertIn(LIVE, out)

    def test_empty_store_says_so(self):
        _, out = self.run_cmd(["list"])
        self.assertIn("No session holds", out)

    # -- durability of the storage discipline --------------------------------

    def test_a_corrupt_store_reads_empty_rather_than_raising(self):
        with open(sh.HOLDS, "w", encoding="utf-8") as handle:
            handle.write("{not json")
        rc, _ = self.run_cmd(["is-held", HELD])
        self.assertEqual(1, rc)

    def test_a_store_with_the_wrong_shape_reads_empty(self):
        with open(sh.HOLDS, "w", encoding="utf-8") as handle:
            json.dump({"version": 1, "holds": ["not", "a", "dict"]}, handle)
        rc, _ = self.run_cmd(["is-held", HELD])
        self.assertEqual(1, rc)

    def test_write_leaves_no_tmp_file_behind(self):
        self.seed(HELD)
        leftovers = [n for n in os.listdir(self.dir) if ".tmp." in n]
        self.assertEqual([], leftovers)

    def test_an_unresolvable_target_is_refused(self):
        rc, _ = self.run_cmd(["hold", "not-a-uuid-and-not-a-pane", "--reason", "x"])
        self.assertEqual(sh.BAD_TARGET, rc)
        self.assertFalse(os.path.exists(sh.HOLDS))


if __name__ == "__main__":
    unittest.main()
