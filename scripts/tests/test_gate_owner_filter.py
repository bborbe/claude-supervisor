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

import contextlib
import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import endpoint_fixture  # noqa: E402
from endpoint_fixture import FixtureEndpoint, dead_url, ledger_row  # noqa: E402

_HERE = os.path.dirname(os.path.abspath(__file__))
_SCRIPT = os.path.join(os.path.dirname(_HERE), "gate-owner-filter.py")

# ⚠️ Isolate the two state roots this suite can WRITE and READ, before the module is
# imported so the constants pick them up and before any subprocess inherits them.
#
# Without this, every subprocess test that passes `--self` appends to the operator's live
# `~/.claude/state/manager-attention-watch/<fixture id>/feed-emissions.jsonl` — measured
# 2026-11-11, one suite run left 67 KB there — and every `evaluate_panes` call reads the
# real `~/.claude/state/worker-manager/`, so a real fleet registration colliding with a
# fixture id would flip a verdict. That is the same machine-dependence `OwnerStub` below
# exists to remove, reached through a second door.
_TEST_STATE = tempfile.mkdtemp(prefix="gate-owner-filter-tests-")
os.environ["GATE_EMISSIONS_DIR"] = os.path.join(_TEST_STATE, "emissions")
os.environ["SUPERVISOR_WORKER_MANAGER_DIR"] = os.path.join(_TEST_STATE, "worker-manager")

_spec = importlib.util.spec_from_file_location("gate_owner_filter", _SCRIPT)
gf = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gf)

ME = "92de43c3-bd91-409d-a33e-824a61e5706a"
PEER = "64b4a415-906d-457f-81f9-48a842007b88"
GATED = "33333333-3333-4333-8333-333333333333"


class OwnerStub:
    """`scripts/owner.py` with its probes removed — the filter must never read this machine.

    The real resolver spawns `session-liveness.py` and `manager-liveness.py`, so an unstubbed
    filter run answers from whatever happens to be armed on the machine running the suite. A
    test whose verdict depends on that asserts nothing, and it drifts the moment a real session
    id collides with a fixture's.
    """

    NOT_OWNER = "not-owner"
    OWNER = "owner"
    CANNOT_TELL = "cannot-tell"

    def __init__(self, verdict="owner", reason="live-armed"):
        self.verdict, self.reason = verdict, reason
        self.asked = []

    def of_session(self, sid, subject="", branch="goal"):
        self.asked.append(sid)
        verdict, reason = self.verdict, self.reason
        return type("Ownership", (), {
            "verdict": verdict,
            "reason": reason,
            "session": "LIVE",
            "loop": "OK" if verdict == "owner" else "STOPPED",
            "describe": lambda self: "session LIVE, loop OK",
        })()

# A one-row feed carrying pane 372, in `who-needs-me.py`'s own render. `--pane` filters
# the feed rather than manufacturing a gate, so every test that names a pane supplies
# one -- and supplying it explicitly is also what keeps the suite off the live store,
# since a bare `--pane` produces the feed by running the real producer.
FEED_372 = "[ 372]  3m  ⚙ some task — awaiting-operator\n"

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


class UnownedIsRoutedToTheFleetManager(unittest.TestCase):
    """The unowned class goes to ONE destination, and the fail-open limb is the safety property.

    Measured 2026-11-11: >10 foreign wakes in ~2 h on one goal manager, every one from a
    producer with no spawner and no session id. `unowned` used to emit to every manager, so
    the class had no destination and every manager inherited all of it. These four cases pin
    all four answers, because three of them are wrong in opposite directions: emitting to
    everyone is the flood, dropping everywhere is a black hole, and routing to a dead
    session is a black hole with extra steps.
    """

    LEDGER = {"worker-1": {"session_id": "worker-1", "parent_session": "manager-x"}}
    LIVE = {"worker-1", "fleet-live", "manager-x", "manager-y"}
    FLEET = "fleet-live"
    ME = "manager-x"

    def call(self, session_id=None, spawner=None, me=_UNSET, fleet=_UNSET, claims=None):
        return gf.verdict(
            session_id,
            spawner,
            self.LEDGER,
            self.LIVE,
            self.ME if me is _UNSET else me,
            claims,
            None,
            self.FLEET if fleet is _UNSET else fleet,
        )

    def test_a_goal_manager_drops_an_unowned_gate(self):
        """The fix: the class leaves every goal manager's feed."""
        self.assertEqual(self.call("worker-1", None), (gf.DROP, gf.UNOWNED_FLEET))

    def test_a_gate_with_no_session_id_is_routed_too(self):
        """The OTHER unowned site, and the highest-volume producer class.

        `sweep-gate` / `fleet-keeper` / `drive-escalate` post with no session id at all,
        which is the early return at the top of `verdict()` rather than the spawner path.
        Without this case a change that routed only the spawnerless site passes every other
        test in this class.
        """
        self.assertEqual(self.call(None, None), (gf.DROP, gf.UNOWNED_FLEET))
        self.assertEqual(
            self.call(None, None, me=self.FLEET), (gf.EMIT, gf.UNOWNED_FLEET)
        )

    def test_an_adopted_pane_keeps_its_gate(self):
        """A goal manager that ADOPTED a spawnerless pane must still see it.

        The adoption contract (`commands/steps/adopt.md`): `ownership-claim.py` records the
        adoption precisely so the adopting manager keeps seeing a spawnerless pane. An
        earlier cut of this fix returned the routed drop before the claim block, which broke
        that silently — the routed drop outranked the claim.
        """
        self.assertEqual(
            self.call("worker-1", None, claims={"worker-1": self.ME}),
            (gf.EMIT, "own-claim"),
        )

    def test_a_peer_claim_still_owns_the_pane(self):
        """A claim a live peer holds drops the pane as `claimed`, not `unowned-fleet`.

        The routed limb sits below the claim block for this reason: the file's asymmetry is
        that a claim may ADD a drop, so a new drop must not be inserted ahead of one.
        """
        self.assertEqual(
            self.call("worker-1", None, claims={"worker-1": "manager-y"}),
            (gf.DROP, "claimed"),
        )

    def test_a_peer_managers_own_gate_is_not_relabelled(self):
        """Hop 3b still wins: a live peer manager's own gate drops as `peer-manager-own`.

        Same verdict, different bucket — and the bucket is what `counts_line` and
        `--explain` report, so relabelling it would misattribute the drop.
        """
        self.assertEqual(
            self.call("manager-y", None), (gf.DROP, "peer-manager-own")
        )

    def test_the_fleet_manager_emits_the_same_gate(self):
        """The destination: exactly one manager receives it, so it is routed, not silenced."""
        self.assertEqual(
            self.call("worker-1", None, me=self.FLEET),
            (gf.EMIT, gf.UNOWNED_FLEET),
        )

    def test_no_live_fleet_manager_falls_open_to_every_manager(self):
        """An unresolvable destination is not a licence to silence a gate."""
        self.assertEqual(self.call("worker-1", None, fleet=None), (gf.EMIT, gf.UNOWNED))

    def test_a_diagnostic_run_with_no_self_still_emits(self):
        """`--pane` with no `--self` has no reader to route FOR, and a silent probe reads
        as "this pane is fine"."""
        self.assertEqual(self.call("worker-1", None, me=None), (gf.EMIT, gf.UNOWNED))

    def test_the_routed_drop_never_reaches_a_resolved_drop(self):
        """A peer-manager drop is a resolved fact and must not be re-routed.

        The new branch may only re-route an `unowned` emit; a claim may add a drop and never
        remove one, and the same asymmetry holds here in the other direction.
        """
        self.assertEqual(
            self.call("worker-1", "manager-y", fleet=self.FLEET),
            (gf.DROP, "peer-manager"),
        )

    def test_fleet_resolution_skips_a_dead_registration(self):
        """A registration whose session is not live resolves nothing.

        Routing the class to a dead session would take the gate out of every goal manager's
        feed and deliver it nowhere — strictly worse than the flood.
        """
        with tempfile.TemporaryDirectory() as directory:
            for sid, at in (("fleet-dead", "2026-11-11T10:00:00+00:00"),
                            ("fleet-live", "2026-11-11T09:00:00+00:00")):
                with open(os.path.join(directory, f"{sid}.json"), "w", encoding="utf-8") as h:
                    json.dump({"branch": "fleet", "resolved_at": at}, h)
            self.assertEqual(gf.fleet_manager(self.LIVE, directory), "fleet-live")
            self.assertIsNone(gf.fleet_manager({"nobody"}, directory))
            self.assertIsNone(gf.fleet_manager(None, directory))

    def test_fleet_resolution_prefers_the_newest_registration(self):
        with tempfile.TemporaryDirectory() as directory:
            for sid, at in (("fleet-old", "2026-11-10T10:00:00+00:00"),
                            ("fleet-live", "2026-11-11T09:00:00+00:00")):
                with open(os.path.join(directory, f"{sid}.json"), "w", encoding="utf-8") as h:
                    json.dump({"branch": "fleet", "resolved_at": at}, h)
            self.assertEqual(
                gf.fleet_manager({"fleet-old", "fleet-live"}, directory), "fleet-live"
            )


