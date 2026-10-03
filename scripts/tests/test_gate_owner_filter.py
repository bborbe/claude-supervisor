#!/usr/bin/env python3
"""Tests for scripts/gate-owner-filter.py.

Covers the ownership decision and the four hops that feed it. Each case guards a
defect the filter has actually shipped or would ship:

  * the keep-set is WIDE -- drop only on a live peer manager. A naive
    `spawner in live_managers` includes the watcher's own session and drops the
    manager's own workers, which makes the watcher silent for exactly the panes
    it exists to report.
  * unreadable ledger/registry is UNKNOWN, not empty -- {} makes every spawner a
    manager and would drop the whole fleet.
  * the pane->session source is the event log, not the retired `*.needs.json`
    store, which carries nothing for the live feed (measured 2026-09-25: 0 of 9
    feed panes had a record).
  * a CLAIM is the one ownership input that is not the spawn edge, and it drops
    ONLY for a live peer manager -- so both halves are pinned: a claimed pane
    drops, and the same unclaimed pane still emits.

Run: python3 -m unittest discover -s scripts/tests -v
"""

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_SCRIPT = os.path.join(os.path.dirname(_HERE), "gate-owner-filter.py")

_spec = importlib.util.spec_from_file_location("gate_owner_filter", _SCRIPT)
gf = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gf)

ME = "92de43c3-bd91-409d-a33e-824a61e5706a"
PEER = "64b4a415-906d-457f-81f9-48a842007b88"
GATED = "33333333-3333-4333-8333-333333333333"

# Distinct from None, which is a MEANINGFUL value here (unknown liveness).
_UNSET = object()


class Verdict(unittest.TestCase):
    """The keep-set, branch by branch. Drop ONLY on a live peer manager."""

    # One ledger holding a single worker, so its spawner is a worker-spawner.
    LEDGER = {"worker-1": {"session_id": "worker-1", "parent_session": "manager-x"}}
    LIVE = {"worker-1", "manager-x", "manager-y"}

    def call(self, session_id, spawner, ledger=_UNSET, live=_UNSET, me=ME):
        return gf.verdict(
            session_id,
            spawner,
            self.LEDGER if ledger is _UNSET else ledger,
            self.LIVE if live is _UNSET else live,
            me,
        )

    def test_peer_manager_is_dropped(self):
        """The one drop case: a live manager that is not this watcher."""
        self.assertEqual(self.call("w", "manager-y"), (gf.DROP, "peer-manager"))

    def test_own_workers_are_kept(self):
        """A manager's own panes are the ones it EXISTS to see."""
        self.assertEqual(self.call("w", ME), (gf.EMIT, "own-worker"))

    def test_own_session_is_kept(self):
        self.assertEqual(self.call(ME, None), (gf.EMIT, "self"))

    def test_worker_spawned_worker_is_kept(self):
        """The spawner has a ledger record, so it was itself spawned -- a worker."""
        self.assertEqual(
            self.call("w", "worker-1"), (gf.EMIT, "worker-spawner")
        )

    def test_unowned_pane_is_kept(self):
        self.assertEqual(self.call("w", None), (gf.EMIT, "unowned"))

    def test_session_with_no_id_is_kept(self):
        self.assertEqual(self.call(None, None), (gf.EMIT, "unowned"))

    def test_dead_manager_is_kept(self):
        """A dead manager's panes are nobody's to route, so they emit."""
        self.assertEqual(
            self.call("w", "manager-gone"), (gf.EMIT, "dead-manager")
        )

    def test_unknown_liveness_fails_open(self):
        """An unreadable registry is UNKNOWN -- fail open, never drop the fleet."""
        self.assertEqual(
            self.call("w", "manager-y", live=None), (gf.EMIT, "liveness-unknown")
        )

    def test_naive_live_manager_test_would_over_drop(self):
        """The defect this guards: `spawner in live_managers` drops own workers.

        ME is live and a manager, so a naive membership test would drop the
        watcher's own panes. The keep-set must exclude self first.
        """
        live_managers = {"manager-x", "manager-y", ME}
        naive_drops_own_worker = ME in live_managers
        self.assertTrue(naive_drops_own_worker, "fixture no longer exercises the bug")
        self.assertEqual(self.call("w", ME)[0], gf.EMIT)


class IsManager(unittest.TestCase):
    def test_parent_with_no_own_record_is_a_manager(self):
        self.assertTrue(gf.is_manager("1217e759", {}))

    def test_parent_with_its_own_record_is_a_worker(self):
        ledger = {"w": {"parent_session": "m"}}
        self.assertFalse(gf.is_manager("w", ledger))

    def test_missing_id_is_not_a_manager(self):
        self.assertFalse(gf.is_manager(None, {}))


