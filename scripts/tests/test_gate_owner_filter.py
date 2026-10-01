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

Run: python3 -m unittest discover -s scripts/tests -v
"""

import importlib.util
import json
import os
import tempfile
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_SCRIPT = os.path.join(os.path.dirname(_HERE), "gate-owner-filter.py")

_spec = importlib.util.spec_from_file_location("gate_owner_filter", _SCRIPT)
gf = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gf)

ME = "92de43c3-bd91-409d-a33e-824a61e5706a"
PEER = "64b4a415-906d-457f-81f9-48a842007b88"

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


if __name__ == "__main__":
    unittest.main()
