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
  * a manager's OWN gate (hop 3b) drops on the GATED SESSION being a live peer
    manager, not only on its spawner being one. The branch keys on ledger
    membership rather than on an empty spawner, so a WORKER whose
    `parent_session` never resolved is not mistaken for a manager -- and a dead
    peer manager's own gate stays kept (SC3).
  * an EMPTY feed is a SUCCESSFUL READ, not a usage error -- the guard tests
    whether a source was SUPPLIED, not whether it produced rows. Collapsing the
    two made the documented watcher arm exit 2 on every quiet tick, so a
    `Monitor` armed with it died with `script failed (exit 2)` within seconds
    (measured 2026-10-05, manager loop tick 154). The negative control is pinned
    beside it: a call with neither `--pane` nor `--feed` still exits 2, so the
    fix cannot be "delete the guard".

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

    def test_worker_of_a_manager_opened_through_the_worker_path_is_dropped(self):
        """The measured 2026-10-06 incident, at the hop it actually travels.

        The CDB in Weldall Manager was spawned by the Fleet Manager, so it HAS a
        ledger record -- hop 3b's `not spawner` guard never fires for it, and the
        gates that re-escalated are its WORKERS', resolved here at hop 3. The
        record's `role` is the whole difference: without it this returns
        `worker-spawner`, which is the reported bug.
        """
        ledger = {
            "mgr-spawned": {
                "session_id": "mgr-spawned",
                "parent_session": "fleet",
                "role": "manager",
            },
            "w": {"session_id": "w", "parent_session": "mgr-spawned", "role": "agent"},
        }
        live = {"mgr-spawned", "w", "fleet"}
        self.assertEqual(
            self.call("w", "mgr-spawned", ledger=ledger, live=live),
            (gf.DROP, "peer-manager"),
        )

    def test_worker_of_a_role_agent_spawner_still_emits(self):
        """The inverse control at the same hop: a fix that dropped every gate
        would satisfy the case above and destroy the filter."""
        ledger = {
            "w2": {"session_id": "w2", "parent_session": "w", "role": "agent"},
            "w": {"session_id": "w", "parent_session": "mgr", "role": "agent"},
        }
        live = {"w2", "w", "mgr"}
        self.assertEqual(
            self.call("w2", "w", ledger=ledger, live=live),
            (gf.EMIT, "worker-spawner"),
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

    def test_dead_role_manager_spawner_is_still_kept(self):
        """The widened class reaches hop 4 too, and must still emit there.

        `is_manager` now returns True for a record declaring `role: "manager"`, so a
        spawner holding such a record and ABSENT from the live set is a new arrival
        at the dead-manager arm. It must emit: a dead manager cannot act on its own
        gate, so nothing duplicates this wake and dropping it would leave the gate
        owned by nobody.
        """
        ledger = {
            "mgr-dead": {
                "session_id": "mgr-dead",
                "parent_session": "fleet",
                "role": "manager",
            },
            "w": {"session_id": "w", "parent_session": "mgr-dead", "role": "agent"},
        }
        self.assertEqual(
            self.call("w", "mgr-dead", ledger=ledger, live={"w"}),
            (gf.EMIT, "dead-manager"),
        )

    def test_live_spawnerless_role_manager_own_gate_is_dropped(self):
        """Hop 3b's second arm, newly reachable.

        A record-bearing manager normally resolves at hop 3 through its spawner and
        never reaches hop 3b -- but one whose `parent_session` never resolved has no
        spawner and does. Its own gate must drop as `peer-manager-own`, exactly as a
        record-less manager's does.
        """
        ledger = {
            "mgr-nospawner": {
                "session_id": "mgr-nospawner",
                "parent_session": None,
                "role": "manager",
            }
        }
        self.assertEqual(
            self.call("mgr-nospawner", None, ledger=ledger, live={"mgr-nospawner"}),
            (gf.DROP, "peer-manager-own"),
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

    # --- HOP 3b: the gated session is ITSELF a manager ---------------------
    #
    # Hop 3 reads the spawn edge. A manager that raises a gate on itself has
    # none -- an operator-started manager was never spawned -- so hop 2 resolves
    # no spawner and the pane read `unowned`, waking every other manager's
    # watcher. These pin the new branch and, just as importantly, its edges.

    def test_live_peer_managers_own_gate_is_dropped(self):
        """The defect this rule removes: a manager's OWN pane, not its workers'."""
        self.assertEqual(
            self.call("manager-z", None, live={"manager-z"}),
            (gf.DROP, "peer-manager-own"),
        )

    def test_own_session_is_not_dropped_by_hop_3b(self):
        """Self-exclusion outranks the new rule, or the watcher silences itself."""
        self.assertEqual(self.call(ME, None, live={ME}), (gf.EMIT, "self"))

    def test_dead_peer_managers_own_gate_is_kept(self):
        """SC3: a dead manager cannot act on its own gate, so nothing duplicates
        this wake and dropping it would leave the gate owned by nobody."""
        self.assertEqual(self.call("manager-z", None, live=set()), (gf.EMIT, "unowned"))

    def test_unknown_liveness_keeps_a_managers_own_gate(self):
        """An unreadable registry is UNKNOWN -- fail open, as every hop does."""
        self.assertEqual(self.call("manager-z", None, live=None), (gf.EMIT, "unowned"))

    def test_worker_with_unresolved_parent_is_not_dropped(self):
        """Hop 3b keys on LEDGER MEMBERSHIP, not on an empty spawner.

        A worker whose spawn chain never resolved to a registered session carries
        `parent_session: null` too (CLAUDE.md § The spawn ledger) -- but it has a
        ledger record of its OWN, so it is not a manager and must keep emitting.
        Keying the new branch on the empty spawner would drop every such worker,
        which is a live shape rather than a hypothetical one.
        """
        ledger = {"w-null": {"session_id": "w-null", "parent_session": None}}
        self.assertEqual(
            self.call("w-null", None, ledger=ledger, live={"w-null"}),
            (gf.EMIT, "unowned"),
        )


class IsManager(unittest.TestCase):
    def test_parent_with_no_own_record_is_a_manager(self):
        self.assertTrue(gf.is_manager("1217e759", {}))

    def test_parent_with_its_own_record_is_a_worker(self):
        ledger = {"w": {"parent_session": "m"}}
        self.assertFalse(gf.is_manager("w", ledger))

    def test_missing_id_is_not_a_manager(self):
        self.assertFalse(gf.is_manager(None, {}))

    def test_a_record_declaring_the_manager_role_is_a_manager(self):
        """The case the spawn edge cannot reach: a manager opened through
        `spawn_agent(role="manager")` has a record of its own, and reading that as
        proof of "worker" made every peer re-escalate its workers' gates."""
        ledger = {"m": {"parent_session": "fleet", "role": "manager"}}
        self.assertTrue(gf.is_manager("m", ledger))

    def test_a_record_declaring_the_agent_role_is_still_a_worker(self):
        """The inverse control: recording a role must not make every record a
        manager. A fix that dropped every gate would satisfy the manager case
        above and destroy the filter."""
        ledger = {"w": {"parent_session": "m", "role": "agent"}}
        self.assertFalse(gf.is_manager("w", ledger))

    def test_a_record_with_no_role_is_a_worker(self):
        """Two shapes, one behaviour, and BOTH are real.

        A record written before the field existed has no `role` KEY at all; a record
        written by the cluster path carries an explicit `role: null` (the agent
        literal in `supervisor.mjs` hardcodes it there, because that path returns
        before role resolution). `record.get("role")` reads both as absent, which is
        what this asserts. The fixture used to carry only the explicit-null shape
        while its docstring claimed the key-less one — two different records.
        """
        self.assertFalse(gf.is_manager("w", {"w": {"parent_session": "m"}}))
        self.assertFalse(gf.is_manager("w", {"w": {"parent_session": "m", "role": None}}))


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

    def test_replayed_row_is_flagged(self):
        """The mark the feed stamps on an event-log-fallback row is carried.

        ⚠️ This is the guard, not decoration: `who-needs-me.py` stamps
        `⟳replay` on every row it returns when the attention store is
        unreachable, precisely so a manager does not act on a gate answered
        hours ago. `feed_rows` used to return the pane id alone and discard the
        rest of the line, so the filter emitted `864` for a row the feed had
        rendered `5h49m  ⟳replay  ⚙ …`.
        """
        feed = [
            "Needs you (2)\n",
            "  [ 864]  5h49m  ⟳replay  ⚙ The Bucket Refusal Validates Shape\n",
            "  [ 417]     0m  ⚙ PR Review - 2026W40-thu\n",
        ]
        self.assertEqual(gf.feed_rows(feed), [("864", True), ("417", False)])

    def test_live_rows_carry_no_mark(self):
        """A responsive store's render is unchanged — no mark is ever present."""
        feed = ["  [ 417]     0m  ⚙ a\n", "  [  90]    18m  Vuln Fix Agent\n"]
        self.assertEqual(gf.feed_rows(feed), [("417", False), ("90", False)])

    def test_mark_is_read_from_the_row_it_appears_on(self):
        """A mark on one row must not leak to its neighbours."""
        feed = [
            "  [ 1]  0m  ⟳replay  ⚙ a\n",
            "  [ 2]  0m  ⚙ b\n",
            "  [ 3]  0m  ⟳replay  ⚙ c\n",
        ]
        self.assertEqual(gf.feed_rows(feed), [("1", True), ("2", False), ("3", True)])

    def test_panes_from_feed_is_feed_rows_projected(self):
        """The old contract is preserved: ids alone, same order, same dedup."""
        feed = ["  [ 7] a\n", "  [ 7] b\n", "  [ 8] c\n"]
        self.assertEqual(gf.panes_from_feed(feed), ["7", "8"])
        self.assertEqual(gf.feed_rows(feed), [("7", False), ("8", False)])


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

    def test_full_chain_pane_to_a_peer_managers_own_gate(self):
        """The measured shape: an operator-started manager's OWN pane.

        Session `6f8ed1f6` (Work Approval Manager) raised a gate on its own pane
        162, and the filter emitted it as `unowned` -- the exact class it exists
        to drop. It has no ledger record of its own, which is what makes it a
        manager, and it is live, which is what makes it a peer.
        """
        items = [{"pane": "162", "session_id": "6f8ed1f6", "ts": 1}]
        call, reason, session_id, spawner = gf.evaluate(
            "162", {}, {"6f8ed1f6"}, items, ME
        )
        self.assertEqual((call, reason), (gf.DROP, "peer-manager-own"))
        self.assertEqual(session_id, "6f8ed1f6")
        self.assertIsNone(spawner)


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

    def test_a_claim_cannot_weaken_a_resolved_spawner_drop(self):
        """A claim may only ADD a drop, never remove one.

        A live peer spawner is already a resolved DROP. A claim on the same pane
        must not change the outcome, whatever its own state.
        """
        self.assertEqual(
            self.call("w", PEER, {"w": "manager-y"}), (gf.DROP, "peer-manager")
        )

    def test_a_stale_claim_does_not_undo_a_peer_manager_drop(self):
        """The blocking case: a live peer spawner plus a claim whose holder is gone.

        Resolving the claim first made this read `EMIT/claim-dead`, converting a
        resolved peer-manager drop into an emit and re-creating the peer-owned
        wake the filter exists to remove. The spawner path is resolved first now,
        so the drop stands.
        """
        self.assertEqual(
            self.call("w", PEER, {"w": "manager-gone"}), (gf.DROP, "peer-manager")
        )

    def test_a_live_peer_claim_still_drops_a_spawnerless_pane(self):
        """The claim's own job is untouched: it adds the drop where none existed."""
        self.assertEqual(self.call("w", None, {"w": "manager-y"}), (gf.DROP, "claimed"))

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

    # --- the claim x hop-3b intersection -----------------------------------
    #
    # Hop 3b sits ABOVE the claim block, so without an explicit guard it would
    # drop an adopted pane before the claim was ever read -- silencing the gate
    # for the one manager that undertook to route it, while the peer's own
    # watcher exits at the `self` check and nobody is left to report it. The
    # fixture that hid this: `LIVE` above does not contain `"w"`, so hop 3b's
    # liveness guard kept every other case in this class inert.

    def test_adopted_live_managers_own_gate_is_still_kept(self):
        """`own-claim` outranks hop 3b: an adopted pane is one we EXIST to see."""
        self.assertEqual(
            self.call("manager-z", None, {"manager-z": ME}, live={"manager-z"}),
            (gf.EMIT, "own-claim"),
        )

    def test_a_peers_claim_on_a_live_managers_own_gate_still_drops(self):
        """The guard is scoped to OUR adoption only -- a peer's claim does not
        rescue the pane, and the reason names the rule that actually fired."""
        self.assertEqual(
            self.call("manager-z", None, {"manager-z": PEER}, live={"manager-z"}),
            (gf.DROP, "peer-manager-own"),
        )


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


class FeedEmitCarriesTheReplayMark(unittest.TestCase):
    """The watcher's entire input is this line, so the mark has to be on it.

    `--feed` prints a pane id and nothing else, which made a replayed row
    byte-identical to a live one. The arm
    `who-needs-me.py --section needs-you | gate-owner-filter.py --feed --self <sid>`
    hands the watcher whatever this prints, so a row the feed had already marked
    `⟳replay` reached it as a bare `864` and the watcher woke on a gate that had
    been answered. Measured 2026-10-03 (Manager Layer, ticks 89-90): **10
    firings in ~70 minutes, 2 real.**

    Both rows are KEPT here — an empty ledger and registry fail open, so each
    pane reads `unowned` — which is what makes the two lines comparable. The
    assertion is that they differ, not that either is present.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = self._tmp.name
        self.state = os.path.join(self.dir, "state")
        self.registry = os.path.join(self.dir, "registry")
        self.ledger = os.path.join(self.dir, "ledger")
        for path in (self.state, self.registry, self.ledger):
            os.makedirs(path)

    def emit(self, feed):
        return subprocess.run(
            [sys.executable, _SCRIPT, "--feed", "--self", ME,
             "--state-dir", self.state, "--registry-dir", self.registry,
             "--ledger-dir", self.ledger,
             "--claims-file", os.path.join(self.dir, "no-claims.json")],
            input=feed, capture_output=True, text=True,
        )

    def test_replayed_and_live_rows_are_distinguishable(self):
        result = self.emit(
            "Needs you (2)\n"
            "  [ 864]  5h49m  ⟳replay  ⚙ an answered gate\n"
            "  [ 417]     0m  ⚙ a live gate\n"
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        lines = result.stdout.splitlines()
        self.assertEqual(len(lines), 2, result.stdout)
        replayed, live = lines
        self.assertNotEqual(replayed, live)
        self.assertIn("864", replayed)
        self.assertIn(gf.REPLAY_MARK, replayed)
        self.assertEqual(live, "417")

    def test_a_live_only_feed_renders_bare_pane_ids(self):
        """No regression: a responsive store's output is byte-for-byte unchanged."""
        result = self.emit("Needs you (1)\n  [ 417]     0m  ⚙ a live gate\n")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "417\n")


class SummaryLine(unittest.TestCase):
    """The stderr counters, which nothing asserted on before this change.

    The line exists so a broken watcher stops looking like a quiet fleet, and
    this change adds a third bucket to it. A typo in the f-string, or a bucket
    name drifting from the reason `verdict` actually returns, would render as
    `dropped(peer-manager-own): 0` on every run -- the exact misread the
    split-by-reason line was built to prevent. Pinning both counts in ONE run
    keeps each bucket tied to its reason string.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = self._tmp.name
        self.state = os.path.join(self.dir, "state")
        self.registry = os.path.join(self.dir, "registry")
        self.ledger = os.path.join(self.dir, "ledger")
        for path in (self.state, self.registry, self.ledger):
            os.makedirs(path)
        # Two panes off one live manager: its OWN gate (no ledger record at all,
        # so hop 3b's branch) and its worker's (a ledger record, so hop 3's).
        with open(os.path.join(self.state, "a.events.jsonl"), "w", encoding="utf-8") as handle:
            handle.write(json.dumps(
                {"type": "open", "item_id": "1", "pane": "10", "session_id": "own-mgr"}
            ) + "\n")
            handle.write(json.dumps(
                {"type": "open", "item_id": "2", "pane": "11", "session_id": "w1"}
            ) + "\n")
        with open(os.path.join(self.registry, "p.json"), "w", encoding="utf-8") as handle:
            json.dump({"sessionId": "own-mgr"}, handle)
        with open(os.path.join(self.ledger, "w1.json"), "w", encoding="utf-8") as handle:
            json.dump({"session_id": "w1", "parent_session": "own-mgr"}, handle)

    def test_each_drop_bucket_is_counted_under_its_own_reason(self):
        result = subprocess.run(
            [sys.executable, _SCRIPT, "--pane", "10", "--pane", "11", "--self", ME,
             "--state-dir", self.state, "--registry-dir", self.registry,
             "--ledger-dir", self.ledger,
             "--claims-file", os.path.join(self.dir, "no-claims.json")],
            capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("gates: 2", result.stderr, result.stderr)
        self.assertIn("emit: 0", result.stderr, result.stderr)
        self.assertIn("dropped(peer-manager): 1", result.stderr, result.stderr)
        self.assertIn("dropped(peer-manager-own): 1", result.stderr, result.stderr)


class EmptyFeedIsNotAUsageError(unittest.TestCase):
    """An empty feed is a successful read; only a sourceless call is an error.

    The two states that leave the derived row set empty are different facts, and
    the guard used to collapse them. `--feed` with no `[<pane>]` rows is a
    *successful* read of an empty feed -- the common case -- while a call
    carrying neither `--pane` nor `--feed` is malformed. Testing the row set
    instead of the source made the documented watcher arm

        who-needs-me.py --section needs-you | gate-owner-filter.py --feed --self <sid>

    exit 2 with `pass --pane, --feed, or both` on every quiet tick, so a
    `Monitor` armed with it died with `script failed (exit 2)` within seconds
    while its sibling watchers stayed up: the manager's push channel was dead
    exactly when the fleet was quiet, and the failure read as a bad command
    rather than as a lost watch. Measured 2026-10-05 (manager loop tick 154) --
    the feed read `Needs you (0)` / `Nothing needs you.` and the pipeline died
    with the usage error, reproduced in both argument orders and against a
    synthetic empty feed on stdin.

    Both halves are pinned. The empty-feed cases would pass on a filter that
    deleted the guard outright, so the sourceless call is asserted beside them:
    the fix is "test the source", never "drop the check".
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = self._tmp.name
        self.state = os.path.join(self.dir, "state")
        self.registry = os.path.join(self.dir, "registry")
        self.ledger = os.path.join(self.dir, "ledger")
        for path in (self.state, self.registry, self.ledger):
            os.makedirs(path)

    def invoke(self, *argv, feed=None):
        return subprocess.run(
            [sys.executable, _SCRIPT,
             "--state-dir", self.state, "--registry-dir", self.registry,
             "--ledger-dir", self.ledger,
             "--claims-file", os.path.join(self.dir, "no-claims.json"), *argv],
            input=feed, capture_output=True, text=True,
        )

    def test_an_empty_feed_exits_zero_and_emits_nothing(self):
        result = self.invoke("--feed", "--self", ME, feed="Needs you (0)\nNothing needs you.\n")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "", result.stdout)
        self.assertIn("gates: 0", result.stderr, result.stderr)

    def test_argument_order_does_not_matter(self):
        """The measured failure reproduced in both orders, so both are pinned."""
        result = self.invoke("--self", ME, "--feed", feed="Needs you (0)\n")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "")

    def test_an_empty_stdin_is_the_same_read(self):
        """No header line at all -- the synthetic case the reproduction used."""
        result = self.invoke("--feed", "--self", ME, feed="")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "")

    def test_a_call_with_no_source_at_all_still_errors(self):
        """The negative control: widening the guard must not delete it."""
        result = self.invoke("--self", ME)
        self.assertEqual(result.returncode, 2, result.stdout)
        self.assertIn("pass --pane, --feed, or both", result.stderr)

    def test_pane_alone_is_still_a_source(self):
        """`--pane` is the guard's other half and must keep working."""
        result = self.invoke("--pane", "372", "--self", ME)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("gates: 1", result.stderr, result.stderr)


