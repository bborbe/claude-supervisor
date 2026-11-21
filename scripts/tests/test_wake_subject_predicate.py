#!/usr/bin/env python3
"""Parity test for the shared wake-subject predicate.

The wake job in `bborbe/attention-controller` and the Python arms in this repo
must agree on ONE rule: does a card's producer subject belong to this manager.
The fixture `scripts/testdata/wake-subject-predicate.json` is that contract — it
is vendored byte-identically from the controller repo (same sha256), and
`pkg/wake-ownership.go`'s `WakeOwnership.Keeps` asserts the same cases. A change
to the rule on either side that the other does not carry fails that side's test.

What is shared is the DECISION, not the loading: this module resolves subjects
from the vault and the ledger, the Go job resolves them from `worker-manager` and
`context` records. Those loaders are deliberately separate; the verdict is not.

Run: python3 -m unittest discover -s scripts/tests -v
"""

import importlib.util
import json
import os
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_SCRIPTS = os.path.dirname(_HERE)
_FIXTURE = os.path.join(_SCRIPTS, "testdata", "wake-subject-predicate.json")
_OWNERSHIP = os.path.join(_SCRIPTS, "session-ownership.py")
_ARMS = ("gate-owner-filter.py", "fleet-board.py", "gate-answer-check.py")

_spec = importlib.util.spec_from_file_location("session_ownership", _OWNERSHIP)
ownership = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ownership)


class WakeSubjectPredicateTest(unittest.TestCase):
    """The shared fixture, asserted against the Python implementation."""

    def test_fixture_is_present_and_wide_enough(self):
        with open(_FIXTURE, encoding="utf-8") as handle:
            fixture = json.load(handle)
        self.assertIn("cases", fixture)
        self.assertGreaterEqual(len(fixture["cases"]), 6)

    def test_every_fixture_case_matches_the_go_rule(self):
        with open(_FIXTURE, encoding="utf-8") as handle:
            fixture = json.load(handle)
        for case in fixture["cases"]:
            with self.subTest(case=case["name"]):
                self.assertEqual(
                    ownership.wake_subject_keeps(
                        case["manager"]["subject"],
                        case["manager"]["fleet"],
                        case["producer"]["subject"],
                        case["producer"]["resolved"],
                    ),
                    case["keep"],
                    "fixture case %r" % case["name"],
                )

    def test_the_rule_is_defined_once(self):
        """No arm may carry a private copy of the rule.

        The rule lives in `session-ownership.py` alone. An arm that re-implements
        it is the three-copies defect this fixture exists to prevent, so the test
        fails if any arm defines its own `wake_subject_keeps`.
        """
        definitions = []
        for name in ("session-ownership.py",) + _ARMS:
            with open(os.path.join(_SCRIPTS, name), encoding="utf-8") as handle:
                for line in handle:
                    if line.startswith("def wake_subject_keeps"):
                        definitions.append(name)
        self.assertEqual(
            definitions,
            ["session-ownership.py"],
            "wake_subject_keeps must be defined only in session-ownership.py",
        )


if __name__ == "__main__":
    unittest.main()