class EmissionsLedger(unittest.TestCase):
    """One record per decision, with a `ts` so a window can be cut out of the file.

    ⚠️ The `ts` is the whole point: the ledger lives as long as the manager session, so
    without a timestamp every count is a whole-file count and a `0` could come from before
    the fix it is meant to certify.
    """

    def rows(self):
        return [
            {"pane": "1", "verdict": gf.EMIT, "reason": "own-worker",
             "session_id": "w-own", "spawner": "me"},
            {"pane": "2", "verdict": gf.DROP, "reason": gf.UNOWNED_FLEET,
             "session_id": "w-foreign", "spawner": None},
        ]

    def test_one_line_per_decision_carrying_ts_and_the_resolved_owner(self):
        with tempfile.TemporaryDirectory() as directory:
            path = gf.emissions_path("me", directory)
            self.assertIsNone(gf.log_emissions(self.rows(), [], _Scope(), "me", root=directory))
            with open(path, encoding="utf-8") as handle:
                records = [json.loads(line) for line in handle if line.strip()]
            self.assertEqual(len(records), 2)
            for record in records:
                self.assertTrue(record["ts"])
            emitted = [r for r in records if r["decision"] == gf.EMIT]
            self.assertEqual(len(emitted), 1)
            self.assertEqual(emitted[0]["task"], "A Task")
            self.assertEqual(emitted[0]["goal"], "A Goal")
            dropped = [r for r in records if r["decision"] == gf.DROP]
            self.assertEqual(dropped[0]["reason"], gf.UNOWNED_FLEET)

    def test_an_unattributed_session_records_no_task_and_no_goal(self):
        """A gate whose session resolves to no page is recorded as unattributed, never
        silently attributed to whatever page happened to be first."""
        with tempfile.TemporaryDirectory() as directory:
            rows = [{"pane": "9", "verdict": gf.EMIT, "reason": gf.UNOWNED,
                     "session_id": "nobody", "spawner": None}]
            gf.log_emissions(rows, [], _Scope(), "me", root=directory)
            with open(gf.emissions_path("me", directory), encoding="utf-8") as handle:
                record = json.loads(handle.readline())
            self.assertIsNone(record["task"])
            self.assertIsNone(record["goal"])

    def test_no_self_id_writes_nothing(self):
        """No manager, no ledger — a diagnostic run must not invent one."""
        with tempfile.TemporaryDirectory() as directory:
            self.assertIsNone(gf.emissions_path(None, directory))
            self.assertIsNone(gf.log_emissions(self.rows(), [], _Scope(), None, root=directory))
            self.assertEqual(os.listdir(directory), [])

    def test_the_ledger_is_bounded(self):
        """Append-only for the life of a session, so it needs a bound.

        Measured 2026-11-11 on a live manager: ~25 k records/day with no rotation at all.
        The trim keeps the NEWEST lines, so a window cut after a rotation still reads the
        recent past and loses only history older than the bound.
        """
        with tempfile.TemporaryDirectory() as directory:
            path = gf.emissions_path("me", directory)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("".join('{"n": %d}\n' % i for i in range(50)))
            self.assertTrue(gf.trim_emissions(path, keep=10))
            with open(path, encoding="utf-8") as handle:
                lines = handle.readlines()
            self.assertEqual(len(lines), 10)
            self.assertIn('"n": 49', lines[-1])
            # A file already inside the bound is left untouched rather than rewritten.
            self.assertFalse(gf.trim_emissions(path, keep=10))


class _Entry:
    def __init__(self, title, goals):
        self.title = title
        self.kind = "task"
        self.goals = goals