class UpstreamRefusalFailsLoudly(unittest.TestCase):
    """A REFUSED upstream must not read as an empty feed.

    The sibling class above pins the other half of the same distinction: an empty
    feed is a real answer and must keep exiting 0. Both halves are needed, because
    the two states leave an identical row set and only the source tells them apart.

    `who-needs-me.py` refuses a read when the WezTerm mux socket is unreadable, and
    writes `REFUSAL_MARKER` to its STDOUT so the refusal survives the pipe. Measured
    2026-10-06 against v0.106.0, with the socket unreachable:

        who-needs-me.py --section needs-you | gate-owner-filter.py --feed --self <sid>

    printed `gates: 0  emit: 0  dropped(peer-manager): 0  dropped(peer-manager-own):
    0  dropped(claimed): 0` and exited 0. A manager arming that as a `Monitor` got a
    watcher that never fired and looked alive — a healthy empty queue, certified by
    the success code, for a transport that never answered. The refusal text reached
    only stderr, which the pipe does not carry.

    ⚠️ **No `gates:` line is printed on this path, and that is the point.** A marker
    printed BESIDE the false claim is not a fix — `gates: 0` still reads as a
    measurement, and a consumer parsing it cannot tell it from a quiet fleet. The
    line that lies is the line that must not be printed.
    """

    REFUSAL = f"{gf.REFUSAL_MARKER}: WezTerm pane list unreadable — mux socket silent"

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = self._tmp.name
        self.state = os.path.join(self.dir, "state")
        self.registry = os.path.join(self.dir, "registry")
        self.ledger = os.path.join(self.dir, "ledger")
        for path in (self.state, self.registry, self.ledger):
            os.makedirs(path)

    def invoke(self, *argv, feed=None):
        return subprocess.run(
            [sys.executable, _SCRIPT,
             "--state-dir", self.state, "--registry-dir", self.registry,
             "--ledger-dir", self.ledger,
             "--claims-file", os.path.join(self.dir, "no-claims.json"), *argv],
            input=feed, capture_output=True, text=True,
        )

    # --- SC(b): the pipeline fails loudly ------------------------------------

    def test_a_refused_upstream_exits_non_zero(self):
        result = self.invoke("--feed", "--self", ME, feed=self.REFUSAL + "\n")
        self.assertNotEqual(0, result.returncode, result.stdout)

    def test_a_refused_upstream_prints_no_gate_count(self):
        """The line that lies is the line that must not be printed."""
        result = self.invoke("--feed", "--self", ME, feed=self.REFUSAL + "\n")
        self.assertNotIn("gates:", result.stderr, result.stderr)

    def test_a_refused_upstream_surfaces_the_refusal_text(self):
        """Discarding the message was half the defect; the reader must see it."""
        result = self.invoke("--feed", "--self", ME, feed=self.REFUSAL + "\n")
        self.assertIn("upstream refused", result.stderr)
        self.assertIn("mux socket silent", result.stderr)

    # --- SC(c): the control, pinned beside it --------------------------------

    def test_an_unmarked_empty_feed_still_exits_zero(self):
        """The only difference from the case above is the marker."""
        result = self.invoke("--feed", "--self", ME, feed="")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("gates: 0", result.stderr)

    def test_a_healthy_feed_is_unaffected(self):
        result = self.invoke("--feed", "--self", ME,
                             feed="Needs you (0)\nNothing needs you.\n")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("gates: 0", result.stderr)

    # --- the extractor, in isolation -----------------------------------------

    def test_feed_refusal_is_none_without_the_marker(self):
        self.assertIsNone(gf.feed_refusal(["Needs you (0)\n", "Nothing needs you.\n"]))

    def test_feed_refusal_returns_the_line_it_matched(self):
        self.assertIn("mux socket silent", gf.feed_refusal([self.REFUSAL + "\n"]))

    def test_feed_refusal_tolerates_leading_whitespace(self):
        self.assertIsNotNone(gf.feed_refusal(["   " + self.REFUSAL + "\n"]))

    def test_feed_refusal_ignores_a_marker_that_is_not_leading(self):
        """Matching is on the line's first token, so prose quoting it is not a refusal."""
        self.assertIsNone(gf.feed_refusal([f"see {gf.REFUSAL_MARKER}: for details\n"]))

    def test_a_refusal_line_is_not_parsed_as_a_pane_row(self):
        """`feed_rows` matches `[<pane>]`; the marker must never look like one."""
        self.assertEqual(gf.feed_rows([self.REFUSAL + "\n"]), [])


if __name__ == "__main__":
    unittest.main()
