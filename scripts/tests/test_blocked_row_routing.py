#!/usr/bin/env python3
"""Tests for scripts/blocked-row-routing.py — SC2's routing-record fixture set.

SC2 asks for one positive and three negatives, plus the "waiting on someone" case:

  * a fixture 🔴 BLOCKED closer naming no owner produces a routing record naming the
    subject's manager for that session;
  * a closer whose owner is a card id, a manager session id or an `⏰ Ends:` observable
    produces NONE;
  * a closer saying only "waiting on someone" is treated as no owner.

The properties worth pinning beyond those:

  * **The owner decision is the LINT's, not a second copy.** `closer-gate-lint.py` refuses an
    ownerless closer, so if this script re-implemented `owner_named` the two would drift and
    a row could be refused by the lint while being routed as ownerless, or vice versa.
  * **An unresolvable manager is not "no routing".** Silence would read as "an owner was
    found", which is the failure this script exists to end.

Run: python3 -m unittest discover -s scripts/tests
"""

import contextlib
import importlib.util
import io
import os
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_SCRIPT = os.path.join(os.path.dirname(_HERE), "blocked-row-routing.py")

_spec = importlib.util.spec_from_file_location("blocked_row_routing", _SCRIPT)
brr = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(brr)

TASK = "Some Blocked Subject"


def _resolver(manager="Topic Manager", source="topic"):
    return lambda title: (manager, source)


class RoutesOnlyWhenOwnerless(unittest.TestCase):
    """SC2's positive and its three negatives."""

    def test_a_closer_naming_no_owner_is_routed_to_the_manager(self):
        rec = brr.routing("waiting on the build", "", TASK, resolver=_resolver())
        self.assertIsNotNone(rec)
        self.assertEqual(rec["manager"], "Topic Manager")
        self.assertEqual(rec["source"], "topic")

    def test_a_closer_naming_a_card_id_is_not_routed(self):
        closer = "approve: reply `go` — card " + "7e55adaf" * 4
        self.assertIsNone(brr.routing(closer, closer, TASK, resolver=_resolver()))

    def test_a_closer_naming_a_manager_is_not_routed(self):
        for closer in ("escalate to the manager", "the supervisor is handling it"):
            with self.subTest(closer=closer):
                self.assertIsNone(brr.routing(closer, closer, TASK, resolver=_resolver()))

    def test_a_closer_declaring_an_ends_observable_is_not_routed(self):
        # The two-haystack split: `⏰ Ends:` is conventionally its OWN line below the body.
        body = "blocked on the release"
        row = body + "\n⏰ Ends: v0.295.0 tagged"
        self.assertIsNone(brr.routing(body, row, TASK, resolver=_resolver()))

    def test_waiting_on_someone_is_treated_as_no_owner(self):
        # It names a wait, not a recipient — the same reading the lint takes.
        rec = brr.routing("waiting on someone", "waiting on someone", TASK, resolver=_resolver())
        self.assertIsNotNone(rec)
        self.assertEqual(rec["manager"], "Topic Manager")


class ComposesTheLint(unittest.TestCase):
    def test_the_owner_decision_comes_from_the_lint(self):
        # Same body, lint loaded by this module: the verdicts must agree, or the two have
        # drifted and a row can be refused by one while routed by the other.
        lint = brr._load("closer-gate-lint.py", "closer_gate_lint")
        for closer in ("approve: card " + "7e55adaf" * 4,
                       "escalate to the manager",
                       "waiting on the build",
                       "waiting on someone"):
            with self.subTest(closer=closer):
                self.assertEqual(
                    lint.owner_named(closer, closer),
                    brr.routing(closer, closer, TASK, resolver=_resolver()) is None,
                    closer)


class UnresolvableIsNotSilence(unittest.TestCase):
    def test_an_unresolvable_manager_propagates_rather_than_reading_as_no_routing(self):
        # `routing()` raises so `main()` can print a reason and exit 2. Returning None here
        # would be indistinguishable from "an owner was found" — the failure this ends.
        def boom(title):
            raise RuntimeError("the registry could not be read")

        with self.assertRaises(RuntimeError):
            brr.routing("waiting on the build", "", TASK, resolver=boom)

    def test_cli_prints_no_route_for_a_closer_that_names_an_owner(self):
        # A card-id closer names an owner, so no board is consulted and no routing happens.
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(io.StringIO()):
            code = brr.main(["--closer", "approve: card " + "7e55adaf" * 4, "--task", TASK])
        self.assertEqual(code, 0, buf.getvalue())
        self.assertIn("NO-ROUTE", buf.getvalue())


if __name__ == "__main__":
    unittest.main()
