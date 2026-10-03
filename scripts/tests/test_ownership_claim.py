#!/usr/bin/env python3
"""Tests for scripts/ownership-claim.py.

The writer half of the ownership claim. `gate-owner-filter.py` only ever READS
the store, so every path that can corrupt it -- the flock read-modify-write, the
stale takeover, the `HELD` exit contract, the manager-id resolution -- is
unexercised by the reader's tests and lives here. The writer is the half where a
bug is silent: a claim that is never written reads exactly like no claim at all.

Run: python3 -m unittest discover -s scripts/tests -v
"""

import argparse
import importlib.util
import json
import os
import tempfile
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_SCRIPT = os.path.join(os.path.dirname(_HERE), "ownership-claim.py")

_spec = importlib.util.spec_from_file_location("ownership_claim", _SCRIPT)
oc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(oc)

ME = "11111111-1111-4111-8111-111111111111"
PEER = "22222222-2222-4222-8222-222222222222"
GATED = "33333333-3333-4333-8333-333333333333"


class Base(unittest.TestCase):
    """Redirects the store and the registry to temp dirs.

    Both are module globals, so patching them here reaches `_read`, `_write`,
    `Locked` and `live_sessions` without touching the operator's real store.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = self._tmp.name
        self.claims = os.path.join(self.dir, "ownership-claims.json")
        self.registry = os.path.join(self.dir, "sessions")
        os.makedirs(self.registry)

        self._orig = (oc.CLAIMS, oc.LOCK, oc.REGISTRY)
        oc.CLAIMS = self.claims
        oc.LOCK = self.claims + ".lock"
        oc.REGISTRY = self.registry
        self.addCleanup(self._restore)

    def _restore(self):
        oc.CLAIMS, oc.LOCK, oc.REGISTRY = self._orig

    def live(self, *session_ids):
        """Register sessions, which is what makes a held claim look live."""
        for session_id in session_ids:
            path = os.path.join(self.registry, session_id + ".json")
            with open(path, "w", encoding="utf-8") as handle:
                json.dump({"sessionId": session_id}, handle)

    def claim(self, session, manager, note=""):
        return oc.cmd_claim(
            argparse.Namespace(session=session, manager=manager, note=note)
        )

    def release(self, session):
        return oc.cmd_release(argparse.Namespace(session=session))

    def store(self):
        with open(self.claims, encoding="utf-8") as handle:
            return json.load(handle)["claims"]


class Claim(Base):
    def test_claim_writes_the_entry(self):
        self.assertEqual(self.claim(GATED, ME), 0)
        self.assertEqual(self.store()[GATED]["manager"], ME)
        self.assertEqual(self.store()[GATED]["session"], GATED)

    def test_claim_creates_a_missing_store(self):
        self.assertFalse(os.path.exists(self.claims))
        self.claim(GATED, ME)
        self.assertTrue(os.path.exists(self.claims))

    def test_reclaim_by_the_same_manager_is_not_refused(self):
        """Its own cadence, not a conflict -- refusing it would deadlock the asker."""
        self.claim(GATED, ME)
        self.assertEqual(self.claim(GATED, ME, note="still mine"), 0)
        self.assertEqual(self.store()[GATED]["note"], "still mine")

    def test_held_by_a_live_manager_returns_held(self):
        self.live(PEER)
        self.claim(GATED, PEER)
        self.assertEqual(self.claim(GATED, ME), oc.HELD)

    def test_held_exit_code_is_three(self):
        """The contract every caller branches on; a renumber reads as success."""
        self.assertEqual(oc.HELD, 3)

    def test_held_does_not_overwrite_the_holder(self):
        self.live(PEER)
        self.claim(GATED, PEER)
        self.claim(GATED, ME)
        self.assertEqual(self.store()[GATED]["manager"], PEER)

    def test_stale_holder_is_taken_over(self):
        """A gone holder's claim is nobody's -- the rule a dead spawner gets."""
        self.claim(GATED, PEER)  # PEER is never registered, so it reads dead
        self.assertEqual(self.claim(GATED, ME), 0)
        self.assertEqual(self.store()[GATED]["manager"], ME)

    def test_a_live_holder_is_not_taken_over(self):
        """The other half of the takeover rule, or it would always take over."""
        self.live(PEER)
        self.claim(GATED, PEER)
        self.assertEqual(self.claim(GATED, ME), oc.HELD)
        self.assertEqual(self.store()[GATED]["manager"], PEER)

    def test_claims_are_independent_per_session(self):
        other = "44444444-4444-4444-8444-444444444444"
        self.live(PEER)
        self.claim(GATED, PEER)
        self.assertEqual(self.claim(other, ME), 0)
        self.assertEqual(self.store()[GATED]["manager"], PEER)
        self.assertEqual(self.store()[other]["manager"], ME)


class Release(Base):
    def test_release_drops_the_entry(self):
        self.claim(GATED, ME)
        self.assertEqual(self.release(GATED), 0)
        self.assertNotIn(GATED, self.store())

    def test_releasing_an_absent_claim_is_not_an_error(self):
        self.assertEqual(self.release(GATED), 0)

    def test_release_is_not_manager_checked(self):
        """Deliberate, and pinned so the asymmetry stays a decision.

        Any manager may release any claim: releasing fails toward emitting the
        pane again, which is this filter's safe direction, while a refused
        release would leave a stale claim suppressing panes with no way out.
        """
        self.live(PEER)
        self.claim(GATED, PEER)
        self.release(GATED)
        self.assertNotIn(GATED, self.store())

    def test_a_released_session_is_claimable_again(self):
        self.claim(GATED, ME)
        self.release(GATED)
        self.assertEqual(self.claim(GATED, ME), 0)

    def test_non_dict_entry_is_treated_as_absent_on_claim(self):
        """The reader skips this shape; the writer must not traceback on it.

        A hand-edited or truncated entry would otherwise kill the whole claim
        with an AttributeError while every watcher read straight past it.
        """
        with open(self.claims, "w", encoding="utf-8") as handle:
            json.dump({"claims": {GATED: ["not", "a", "dict"]}}, handle)
        self.assertEqual(self.claim(GATED, ME), 0)
        self.assertEqual(self.store()[GATED]["manager"], ME)

    def test_non_dict_entry_survives_release(self):
        with open(self.claims, "w", encoding="utf-8") as handle:
            json.dump({"claims": {GATED: "oops"}}, handle)
        self.assertEqual(self.release(GATED), 0)
        self.assertNotIn(GATED, self.store())


class ManagerId(Base):
    VARS = ("CLAUDE_CODE_SESSION_ID", "CLAUDE_SESSION_ID")

    def setUp(self):
        super().setUp()
        self._saved = {var: os.environ.get(var) for var in self.VARS}
        for var in self.VARS:
            os.environ.pop(var, None)
        self.addCleanup(self._restore_env)

    def _restore_env(self):
        for var, value in self._saved.items():
            if value is None:
                os.environ.pop(var, None)
            else:
                os.environ[var] = value

    def test_explicit_manager_wins(self):
        self.assertEqual(oc.manager_id(argparse.Namespace(manager=ME)), ME)

    def test_falls_back_to_the_session_env_var(self):
        os.environ["CLAUDE_CODE_SESSION_ID"] = ME
        self.assertEqual(oc.manager_id(argparse.Namespace(manager=None)), ME)

    def test_absent_everywhere_exits_rather_than_guessing(self):
        """A guessed claimer would file the claim under a session that is not
        the one that asked -- worse than refusing, because it looks recorded."""
        with self.assertRaises(SystemExit):
            oc.manager_id(argparse.Namespace(manager=None))


class Read(Base):
    def test_missing_file_reads_as_empty(self):
        self.assertEqual(oc._read(), {"version": 1, "claims": {}})

    def test_malformed_json_reads_as_empty(self):
        with open(self.claims, "w", encoding="utf-8") as handle:
            handle.write("{not json")
        self.assertEqual(oc._read()["claims"], {})

    def test_wrong_shape_reads_as_empty(self):
        with open(self.claims, "w", encoding="utf-8") as handle:
            json.dump({"claims": []}, handle)
        self.assertEqual(oc._read()["claims"], {})

    def test_round_trip_preserves_other_entries(self):
        self.claim(GATED, ME)
        self.claim("44444444-4444-4444-8444-444444444444", PEER)
        self.assertEqual(len(oc._read()["claims"]), 2)


class List(Base):
    def test_empty_store_renders_without_error(self):
        self.assertEqual(oc.cmd_list(argparse.Namespace()), 0)

    def test_a_live_holder_renders(self):
        self.live(PEER)
        self.claim(GATED, PEER)
        self.assertEqual(oc.cmd_list(argparse.Namespace()), 0)

    def test_a_dead_holder_renders(self):
        """`list` is the operator's only read of the store, so it must not
        crash on the shape takeover exists to handle."""
        self.claim(GATED, PEER)
        self.assertEqual(oc.cmd_list(argparse.Namespace()), 0)


if __name__ == "__main__":
    unittest.main()
