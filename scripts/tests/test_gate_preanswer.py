#!/usr/bin/env python3
"""Fixtures for `scripts/gate-preanswer.py` — the model-turn-free half of the gate path.

Every case here is one way the pre-answer path must refuse to settle a card, because a card it
settles silently is a gate answered with no judgment behind it.

Run: python3 -m unittest discover -s scripts/tests -v
"""

import contextlib
import importlib.util
import io
import os
import unittest
from unittest import mock

_SCRIPTS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load(name, filename):
    spec = importlib.util.spec_from_file_location(name, os.path.join(_SCRIPTS, filename))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


gp = _load("gate_preanswer", "gate-preanswer.py")

SELF = "7253a6aa-0000-4000-8000-000000000001"
A = "00e661592dd0d8d18b4e0aa96361867e"
B = "11e661592dd0d8d18b4e0aa96361867f"


def ok(tier="allowlist", verdict="answerable"):
    return {"verdict": verdict, "tier": tier, "expect": "hash-%s" % tier}


class Identity(unittest.TestCase):
    def test_environment_is_the_identity(self):
        self.assertEqual(gp.session_id({"CLAUDE_CODE_SESSION_ID": SELF}), (SELF, None))

    def test_missing_environment_is_refused(self):
        sid, why = gp.session_id({})
        self.assertIsNone(sid)
        self.assertIn("unset", why)

    def test_malformed_environment_is_refused(self):
        sid, why = gp.session_id({"CLAUDE_CODE_SESSION_ID": "not-a-session"})
        self.assertIsNone(sid)
        self.assertIn("not a session id", why)

    def test_main_refuses_without_an_identity_before_reading_a_card(self):
        out = io.StringIO()
        # clear=True is load-bearing: this suite runs INSIDE a Claude session, so the variable
        # is set in the ambient environment and an unpatched main() would probe a real card.
        with mock.patch.dict(os.environ, {}, clear=True), contextlib.redirect_stdout(out):
            rc = gp.main(["--item", A])
        self.assertEqual(rc, 2)
        self.assertIn("REFUSED", out.getvalue())


class Preanswer(unittest.TestCase):
    def _verdicts(self, probe, check=None):
        calls = []

        def verdict(self_id, item_id, *extra):
            calls.append(extra)
            # The re-check is the call that carries the ceiling; the probe is the bare `--dry`.
            return check if "--max-tier" in extra else probe
        return verdict, calls

    def test_allowlist_answerable_is_settled_by_this_path(self):
        verdict, calls = self._verdicts(ok(), ok())
        ran = []
        settled, why = gp.preanswer(SELF, A, run=lambda argv: (ran.append(argv), (0, "", ""))[1],
                                    verdict=verdict)
        self.assertTrue(settled, why)
        self.assertEqual(calls[0], ("--dry",), "the probe must be the no-book read")
        argv = ran[0]
        self.assertEqual(argv[argv.index("--by") + 1], gp.PREANSWER_BY)
        self.assertEqual(argv[argv.index("--expect") + 1], "hash-allowlist")

    def test_manager_tier_is_left_for_the_dispatch(self):
        verdict, _ = self._verdicts(ok(tier="manager"))
        settled, why = gp.preanswer(SELF, A, run=lambda argv: (1, "", ""), verdict=verdict)
        self.assertFalse(settled)
        self.assertIn("not allowlist", why)

    def test_operator_tier_is_left_for_the_operator(self):
        verdict, _ = self._verdicts(ok(tier="operator"))
        self.assertFalse(gp.preanswer(SELF, A, run=lambda argv: (1, "", ""), verdict=verdict)[0])

    def test_not_answerable_is_left(self):
        verdict, _ = self._verdicts(ok(verdict="escalate"))
        settled, why = gp.preanswer(SELF, A, run=lambda argv: (1, "", ""), verdict=verdict)
        self.assertFalse(settled)
        self.assertIn("not answerable", why)

    def test_a_changed_card_is_refused_not_settled(self):
        verdict, _ = self._verdicts(ok(), ok())
        settled, why = gp.preanswer(SELF, A, run=lambda argv: (4, "", "expect mismatch"),
                                    verdict=verdict)
        self.assertFalse(settled)
        self.assertIn("answer refused", why)

    def test_a_recheck_that_no_longer_reads_answerable_is_left(self):
        verdict, _ = self._verdicts(ok(), ok(verdict="skip"))
        settled, why = gp.preanswer(SELF, A, run=lambda argv: (0, "", ""), verdict=verdict)
        self.assertFalse(settled)
        self.assertIn("re-check", why)

    def test_the_answer_is_the_existing_scripts_contract(self):
        verdict, _ = self._verdicts(ok(), ok())
        seen = []
        gp.preanswer(SELF, A, run=lambda argv: (seen.append(argv), (4, "", "x"))[1], verdict=verdict)
        self.assertIn("--answer", seen[0])
        self.assertIn("--expect", seen[0])

    def test_the_allowlist_ceiling_is_on_both_acting_steps(self):
        """The invariant must hold where the card is settled, not only at the probe."""
        verdict, calls = self._verdicts(ok(), ok())
        seen = []
        gp.preanswer(SELF, A, run=lambda argv: (seen.append(argv), (0, "", ""))[1], verdict=verdict)
        self.assertIn(("--max-tier", "allowlist"), calls, "the check must carry the ceiling")
        self.assertTrue(seen)
        for argv in seen:
            self.assertEqual(argv[argv.index("--max-tier") + 1], "allowlist", argv)

    def test_a_failed_answer_releases_the_booking(self):
        verdict, _ = self._verdicts(ok(), ok())
        seen = []
        settled, why = gp.preanswer(
            SELF, A, run=lambda argv: (seen.append(argv), (4, "", "refused"))[1], verdict=verdict)
        self.assertFalse(settled, why)
        self.assertTrue(any("--release" in argv for argv in seen),
                        "a failed answer must release the booked card")


class Process(unittest.TestCase):
    def test_settled_tokens_are_stripped_and_passed_ones_kept(self):
        pre = lambda self_id, item_id: (item_id == A, "")
        out, settled, passed = gp.process("FEED new: 2 hst:%s session=x | hst:%s session=y" % (A, B),
                                          SELF, pre=pre)
        self.assertEqual(settled, [A])
        self.assertEqual(passed, [B])
        self.assertNotIn(A, out)
        self.assertIn(B, out)

    def test_a_repeated_token_is_probed_once(self):
        calls = []

        def pre(self_id, item_id):
            calls.append(item_id)
            return True, ""
        gp.process("hst:%s hst:%s" % (A, A), SELF, pre=pre)
        self.assertEqual(calls, [A])

    def test_a_line_with_nothing_settled_is_unchanged(self):
        pre = lambda self_id, item_id: (False, "")
        line = "FEED new: 1 hst:%s session=x" % A
        self.assertEqual(gp.process(line, SELF, pre=pre)[0], line)

    def test_a_line_with_no_token_passes_through(self):
        self.assertEqual(gp.process("LIVENESS  0 open", SELF, pre=lambda *a: (True, ""))[0],
                         "LIVENESS  0 open")


if __name__ == "__main__":
    unittest.main()
