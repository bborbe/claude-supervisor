#!/usr/bin/env python3
"""Tests for the reload-route rule in scripts/attention-ask.py.

A manager that needs another session to pick up a new plugin version has a
script it can run itself — `/supervisor:restart <sid>` — and an operator-only
harness command it cannot pull, `/reload-plugins`. Offering the second while
the first is available spends the operator's attention on a route the manager
could have taken, which is the defect this rule exists to close.

The rule has three branches and one bypass, and each is asserted here:

  * subject live        -> refused, no store item
  * no `--subject`      -> refused, no store item (the guard cannot be bypassed
                           by simply omitting the flag)
  * subject absent      -> accepted, with the `no addressable session` note
  * no reload anywhere  -> not this rule's business

Plus the failing-direction assertion: with the rule disabled the live-subject
card is accepted, so the fixture is proving the rule rather than passing on its
own.

Run: python3 -m unittest discover -s scripts/tests -v
"""

import importlib.util
import os
import unittest
from unittest import mock

_HERE = os.path.dirname(os.path.abspath(__file__))
_SCRIPT = os.path.join(os.path.dirname(_HERE), "attention-ask.py")

_spec = importlib.util.spec_from_file_location("attention_ask_reload", _SCRIPT)
ask = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ask)

RELOAD_OPTIONS = [
    {"label": "I run /reload-plugins in the Fleet Manager tab",
     "recommended": True},
    {"label": "Restart the Fleet Manager session instead",
     "recommended": False},
]
# The shape the rewrite leaves as the remaining defect: reload, and no restart
# option beside it. The rule is deliberately NOT conditional on a restart option
# being present, so this must be refused on exactly the same grounds.
RELOAD_ONLY = [{"label": "I run /reload-plugins in the tab", "recommended": True}]
RESTART_ONLY = [{"label": "Restart the session", "recommended": True}]

SUBJECT = "2de5efc8"


class ReloadRouteRule(unittest.TestCase):
    def verdict(self, options, payload="", subject=SUBJECT, live=None):
        with mock.patch.object(ask, "subject_live", return_value=live):
            return ask.reload_route_verdict(options, payload, subject)

    # --- branch 1: an addressable session is refused -----------------------
    def test_live_subject_is_refused(self):
        refusal, note = self.verdict(RELOAD_OPTIONS, live=True)
        self.assertIsNotNone(refusal)
        self.assertIsNone(note)
        self.assertIn("/supervisor:restart", refusal)
        self.assertIn(SUBJECT, refusal)

    def test_live_subject_is_refused_for_a_reload_only_card(self):
        # The route rule is not conditional on a restart option being present:
        # a reload-only card is the shape the guidance rewrite leaves behind.
        refusal, _ = self.verdict(RELOAD_ONLY, live=True)
        self.assertIsNotNone(refusal)

    def test_live_subject_is_refused_when_only_the_payload_names_the_route(self):
        refusal, _ = self.verdict(RESTART_ONLY, payload="please run /reload-plugins",
                                  live=True)
        self.assertIsNotNone(refusal)

    # --- branch 2: the flag cannot be omitted to bypass the rule -----------
    def test_missing_subject_is_refused(self):
        refusal, _ = self.verdict(RELOAD_OPTIONS, subject="", live=None)
        self.assertIsNotNone(refusal)
        self.assertIn("--subject", refusal)

    def test_missing_subject_is_refused_even_when_the_target_is_dead(self):
        # "I could not name a subject" is not "there is no addressable session".
        refusal, _ = self.verdict(RELOAD_ONLY, subject="", live=False)
        self.assertIsNotNone(refusal)

    # --- branch 3: a genuinely unaddressable target is accepted, with why ---
    def test_absent_subject_is_accepted_with_the_note(self):
        refusal, note = self.verdict(RELOAD_ONLY, live=False)
        self.assertIsNone(refusal)
        self.assertIn("no addressable session", note)
        self.assertIn(SUBJECT, note)

    # --- branch 4: cards that name no reload route are untouched -----------
    def test_restart_only_card_is_untouched(self):
        self.assertEqual(self.verdict(RESTART_ONLY, live=True), (None, None))
        self.assertEqual(self.verdict(RESTART_ONLY, subject="", live=True),
                         (None, None))

    # --- the failing-direction assertion ----------------------------------
    def test_rule_is_load_bearing(self):
        """With the rule disabled the live-subject card is accepted.

        Without this the suite would pass on a build where the rule does
        nothing: every branch above would still hold vacuously.
        """
        refusal, _ = self.verdict(RELOAD_OPTIONS, live=True)
        self.assertIsNotNone(refusal)
        with mock.patch.object(ask, "names_reload_route", return_value=False):
            self.assertEqual(self.verdict(RELOAD_OPTIONS, live=True), (None, None))


if __name__ == "__main__":
    unittest.main()