class SessionForPane(unittest.TestCase):
    def test_newest_open_item_wins(self):
        """Pane ids are reused, so a stale item can wear a live pane's id."""
        items = [
            {"pane": "476", "session_id": "old", "ts": 1},
            {"pane": "476", "session_id": "new", "ts": 2},
        ]
        self.assertEqual(gf.session_for_pane("476", items), "new")

    def test_unknown_pane_is_none(self):
        self.assertIsNone(gf.session_for_pane("999", [{"pane": "1", "session_id": "s"}]))

    def test_pane_matches_across_types(self):
        self.assertEqual(gf.session_for_pane(476, [{"pane": "476", "session_id": "s"}]), "s")


class LogItems(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = self._tmp.name
        self.addCleanup(self._tmp.cleanup)

    def log(self, name, lines):
        with open(os.path.join(self.dir, name), "w", encoding="utf-8") as handle:
            for line in lines:
                handle.write(json.dumps(line) + "\n")

    def test_closed_item_is_still_returned(self):
        """The defect this guards: dropping closed items breaks the pane hop.

        Measured 2026-09-25 — a gate the feed reported on pane 1908 had all 29 of
        its items closed by the time this read ran, so excluding them returned no
        session and the filter failed open, emitting a pane owned by another live
        manager. Whether the gate is open was already decided upstream.
        """
        self.log("a.events.jsonl", [
            {"type": "open", "item_id": "1", "pane": "5", "session_id": "s"},
            {"type": "close", "item_id": "1"},
        ])
        items = gf.log_items(self.dir)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["state"], "answered")
        self.assertEqual(gf.session_for_pane("5", items), "s")

    def test_open_item_is_marked_open(self):
        self.log("a.events.jsonl", [{"type": "open", "item_id": "1", "pane": "5"}])
        items = gf.log_items(self.dir)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["state"], "open")

    def test_torn_line_is_skipped_not_fatal(self):
        """The hook appends while this reads, so a torn final line is expected."""
        with open(os.path.join(self.dir, "a.events.jsonl"), "w", encoding="utf-8") as handle:
            handle.write(json.dumps({"type": "open", "item_id": "1", "pane": "5"}) + "\n")
            handle.write('{"type": "open", "item_id": "2"')
        self.assertEqual(len(gf.log_items(self.dir)), 1)

    def test_missing_dir_is_empty(self):
        self.assertEqual(gf.log_items(os.path.join(self.dir, "nope")), [])

    def test_closed_item_still_resolves_its_pane(self):
        """The end-to-end shape of the race: feed says gated, log says closed."""
        self.log("a.events.jsonl", [
            {"type": "open", "item_id": "1", "pane": "1908", "session_id": "82bf6c3a", "ts": 1},
            {"type": "close", "item_id": "1"},
        ])
        items = gf.log_items(self.dir)
        self.assertEqual(gf.session_for_pane("1908", items), "82bf6c3a")


class PanesFromFeed(unittest.TestCase):
    def test_reads_the_row_marker_in_order(self):
        feed = [
            "Needs you (2)\n",
            "  [1892]    55m  ⚙ some task\n",
            "         wezterm cli activate-pane --pane-id 1892\n",
            "  [1850]     9m  ⚙ another\n",
        ]
        self.assertEqual(gf.panes_from_feed(feed), ["1892", "1850"])

    def test_activate_flag_alone_is_not_a_row(self):
        """The flag also rides a row whose pane may already be gone."""
        self.assertEqual(gf.panes_from_feed(["  wezterm cli activate-pane --pane-id 7\n"]), [])

    def test_duplicate_panes_collapse(self):
        self.assertEqual(gf.panes_from_feed(["  [7] a\n", "  [7] b\n"]), ["7"])

    def test_right_aligned_marker_is_read(self):
        """The live render pads the id inside the brackets — `[ 417]`, `[   0]`.

        ⚠️ The fixtures above use unpadded ids, which is exactly why the padded
        shape went unmatched in production while every test here passed: a
        constructed probe whose input shape the real feed never emits. Measured
        2026-10-01 against a live render carrying 8 pane rows — `panes_from_feed`
        returned none of them and the caller exited `pass --pane, --feed, or
        both` against a feed that had them.
        """
        feed = [
            "Needs you (3)\n",
            "  [ 417]     0m  ⚙ PR Review - 2026W40-thu\n",
            "  [  90]    18m  Vuln Fix Agent\n",
            "  [   0]     0m  Fleet Manager\n",
        ]
        self.assertEqual(gf.panes_from_feed(feed), ["417", "90", "0"])


