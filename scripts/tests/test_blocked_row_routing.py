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


def _resolver(manager="Topic Manager", source="chain"):
    return lambda title: (manager, source)


class RoutesOnlyWhenOwnerless(unittest.TestCase):
    """SC2's positive and its three negatives."""

    def test_a_closer_naming_no_owner_is_routed_to_the_manager(self):
        rec = brr.routing("waiting on the build", "", TASK, resolver=_resolver())
        self.assertIsNotNone(rec)
        self.assertEqual(rec["manager"], "Topic Manager")
        self.assertEqual(rec["source"], "chain")

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
    """⚠️ Composition, not agreement. A byte-identical private copy of `OWNER_RE` would
    agree with the lint on every fixture and still be the drift this forbids — so the test
    stubs the loaded module and asserts `routing()` OBEYS it."""

    def _with_fake_lint(self, verdict):
        class FakeLint:
            @staticmethod
            def owner_named(body, text):
                return verdict

        original = brr._load
        brr._load = lambda f, n: FakeLint if f == "closer-gate-lint.py" else original(f, n)
        self.addCleanup(setattr, brr, "_load", original)

    def test_a_lint_that_says_owner_named_produces_no_route(self):
        self._with_fake_lint(True)
        self.assertIsNone(brr.routing("waiting on the build", "", TASK, resolver=_resolver()))

    def test_a_lint_that_says_no_owner_routes(self):
        # The inverse, and the fixture is chosen for it: `the manager is on it` is a body a
        # private copy of `OWNER_RE` DOES match (its `\bmanager\b` arm). So a `routing()`
        # that ORed in a private regex returns None here and fails — where a fixture no
        # private copy matches ("waiting on the build") would pass and leave that drift
        # undetected. This is the direction that catches an ADDED copy, not only a replaced one.
        self._with_fake_lint(False)
        rec = brr.routing("the manager is on it", "", TASK, resolver=_resolver())
        self.assertIsNotNone(rec)
        self.assertEqual(rec["manager"], "Topic Manager")


class KnownFalseNegative(unittest.TestCase):
    """DOCUMENTED-WRONG behaviour, pinned so a wiring must meet it rather than inherit it."""

    def test_a_hex_wait_is_a_known_false_no_route(self):
        # `OWNER_RE`'s first arm is `\b[0-9a-f]{8,32}\b`, so a commit SHA or an 8-digit date
        # reads as an OWNER. A real "no recipient" row is therefore silently dropped — the
        # failure this script exists to end. Not fixable here: the predicate belongs to
        # `closer-gate-lint.py`, and a private copy is the drift this module forbids.
        for closer in ("waiting on commit 64b708ab",
                       "waiting on 20261112 nightly",
                       "waiting on abc12345"):
            with self.subTest(closer=closer):
                self.assertIsNone(
                    brr.routing(closer, closer, TASK, resolver=_resolver()),
                    "if this now ROUTES, the hex arm was fixed — delete this test and say so")


class UnresolvableIsNotSilence(unittest.TestCase):
    def test_an_unresolvable_manager_propagates_rather_than_reading_as_no_routing(self):
        # `routing()` raises so `main()` can print a reason and exit 2. Returning None here
        # would be indistinguishable from "an owner was found" — the failure this ends.
        def boom(title):
            raise RuntimeError("the registry could not be read")

        with self.assertRaises(RuntimeError):
            brr.routing("waiting on the build", "", TASK, resolver=boom)

    def test_main_exits_2_when_the_manager_cannot_be_resolved(self):
        # The CLI contract the class name promises, asserted through `main` — not only
        # through `routing()`.
        original = os.environ.get("SESSIONS_DIR")
        os.environ["SESSIONS_DIR"] = "/tmp/does-not-exist-brr-test"
        self.addCleanup(
            lambda: os.environ.pop("SESSIONS_DIR", None) if original is None
            else os.environ.__setitem__("SESSIONS_DIR", original))
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            code = brr.main(["--closer", "waiting on the build", "--task", "X"])
        self.assertEqual(code, 2)

    def test_the_ends_haystack_is_carried_by_row(self):
        # `--row` is what makes a separate `⏰ Ends:` line visible; exercised through `main`.
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(io.StringIO()):
            code = brr.main(["--closer", "blocked on the release",
                             "--row", "blocked on the release\n⏰ Ends: v0.295.0 tagged",
                             "--task", TASK])
        self.assertEqual(code, 0, buf.getvalue())
        self.assertIn("NO-ROUTE", buf.getvalue())

    def test_cli_prints_route_when_the_row_is_ownerless(self):
        # The ROUTE path through `main` — otherwise only NO-ROUTE and exit 2 are exercised
        # there. A fake escalation-manager module keeps it board-free.
        class FakeEscalation:
            @staticmethod
            def resolve(title):
                return "Topic Manager", "chain"

        original = brr._load
        brr._load = lambda f, n: FakeEscalation if f == "escalation-manager.py" else original(f, n)
        self.addCleanup(setattr, brr, "_load", original)

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(io.StringIO()):
            code = brr.main(["--closer", "waiting on the build", "--task", TASK])
        self.assertEqual(code, 0, buf.getvalue())
        self.assertIn("ROUTE\tTopic Manager\tchain", buf.getvalue())

    def test_cli_prints_no_route_for_a_closer_that_names_an_owner(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(io.StringIO()):
            code = brr.main(["--closer", "approve: card " + "7e55adaf" * 4, "--task", TASK])
        self.assertEqual(code, 0, buf.getvalue())
        self.assertIn("NO-ROUTE", buf.getvalue())


if __name__ == "__main__":
    unittest.main()