class _Scope:
    """The two sessions the ledger tests use: one attributed, one not."""

    def __init__(self):
        self.sessions = {
            "w-own": [_Entry("A Task", ["A Goal"])],
            "w-foreign": [_Entry("B Task", ["B Goal"])],
        }


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

    def test_unreadable_ledger_store_is_empty_dict(self):
        """⚠️ **The collapse is this module's contract, not an oversight.** The filter fails
        OPEN, so an unreadable store and an empty one both answer `{}` and every gate is kept
        — the safe direction, since an unreadable ledger resolves every spawner as a manager.
        `load_ledger_checked` is where the distinction survives, and `adopt-orphans.py` calls
        that one precisely because it must REFUSE rather than fail open.
        """
        self.assertEqual(gf.load_ledger(dead_url()), {})

    def test_missing_registry_dir_is_none_not_empty_set(self):
        self.assertIsNone(gf.live_ids("/nonexistent/registry"))

    def test_present_but_empty_registry_is_an_empty_set(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(gf.live_ids(d), set())


class LoadLedger(unittest.TestCase):
    def test_keys_by_session_id(self):
        """⚠️ Keyed by the row's own `session_id`, never `agent_id` — that field is a
        per-process counter that restarts at 0 on every reconnect, and 215 of the 1891 ledger
        rows claim `agent_1`, so a map keyed on it would keep one row and drop the rest."""
        with FixtureEndpoint(ledger_rows=[ledger_row("abc", spawner_session_id="mgr")]) as fx:
            self.assertEqual(gf.load_ledger(fx.url)["abc"]["spawner_session_id"], "mgr")

    def test_a_non_object_row_is_skipped(self):
        """`null`, a list and a scalar are all valid JSON, so the store can serve one and the
        reader must skip it rather than raise on the next `.get`. ⚠️ Served as ROWS, which is
        the only shape the endpoint has — the file-era version of this case wrote a malformed
        `.json` file, and that fixture no longer reaches the reader at all."""
        with FixtureEndpoint(ledger_rows=[None, "not-a-row", 7]) as fx:
            self.assertEqual(gf.load_ledger(fx.url), {})


class CorruptRecordsFailOpen(unittest.TestCase):
    """A record that parses but is not an object must be SKIPPED, never raise.

    `null`, a list and a scalar are all valid JSON, so `json.load` returns them
    happily and the next `.get` raises `AttributeError` out of `main()`. The
    module's central contract is fail-open -- an unresolvable input keeps the
    gate -- and a crash is the opposite of that: it takes the whole filter down
    for every pane, not just the one record. Three readers had the shape
    (`load_ledger`, `live_ids`, `log_items`), which is why this class tests the
    class rather than the single surface the report named.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = self._tmp.name
        self.state = os.path.join(self.dir, "state")
        self.registry = os.path.join(self.dir, "registry")
        # ⚠️ The ledger half is a fixture ENDPOINT; only the registry is still a directory.
        self.endpoint = FixtureEndpoint()
        self.addCleanup(self.endpoint.__exit__, None, None, None)
        for path in (self.state, self.registry):
            os.makedirs(path)
        with open(os.path.join(self.state, "a.events.jsonl"), "w", encoding="utf-8") as handle:
            handle.write(json.dumps({
                "type": "open", "item_id": "1", "pane": "372", "session_id": GATED,
            }) + "\n")

    def write(self, directory, name, text):
        with open(os.path.join(directory, name), "w", encoding="utf-8") as handle:
            handle.write(text)

    def run_filter(self):
        # `--pane` FILTERS the feed, so it needs one: a call naming a pane the feed does
        # not carry now yields no gate at all. Passing the feed explicitly also keeps the
        # suite off the live store -- `--pane` alone produces the feed itself.
        return subprocess.run(
            [sys.executable, _SCRIPT, "--feed", "--pane", "372", "--self", ME, "--explain",
             "--state-dir", self.state, "--registry-dir", self.registry,
             "--endpoint", self.endpoint.url],
            input=FEED_372, capture_output=True, text=True,
        )

    def test_a_null_ledger_row_exits_zero(self):
        """SC3's probe, end to end: pointed at a store serving `null` as a row, the filter
        exits 0 and omits that record rather than raising `AttributeError`."""
        self.endpoint.add_ledger_row(None)
        result = self.run_filter()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("AttributeError", result.stderr)

    def test_a_null_ledger_row_is_omitted(self):
        self.endpoint.add_ledger_row(None)
        self.assertEqual(gf.load_ledger(self.endpoint.url), {})

    def test_a_null_registry_entry_is_skipped(self):
        """The registry is Claude Code's own file, not ours, so a shape this reader did
        not anticipate is a live possibility rather than a hypothetical."""
        self.write(self.registry, "null.json", "null")
        self.assertEqual(gf.live_ids(self.registry), set())

    def test_a_null_event_line_is_skipped(self):
        """The event log is append-only and shared, so one such line would otherwise take
        the whole pane->session hop down and fail the filter open for every gate."""
        self.write(self.state, "b.events.jsonl", "null\n")
        self.assertEqual([i["item_id"] for i in gf.log_items(self.state)], ["1"])


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
        # ⚠️ An EMPTY fixture store, deliberately — not the real one. The file-era version
        # pointed `--ledger-dir` at a path that did not exist, which read as an empty ledger;
        # switching to the endpoint without this would make these cases read the MACHINE's
        # fleet, and the verdicts below would then depend on whoever happens to be running.
        self.endpoint = FixtureEndpoint()
        self.addCleanup(self.endpoint.__exit__, None, None, None)
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
        # See the sibling helper above: `--pane` filters a feed, so the feed is passed.
        return subprocess.run(
            [sys.executable, _SCRIPT, "--feed", "--pane", "372", "--self", ME, "--explain",
             "--state-dir", self.state, "--registry-dir", self.registry,
             "--endpoint", self.endpoint.url, *extra],
            input=FEED_372, capture_output=True, text=True, env=env,
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
        self.endpoint = FixtureEndpoint()
        self.addCleanup(self.endpoint.__exit__, None, None, None)
        for path in (self.state, self.registry):
            os.makedirs(path)

    def emit(self, feed):
        return subprocess.run(
            [sys.executable, _SCRIPT, "--feed", "--self", ME,
             "--state-dir", self.state, "--registry-dir", self.registry,
             "--endpoint", self.endpoint.url,
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
        # ⚠️ **The ledger is a fixture ENDPOINT now, not a directory.** `spawn-ledger.py`
        # reads `GET /api/1.0/session-ledger`, so a directory-shaped fixture would leave this
        # run with an EMPTY ledger — and hop 3b's branch would then cover both panes, which
        # reads as `dropped(peer-manager-own): 2` and still looks like a working filter.
        self.endpoint = FixtureEndpoint()
        self.addCleanup(self.endpoint.__exit__, None, None, None)
        for path in (self.state, self.registry):
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
        self.endpoint.add_ledger_row(ledger_row("w1", spawner_session_id="own-mgr"))

    def test_each_drop_bucket_is_counted_under_its_own_reason(self):
        result = subprocess.run(
            [sys.executable, _SCRIPT, "--feed", "--pane", "10", "--pane", "11",
             "--self", ME,
             "--state-dir", self.state, "--registry-dir", self.registry,
             "--endpoint", self.endpoint.url,
             "--claims-file", os.path.join(self.dir, "no-claims.json")],
            input="[ 10]  3m  ⚙ task a — awaiting-operator\n"
                  "[ 11]  4m  ⚙ task b — awaiting-operator\n",
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
        self.endpoint = FixtureEndpoint()
        self.addCleanup(self.endpoint.__exit__, None, None, None)
        for path in (self.state, self.registry):
            os.makedirs(path)
        # A stand-in for `who-needs-me.py`, so a bare `--pane` (which now produces the
        # feed itself) is exercised without reaching the operator's live store.
        self.producer_script = os.path.join(self.dir, "feed.py")
        with open(self.producer_script, "w", encoding="utf-8") as handle:
            handle.write("import sys\nsys.stdout.write(%r)\n" % FEED_372)
        self.producer = f"{sys.executable} {self.producer_script}"

    def invoke(self, *argv, feed=None):
        return subprocess.run(
            [sys.executable, _SCRIPT,
             "--state-dir", self.state, "--registry-dir", self.registry,
             "--endpoint", self.endpoint.url,
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

    def test_pane_alone_is_still_a_source_and_now_reads_the_feed(self):
        """`--pane` is the guard's other half, and it now FILTERS rather than invents.

        Measured 2026-10-16: `--self beac5fd0 --pane 1832 --explain` read `gates: 1
        emit: 1  reason: own-worker` while the authoritative path over the same pane
        read `gates: 0  emit: 0`. The flag still stands alone as a source -- the guard
        is unchanged -- but the panes now come from the feed it consults.
        """
        result = self.invoke(
            "--pane", "372", "--self", ME, "--explain", "--producer", self.producer
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("gates: 1", result.stderr, result.stderr)
        self.assertNotIn("no feed gate", result.stdout)

    def test_a_named_pane_absent_from_the_feed_yields_no_gate(self):
        """The defect this flag carried: `--explain` answered "yes" for ANY pane.

        A named pane the feed does not carry now yields nothing and says so, which is
        what makes "the feed does not carry it" distinguishable from "the feed was
        never consulted" -- a distinction the manufacturing build could not express.
        """
        result = self.invoke(
            "--pane", "999", "--self", ME, "--explain", "--producer", self.producer
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("gates: 0", result.stderr, result.stderr)
        self.assertIn("emit: 0", result.stderr, result.stderr)
        self.assertIn("no feed gate for pane 999", result.stdout)


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
        self.endpoint = FixtureEndpoint()
        self.addCleanup(self.endpoint.__exit__, None, None, None)
        for path in (self.state, self.registry):
            os.makedirs(path)

    def invoke(self, *argv, feed=None):
        return subprocess.run(
            [sys.executable, _SCRIPT,
             "--state-dir", self.state, "--registry-dir", self.registry,
             "--endpoint", self.endpoint.url,
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


class PollModeEmitsOnlyNewKeptPanes(unittest.TestCase):
    """`--interval` makes the one-shot pipe an armable, change-detecting watcher.

    The rule's arm is a `Monitor` over
    `who-needs-me.py --section needs-you | gate-owner-filter.py --feed --self <sid>`.
    `--feed` reads stdin ONCE, so that Monitor fires on the first read and the
    process exits — the manager's push channel dies silently, with no error and no
    marker. Measured 2026-10-07 against v0.115.5: `--help` exposed no watch flag
    and the source had zero hits for `while True|time.sleep`.

    ⚠️ **The loop must be a CHANGE-DETECTOR, not a re-emitter.** A shell
    `while true; do … | gate-owner-filter.py --feed …; sleep N; done` around the
    one-shot command re-emits every kept pane on every poll, turning a push
    channel into a 1/NHz poll of the manager's entire turn budget — the cost this
    filter exists to remove. So a pane is emitted when it ENTERS the kept set and
    never on a re-poll, and the first two cases below pin exactly that pair.

    ⚠️ **A `LIVENESS` line per poll is load-bearing, not decoration.** A
    change-detector is silent when nothing changes, and silence is byte-identical
    to a dead arm — the defect `manager-attention-watch.py` closed for its own arm
    with the same record. Its presence answers *alive or stuck?* and its age is
    the arm's last heartbeat.

    ⚠️ **A refusal ends the loop rather than being retried.** The one-shot
    contract is "do not arm a watcher on this pipeline's output"; swallowing a
    refused read would keep a dead push channel looking alive for the `Monitor`'s
    whole 30-minute bound.
    """

    ONE = "  [ 98765] 0m  first gate\n"
    TWO = "  [ 98765] 0m  first gate\n  [ 98766] 0m  second gate\n"
    EMPTY = "Needs you (0)\nNothing needs you.\n"

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = self._tmp.name
        self.state = os.path.join(self.dir, "state")
        self.registry = os.path.join(self.dir, "registry")
        self.endpoint = FixtureEndpoint()
        self.addCleanup(self.endpoint.__exit__, None, None, None)
        for path in (self.state, self.registry):
            os.makedirs(path)
        self.counter = os.path.join(self.dir, "count")

    def producer(self, *frames, prelude="", exit_code=0):
        """A `--producer` command printing `frames[i]` on poll i, the last repeating.

        A real command string, not a fixture, because `--producer` is invoked
        exactly as the rule's arm invokes the pipeline.

        `prelude` is Python source spliced in AFTER the poll counter is advanced
        and before the frame is written, with `n` bound to the current poll. It is
        what lets a test change the WORLD between polls — the registry, the ledger,
        the claims file — rather than only the feed, which is the axis the
        per-poll re-read has to be proven on. `exit_code` makes the producer fail,
        which the loop must not read as a quiet fleet.
        """
        frames_path = os.path.join(self.dir, "frames.json")
        with open(frames_path, "w", encoding="utf-8") as handle:
            json.dump(list(frames), handle)
        script = os.path.join(self.dir, "producer.py")
        with open(script, "w", encoding="utf-8") as handle:
            handle.write(
                "import json, sys\n"
                f"COUNTER = {self.counter!r}\n"
                f"FRAMES = json.load(open({frames_path!r}))\n"
                "try:\n"
                "    n = int(open(COUNTER).read())\n"
                "except Exception:\n"
                "    n = 0\n"
                "n += 1\n"
                "open(COUNTER, 'w').write(str(n))\n"
                + (prelude if prelude.endswith("\n") or not prelude else prelude + "\n")
                + "sys.stdout.write(FRAMES[min(n - 1, len(FRAMES) - 1)])\n"
                f"sys.exit({exit_code})\n"
            )
        return f"{sys.executable} {script}"

    def invoke(self, *argv):
        # ⚠️ `--feed-state-dir` is REQUIRED here, not optional hygiene. The arm's
        # memory defaults to a real, shared `~/.claude/state/gate-feed/<self>.json`,
        # and every test in this class passes the same `ME` — so without this the
        # suite reads and writes ONE file across all of them, in the developer's
        # home, and the second test to run sees the first one's panes as already
        # emitted. Measured 2026-10-12: 4 failures in this class, all of them a
        # pane that should have been news being suppressed by a sibling test's
        # leftover entry.
        return subprocess.run(
            [sys.executable, _SCRIPT,
             "--state-dir", self.state, "--registry-dir", self.registry,
             "--endpoint", self.endpoint.url,
             "--feed-state-dir", os.path.join(self.dir, "feed-state"),
             "--claims-file", os.path.join(self.dir, "no-claims.json"), *argv],
            capture_output=True, text=True,
        )

    def poll(self, frames, polls=None, **producer_kwargs):
        """A bounded poll loop over `frames` — `--interval 0` so the test never sleeps."""
        return self.invoke(
            "--feed", "--self", ME, "--interval", "0",
            "--max-polls", str(polls if polls is not None else len(frames)),
            "--producer", self.producer(*frames, **producer_kwargs),
        )

    # --- the change-detector: emit on ENTRY, never on re-poll ----------------

    def test_a_stable_kept_pane_is_emitted_once(self):
        """The half a shell `while true; … | filter …; sleep N; done` gets wrong."""
        result = self.poll([self.ONE, self.ONE, self.ONE])
        self.assertEqual(result.stdout.split(), ["98765"], result.stdout)

    def test_a_pane_entering_the_kept_set_is_emitted(self):
        result = self.poll([self.ONE, self.TWO, self.TWO])
        self.assertEqual(result.stdout.split(), ["98765", "98766"], result.stdout)

    def test_a_pane_that_leaves_and_returns_is_news_again(self):
        """A gate that cleared and re-opened is a new gate, not a remembered one."""
        result = self.poll([self.ONE, self.EMPTY, self.ONE])
        self.assertEqual(result.stdout.split(), ["98765", "98765"], result.stdout)

    def test_an_empty_first_poll_emits_nothing_and_keeps_running(self):
        result = self.poll([self.EMPTY, self.EMPTY, self.ONE])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.split(), ["98765"], result.stdout)

    # --- the LIVENESS record: a quiet arm must not read as a dead one --------

    def test_every_poll_writes_a_liveness_line(self):
        result = self.poll([self.ONE, self.ONE, self.ONE])
        self.assertEqual(result.stderr.count("LIVENESS"), 3, result.stderr)

    def test_the_liveness_line_carries_the_counts_and_the_change_state(self):
        result = self.poll([self.ONE, self.ONE])
        self.assertIn("gates: 1", result.stderr)
        self.assertIn("new: 1  change", result.stderr)
        self.assertIn("new: 0  no change", result.stderr)

    def test_the_bound_stops_the_loop(self):
        """`--max-polls` is what makes the loop testable at all."""
        result = self.poll([self.ONE, self.ONE, self.ONE, self.ONE], polls=2)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr.count("LIVENESS"), 2, result.stderr)

    # --- a refusal ends the loop, loudly -------------------------------------

    def test_a_refusal_ends_the_loop_non_zero(self):
        refusal = f"{gf.REFUSAL_MARKER}: WezTerm pane list unreadable\n"
        result = self.poll([refusal, self.ONE])
        self.assertNotEqual(0, result.returncode, result.stdout)
        self.assertEqual(result.stdout.split(), [], result.stdout)

    def test_a_refusal_prints_no_gate_count(self):
        """The line that lies is the line that must not be printed."""
        refusal = f"{gf.REFUSAL_MARKER}: WezTerm pane list unreadable\n"
        result = self.poll([refusal, self.ONE])
        self.assertNotIn("gates:", result.stderr, result.stderr)
        self.assertIn("upstream refused", result.stderr)

    # --- the guards: poll mode is not combinable with the other shapes -------

    def test_interval_without_feed_is_a_usage_error(self):
        result = self.invoke("--interval", "5", "--self", ME)
        self.assertNotEqual(0, result.returncode)
        self.assertIn("--feed", result.stderr)

    def test_interval_with_pane_is_a_usage_error(self):
        result = self.invoke("--interval", "5", "--feed", "--pane", "9", "--self", ME)
        self.assertNotEqual(0, result.returncode)
        self.assertIn("--pane", result.stderr)

    def test_max_polls_without_interval_is_a_usage_error(self):
        result = self.invoke("--feed", "--max-polls", "3", "--self", ME)
        self.assertNotEqual(0, result.returncode)
        self.assertIn("--interval", result.stderr)

    # --- ownership inputs are re-read EVERY poll -----------------------------

    def test_a_session_that_becomes_live_mid_arm_stops_being_emitted(self):
        """The per-poll re-read, proven on the axis the feed cannot show.

        Local review round 2026-10-07 found this: `poll()` re-read the FEED every
        iteration but judged it against ownership state loaded once at arm time, so
        a peer manager appearing — or dying — inside a 30-minute arm was invisible,
        and both failure modes are the ones this filter exists to remove. The feed
        here is byte-identical on both polls; only the registry moves.
        """
        sid = "44444444-4444-4444-8444-444444444444"
        # `item_id` and `type: "open"` are both required by `log_items` — it keys
        # the fold on the first and only records the second as an opening line, so
        # a fixture carrying just pane and session resolves no session at all and
        # the test would pass for the wrong reason.
        with open(
            os.path.join(self.state, f"{sid}.events.jsonl"), "w", encoding="utf-8"
        ) as handle:
            handle.write(
                json.dumps(
                    {
                        "item_id": "test-item-1",
                        "session_id": sid,
                        "pane": "98765",
                        "type": "open",
                    }
                )
                + "\n"
            )
        record = os.path.join(self.registry, "4242.json")

        result = self.poll(
            [self.ONE, self.ONE],
            prelude=(
                "if n >= 2:\n"
                f"    open({record!r}, 'w').write("
                f"json.dumps({{'sessionId': {sid!r}, 'status': 'idle'}}))\n"
            ),
        )

        # Poll 1 — not live and no ledger record of its own, so it is nobody's and
        # emits. Poll 2 — live, still record-less, so hop 3b reads it as a manager
        # and drops it. Same feed, opposite verdict.
        self.assertEqual(result.stdout.split(), ["98765"], result.stdout)
        self.assertIn("dropped(peer-manager-own): 1", result.stderr)

    # --- a failed producer is not a quiet fleet ------------------------------

    def test_a_producer_that_fails_ends_the_loop_non_zero(self):
        """Empty stdout from a CRASHED producer read as a healthy empty queue.

        `feed_refusal` separates "read, and empty" from "never read" by a marker on
        stdout, but a producer that dies printing nothing writes no marker — so it
        landed in the healthy-empty case and was certified by `gates: 0` on every
        poll forever. Only the exit status distinguishes them.
        """
        result = self.poll([self.ONE, self.ONE], exit_code=1)
        self.assertNotEqual(0, result.returncode, result.stdout)
        self.assertEqual(result.stdout.split(), [], result.stdout)

    def test_a_failed_producer_prints_no_gate_count(self):
        result = self.poll([self.ONE, self.ONE], exit_code=1)
        self.assertNotIn("gates:", result.stderr, result.stderr)
        self.assertIn("producer exited 1", result.stderr)

    # --- --interval bounds and combinations ----------------------------------

    def test_a_negative_interval_is_a_usage_error(self):
        """`time.sleep` rejects it outright, so it used to reach the loop and crash."""
        result = self.invoke("--feed", "--interval", "-1", "--self", ME)
        self.assertEqual(2, result.returncode, result.stdout)
        self.assertIn("negative", result.stderr)

    def test_interval_zero_without_a_bound_is_a_usage_error(self):
        """`--interval 0` is legal only bounded — unbounded it is a busy-loop."""
        result = self.invoke("--feed", "--interval", "0", "--self", ME)
        self.assertEqual(2, result.returncode, result.stdout)
        self.assertIn("--max-polls", result.stderr)

    def test_interval_with_explain_is_a_usage_error(self):
        """The diagnostic shapes print every row per poll and would wake a Monitor."""
        result = self.invoke("--feed", "--interval", "60", "--explain", "--self", ME)
        self.assertEqual(2, result.returncode, result.stdout)
        self.assertIn("--explain", result.stderr)

    def test_interval_with_json_is_a_usage_error(self):
        result = self.invoke("--feed", "--interval", "60", "--json", "--self", ME)
        self.assertEqual(2, result.returncode, result.stdout)
        self.assertIn("--json", result.stderr)

    # --- the one-shot contract is untouched by the new flags -----------------

    def test_omitting_interval_still_reads_stdin_once(self):
        result = subprocess.run(
            [sys.executable, _SCRIPT,
             "--state-dir", self.state, "--registry-dir", self.registry,
             "--endpoint", self.endpoint.url,
             "--claims-file", os.path.join(self.dir, "no-claims.json"),
             "--feed", "--self", ME],
            input=self.ONE, capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.split(), ["98765"], result.stdout)
        self.assertNotIn("LIVENESS", result.stderr)


class FeedStateSurvivesTheReArm(unittest.TestCase):
    """The arm's memory — what it has already surfaced crosses the re-arm.

    `poll()` was a change-detector WITHIN an arm and had no memory ACROSS one:
    `previous = None` opened every arm, so the first poll of each treated every
    held gate as newly entered. A `Monitor` is bounded at 30 min and re-armed, so
    a held gate cost a full manager turn every arm for as long as it stayed open.
    Measured 2026-10-12 on the Fleet Manager: panes 1065, 1151, 1175, 1094, 1193,
    1191, 1196 re-reported ~8 times over a ~600k-token context each, for no state
    change.

    ⚠️ Every test here drives a SEPARATE arm, and that is the whole point: each
    `poll()` is a new process with its own producer counter, so the state file is
    the ONLY thing that crosses between them. A test that reused one process would
    re-prove the in-process detector that already worked.

    ⚠️ The backstop tests exist because the fix removes one. The re-arm used to be
    what retried a wake the manager never saw; once the emit is persisted, nothing
    does — dedup plus event-only delivery, a permanent silent stall. So a held gate
    is re-emitted once its entry expires, and both directions are pinned below.
    """

    ONE = "  [ 98765] 0m  first gate\n"
    TWO = "  [ 98765] 0m  first gate\n  [ 98766] 0m  second gate\n"
    EMPTY = "Needs you (0)\nNothing needs you.\n"
    SID = "55555555-5555-4555-8555-555555555555"

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = self._tmp.name
        self.state = os.path.join(self.dir, "state")
        self.registry = os.path.join(self.dir, "registry")
        # ⚠️ The ledger half is a fixture ENDPOINT now; only the registry is still a
        # directory. An empty store is the right default here — the gates in these cases are
        # decided by the feed state and the registry, and reading the MACHINE's fleet instead
        # would make every verdict depend on whoever happens to be running.
        self.endpoint = FixtureEndpoint()
        self.addCleanup(self.endpoint.__exit__, None, None, None)
        self.feed_state = os.path.join(self.dir, "feed-state")
        for path in (self.state, self.registry):
            os.makedirs(path)
        self.arm = 0
        self.ts = 0

    # --- fixtures ------------------------------------------------------------

    def gate(self, pane, detail):
        """APPEND an event-log item for `pane` — the record the fingerprint reads.

        The gate's own text lives here, not on the feed line: the feed renders an
        AGE (`0m`, `5h49m`) beside every row, so a fingerprint taken off the line
        would differ on every poll and re-emit everything. `detail` is the stable
        identity, and `notify-gate.py` keys its own dedup on the same field.

        Appends rather than rewrites: `log_items` folds the log by `item_id` and
        `newest_item_for_pane` picks the newest by `ts`, so a second call for one
        pane must be a NEWER record and a call for another pane must not erase it.
        """
        self.ts += 1
        with open(
            os.path.join(self.state, f"{self.SID}.events.jsonl"), "a", encoding="utf-8"
        ) as handle:
            handle.write(
                json.dumps({
                    "item_id": f"item-{pane}-{self.ts}", "session_id": self.SID,
                    "pane": pane, "type": "open", "detail": detail, "ts": self.ts,
                }) + "\n"
            )

    def producer(self, frames, counter, tag):
        frames_path = os.path.join(self.dir, f"frames-{tag}.json")
        with open(frames_path, "w", encoding="utf-8") as handle:
            json.dump(list(frames), handle)
        script = os.path.join(self.dir, f"producer-{tag}.py")
        with open(script, "w", encoding="utf-8") as handle:
            handle.write(
                "import json, sys\n"
                f"FRAMES = json.load(open({frames_path!r}))\n"
                "try:\n"
                f"    n = int(open({counter!r}).read())\n"
                "except Exception:\n"
                "    n = 0\n"
                "n += 1\n"
                f"open({counter!r}, 'w').write(str(n))\n"
                "sys.stdout.write(FRAMES[min(n - 1, len(FRAMES) - 1)])\n"
            )
        return f"{sys.executable} {script}"

    def invoke(self, *argv, feed_state_dir=None, stdin_text=""):
        return subprocess.run(
            [sys.executable, _SCRIPT,
             "--state-dir", self.state, "--registry-dir", self.registry,
             "--endpoint", self.endpoint.url,
             "--feed-state-dir", feed_state_dir or self.feed_state,
             "--claims-file", os.path.join(self.dir, "no-claims.json"), *argv],
            input=stdin_text, capture_output=True, text=True,
        )

    def poll(self, frames, polls=None, feed_state_dir=None, reemit_after=None):
        """ONE arm of the loop — a fresh process and a fresh producer counter."""
        self.arm += 1
        counter = os.path.join(self.dir, f"count-{self.arm}")
        argv = ["--feed", "--self", ME, "--interval", "0",
                "--max-polls", str(polls if polls is not None else len(frames)),
                "--producer", self.producer(frames, counter, f"arm{self.arm}")]
        if reemit_after is not None:
            argv += ["--reemit-after", str(reemit_after)]
        return self.invoke(*argv, feed_state_dir=feed_state_dir)

    def stored(self):
        with open(
            os.path.join(self.feed_state, f"{ME}.json"), encoding="utf-8"
        ) as handle:
            return json.load(handle)

    def age_the_store(self, seconds):
        """Push every stored entry's clock back, as a lost wake's arm would."""
        path = os.path.join(self.feed_state, f"{ME}.json")
        data = self.stored()
        for entry in data["gates"].values():
            entry["emitted_at"] -= seconds
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(data, handle)

    # --- SC1: one entry per open gate ----------------------------------------

    def test_the_store_holds_one_entry_per_open_gate(self):
        self.gate("98765", "first gate")
        self.gate("98766", "second gate")
        result = self.poll([self.TWO], polls=1)
        self.assertEqual(result.returncode, 0, result.stderr)
        gates = self.stored()["gates"]
        self.assertEqual(sorted(gates), ["98765", "98766"], gates)
        for pane, entry in gates.items():
            self.assertTrue(entry["fingerprint"], (pane, entry))
            self.assertGreater(entry["emitted_at"], 0, (pane, entry))

    def test_the_store_is_keyed_on_the_self_session(self):
        self.gate("98765", "first gate")
        self.poll([self.ONE], polls=1)
        self.assertTrue(os.path.exists(os.path.join(self.feed_state, f"{ME}.json")))

    # --- SC2: a re-arm is silent; a changed question is news -----------------

    def test_a_re_arm_with_unchanged_gates_emits_nothing(self):
        """The defect itself: the first poll of EVERY arm re-emitted the kept set."""
        self.gate("98765", "first gate")
        self.gate("98766", "second gate")
        first = self.poll([self.TWO], polls=2)
        self.assertEqual(first.stdout.split(), ["98765", "98766"], first.stdout)
        second = self.poll([self.TWO], polls=2)
        self.assertEqual(second.stdout.split(), [], second.stdout)
        self.assertIn("new: 0  no change", second.stderr)

    def test_a_changed_question_on_one_pane_emits_only_that_pane(self):
        self.gate("98765", "first gate")
        self.gate("98766", "second gate")
        self.poll([self.TWO], polls=1)
        self.gate("98766", "second gate, re-worded")
        result = self.poll([self.TWO], polls=1)
        self.assertEqual(result.stdout.split(), ["98766"], result.stdout)

    def test_a_re_render_of_the_same_question_is_not_news(self):
        """`normalise()` collapses whitespace, so re-wrapping keeps one identity."""
        self.gate("98765", "first gate")
        self.poll([self.ONE], polls=1)
        self.gate("98765", "first   gate")
        result = self.poll([self.ONE], polls=1)
        self.assertEqual(result.stdout.split(), [], result.stdout)

    # --- SC3: a closed gate is pruned, a reopened one is news again ----------

    def test_a_closed_gate_is_pruned_from_the_store(self):
        self.gate("98765", "first gate")
        self.poll([self.ONE], polls=1)
        self.assertIn("98765", self.stored()["gates"])
        self.poll([self.EMPTY], polls=1)
        self.assertNotIn("98765", self.stored()["gates"])

    def test_a_reopened_gate_is_emitted_again(self):
        self.gate("98765", "first gate")
        self.poll([self.ONE], polls=1)
        self.poll([self.EMPTY], polls=1)
        result = self.poll([self.ONE], polls=1)
        self.assertEqual(result.stdout.split(), ["98765"], result.stdout)

    # --- the backstop: a lost wake must not become a permanent silence -------

    def test_a_held_gate_is_re_emitted_once_its_entry_expires(self):
        """Without this, dedup plus event-only delivery is a permanent silent stall."""
        self.gate("98765", "first gate")
        self.poll([self.ONE], polls=1)
        self.assertEqual(self.poll([self.ONE], polls=1).stdout.split(), [])
        self.age_the_store(3600)
        result = self.poll([self.ONE], polls=1)
        self.assertEqual(result.stdout.split(), ["98765"], result.stdout)

    def test_reemit_after_zero_disables_the_backstop(self):
        self.gate("98765", "first gate")
        self.poll([self.ONE], polls=1)
        self.age_the_store(10 ** 7)
        result = self.poll([self.ONE], polls=1, reemit_after=0)
        self.assertEqual(result.stdout.split(), [], result.stdout)

    # --- an absent or corrupt store is FIRST RUN, never "unchanged" ----------

    def test_a_missing_store_emits_every_kept_gate(self):
        self.gate("98765", "first gate")
        result = self.poll([self.ONE], polls=1)
        self.assertEqual(result.stdout.split(), ["98765"], result.stdout)

    def test_a_corrupt_store_reads_as_first_run_not_as_no_change(self):
        """Reading it as 'nothing changed' would silently suppress every gate."""
        self.gate("98765", "first gate")
        os.makedirs(self.feed_state, exist_ok=True)
        with open(
            os.path.join(self.feed_state, f"{ME}.json"), "w", encoding="utf-8"
        ) as handle:
            handle.write("{ not json at all")
        result = self.poll([self.ONE], polls=1)
        self.assertEqual(result.stdout.split(), ["98765"], result.stdout)

    # --- the write itself ----------------------------------------------------

    def test_the_store_is_written_atomically_leaving_no_tmp(self):
        self.gate("98765", "first gate")
        self.poll([self.ONE], polls=2)
        leftovers = [f for f in os.listdir(self.feed_state) if f.endswith(".tmp")]
        self.assertEqual(leftovers, [], leftovers)

    def test_an_unwritable_store_is_reported_once_and_fails_open(self):
        """A store that cannot be written must not leave the arm looking fixed.

        Fail-open means the PRE-fix behaviour — every kept pane re-emitted on every
        poll — which is a wake rather than a silence. The warning is what keeps that
        degradation visible instead of reading as a fixed arm.
        """
        blocker = os.path.join(self.dir, "blocker")
        with open(blocker, "w", encoding="utf-8") as handle:
            handle.write("not a directory")
        result = self.poll(
            [self.ONE, self.ONE, self.ONE],
            feed_state_dir=os.path.join(blocker, "feed-state"),
        )
        self.assertEqual(result.stderr.count("STATE  unwritable"), 1, result.stderr)
        self.assertEqual(result.stdout.split(), ["98765"] * 3, result.stdout)

    # --- the flag is required, because without it the fix is a no-op ---------

    def test_interval_without_self_is_a_usage_error(self):
        """Without a key there is no store, so the arm would silently stay unfixed."""
        result = self.invoke("--feed", "--interval", "5", "--max-polls", "1")
        self.assertEqual(2, result.returncode, result.stdout)
        self.assertIn("--self", result.stderr)

    def test_the_one_shot_shape_writes_no_store(self):
        self.gate("98765", "first gate")
        result = self.invoke("--feed", "--self", ME, stdin_text=self.ONE)
        self.assertEqual(result.stdout.split(), ["98765"], result.stdout)
        self.assertFalse(os.path.exists(self.feed_state), os.listdir(self.dir))


class _FakeScope:
    """The narrowest stand-in for `session-scope.Scope`: one answer per session id."""

    def __init__(self, answers):
        self.answers = answers

    def exclusion(self, session_id, self_id):
        return self.answers.get(session_id, (False, ""))


class ScopeExclusion(unittest.TestCase):
    """The third verdict, branch by branch.

    It may only turn a NON-EXCLUDE verdict into an EXCLUDE -- never a pane this watcher
    spawned (`own-worker`), never its own session (`self`), and never one it adopted
    (`own-claim`). The `DROP` case is why it is "non-exclude" rather than "EMIT": the
    scope SUPERSEDES a spawn-edge drop too.
    """

    LEDGER = {"worker-1": {"session_id": "worker-1", "parent_session": "manager-x"}}
    LIVE = {"worker-1", "manager-x", "manager-y"}

    def call(self, session_id, spawner, scope, ledger=_UNSET, live=_UNSET, claims=_UNSET):
        return gf.verdict(
            session_id,
            spawner,
            self.LEDGER if ledger is _UNSET else ledger,
            self.LIVE if live is _UNSET else live,
            ME,
            None if claims is _UNSET else claims,
            scope,
        )

    def test_a_foreign_session_is_excluded_and_names_its_goal(self):
        scope = _FakeScope({"w": (True, "foreign-goal:Some Other Goal")})
        self.assertEqual(self.call("w", "nobody", scope),
                         (gf.EXCLUDE, "foreign-goal:Some Other Goal"))

    def test_an_owner_unknown_session_is_excluded_too(self):
        scope = _FakeScope({"w": (True, "owner-unknown")})
        self.assertEqual(self.call("w", "nobody", scope), (gf.EXCLUDE, "owner-unknown"))

    def test_a_scoped_own_session_falls_through_to_the_spawner_path(self):
        """Own is not a verdict — it leaves every existing rule exactly as it was.

        Asserted against the UNSCOPED answer for the same inputs, so the pin is the property
        rather than a ledger row this test happens to carry.
        """
        scope = _FakeScope({})
        for session_id, spawner in (("w", "manager-y"), ("worker-1", "manager-x"), ("w", None)):
            self.assertEqual(
                self.call(session_id, spawner, scope),
                self.call(session_id, spawner, None),
                (session_id, spawner),
            )

    def test_no_scope_changes_nothing(self):
        self.assertEqual(self.call("w", "manager-y", None), (gf.DROP, "peer-manager"))

    def test_a_pane_this_watcher_spawned_is_never_excluded(self):
        """`own-worker` returns above the scope check: these are the panes it EXISTS to see."""
        scope = _FakeScope({"w": (True, "foreign-goal:Some Other Goal")})
        self.assertEqual(self.call("w", ME, scope), (gf.EMIT, "own-worker"))

    def test_the_asking_session_is_never_excluded(self):
        scope = _FakeScope({ME: (True, "foreign-goal:Some Other Goal")})
        self.assertEqual(self.call(ME, "manager-y", scope), (gf.EMIT, "self"))

    def test_the_scope_outranks_a_spawn_edge_drop_for_a_foreign_pane(self):
        """The durable answer wins, and it is the only one that names the real owner."""
        scope = _FakeScope({"w": (True, "foreign-goal:Some Other Goal")})
        self.assertEqual(self.call("w", "manager-y", scope),
                         (gf.EXCLUDE, "foreign-goal:Some Other Goal"))

    def test_an_adopted_pane_is_never_excluded_by_the_scope(self):
        """A gate this manager undertook to route is one it EXISTS to see.

        The scope block sits ABOVE the claims path, so without the `adopted_by_self`
        guard the durable answer silenced exactly the pane the claim was recorded for --
        the fleet-silence direction `session-scope.py` names as the dangerous one. Hop 3b
        already carries this guard; the scope block above it did not.
        """
        scope = _FakeScope({"w": (True, "foreign-goal:Some Other Goal")})
        self.assertEqual(self.call("w", None, scope, claims={"w": ME}),
                         (gf.EMIT, "own-claim"))

    def test_the_claim_outranks_the_scope_and_only_the_claim_does(self):
        """The precedence pinned in both directions, so neither half can drift alone.

        An own claim wins over the scope; the SAME foreign scope with no claim still
        excludes. Asserting only the first half would pass on a build that dropped the
        scope check entirely, and only the second would pass on the bug.
        """
        scope = _FakeScope({"w": (True, "foreign-goal:Some Other Goal")})
        self.assertEqual(self.call("w", None, scope, claims={"w": ME}),
                         (gf.EMIT, "own-claim"))
        self.assertEqual(self.call("w", None, scope, claims={}),
                         (gf.EXCLUDE, "foreign-goal:Some Other Goal"))


class RoleHumanExclusion(unittest.TestCase):
    """A `role: human` row is the operator's own workspace, so no manager routes it.

    The reported case (2026-10-27, Fleet Manager `ac24db61`, pane 2165) is a human row the
    MANAGER ITSELF spawned, so it travels the `own-worker` branch -- the one that exists for
    the panes a manager EXISTS to see, and which returned EMIT before any role was read. The
    first test pins that ordering; the controls pin that the exclusion does not widen into
    silence, which is the failure direction every other hop here fails open to avoid.
    """

    HUMAN = "human-1"
    AGENT = "agent-1"
    MGR = "mgr-1"
    LEDGER = {
        HUMAN: {"session_id": HUMAN, "spawner_session_id": ME, "role": "human"},
        AGENT: {"session_id": AGENT, "spawner_session_id": ME, "role": "agent"},
        MGR: {"session_id": MGR, "role": "manager"},
        "nobody": {"session_id": "nobody"},
    }
    # `other-manager` is LIVE so the peer-manager test exercises the `DROP` path rather
    # than the dead-manager one: a spawner absent from LIVE emits, which would make the
    # case pass for the wrong reason.
    LIVE = {HUMAN, AGENT, MGR, ME, "other-manager"}

    def call(self, session_id, spawner, ledger=None, live=None):
        return gf.verdict(
            session_id,
            spawner,
            self.LEDGER if ledger is None else ledger,
            self.LIVE if live is None else live,
            ME,
        )

    def test_the_reported_case_is_excluded_above_own_worker(self):
        """The manager spawned the human row, so `own-worker` would have emitted it."""
        self.assertEqual(self.call(self.HUMAN, ME), (gf.EXCLUDE, "role-human"))

    def test_a_human_row_with_no_spawner_is_excluded_too(self):
        """The operator-started half of the population: no spawner, still not routable."""
        self.assertEqual(self.call(self.HUMAN, None), (gf.EXCLUDE, "role-human"))

    def test_a_human_row_spawned_by_another_live_manager_is_excluded_too(self):
        """The match is deliberately NOT scoped to this watcher's own spawns.

        `role` is a global ledger fact, so a peer manager's human row is excluded as well —
        and that case is the one where the spawn edge would otherwise return a `DROP`
        (`peer-manager`), so it pins that the role rule outranks `spawner_verdict` and not
        only the `own-worker` branch above it.
        """
        # The premise, asserted so the case cannot pass for the wrong reason: without the
        # role rule this spawn edge resolves to a DROP, so the exclusion is what changes it.
        self.assertEqual(gf.spawner_verdict("other-manager", self.LEDGER, self.LIVE),
                         (gf.DROP, "peer-manager"))
        self.assertEqual(self.call(self.HUMAN, "other-manager"),
                         (gf.EXCLUDE, "role-human"))

    def test_the_exclusion_outranks_the_scope_block(self):
        """The role names the row better than whichever goal page happens to claim it."""
        scope = _FakeScope({self.HUMAN: (True, "foreign-goal:Some Other Goal")})
        self.assertEqual(
            gf.verdict(self.HUMAN, ME, self.LEDGER, self.LIVE, ME, None, scope),
            (gf.EXCLUDE, "role-human"),
        )

    def test_the_asking_session_is_still_kept(self):
        """`self` returns above the role check: a watcher always sees its own gate."""
        ledger = {ME: {"session_id": ME, "role": "human"}}
        self.assertEqual(self.call(ME, None, ledger=ledger), (gf.EMIT, "self"))

    def test_an_agent_row_the_watcher_spawned_still_emits(self):
        """The control that stops the exclusion widening into silence."""
        self.assertEqual(self.call(self.AGENT, ME), (gf.EMIT, "own-worker"))

    def test_a_manager_row_is_unaffected(self):
        self.assertEqual(self.call(self.MGR, None), (gf.DROP, "peer-manager-own"))

    def test_a_record_with_no_role_is_not_human(self):
        """A pre-field record must keep every existing verdict -- `role` is positive only."""
        self.assertEqual(self.call("nobody", ME), (gf.EMIT, "own-worker"))

    def test_a_null_role_is_not_human(self):
        ledger = {"x": {"session_id": "x", "spawner_session_id": ME, "role": None}}
        self.assertEqual(self.call("x", ME, ledger=ledger), (gf.EMIT, "own-worker"))

    def test_a_session_with_no_record_is_not_human(self):
        self.assertEqual(self.call("unrecorded", ME), (gf.EMIT, "own-worker"))

    def test_is_human_matches_only_an_explicit_human(self):
        self.assertTrue(gf.is_human(self.HUMAN, self.LEDGER))
        for session_id in (self.AGENT, self.MGR, "nobody", "unrecorded", None, ""):
            self.assertFalse(gf.is_human(session_id, self.LEDGER), session_id)

    def test_counts_line_reports_the_human_exclusion_in_its_own_bucket(self):
        """Never folded into `dropped(...)`, and never into the vault's exclusions."""
        rows = [
            {"pane": "1", "replayed": False, "verdict": gf.EXCLUDE,
             "reason": "role-human", "session_id": "h", "spawner": ME},
            {"pane": "2", "replayed": False, "verdict": gf.EXCLUDE,
             "reason": "foreign-goal:Some Goal", "session_id": "f", "spawner": ME},
        ]
        line = gf.counts_line(rows)
        self.assertIn("excluded(role-human): 1", line)
        self.assertIn("excluded(foreign-goal): 1", line)


class RoleHumanEndToEnd(unittest.TestCase):
    """The whole path the watcher's arm runs, not just `verdict()`.

    A `verdict()`-level pin cannot catch a renderer that prints an EXCLUDE row on the bare
    `--feed` shape, and that shape IS the watcher's input -- so the exclusion is asserted
    where the manager would actually be woken: two panes, one human and one agent, both
    spawned by this watcher.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = self._tmp.name
        self.state = os.path.join(self.dir, "state")
        self.registry = os.path.join(self.dir, "registry")
        self.endpoint = FixtureEndpoint()
        self.addCleanup(self.endpoint.__exit__, None, None, None)
        for path in (self.state, self.registry):
            os.makedirs(path)
        with open(os.path.join(self.state, "a.events.jsonl"), "w", encoding="utf-8") as handle:
            handle.write(json.dumps(
                {"type": "open", "item_id": "1", "pane": "2165", "session_id": "human-1"}
            ) + "\n")
            handle.write(json.dumps(
                {"type": "open", "item_id": "2", "pane": "2106", "session_id": "agent-1"}
            ) + "\n")
        self.endpoint.add_ledger_row(
            ledger_row("human-1", spawner_session_id=ME, role="human")
        )
        self.endpoint.add_ledger_row(
            ledger_row("agent-1", spawner_session_id=ME, role="agent")
        )

    def run_filter(self, *extra):
        return subprocess.run(
            [sys.executable, _SCRIPT, "--feed", "--self", ME,
             "--state-dir", self.state, "--registry-dir", self.registry,
             "--endpoint", self.endpoint.url,
             "--claims-file", os.path.join(self.dir, "no-claims.json"),
             "--no-scope", *extra],
            input="[2165]\n[2106]\n", capture_output=True, text=True,
        )

    def test_the_bare_feed_drops_the_human_pane_and_keeps_the_agent_pane(self):
        result = self.run_filter()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.split(), ["2106"], result.stdout)
        self.assertIn("excluded(role-human): 1", result.stderr)

    def test_explain_names_the_human_row_instead_of_hiding_it(self):
        result = self.run_filter("--explain")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("exclude role-human", result.stdout, result.stdout)
        # The verdict column is padded to the widest verdict on the run, so match the
        # reason rather than a fixed-width prefix -- the padding is not the contract.
        self.assertIn("own-worker", result.stdout, result.stdout)
        self.assertIn("2106", result.stdout, result.stdout)


class ScopeCli(unittest.TestCase):
    """The scope CLI seam, end to end through `_session_scope()` / `main()`.

    Every other scope case in this file drives `_FakeScope`, so nothing pinned the real
    `spec_from_file_location("session_scope", …)` + `load(self_id, vault_paths)` seam, nor
    the two flags that reach it, nor the `SCOPE  unresolved:` line the FILTER emits. A
    rename of the loaded module or a signature drift on `load` would have passed the suite.

    The fixture's own vault, state and registry are used, so the pane→session mapping is the
    real one; only the ledger endpoint is stubbed, which keeps the test offline.
    """

    #: The fixture's manager stamp — `replay.py`'s `SELF`, not this file's `ME`.
    SELF = "49b471f1-a8a6-48b0-b2b2-14b9cb2437e3"
    BASE = os.path.join(_HERE, "fixtures", "gate-owner-foreign-goal")

    def setUp(self):
        self._orig_owner = gf._OWNER
        gf._OWNER = OwnerStub()
        self.addCleanup(self._restore)

    def _restore(self):
        gf._OWNER = self._orig_owner

    def run_filter(self, *argv):
        """`(rc, stdout, stderr)` from `main()` with `argv`, ledger stubbed."""
        with open(os.path.join(self.BASE, "feed.txt"), encoding="utf-8") as handle:
            feed = handle.read()
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(sys, "argv", ["gate-owner-filter.py"] + list(argv)), \
             mock.patch.object(sys, "stdin", io.StringIO(feed)), \
             mock.patch.object(gf, "load_ledger", return_value={}), \
             contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = gf.main()
        return rc, out.getvalue(), err.getvalue()

    def common(self):
        return [
            "--state-dir", os.path.join(self.BASE, "state"),
            "--registry-dir", os.path.join(self.BASE, "registry"),
            "--claims-file", os.path.join(self.BASE, "claims.json"),
            "--feed", "--explain",
        ]

    def test_the_importlib_seam_reaches_the_real_session_scope(self):
        """A rename of the module, or a changed `load` signature, fails here.

        ⚠️ The ownership input is stubbed on the REAL session-scope module too: this test
        drives `load()` directly rather than through the filter, so the filter's own stub
        never applies and the read would answer from whatever is armed on this machine.
        """
        scope_mod = gf._session_scope()
        original = scope_mod._OWNER
        scope_mod._OWNER = OwnerStub()
        self.addCleanup(setattr, scope_mod, "_OWNER", original)
        scope = scope_mod.load(self.SELF, [os.path.join(self.BASE, "vault")])
        self.assertTrue(scope.resolved, scope.reason)
        # The fixture's own task — the CONTROL pane's page — must land in the own set, or
        # the scope would exclude the very pane the fixture exists to protect.
        self.assertIn("Own Worker Control Task", scope.own_titles)

    def test_scope_vault_excludes_the_foreign_panes(self):
        rc, out, err = self.run_filter(
            "--self", self.SELF,
            "--scope-vault", os.path.join(self.BASE, "vault"),
            *self.common(),
        )
        self.assertEqual(rc, 0)
        self.assertIn("excluded(foreign-goal): 3", err)
        self.assertIn("excluded(owner-unknown): 2", err)
        self.assertIn("emit: 1", err)
        self.assertIn("exclude foreign-goal:", out)

    def test_a_stopped_manager_filters_nothing(self):
        """THE BOUNDARY: a stood-down manager's watcher must not exclude other goals' gates.

        A manager that ran `/supervisor:stop` keeps its session, so its attention watcher keeps
        running — while the round that would make it a manager is exactly what stopped. With
        the scope applied it would keep hiding gates that belong to goals it no longer serves,
        so the ownership verdict comes first and a `not-owner` answer drops the scope entirely.
        """
        gf._OWNER = OwnerStub("not-owner", "loop-stopped")
        rc, out, err = self.run_filter(
            "--self", self.SELF,
            "--scope-vault", os.path.join(self.BASE, "vault"),
            *self.common(),
        )
        self.assertEqual(rc, 0)
        self.assertIn("SCOPE  unresolved:", err)
        self.assertIn("is not an owner (loop-stopped", err)
        self.assertIn("emit: 6", err)
        self.assertIn("excluded(foreign-goal): 0", err)

    def test_no_scope_emits_every_row(self):
        """The flag disables the read entirely — the pre-scope behaviour, pinned."""
        rc, out, err = self.run_filter("--self", self.SELF, "--no-scope", *self.common())
        self.assertEqual(rc, 0)
        self.assertIn("emit: 6", err)
        self.assertIn("excluded(foreign-goal): 0", err)
        self.assertNotIn("SCOPE  unresolved:", err)
        self.assertNotIn("exclude ", out)

    def test_an_unresolved_scope_announces_the_filters_own_line(self):
        """`session-scope.py` emits a same-prefixed line, so the SUFFIX is what pins this one."""
        rc, _out, err = self.run_filter(
            "--self", self.SELF,
            "--scope-vault", "/nonexistent-vault-for-this-test",
            *self.common(),
        )
        self.assertEqual(rc, 0)
        self.assertIn("SCOPE  unresolved:", err)
        self.assertIn("— no row is excluded", err)
        self.assertIn("emit: 6", err)


class ForeignGoalFixture(unittest.TestCase):
    """The five measured foreign firings, replayed end to end through the CLI.

    Fixture: `scripts/tests/fixtures/gate-owner-foreign-goal/`. The stub registry holds only
    this manager, so every foreign spawner reads `dead-manager` — the firing-time shape, where
    the live ownership chain does not resolve and the filter used to emit every foreign gate.
    """

    @classmethod
    def setUpClass(cls):
        import importlib.util as ilu

        path = os.path.join(_HERE, "fixtures", "gate-owner-foreign-goal", "replay.py")
        spec = ilu.spec_from_file_location("foreign_goal_replay", path)
        cls.replay = ilu.module_from_spec(spec)
        spec.loader.exec_module(cls.replay)
        cls.scoped = cls.replay.run(scope=True)
        cls.unscoped = cls.replay.run(scope=False)
        # THE NEGATIVE CONTROL for the ownership gate: the same session, the same page stamp,
        # the same vault — only the loop marker differs. It must exclude nothing.
        cls.stopped = cls.replay.run(scope=True, stopped=True)

    def rows(self, result="json"):
        return json.loads(self.scoped[result].stdout)

    def by_pane(self):
        return {row["pane"]: row for row in self.rows()}

    def test_the_five_foreign_panes_leave_the_feed(self):
        self.assertEqual(self.scoped["feed"].stdout.split(), [self.replay.CONTROL],
                         self.scoped["feed"].stdout)

    def test_a_stopped_manager_excludes_nothing(self):
        """THE BOUNDARY, through the real code path rather than a stubbed verdict.

        The fixture's `--self` keeps its session and its page stamp; only the loop marker
        differs (`stopped=True` writes `.stopped` instead of an armed cadence). A manager that
        ran `/supervisor:stop` is not an owner, so its scope must not hide another goal's gates
        — the resolver says `not-owner` and the filter falls back to excluding nothing.

        ⚠️ **This is what makes the armed case above load-bearing.** Without it, the fixture
        would pass identically if the ownership gate were deleted, and the three exclusions it
        asserts would be attributing work to a manager whose round has stopped.
        """
        out, err = self.stopped["json"].stdout, self.stopped["json"].stderr
        self.assertIn("SCOPE  unresolved:", err)
        self.assertIn("is not an owner (loop-stopped", err)
        # All six rows survive, and none is excluded as foreign: the control emits, the five
        # foreign panes are back in the feed as ordinary unowned rows.
        self.assertEqual(len(json.loads(out)), 6, out)
        self.assertIn("excluded(foreign-goal): 0", self.stopped["feed"].stderr)
        self.assertEqual(
            sorted(self.stopped["feed"].stdout.split()),
            sorted(list(self.replay.FOREIGN) + [self.replay.CONTROL]),
            self.stopped["feed"].stdout,
        )

    def test_the_own_worker_control_still_emits(self):
        """Without this the fixture would pass on a filter that excludes everything."""
        self.assertEqual(self.by_pane()[self.replay.CONTROL]["verdict"], gf.EMIT)
        self.assertEqual(self.by_pane()[self.replay.CONTROL]["reason"], "own-worker")

    def test_all_six_rows_survive_in_json(self):
        """A pane missing from `--json` is a FAIL: a silent drop is what a fixed row looks like."""
        self.assertEqual(
            set(self.by_pane()),
            set(self.replay.FOREIGN) | {self.replay.CONTROL},
        )

    def test_each_foreign_row_names_its_owner(self):
        expected = {
            "269": "foreign-goal:PR Reviews Return a Verdict Within Minutes, Not Hours",
            "260": "foreign-goal:All Octopus Builds Green on Dev, Staging, and Master",
            "272": "owner-unknown",
            "278": "foreign-goal:Every Recurring Task Is Structurally Valid and Linked to a Runbook",
            "279": "owner-unknown",
        }
        rows = self.by_pane()
        for pane, reason in expected.items():
            self.assertEqual(rows[pane]["verdict"], gf.EXCLUDE, rows[pane])
            self.assertEqual(rows[pane]["reason"], reason, rows[pane])

    def test_counts_line_reports_an_exclusion_apart_from_a_drop(self):
        stderr = self.scoped["feed"].stderr
        self.assertIn("excluded(foreign-goal): 3", stderr)
        self.assertIn("excluded(owner-unknown): 2", stderr)
        self.assertIn("dropped(peer-manager): 0", stderr)
        self.assertIn("emit: 1", stderr)

    def test_without_the_scope_input_every_row_is_emitted(self):
        """The before-reading, pinned in-tree: the same fixture, the pre-fix path."""
        self.assertEqual(
            self.unscoped["feed"].stdout.split(),
            list(self.replay.FOREIGN) + [self.replay.CONTROL],
            self.unscoped["feed"].stdout,
        )
        self.assertIn("emit: 6", self.unscoped["feed"].stderr)
        self.assertIn("excluded(foreign-goal): 0", self.unscoped["feed"].stderr)

    def test_the_pre_fix_path_prints_the_same_five_the_task_measured(self):
        """5 of 5 — not "at least one", and not a single case."""
        printed = self.unscoped["feed"].stdout.split()
        self.assertEqual([pane for pane in printed if pane in self.replay.FOREIGN],
                         list(self.replay.FOREIGN))




_PANELESS_FIXTURE = os.path.join(_HERE, "fixtures", "paneless-gate-feed.txt")
_HST_ITEM = "f712443d00000000000000000000aaaa"


class PanelessHeadlessGateReachesTheKeptSet(unittest.TestCase):
    """A `[ hst]` row is a gate, keyed `hst:<store item id>`.

    ⚠️ The digit-only row match skipped every headless worker's gate, so the filter
    read `gates: 0` over a feed carrying the gate and the armed watcher never woke —
    measured 2026-11-03, 11 of 13 live gates rendered `[ hst]`. The fixture is the
    two-row feed the task names: it read `gates: 1` before this change.
    """

    def feed(self):
        with open(_PANELESS_FIXTURE, encoding="utf-8") as handle:
            return handle.read()

    def test_feed_rows_keys_the_headless_row_on_its_store_item(self):
        rows = gf.feed_rows(self.feed().splitlines(keepends=True))
        self.assertEqual(rows, [("hst:" + _HST_ITEM, False), ("417", False)])

    def test_a_headless_row_without_an_answer_line_is_not_a_gate(self):
        feed = ["  [ hst]  1m  x\n", "         headless\n", "  [ 417]  2m  y\n"]
        self.assertEqual(gf.feed_rows(feed), [("417", False)])

    def test_an_answer_line_is_never_borrowed_from_the_next_row(self):
        feed = [
            "  [ hst]  1m  x\n",
            "  [ 417]  2m  y\n",
            "         answer: python3 a/attention-answer.py answer abc\n",
        ]
        self.assertEqual(gf.feed_rows(feed), [("417", False)])

    def test_the_question_mark_row_stays_unparsed(self):
        """A pane-less row with nothing to answer may be an exited session."""
        self.assertEqual(gf.feed_rows(["  [   ?]  5m  ⟳replay  x\n"]), [])

    def test_the_fixture_reads_two_gates_two_emits(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = subprocess.run(
                [sys.executable, _SCRIPT, "--feed", "--no-scope",
                 "--state-dir", tmp, "--registry-dir", tmp,
                 "--endpoint", dead_url(),
                 "--claims-file", os.path.join(tmp, "none.json")],
                input=self.feed(), capture_output=True, text=True,
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("gates: 2  emit: 2", result.stderr)
        # The store is unreachable here, so the headless row fails OPEN and says so.
        self.assertEqual(
            result.stdout.splitlines(),
            ["hst:" + _HST_ITEM + "  store=unreachable", "417"],
        )

    def test_the_store_resolves_the_session_and_ownership_applies(self):
        """The store's `producer_id` is the session; a live peer manager's worker drops."""
        payload = json.dumps(
            [{"item_id": _HST_ITEM, "producer_id": GATED, "payload": "Bash: date"}]
        ).encode("utf-8")
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = payload
        with mock.patch("urllib.request.urlopen", return_value=response):
            items = gf.headless_items(["hst:" + _HST_ITEM, "417"], "http://x")
        self.assertEqual(gf.session_for_pane("hst:" + _HST_ITEM, items), GATED)
        ledger = {
            GATED: {"session_id": GATED, "parent_session": PEER},
            PEER: {"session_id": PEER, "label": "manager", "role": "manager"},
        }
        with mock.patch.object(gf, "is_manager", side_effect=lambda s, _l: s == PEER):
            call, reason, sid, _ = gf.evaluate(
                "hst:" + _HST_ITEM, ledger, {GATED, PEER}, items, ME
            )
        self.assertEqual((call, reason, sid), (gf.DROP, "peer-manager", GATED))

    def test_an_unreachable_store_fails_open(self):
        self.assertEqual(gf.headless_items(["hst:" + _HST_ITEM], dead_url()), [])

    def test_the_checked_read_names_a_store_failure(self):
        """`[]` alone cannot tell a failed read from a real absence; this twin can."""
        records, error = gf.headless_items_checked(["hst:" + _HST_ITEM], dead_url())
        self.assertEqual(records, [])
        self.assertIn("attention store read failed", error)

    def test_the_checked_read_returns_no_error_on_a_real_absence(self):
        payload = json.dumps([{"item_id": "other", "producer_id": "s"}]).encode("utf-8")
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = payload
        with mock.patch("urllib.request.urlopen", return_value=response):
            records, error = gf.headless_items_checked(["hst:" + _HST_ITEM], "http://x")
        self.assertEqual((records, error), ([], None))

    def test_the_owner_line_names_the_session(self):
        feed = ["  [ hst]  1m  x\n",
                "         answer: python3 a/attention-answer.py answer abc\n",
                "         owner: sid-123\n"]
        self.assertEqual(gf.owner_of(feed, 0), "sid-123")

    def test_an_owner_line_is_never_borrowed_from_the_next_row(self):
        feed = ["  [   ?]  1m  x\n", "  [ 417]  2m  y\n", "         owner: sid-123\n"]
        self.assertIsNone(gf.owner_of(feed, 0))

    def test_a_headless_fingerprint_survives_a_flapping_store(self):
        """The store read can fail on one poll and answer on the next; the key is the gate."""
        key = "hst:" + _HST_ITEM
        resolved = [{"pane": key, "session_id": GATED, "detail": "Bash: date", "ts": 1}]
        self.assertEqual(gf.gate_fingerprint(key, []), gf.gate_fingerprint(key, resolved))

    def test_a_second_poll_on_the_same_headless_gate_emits_nothing(self):
        key = "hst:" + _HST_ITEM
        first, state = gf.emit_decisions([key], {}, [], 100.0, 3600)
        self.assertEqual(first, {key})
        again, _ = gf.emit_decisions([key], state, [], 160.0, 3600)
        self.assertEqual(again, set())

    def test_the_wake_line_names_the_session_or_one_of_two_fixed_tokens(self):
        args = mock.Mock(json=False, explain=False, feed=True)
        rows = [
            {"pane": "hst:a", "replayed": False, "verdict": gf.EMIT, "session_id": GATED,
             "store": gf.STORE_OPEN},
            {"pane": "hst:b", "replayed": False, "verdict": gf.EMIT, "session_id": None,
             "store": gf.STORE_OPEN},
            {"pane": "hst:c", "replayed": False, "verdict": gf.EMIT, "session_id": None,
             "store": gf.STORE_UNREAD},
            {"pane": "417", "replayed": False, "verdict": gf.EMIT, "session_id": GATED},
        ]
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            gf.render(rows, args)
        self.assertEqual(
            out.getvalue().splitlines(),
            [f"hst:a  session={GATED}", "hst:b  session=unattributed",
             "hst:c  store=unreachable", "417"],
        )


# Four `[ hst]` rows, one per store state the filter must tell apart.
_OPEN_OWNED = "a0000000000000000000000000000001"
_OPEN_UNATTRIBUTED = "b0000000000000000000000000000002"
_CLOSED = "c0000000000000000000000000000003"
_ANSWERED = "d0000000000000000000000000000004"
_UNATTRIBUTED_TOKEN = "session=unattributed"
_STORE_FAILED_TOKEN = "store=unreachable"


def _hst_row(item_id, label):
    return (
        f"  [ hst]     1m  {label}    awaiting-operator · permission: Bash: date\n"
        "         headless — no pane to jump to; answer with the command below\n"
        f"         answer: python3 x/attention-answer.py answer {item_id} --decision allow|deny\n"
    )


_FOUR_ROW_FEED = "Needs you (4)\n" + "".join(
    _hst_row(item, label)
    for item, label in (
        (_OPEN_OWNED, "open-owned"),
        (_OPEN_UNATTRIBUTED, "open-unattributed"),
        (_CLOSED, "closed-card"),
        (_ANSWERED, "answered-card"),
    )
)


class _AttentionStub:
    """The attention store's OPEN list — `GET /api/1.0/attention` — and nothing else.

    The real route returns open items only (`attention-card-lookup.py`), so the closed and
    answered cards are modelled the way the store models them: by their ABSENCE from a
    successful read. Every other route 404s, so the ledger read fails open to `{}`.
    """

    def __init__(self, rows):
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        import threading

        body = json.dumps(rows).encode("utf-8")

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_a):
                pass

            def do_GET(self):
                if self.path.split("?", 1)[0] == "/api/1.0/attention":
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                self.send_response(404)
                self.send_header("Content-Length", "0")
                self.end_headers()

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def url(self):
        host, port = self.server.server_address[:2]
        return "http://%s:%d" % (host, port)

    def __enter__(self):
        return self

    def __exit__(self, *_a):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


_OPEN_LIST = [
    {"item_id": _OPEN_OWNED, "producer_id": GATED, "payload": "Bash: date", "state": "open"},
    {"item_id": _OPEN_UNATTRIBUTED, "producer_id": "", "payload": "Bash: date", "state": "open"},
]


def _run_feed(endpoint):
    with tempfile.TemporaryDirectory() as tmp:
        return subprocess.run(
            [sys.executable, _SCRIPT, "--feed", "--no-scope",
             "--state-dir", tmp, "--registry-dir", tmp,
             "--endpoint", endpoint,
             "--claims-file", os.path.join(tmp, "none.json")],
            input=_FOUR_ROW_FEED, capture_output=True, text=True,
        )


class ClosedCardsDoNotWakeTheFeed(unittest.TestCase):
    """A closed or answered card is not a gate; a failed store read still is one.

    ⚠️ The open list is the store's whole answer, so "absent from a GOOD read" and "the
    read FAILED" were the same empty record, and both rendered `session=unresolved` and
    woke the manager. Measured 2026-11-05 to 11-07: 3 of 3, then 7 of 10, then 18 of 18
    wake rows on one goal manager's arm were dead or foreign, and the arm was stopped.

    ⚠️ Row (b) — an OPEN gate whose `producer_id` is empty — is the discriminator. It
    carried `unresolved` too, so a fix that drops every `unresolved` row drops a live
    gate and fails `test_the_four_row_fixture_emits_exactly_the_two_open_cards`. The
    drop must key on the store's state, never on the label.
    """

    def test_the_four_row_fixture_emits_exactly_the_two_open_cards(self):
        with _AttentionStub(_OPEN_LIST) as store:
            result = _run_feed(store.url)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            result.stdout.splitlines(),
            [f"hst:{_OPEN_OWNED}  session={GATED}",
             f"hst:{_OPEN_UNATTRIBUTED}  {_UNATTRIBUTED_TOKEN}"],
        )
        self.assertIn("gates: 4  emit: 2", result.stderr)
        self.assertIn("dropped(not-open): 2", result.stderr)

    def test_an_unreachable_store_still_emits_all_four_and_says_so(self):
        result = _run_feed(dead_url())
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            result.stdout.splitlines(),
            [f"hst:{item}  {_STORE_FAILED_TOKEN}"
             for item in (_OPEN_OWNED, _OPEN_UNATTRIBUTED, _CLOSED, _ANSWERED)],
        )
        self.assertIn("dropped(not-open): 0", result.stderr)

    def test_the_two_fail_open_tokens_are_separable_by_grep(self):
        """Proven mechanically over the combined output, never by prose.

        Each fixed token is matched with `grep -F`. The two match sets must both be
        non-empty and disjoint, no line may still say `unresolved`, and every line matching
        neither token must carry a resolved session id.
        """
        with _AttentionStub(_OPEN_LIST) as store:
            healthy = _run_feed(store.url).stdout
        failed = _run_feed(dead_url()).stdout
        combined = healthy + failed
        lines = combined.splitlines()

        def grep(token):
            proc = subprocess.run(["grep", "-F", "--", token], input=combined,
                                  capture_output=True, text=True)
            return set(proc.stdout.splitlines())

        unattributed, store_failed = grep(_UNATTRIBUTED_TOKEN), grep(_STORE_FAILED_TOKEN)
        self.assertEqual(len(unattributed), 1)
        self.assertEqual(len(store_failed), 4)
        self.assertEqual(unattributed & store_failed, set())
        self.assertEqual(grep("unresolved"), set())
        resolved = [ln for ln in lines if ln not in unattributed | store_failed]
        self.assertEqual(resolved, [f"hst:{_OPEN_OWNED}  session={GATED}"])

    def test_store_states_names_each_keys_store_state(self):
        keys = ["hst:" + i for i in (_OPEN_OWNED, _OPEN_UNATTRIBUTED, _CLOSED, _ANSWERED)]
        with _AttentionStub(_OPEN_LIST) as store:
            records, error = gf.headless_items_checked(keys + ["417"], store.url)
            states = gf.store_states(keys, records, error)
        self.assertIsNone(error)
        self.assertEqual(states, {
            "hst:" + _OPEN_OWNED: gf.STORE_OPEN,
            "hst:" + _OPEN_UNATTRIBUTED: gf.STORE_OPEN,
            "hst:" + _CLOSED: gf.STORE_NOT_OPEN,
            "hst:" + _ANSWERED: gf.STORE_NOT_OPEN,
        })
        self.assertEqual(sorted(r["pane"] for r in records),
                         ["hst:" + _OPEN_OWNED, "hst:" + _OPEN_UNATTRIBUTED])
        records, error = gf.headless_items_checked(keys, dead_url())
        self.assertEqual(set(gf.store_states(keys, records, error).values()), {gf.STORE_UNREAD})

    def test_a_non_list_body_is_a_failed_read_not_an_empty_store(self):
        """An empty-looking answer must never read as "every card closed" — that is a silence."""
        keys = ["hst:" + _CLOSED]
        with _AttentionStub({"error": "nope"}) as store:
            records, error = gf.headless_items_checked(keys, store.url)
        self.assertIsNotNone(error)
        self.assertEqual(gf.store_states(keys, records, error), {keys[0]: gf.STORE_UNREAD})

    def test_headless_items_keeps_its_contract_for_round_readings(self):
        """`round-readings.py` reads `headless_items` and treats `[]` as unread — unchanged."""
        self.assertEqual(gf.headless_items(["hst:" + _CLOSED], dead_url()), [])


if __name__ == "__main__":
    unittest.main()
