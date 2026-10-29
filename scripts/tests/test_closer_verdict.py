#!/usr/bin/env python3
"""Tests for scripts/closer-verdict.py — the drive leg's class → verdict seam.

The two outcomes this file exists to pin are the task's SC (c): a **routine
continue produces a nudge** and a **genuine operator gate produces none**. They are
asserted together on purpose — a mapping that returned `revive` for everything would
satisfy the first alone, and a mapping that returned `blocked` for everything would
satisfy the second, so neither case is evidence without the other.

Measured 2026-10-29 on round `2026-10-29T08-28-15Z`: of 7 parked rows only 4 carried a
`CLOSERS` row, and the three uncaptured ones split into a routine turn end, a
clock-bound `later (on …)`, and no closer at all — three different verdicts that a
row-present / row-absent reading collapsed into one.
"""

import importlib.util
import os
import subprocess
import sys
import unittest

_SCRIPT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "closer-verdict.py")
_spec = importlib.util.spec_from_file_location("closer_verdict", _SCRIPT)
cv = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cv)


class Verdicts(unittest.TestCase):

    def test_a_routine_continue_is_nudged(self):
        """SC (c), first half: the case the whole task exists for."""
        word, _reason = cv.verdict("routine")
        self.assertEqual("revive", word)

    def test_a_genuine_gate_is_not_nudged(self):
        """SC (c), second half: the fail-closed rule still holds for a real ask."""
        word, _reason = cv.verdict("ask")
        self.assertEqual("blocked", word)

    def test_a_clock_bound_park_is_not_nudged(self):
        # `later (on …)` names the event that resumes the work; until it fires
        # there is nothing to nudge and nothing the operator can answer.
        word, _reason = cv.verdict("parked-wait")
        self.assertEqual("unverifiable", word)

    def test_a_missing_closer_is_never_revived(self):
        word, _reason = cv.verdict("none")
        self.assertEqual("unverifiable", word)

    def test_every_declared_class_has_a_verdict(self):
        for name in cv.VERDICTS:
            self.assertIn(cv.VERDICTS[name][0], ("revive", "blocked", "unverifiable"))

    def test_only_routine_revives(self):
        # The guard against a fix that suppresses or revives everything: exactly one
        # class may reach `revive`, and it is the routine one.
        reviving = [n for n in cv.VERDICTS if cv.VERDICTS[n][0] == "revive"]
        self.assertEqual(["routine"], reviving)


class Cli(unittest.TestCase):

    def run_cli(self, *argv):
        return subprocess.run([sys.executable, _SCRIPT, *argv],
                              capture_output=True, text=True)

    def test_prints_the_verdict_word_first(self):
        out = self.run_cli("--class", "routine")
        self.assertEqual(0, out.returncode)
        self.assertTrue(out.stdout.startswith("revive"), out.stdout)

    def test_an_unknown_class_refuses_rather_than_guessing(self):
        # A typo must not read as a confident verdict.
        out = self.run_cli("--class", "routin")
        self.assertEqual(2, out.returncode)
        self.assertIn("unknown class", out.stderr)
        self.assertEqual("", out.stdout)

    def test_the_closer_is_echoed_when_given(self):
        out = self.run_cli("--class", "ask", "--closer", "pick — 1. hold prod")
        self.assertEqual(0, out.returncode)
        self.assertIn("pick — 1. hold prod", out.stdout)


if __name__ == "__main__":
    unittest.main()