class UnreadableSourcesAreUnknown(unittest.TestCase):
    """An empty read is a SUCCESSFUL read returning a decisive negative.

    Reading "the ledger holds 0 records" as "the probe could not be read" is the
    reasoning error that produced the wrong conclusion at Manager Session:517.
    """

    def test_missing_ledger_dir_is_empty_dict(self):
        self.assertEqual(gf.load_ledger("/nonexistent/ledger"), {})

    def test_missing_registry_dir_is_none_not_empty_set(self):
        self.assertIsNone(gf.live_ids("/nonexistent/registry"))

    def test_present_but_empty_registry_is_an_empty_set(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(gf.live_ids(d), set())


class LoadLedger(unittest.TestCase):
    def test_keys_by_session_id(self):
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "s.json"), "w", encoding="utf-8") as handle:
                json.dump({"session_id": "abc", "parent_session": "mgr"}, handle)
            self.assertEqual(gf.load_ledger(d)["abc"]["parent_session"], "mgr")

    def test_malformed_record_is_skipped(self):
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "bad.json"), "w", encoding="utf-8") as handle:
                handle.write("{not json")
            self.assertEqual(gf.load_ledger(d), {})


class Evaluate(unittest.TestCase):
    def test_full_chain_pane_to_peer_manager(self):
        ledger = {"w1": {"session_id": "w1", "parent_session": PEER}}
        items = [{"pane": "1887", "session_id": "w1", "ts": 1}]
        call, reason, session_id, spawner = gf.evaluate(
            "1887", ledger, {PEER}, items, ME
        )
        self.assertEqual((call, reason), (gf.DROP, "peer-manager"))
        self.assertEqual(session_id, "w1")
        self.assertEqual(spawner, PEER)

    def test_pane_with_no_event_record_is_unowned(self):
        call, reason, session_id, _ = gf.evaluate("1", {}, set(), [], ME)
        self.assertEqual((call, reason, session_id), (gf.EMIT, "unowned", None))


class Claims(unittest.TestCase):
    """A claim is the one ownership input that is not the spawn edge.

    Drop on a claim a LIVE PEER manager holds; emit on one this watcher holds,
    on one whose holder is gone, and against an unreadable registry. Both halves
    of the load-bearing probe live here: a fix that drops every `spawner=None`
    pane fails `test_unclaimed_pane_is_still_emitted`, and a fix that drops none
    fails `test_claimed_pane_is_dropped`.
    """

    LEDGER = {"worker-1": {"session_id": "worker-1", "parent_session": "manager-x"}}
    LIVE = {"worker-1", "manager-x", "manager-y", ME, PEER}

    def call(self, session_id, spawner, claims, live=_UNSET, me=ME):
        return gf.verdict(
            session_id,
            spawner,
            self.LEDGER,
            self.LIVE if live is _UNSET else live,
            me,
            claims,
        )

    def test_claimed_pane_is_dropped(self):
        """Half one: a claim held by a live peer drops the pane."""
        self.assertEqual(self.call("w", None, {"w": PEER}), (gf.DROP, "claimed"))

    def test_unclaimed_pane_is_still_emitted(self):
        """Half two: the same pane with no claim still emits.

        This is the half that fails a fix dropping every `spawner=None` pane --
        the reason the criterion names both halves rather than one.
        """
        self.assertEqual(self.call("w", None, {}), (gf.EMIT, "unowned"))

    def test_claim_overrides_a_peer_manager_spawner_too(self):
        """The claim is checked before the spawner, so it wins on either path."""
        self.assertEqual(self.call("w", PEER, {"w": "manager-y"}), (gf.DROP, "claimed"))

    def test_own_claim_is_kept(self):
        """A pane this watcher adopted is one it EXISTS to see."""
        self.assertEqual(self.call("w", None, {"w": ME}), (gf.EMIT, "own-claim"))

    def test_dead_holder_claim_is_kept(self):
        """A gone manager's claim is nobody's -- the rule for a dead spawner."""
        self.assertEqual(
            self.call("w", None, {"w": "manager-gone"}), (gf.EMIT, "claim-dead")
        )

    def test_claim_against_unreadable_registry_fails_open(self):
        """An unreadable registry is UNKNOWN, so the claim cannot silence a pane."""
        self.assertEqual(
            self.call("w", None, {"w": PEER}, live=None),
            (gf.EMIT, "liveness-unknown"),
        )

    def test_claim_on_another_session_does_not_leak(self):
        """Keyed on the gated session id -- a claim elsewhere must not drop here."""
        self.assertEqual(self.call("w", None, {"other": PEER}), (gf.EMIT, "unowned"))

    def test_self_session_beats_a_claim(self):
        self.assertEqual(self.call(ME, None, {ME: PEER}), (gf.EMIT, "self"))

    def test_own_worker_beats_a_peer_claim(self):
        """The keep-set invariant, which the claim branch sits BELOW on purpose.

        A manager's own workers are the panes it EXISTS to see, so a peer's
        claim must never silence one. The PREDECESSOR warning in the module
        docstring records a measured incident of a manager silently losing four
        of its own workers; hoisting `own-worker` above the claim keeps that
        from coming back through the claim door.
        """
        self.assertEqual(self.call("w", ME, {"w": PEER}), (gf.EMIT, "own-worker"))

    def test_absent_claims_mapping_is_the_pre_claim_behaviour(self):
        self.assertEqual(self.call("w", None, None), (gf.EMIT, "unowned"))


class LoadClaims(unittest.TestCase):
    def write(self, payload):
        tmp = tempfile.NamedTemporaryFile(
            "w", suffix=".json", delete=False, encoding="utf-8"
        )
        tmp.write(payload)
        tmp.close()
        self.addCleanup(os.unlink, tmp.name)
        return tmp.name

    def test_keys_by_gated_session(self):
        path = self.write(json.dumps({
            "version": 1,
            "claims": {"s1": {"session": "s1", "manager": "m1"}},
        }))
        self.assertEqual(gf.load_claims(path), {"s1": "m1"})

    def test_missing_file_is_empty(self):
        self.assertEqual(gf.load_claims("/nonexistent/claims.json"), {})

    def test_malformed_json_is_empty(self):
        self.assertEqual(gf.load_claims(self.write("{not json")), {})

    def test_entry_without_manager_is_skipped(self):
        """A malformed entry is not a claim held by nobody."""
        path = self.write(json.dumps({"claims": {"s1": {"session": "s1"}}}))
        self.assertEqual(gf.load_claims(path), {})

    def test_wrong_shape_is_empty(self):
        self.assertEqual(gf.load_claims(self.write(json.dumps({"claims": []}))), {})


class EvaluateWithClaims(unittest.TestCase):
    def test_full_chain_pane_to_claim(self):
        """The probe's shape: a `spawner=None` pane, claimed by a live peer."""
        items = [{"pane": "372", "session_id": "884016da", "ts": 1}]
        call, reason, session_id, spawner = gf.evaluate(
            "372", {}, {PEER}, items, ME, {"884016da": PEER}
        )
        self.assertEqual((call, reason), (gf.DROP, "claimed"))
        self.assertEqual(session_id, "884016da")
        self.assertIsNone(spawner)

    def test_same_pane_unclaimed_still_emits(self):
        items = [{"pane": "372", "session_id": "884016da", "ts": 1}]
        call, reason, _, _ = gf.evaluate("372", {}, {PEER}, items, ME, {})
        self.assertEqual((call, reason), (gf.EMIT, "unowned"))


class ClaimsFilePath(unittest.TestCase):
    """The CLI path to the new input, end to end.

    The writer (`ownership-claim.py`) resolves its store from
    `SUPERVISOR_OWNERSHIP_CLAIMS`. A reader that honours only its own default
    writes a claim nothing ever consults -- recorded, listed by `list`, and
    silently ignored by every watcher. These run the script as a subprocess so
    the wiring from argv and from the environment is what is exercised.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = self._tmp.name
        self.state = os.path.join(self.dir, "state")
        self.registry = os.path.join(self.dir, "registry")
        os.makedirs(self.state)
        os.makedirs(self.registry)
        with open(os.path.join(self.state, "a.events.jsonl"), "w", encoding="utf-8") as handle:
            handle.write(json.dumps({
                "type": "open", "item_id": "1", "pane": "372", "session_id": GATED,
            }) + "\n")
        with open(os.path.join(self.registry, "p.json"), "w", encoding="utf-8") as handle:
            json.dump({"sessionId": PEER}, handle)
        self.claims = os.path.join(self.dir, "claims.json")

    def write_claims(self):
        with open(self.claims, "w", encoding="utf-8") as handle:
            json.dump({"claims": {GATED: {"session": GATED, "manager": PEER}}}, handle)

    def run_filter(self, *extra, env=None):
        return subprocess.run(
            [sys.executable, _SCRIPT, "--pane", "372", "--self", ME, "--explain",
             "--state-dir", self.state, "--registry-dir", self.registry,
             "--ledger-dir", os.path.join(self.dir, "ledger"), *extra],
            capture_output=True, text=True, env=env,
        )

    def test_claims_file_flag_drops_a_claimed_pane(self):
        self.write_claims()
        result = self.run_filter("--claims-file", self.claims)
        self.assertIn("drop claimed", result.stdout, result.stderr)

    def test_env_override_is_honoured_without_the_flag(self):
        """The writer's env var must reach the reader, or the store is a no-op."""
        self.write_claims()
        env = dict(os.environ, SUPERVISOR_OWNERSHIP_CLAIMS=self.claims)
        result = self.run_filter(env=env)
        self.assertIn("drop claimed", result.stdout, result.stderr)

    def test_absent_claim_file_still_emits(self):
        result = self.run_filter("--claims-file", os.path.join(self.dir, "absent.json"))
        self.assertIn("emit unowned", result.stdout, result.stderr)


if __name__ == "__main__":
    unittest.main()
