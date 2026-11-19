#!/usr/bin/env python3
"""Tests for `scripts/flagged-rows.py`.

Every case is a fixture — none touches the live fleet, the role map or the heartbeat
store. The live behaviour is exercised by the task's DoD run, not here.
"""

import importlib.util
import json
import os
import shutil
import tempfile
import unittest
import unittest.mock

HERE = os.path.dirname(os.path.abspath(__file__))
MODULE = os.path.join(os.path.dirname(HERE), "flagged-rows.py")


def load():
    spec = importlib.util.spec_from_file_location("flagged_rows", MODULE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


fr = load()


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)
    return path


def page(front, body="", provenance="operator"):
    """A frontmatter page whose `flag:` carries provenance by default.

    `flag_set_by: operator` is added whenever the front declares a `flag:` and names no
    setter, because that is the shape of every live flagged row after the 2026-09-30
    legacy disposition. Under the operator's 2026-11-10 ruling an absent setter now
    REFUSES, so a fixture that omitted it would model a row the selector rejects rather
    than the ordinary admitted row each of these tests is about. The refusal shape is
    tested deliberately in `PerKindRefusal`, which passes `provenance=None`.
    """
    if provenance and "flag_set_by" not in front and "flag:" in front:
        front = "%s\nflag_set_by: %s" % (front, provenance)
    return "---\n%s\n---\n%s" % (front, body)


#: `~/.cache/wezterm-role-map.json`'s live shape, as of 2026-10-31. `Hold` is present
#: and is NOT a role target — its chip is null — which is why the resolver reads
#: `commands/open.md` § 3.0's table rather than this map for the enum.
ROLE_MAP = {
    "Managers": {"chip": "orange", "window_id": 0},
    "Direct": {"chip": "cyan", "window_id": 1},
    "Agents": {"chip": "pink", "window_id": 4},
    "Hold": {"chip": None, "window_id": 3},
}


class FlagPredicate(unittest.TestCase):
    """The predicate differs by kind, and the difference is live in the vault."""

    def test_task_accepts_an_unquoted_yaml_boolean(self):
        self.assertTrue(fr.flag_admits(page("flag: true"), "task"))

    def test_task_rejects_a_quoted_scalar(self):
        # `flag: "true"` is a STRING in YAML — `t.get('flag') is True` is False.
        self.assertFalse(fr.flag_admits(page('flag: "true"'), "task"))

    def test_goal_accepts_a_quoted_scalar(self):
        # topic-spawn.py's FLAG_TRUTHY strips quotes; eight of private-personal's
        # nine flagged goals spell it this way.
        self.assertTrue(fr.flag_admits(page('flag: "true"'), "goal"))

    def test_goal_accepts_yes_and_on(self):
        self.assertTrue(fr.flag_admits(page("flag: yes"), "topic"))
        self.assertTrue(fr.flag_admits(page("flag: on"), "topic"))

    def test_absent_flag_is_not_admitted(self):
        self.assertFalse(fr.flag_admits(page("status: in_progress"), "goal"))


class Eligibility(unittest.TestCase):
    """The consumer's gate: flag AND in_progress AND defer_date absent-or-due."""

    def test_eligible_row(self):
        self.assertIsNone(fr.eligible(page("flag: true\nstatus: in_progress"),
                                      "task", "2026-10-31"))

    def test_completed_row_is_not_eligible(self):
        self.assertEqual(fr.eligible(page("flag: true\nstatus: completed"),
                                     "task", "2026-10-31"), "status is completed")

    def test_future_defer_is_not_eligible(self):
        self.assertEqual(
            fr.eligible(page('flag: true\nstatus: in_progress\ndefer_date: "2026-11-01"'),
                        "task", "2026-10-31"),
            "deferred")

    def test_due_defer_is_eligible(self):
        self.assertIsNone(
            fr.eligible(page('flag: true\nstatus: in_progress\ndefer_date: "2026-10-31"'),
                        "task", "2026-10-31"))

    def test_unreadable_page_is_not_eligible(self):
        self.assertEqual(fr.eligible(None, "task", "2026-10-31"), "unreadable page")


class RoleResolution(unittest.TestCase):
    """`template` wins whenever a template exists — including over a disagreement."""

    def setUp(self):
        self.root = tempfile.mkdtemp()
        # The real shape: `frontmatter:` is INDENTED under `spec.template`.
        write(os.path.join(self.root, "start-day.yaml"),
              "spec:\n  name: start-day\n  template:\n    body: |-\n      x\n"
              "    frontmatter:\n      role: human\n      mode: interactive\n")

    def role(self, text):
        return fr.resolve_role(fr.open_queue.frontmatter(text), text, self.root)

    def test_template_wins_over_a_disagreeing_on_disk_role(self):
        # The live Start Day case: a writer flipped the instance to `agent`.
        self.assertEqual(
            self.role(page("role: agent",
                           "# Source\n\nGenerated from `bborbe/nuke/task/"
                           "recurring-schedules/prod/start-day.yaml`.\n")),
            ("human", "template"))

    def test_the_footer_is_read_from_the_body_not_the_frontmatter(self):
        # The `# Source` line is in the body; searching the frontmatter block alone
        # can never match, which is what made `template` unreachable.
        text = page("role: agent",
                    "Generated from `bborbe/nuke/task/recurring-schedules/prod/"
                    "start-day.yaml`.\n")
        self.assertNotIn("bborbe/nuke", fr.open_queue.frontmatter(text))
        self.assertEqual(self.role(text), ("human", "template"))

    def test_on_disk_role_when_no_template(self):
        self.assertEqual(self.role(page("role: agent")), ("agent", "on-disk"))

    def test_body_when_neither(self):
        self.assertEqual(self.role(page("status: in_progress")), ("agent", "body"))

    def test_missing_template_falls_back_to_on_disk(self):
        self.assertEqual(
            self.role(page("role: agent",
                           "# Source\n\n`bborbe/nuke/task/recurring-schedules/prod/"
                           "absent.yaml`\n")),
            ("agent", "on-disk"))

    def normalised(self, template_root):
        root = tempfile.mkdtemp()
        tasks = os.path.join(root, "25 Tasks")
        page_path = os.path.join(tasks, "A Task.md")
        write(page_path, page("flag: true\nstatus: in_progress\nrole: agent",
                              "\n`bborbe/nuke/task/recurring-schedules/prod/absent.yaml`\n"))
        ctx = {"today": "2026-10-31", "role_map": {}, "template_root": template_root,
               "no_liveness": True, "managers": set(), "topic_map": {},
               "mode_default": "interactive", "normalised": [],
               "subjects": {}, "liveness_cache": {}}
        cfg = {"tasks_dir": "25 Tasks", "path": root, "worker_launcher": "/opt/l/cc-v"}
        fr.row_for("v", cfg, "task", page_path, ctx)
        return ctx["normalised"]

    def test_a_footer_naming_a_missing_template_is_reported(self):
        # A named-but-unreadable template must not look like a page with none.
        self.assertEqual(self.normalised(tempfile.mkdtemp()),
                         [{"vault": "v", "name": "A Task", "field": "role_template",
                           "raw": "absent", "used": "unreadable"}])

    def test_a_template_that_declares_no_role_is_not_reported(self):
        # `topic-backup-saturday.yaml` and `shutdown-k3s.yaml` both omit `role:`
        # deliberately; reporting them would cry wolf on every such template.
        root = tempfile.mkdtemp()
        write(os.path.join(root, "absent.yaml"),
              "spec:\n  template:\n    frontmatter:\n      priority: 3\n")
        self.assertEqual(self.normalised(root), [])


class ParkedFor(unittest.TestCase):
    """`parked_for` reads one session's log, once per run, and never live state in a fixture."""

    def test_no_wait_disables_the_read(self):
        self.assertFalse(fr.parked_for({"no_wait": True}, "s1"))

    def test_a_sessionless_row_is_never_parked(self):
        self.assertFalse(fr.parked_for({}, None))

    def test_the_answer_is_cached_per_run(self):
        calls = []
        original = fr.declared_wait.declared_wait
        fr.declared_wait.declared_wait = lambda sid: calls.append(sid) or True
        try:
            ctx = {}
            self.assertTrue(fr.parked_for(ctx, "s1"))
            self.assertTrue(fr.parked_for(ctx, "s1"))
        finally:
            fr.declared_wait.declared_wait = original
        self.assertEqual(calls, ["s1"])


class BranchPartition(unittest.TestCase):
    """The ordered predicate list — first match wins, so the set is a partition."""

    def branch(self, kind="task", role="agent", sid=None, live="none", pane=None,
               chain_live=None, block=None, owner=None, owner_live=None, parked=False):
        return fr.resolve_branch(kind, role, sid, live, pane, chain_live,
                                 block or page("role: agent"),
                                 owner=owner, owner_live=owner_live, parked=parked)

    def test_unknown_outranks_everything(self):
        for verdict in ("UNKNOWN", "AMBIGUOUS", "MALFORMED"):
            self.assertEqual(self.branch(live=verdict)[0], "unknown")

    def test_unknown_outranks_a_human_row(self):
        self.assertEqual(self.branch(role="human", live="UNKNOWN")[0], "unknown")

    def test_human_row_is_direct(self):
        self.assertEqual(self.branch(role="human", live="none")[0], "direct")

    def test_sessionless_human_is_direct_not_create(self):
        self.assertEqual(self.branch(role="human", live="none", sid=None)[0], "direct")

    def test_parked_outranks_resume(self):
        # The criterion's own case: a row whose last closer declared an external wait
        # must never read `resume`, or the tick reopens it on every pass.
        self.assertEqual(self.branch(live="ABSENT", sid="s1", parked=True)[0], "parked")

    def test_parked_outranks_create(self):
        self.assertEqual(self.branch(live="none", sid=None, parked=True)[0], "parked")

    def test_a_live_row_is_never_parked(self):
        # A LIVE row is OPEN. Folding it into `parked` would drop a session that exists
        # out of coverage's numerator, so jump and skip stay in front of the predicate.
        self.assertEqual(
            self.branch(live="LIVE", sid="s1", pane="p1", parked=True)[0], "jump")
        self.assertEqual(self.branch(live="LIVE", sid="s1", parked=True)[0], "skip")

    def test_parked_does_not_outrank_an_unanswerable_probe(self):
        for verdict in ("UNKNOWN", "AMBIGUOUS", "MALFORMED"):
            self.assertEqual(
                self.branch(live=verdict, sid="s1", parked=True)[0], "unknown", verdict)

    def test_parked_does_not_outrank_a_manager_row(self):
        branch, refused = self.branch(live="ABSENT", sid="s1", parked=True,
                                      block=page("role: manager"))
        self.assertEqual(branch, "skip")
        self.assertEqual(refused, ["manager-row"])

    def test_parked_does_not_outrank_open_manager(self):
        self.assertEqual(
            self.branch(kind="goal", live="ABSENT", sid="s1", chain_live=False,
                        parked=True)[0], "open-manager")

    def test_manager_row_is_skip_in_every_reachable_state(self):
        block = page("role: manager")
        for live, sid, pane in (("LIVE", "s1", "p1"), ("ABSENT", "s1", None),
                                ("none", None, None)):
            branch, refused = self.branch(live=live, sid=sid, pane=pane, block=block)
            self.assertEqual(branch, "skip", live)
            self.assertEqual(refused, ["manager-row"])

    def test_sessionless_topic_is_open_manager_not_create(self):
        # `chain_live is False` — the subject has no live manager, proven.
        self.assertEqual(self.branch(kind="topic", live="none", chain_live=False)[0],
                         "open-manager")

    def test_an_unanswerable_manager_probe_does_not_open_a_manager(self):
        # `None` is not `False`: the same fold `unknown` rejects.
        self.assertNotEqual(self.branch(kind="topic", live="none", chain_live=None)[0],
                            "open-manager")

    def test_live_with_pane_jumps(self):
        self.assertEqual(self.branch(live="LIVE", sid="s1", pane="p1")[0], "jump")

    def test_live_without_pane_skips(self):
        self.assertEqual(self.branch(live="LIVE", sid="s1", pane=None)[0], "skip")

    def test_absent_with_id_resumes(self):
        self.assertEqual(self.branch(live="ABSENT", sid="s1")[0], "resume")

    def test_absent_without_id_creates(self):
        self.assertEqual(self.branch(live="ABSENT", sid=None)[0], "create")


class HeldOwnerLive(unittest.TestCase):
    """SC1–SC2: a flagged TASK whose owning subject already has a live manager is held.

    The hold is a branch, not a refusal token, and it sits after every row-own branch:
    a task whose own session is live still jumps or resumes, so only a would-be CREATE
    is suppressed. The FLEET sentinel is excluded, or the whole batch would stop.
    """

    GOAL = {"kind": "goal", "name": "G"}

    def branch(self, **kw):
        return fr.resolve_branch(kw.pop("kind", "task"), kw.pop("role", "agent"),
                                 kw.pop("sid", None), kw.pop("live", "none"),
                                 kw.pop("pane", None), kw.pop("chain_live", None),
                                 kw.pop("block", page("role: agent")),
                                 owner=kw.pop("owner", None),
                                 owner_live=kw.pop("owner_live", None))

    def test_sc1_a_task_under_a_live_goal_is_held(self):
        branch, refused = self.branch(owner=self.GOAL, owner_live=True)
        self.assertEqual(branch, "held-owner-live")
        self.assertEqual(refused, [])

    def test_sc2_a_task_under_an_absent_goal_still_creates(self):
        self.assertEqual(self.branch(owner=self.GOAL, owner_live=False)[0], "create")

    def test_an_unanswerable_owner_probe_creates_never_holds(self):
        # `None` is not `True`: a probe that cannot answer must not suppress work.
        self.assertEqual(self.branch(owner=self.GOAL, owner_live=None)[0], "create")

    def test_the_fleet_sentinel_never_holds(self):
        # Every task with no live goal or topic owner is FLEET-owned; holding on it
        # would suppress the entire batch.
        self.assertEqual(self.branch(owner=fr.FLEET, owner_live=True)[0], "create")

    def test_a_live_task_row_still_jumps_over_the_hold(self):
        self.assertEqual(
            self.branch(live="LIVE", sid="s1", pane="p1",
                        owner=self.GOAL, owner_live=True)[0], "jump")

    def test_a_live_task_row_without_a_pane_still_skips(self):
        self.assertEqual(
            self.branch(live="LIVE", sid="s1", pane=None,
                        owner=self.GOAL, owner_live=True)[0], "skip")

    def test_a_resumable_task_still_resumes_over_the_hold(self):
        self.assertEqual(
            self.branch(live="ABSENT", sid="s1",
                        owner=self.GOAL, owner_live=True)[0], "resume")

    def test_a_human_row_is_direct_not_held(self):
        self.assertEqual(
            self.branch(role="human", owner=self.GOAL, owner_live=True)[0], "direct")

    def test_a_goal_row_is_never_held(self):
        self.assertNotEqual(
            self.branch(kind="goal", owner=self.GOAL, owner_live=True)[0],
            "held-owner-live")

    def test_an_unknown_row_outranks_the_hold(self):
        self.assertEqual(
            self.branch(live="UNKNOWN", owner=self.GOAL, owner_live=True)[0], "unknown")


class DegradedOwner(unittest.TestCase):
    """A registry-alive, heartbeat-degraded row resolves to a NAMED branch, not `unknown`.

    The registry proves the PROCESS exists; the heartbeat proves the SESSION is not
    working. They measure different things, so folding the disagreement into `unknown`
    throws away the owner the same run already resolved. `degraded` never spawns: a
    manager that already exists must not be duplicated, and a live process must not be
    resumed onto a second writer.
    """

    GOAL = {"kind": "goal", "name": "G"}

    def branch(self, **kw):
        return fr.resolve_branch(
            kw.pop("kind", "goal"), kw.pop("role", "agent"), kw.pop("sid", "s1"),
            kw.pop("live", "UNKNOWN"), kw.pop("pane", None),
            kw.pop("chain_live", None), page("role: agent"),
            owner=kw.pop("owner", self.GOAL),
            owner_live=kw.pop("owner_live", None),
            registry_ok=kw.pop("registry_ok", True))

    def test_a_degraded_owned_row_is_named_not_unknown(self):
        branch, refused = self.branch()
        self.assertEqual(branch, "degraded")
        self.assertEqual(refused, [])

    def test_an_ambiguous_or_malformed_row_is_also_degraded(self):
        for verdict in ("AMBIGUOUS", "MALFORMED"):
            self.assertEqual(self.branch(live=verdict)[0], "degraded", verdict)

    def test_a_degraded_row_is_never_resumed(self):
        # The unsafe variant: UNKNOWN -> ABSENT would resume onto a live process.
        self.assertNotEqual(self.branch()[0], "resume")

    def test_a_degraded_row_never_opens_a_manager(self):
        self.assertNotEqual(self.branch(chain_live=True)[0], "open-manager")

    def test_a_degraded_row_never_creates(self):
        self.assertNotEqual(self.branch()[0], "create")

    def test_an_unreadable_registry_still_reads_unknown(self):
        # `registry_ok=False` = the registry probe failed, so no owner is known.
        self.assertEqual(self.branch(registry_ok=False)[0], "unknown")

    def test_a_proven_absent_row_still_resumes(self):
        # Positive control: the degraded branch must not swallow the ABSENT path.
        self.assertEqual(self.branch(live="ABSENT")[0], "resume")

    def test_a_human_row_with_no_verdict_is_direct(self):
        self.assertEqual(self.branch(role="human", live="none")[0], "direct")


class ExitedSessionResumes(unittest.TestCase):
    """An EXITED worker resolves to `resume`, end to end through the real liveness read.

    Claude Code deletes the registry entry when a session exits, so a registry-only
    derivation sees nothing and reads UNKNOWN. The wrapper keeps the exited child in
    its own table and reports it `not-live`, so the consumer reaches ABSENT and the
    row resolves to `resume`. Measured 2026-11-17 under the old UNKNOWN: 21 of 21
    flagged rows carried a session and none resolved to `resume`.
    """

    SID = "4b8275b0-1111-2222-3333-444455556666"

    def probe(self, verdicts):
        resp = unittest.mock.MagicMock()
        resp.status = 200
        resp.read.return_value = json.dumps(verdicts).encode("utf-8")
        resp.__enter__ = lambda s: s
        resp.__exit__ = lambda *a: False
        fr._liveness_cache.update({"read": False, "rows": None})
        with unittest.mock.patch.object(fr.urllib.request, "urlopen", return_value=resp):
            return fr.liveness_of(self.SID)

    def branch(self, live):
        return fr.resolve_branch("task", "agent", self.SID, live, None, None,
                                 page("role: agent"), owner=None, owner_live=None,
                                 registry_ok=True)

    def test_an_exited_session_with_a_not_live_verdict_resolves_to_resume(self):
        live = self.probe([{"session_id": self.SID, "liveness": "not-live"}])
        self.assertEqual(live, "ABSENT")
        self.assertEqual(self.branch(live)[0], "resume")

    def test_a_session_the_endpoint_does_not_list_never_resumes(self):
        # Absent from the wrapper's list is a session it never spawned; it cannot be
        # proven dead, so it must not license a resume.
        live = self.probe([])
        self.assertEqual(live, "UNKNOWN")
        self.assertNotEqual(self.branch(live)[0], "resume")


class RowGate(unittest.TestCase):
    """SC1–SC2 at the ROW level: the emitted row, not just the branch function.

    The facts are seeded through the resolver's own caches — registry names for
    `subject_live` (owner selection) and `liveness_cache` for `subject_liveness`
    (`owner_live`) — so no fixture touches the live fleet.
    """

    def row(self, managers, cache):
        root = tempfile.mkdtemp()
        tasks = os.path.join(root, "25 Tasks")
        path = os.path.join(tasks, "A Task.md")
        write(path, page("flag: true\nflag_set_by: operator\nrole: agent\n"
                         "status: in_progress\ngoals:\n    - '[[G]]'"))
        cfg = {"tasks_dir": "25 Tasks", "path": root,
               "worker_launcher": "/opt/l/cc-v", "claude_script": "/opt/l/cc"}
        ctx = {"today": "2026-11-16", "role_map": ROLE_MAP,
               "template_root": tempfile.mkdtemp(), "no_liveness": False,
               "managers": set(managers), "topic_map": {}, "mode_default": "interactive",
               "normalised": [], "subjects": {}, "liveness_cache": dict(cache),
               "fleet_sid": None}
        row, reason = fr.row_for("v", cfg, "task", path, ctx)
        self.assertIsNone(reason)
        return row

    def test_sc1_the_emitted_row_is_held_and_names_the_goal(self):
        row = self.row({"G"}, {("v", "goal", "G"): True, ("row", None): "none"})
        self.assertEqual(row["branch"], "held-owner-live")
        self.assertEqual(row["owner"], {"kind": "goal", "name": "G"})
        self.assertIs(row["owner_live"], True)

    def test_sc2_the_emitted_row_still_creates_when_no_manager_is_live(self):
        row = self.row(set(), {fr.FLEET_KEY: False, ("row", None): "none"})
        self.assertEqual(row["branch"], "create")


class OwnerResolution(unittest.TestCase):
    """`docs/fleet-surface.md` § Which manager owns a subject — the rungs, in order.

    Liveness is seeded into the cache rather than probed: `subject_liveness` reads it
    first, so these fixtures never touch the live fleet.
    """

    def ctx(self, topic_map=None, managers=()):
        # `no_liveness` is False so `subject_live` reads `managers`; it is an explicit
        # set, so nothing shells out.
        return {"topic_map": topic_map or {}, "subjects": {}, "no_liveness": False,
                "managers": set(managers), "liveness_cache": {}}

    def owner(self, kind, name, block, topic_map=None, managers=()):
        return fr.resolve_owner(kind, name, block, self.ctx(topic_map, managers), "v")

    def test_managed_by_outranks_goals(self):
        block = page("managed_by: '[[A Topic]]'\ngoals:\n    - '[[A Goal]]'")
        self.assertEqual(
            self.owner("task", "T", block, managers={"A Topic", "A Goal"}),
            {"kind": "goal", "name": "A Topic"})

    def test_the_first_goal_with_a_live_manager_wins(self):
        block = page("goals:\n    - '[[First]]'\n    - '[[Second]]'")
        self.assertEqual(self.owner("task", "T", block, managers={"Second"}),
                         {"kind": "goal", "name": "Second"})

    def test_a_dead_first_goal_does_not_win(self):
        block = page("goals:\n    - '[[First]]'\n    - '[[Second]]'")
        self.assertEqual(self.owner("task", "T", block, managers={"Other"}), fr.FLEET)

    def test_a_goal_less_task_falls_to_its_own_topic(self):
        # The task_title rung: named directly in a topic's `## Goals`.
        self.assertEqual(
            self.owner("task", "T", page("status: in_progress"),
                       {"T": "A Topic"}, {"A Topic"}),
            {"kind": "topic", "name": "A Topic"})

    def test_no_rung_falls_to_the_fleet_sentinel(self):
        self.assertEqual(self.owner("task", "T", page("status: in_progress")), fr.FLEET)

    def test_a_goal_owning_a_live_manager_owns_itself(self):
        self.assertEqual(self.owner("goal", "G", page(""), {"G": "A Topic"}, {"G"}),
                         {"kind": "goal", "name": "G"})

    def test_a_managerless_goal_under_a_live_topic_is_owned_by_that_topic(self):
        # Tier 4: the topic manager is ranked to start the goal's manager.
        self.assertEqual(self.owner("goal", "G", page(""), {"G": "A Topic"}, {"A Topic"}),
                         {"kind": "topic", "name": "A Topic"})

    def test_a_managerless_goal_under_a_dead_topic_falls_to_the_fleet(self):
        self.assertEqual(self.owner("goal", "G", page(""), {"G": "A Topic"}), fr.FLEET)

    def test_goal_absent_from_every_index_falls_to_the_fleet(self):
        self.assertEqual(self.owner("goal", "G", page("")), fr.FLEET)

    def test_topic_row_owns_itself_when_its_manager_is_live(self):
        self.assertEqual(self.owner("topic", "T", page(""), managers={"T"}),
                         {"kind": "topic", "name": "T"})

    def test_a_managerless_topic_falls_to_the_fleet_sentinel(self):
        self.assertEqual(self.owner("topic", "T", page("")), fr.FLEET)


class OwnerLiveness(unittest.TestCase):
    """SC9: `true` / `false` when `--check` answers, `null` when it cannot."""

    def ctx(self, live=None, managers=("Fleet Manager",), no_liveness=True, fleet=None):
        cache = dict(live or {})
        if fleet is not None:
            cache[fr.FLEET_KEY] = fleet
        return {"topic_map": {}, "subjects": {}, "no_liveness": no_liveness,
                "managers": set(managers), "liveness_cache": cache, "fleet_sid": None}

    def test_a_live_owner_reads_true(self):
        ctx = self.ctx({("v", "goal", "G"): True})
        self.assertIs(fr.owner_liveness({"kind": "goal", "name": "G"}, "task", "T", ctx, "v")[0],
                      True)

    def test_a_dead_owner_reads_false_not_null(self):
        ctx = self.ctx({("v", "goal", "G"): False})
        self.assertIs(fr.owner_liveness({"kind": "goal", "name": "G"}, "task", "T", ctx, "v")[0],
                      False)

    def test_an_unanswerable_probe_reads_null(self):
        ctx = self.ctx({("v", "goal", "G"): None})
        self.assertIsNone(
            fr.owner_liveness({"kind": "goal", "name": "G"}, "task", "T", ctx, "v")[0])

    def test_the_fleet_sentinel_reads_the_fleet_managers_liveness(self):
        live, chain = fr.owner_liveness(fr.FLEET, "task", "T", self.ctx(fleet=True), "v")
        self.assertIs(live, True)
        self.assertIsNone(chain)

    def test_a_managerless_topic_chains_to_false(self):
        # The fleet owns it, but the ROW's subject has no manager — that is what
        # `open-manager` keys on.
        ctx = self.ctx(managers=("Fleet Manager",), no_liveness=False, fleet=True)
        live, chain = fr.owner_liveness(fr.FLEET, "topic", "T", ctx, "v")
        self.assertIs(live, True)
        self.assertIs(chain, False)

    def test_a_goal_under_a_live_topic_does_not_chain_to_open_manager(self):
        # Tier 4: the topic manager is ranked to start the goal's manager.
        ctx = self.ctx(managers=("A Topic",), no_liveness=False)
        ctx["topic_map"] = {"G": "A Topic"}
        _, chain = fr.owner_liveness(fr.FLEET, "goal", "G", ctx, "v")
        self.assertIs(chain, True)


class Discovery(unittest.TestCase):
    """Vault discovery reads the config; a probe fallback is reported, never silent."""

    def setUp(self):
        self.root = tempfile.mkdtemp()

    def test_declared_key_wins_and_reports_no_fallback(self):
        directory, probe = fr.kind_dir(self.root, {"goals_dir": "23 Goals"},
                                       "goals_dir", ("24 Goals",))
        self.assertTrue(directory.endswith("23 Goals"))
        self.assertIsNone(probe)

    def test_absent_key_probes_and_reports(self):
        os.makedirs(os.path.join(self.root, "23 Goals"))
        directory, probe = fr.kind_dir(self.root, {}, "goals_dir",
                                       ("24 Goals", "23 Goals"))
        self.assertTrue(directory.endswith("23 Goals"))
        self.assertEqual(probe, "23 Goals")

    def test_absent_key_with_no_directory_reports_nothing(self):
        directory, probe = fr.kind_dir(self.root, {}, "goals_dir", ("24 Goals",))
        self.assertIsNone(directory)
        self.assertIsNone(probe)


class TopicIndex(unittest.TestCase):
    """The relation is declared on the TOPIC page, never the goal page."""

    def test_goals_section_maps_goals_to_their_topic(self):
        root = tempfile.mkdtemp()
        write(os.path.join(root, "A Topic.md"),
              page("page_type: topic",
                   "\n## Goals\n\n- [[A Goal]] — why\n- [[Another Goal]]\n\n## Non-goals\n"))
        index = fr.topic_index(root)
        self.assertEqual(index.get("A Goal"), "A Topic")
        self.assertEqual(index.get("Another Goal"), "A Topic")

    def test_the_section_is_bounded_at_the_next_heading(self):
        # A link under `## Non-goals` is not a membership — the index stops at the
        # next heading of the same or higher level.
        root = tempfile.mkdtemp()
        write(os.path.join(root, "A Topic.md"),
              page("page_type: topic",
                   "\n## Goals\n\n- [[A Goal]]\n\n## Non-goals\n\n- [[Not A Goal]]\n"))
        index = fr.topic_index(root)
        self.assertEqual(index.get("A Goal"), "A Topic")
        self.assertNotIn("Not A Goal", index)

    def test_an_unlisted_goal_has_no_topic_membership(self):
        # A goal whose own `topics:` names this topic but which the topic page does
        # NOT list is not a member — only the `## Goals` index predicts a manager.
        root = tempfile.mkdtemp()
        write(os.path.join(root, "A Topic.md"),
              page("page_type: topic", "\n## Goals\n\n- [[A Goal]]\n"))
        self.assertIsNone(fr.topic_index(root).get("An Unlisted Goal"))


class LauncherResolution(unittest.TestCase):
    """`/supervisor:open`'s rule, both arms — never a bare frontmatter copy."""

    def cfg(self, **keys):
        base = {"worker_launcher": "/opt/l/cc-v", "manager_launcher": "/opt/l/cc-m",
                "claude_script": "/opt/l/cc"}
        base.update(keys)
        return {k: v for k, v in base.items() if v is not None}

    def test_a_tasks_own_launcher_beats_the_worker_default(self):
        self.assertEqual(
            fr.resolve_launcher("task", "agent", page("launcher: /srv/cc-x"), self.cfg()),
            ("/srv/cc-x", "task"))

    def test_a_bare_name_resolves_beside_the_vault_launcher(self):
        # fleet-surface: "A bare name resolves beside the vault's own launcher."
        self.assertEqual(fr.resolve_launcher("task", "agent", page("launcher: cc-x"), self.cfg()),
                         ("/opt/l/cc-x", "task"))

    def test_an_absolute_path_is_used_as_given(self):
        self.assertEqual(
            fr.resolve_launcher("task", "agent", page("launcher: /srv/cc-x"), self.cfg()),
            ("/srv/cc-x", "task"))

    def test_no_override_falls_to_the_worker_default(self):
        self.assertEqual(fr.resolve_launcher("task", "agent", page("status: x"), self.cfg()),
                         ("/opt/l/cc-v", "worker-default"))

    def test_a_vault_with_no_worker_default_reports_vault(self):
        self.assertEqual(
            fr.resolve_launcher("task", "agent", page("status: x"),
                                self.cfg(worker_launcher=None)),
            ("/opt/l/cc", "vault"))

    def test_a_goal_uses_the_manager_arm_even_though_it_is_a_goal(self):
        # A goal states how its MANAGER runs; it is never a worker launcher.
        self.assertEqual(fr.resolve_launcher("goal", "agent", page("status: x"), self.cfg()),
                         ("/opt/l/cc-m", "manager-default"))

    def test_a_manager_row_uses_the_manager_arm(self):
        self.assertEqual(fr.resolve_launcher("task", "manager", page("status: x"), self.cfg()),
                         ("/opt/l/cc-m", "manager-default"))

    def test_a_bare_manager_override_resolves_beside_the_manager_launcher(self):
        # Not beside the WORKER launcher — different directory, different binary.
        cfg = {"worker_launcher": "/opt/w/cc-v", "manager_launcher": "/opt/m/cc-m",
               "claude_script": "/opt/l/cc"}
        self.assertEqual(
            fr.resolve_launcher("goal", "manager", page("manager_launcher: cc-z"), cfg),
            ("/opt/m/cc-z", "subject"))

    def test_a_subject_override_outranks_the_configured_manager_launcher(self):
        self.assertEqual(
            fr.resolve_launcher("goal", "manager", page("manager_launcher: cc-z"), self.cfg()),
            ("/opt/l/cc-z", "subject"))

    def test_a_workers_own_launcher_is_ignored_on_the_manager_arm(self):
        # `launcher:` is a worker field. A goal carrying one is not a manager override.
        self.assertEqual(
            fr.resolve_launcher("goal", "manager", page("launcher: cc-x"), self.cfg()),
            ("/opt/l/cc-m", "manager-default"))

    def test_nothing_configured_is_unresolved_not_a_guess(self):
        self.assertEqual(
            fr.resolve_launcher("task", "agent", page("status: x"),
                                {"worker_launcher": None, "claude_script": None}),
            (None, "unresolved"))


class ModeAndRoleNormalisation(unittest.TestCase):
    """A value the fleet cannot honour is normalised and REPORTED, never copied."""

    def test_an_absent_mode_is_not_an_anomaly(self):
        # open.md line 259: a row with no `mode:` opens per fleet config.
        self.assertEqual(fr.resolve_mode(page("status: x"), "interactive"),
                         ("interactive", None))

    def test_an_on_disk_mode_is_kept(self):
        self.assertEqual(fr.resolve_mode(page("mode: headless"), "interactive"),
                         ("headless", None))

    def test_non_interactive_is_normalised_and_reported(self):
        # Live on eight pages; not a value `server/spawn-mode.mjs` accepts.
        self.assertEqual(fr.resolve_mode(page("mode: non_interactive"), "interactive"),
                         ("interactive", "non_interactive"))

    def test_a_known_role_is_kept(self):
        self.assertEqual(fr.normalise_role("human"), ("human", None))

    def test_an_unknown_role_normalises_to_agent_and_reports(self):
        self.assertEqual(fr.normalise_role("robot"), ("agent", "robot"))

    def test_an_unresolvable_launcher_skips_the_row(self):
        root = tempfile.mkdtemp()
        tasks = os.path.join(root, "25 Tasks")
        write(os.path.join(tasks, "A Task.md"),
              page("page_type: task\nflag: true\nstatus: in_progress"))
        ctx = {"today": "2026-10-31", "role_map": {}, "template_root": None,
               "no_liveness": True, "managers": set(), "topic_map": {},
               "mode_default": "interactive", "normalised": [],
               "subjects": {}, "liveness_cache": {}}
        row, reason = fr.row_for("v", {"tasks_dir": "25 Tasks", "path": root}, "task",
                                 os.path.join(tasks, "A Task.md"), ctx)
        self.assertIsNone(row)
        self.assertEqual(reason, "no launcher resolved")


class StartDayRow(unittest.TestCase):
    """One row cloned from the real `Start Day - 2026-10-31` page.

    The only fixture that exercises template + on-disk + role + mode together: a
    `# Source` footer naming the schedule YAML, an on-disk `role: human` that AGREES
    with it, and `mode: interactive` on disk. The live page is `status: completed`, so
    the fixture flips only that — everything else is the real shape.
    """

    #: The live page's frontmatter, minus the fields that make it ineligible.
    FRONT = """approved_by: operator
assignee: bborbe
category: deep-work
flag: true
flag_set_by: operator
launcher: cc-private-claude
mode: interactive
page_type: task
phase: execution
priority: 1
role: human
status: in_progress"""

    FOOTER = ("\nGenerated by [[recurring-task-creator]] from "
              "`bborbe/nuke/task/recurring-schedules/prod/start-day.yaml`.\n")

    def row(self):
        root = tempfile.mkdtemp()
        templates = tempfile.mkdtemp()
        # The real shape: `frontmatter:` is INDENTED under `spec.template`.
        write(os.path.join(templates, "start-day.yaml"),
              "spec:\n  title: Start Day\n  template:\n    body: |-\n      hello\n"
              "    frontmatter:\n      launcher: cc-private-claude\n"
              "      mode: interactive\n      # Human-only: the operator drives this.\n"
              "      role: human\n      priority: 1\n")
        tasks = os.path.join(root, "25 Tasks")
        page_path = os.path.join(tasks, "Start Day - 2026-10-31.md")
        write(page_path, page(self.FRONT, self.FOOTER))
        cfg = {"tasks_dir": "25 Tasks", "path": root,
               "worker_launcher": "/opt/l/cc-v", "claude_script": "/opt/l/cc"}
        ctx = {"today": "2026-10-31", "role_map": ROLE_MAP, "template_root": templates,
               "no_liveness": True, "managers": set(), "topic_map": {},
               "mode_default": "interactive", "normalised": [],
               "subjects": {}, "liveness_cache": {}}
        row, reason = fr.row_for("v", cfg, "task", page_path, ctx)
        self.assertIsNone(reason)
        return row

    def test_the_row_reads_template_role_and_on_disk_mode_together(self):
        row = self.row()
        self.assertEqual(row["role"], "human")
        self.assertEqual(row["role_source"], "template")
        self.assertEqual(row["mode"], "interactive")

    def test_a_human_row_routes_to_the_direct_window_with_the_cyan_chip(self):
        row = self.row()
        self.assertEqual(row["window"], "Direct")
        self.assertEqual(row["chip"], "cyan")
        self.assertEqual(row["branch"], "direct")

    def test_the_tasks_own_launcher_wins_over_the_worker_default(self):
        row = self.row()
        self.assertEqual(row["launcher"], "/opt/l/cc-private-claude")
        self.assertEqual(row["launcher_source"], "task")

    def test_the_row_is_eligible_and_unrefused(self):
        row = self.row()
        self.assertEqual(row["name"], "Start Day - 2026-10-31")
        self.assertEqual(row["blocked_by"], [])
        self.assertEqual(row["refused"], [])


class RowShape(unittest.TestCase):
    """Every row carries all 19 fields, and none is silently absent."""

    FIELDS = ("kind", "vault", "owner", "owner_live", "name", "role", "role_source",
              "mode", "launcher", "launcher_source", "window", "window_id", "chip",
              "session_id", "liveness", "branch", "pane", "blocked_by", "refused")

    def test_a_row_carries_all_nineteen_fields(self):
        root = tempfile.mkdtemp()
        tasks = os.path.join(root, "25 Tasks")
        write(os.path.join(tasks, "A Task.md"),
              page("page_type: task\nflag: true\nstatus: in_progress\nrole: agent"))
        cfg = {"tasks_dir": "25 Tasks", "path": root, "worker_launcher": "/opt/l/cc-v"}
        ctx = {"today": "2026-10-31", "role_map": {}, "template_root": None,
               "no_liveness": True, "managers": set(), "topic_map": {},
               "mode_default": "interactive", "normalised": [],
               "subjects": {}, "liveness_cache": {}}
        row, reason = fr.row_for("v", cfg, "task", os.path.join(tasks, "A Task.md"), ctx)
        self.assertIsNone(reason)
        self.assertEqual(set(row), set(self.FIELDS))
        self.assertEqual(row["name"], "A Task")
        self.assertEqual(row["kind"], "task")
        self.assertEqual(row["launcher"], "/opt/l/cc-v")
        self.assertEqual(row["launcher_source"], "worker-default")
        self.assertEqual(row["mode"], "interactive")


class WindowIdIsNeverStale(unittest.TestCase):
    """SC7: the map is read on every run, so a rewrite shows up in the next row."""

    def row_with_map(self, role_map_path):
        root = tempfile.mkdtemp()
        tasks = os.path.join(root, "25 Tasks")
        write(os.path.join(tasks, "A Task.md"),
              page("page_type: task\nflag: true\nstatus: in_progress\n"
                   "role: agent\nflag_set_by: operator"))
        cfg = {"tasks_dir": "25 Tasks", "path": root, "worker_launcher": "/opt/l/cc-v"}
        ctx = {"today": "2026-11-01", "role_map": fr.role_map(role_map_path),
               "template_root": None, "no_liveness": True, "managers": set(),
               "topic_map": {}, "mode_default": "interactive", "normalised": [],
               "subjects": {}, "liveness_cache": {}}
        row, reason = fr.row_for("v", cfg, "task", os.path.join(tasks, "A Task.md"), ctx)
        self.assertIsNone(reason)
        return row

    def test_a_rewritten_map_reaches_the_next_row(self):
        path = os.path.join(tempfile.mkdtemp(), "role-map.json")
        write(path, json.dumps({"Agents": {"chip": "pink", "window_id": 4}}))
        self.assertEqual(self.row_with_map(path)["window_id"], 4)
        write(path, json.dumps({"Agents": {"chip": "red", "window_id": 9}}))
        row = self.row_with_map(path)
        self.assertEqual(row["window_id"], 9)
        self.assertEqual(row["chip"], "red")

    def test_a_missing_map_falls_back_to_the_documented_chip(self):
        row = self.row_with_map(os.path.join(tempfile.mkdtemp(), "absent.json"))
        self.assertIsNone(row["window_id"])
        self.assertEqual(row["chip"], fr.MISSING_MAP_CHIP)


class PerKindRefusal(unittest.TestCase):
    """The provenance rule is UNIFORM across the three subjects (operator ruling 2026-11-10).

    Before that ruling a task refused an agent-set flag while a goal or topic accepted any
    provenance — the split these tests used to pin. It is now one rule: an agent-set or
    absent `flag_set_by` opens nothing at any tier.
    """

    def test_an_agent_set_task_is_refused(self):
        self.assertEqual(
            fr.refusals("task", page("flag: true\nflag_set_by: agent")),
            ["agent-set-flag"])

    def test_an_absent_flag_set_by_refuses_a_task(self):
        # After the 2026-09-30 legacy disposition no live row should be missing it.
        self.assertEqual(
            fr.refusals("task", page("flag: true", provenance=None)),
            ["agent-set-flag"])

    def test_operator_and_legacy_open_a_task(self):
        for actor in ("operator", "legacy"):
            self.assertEqual(
                fr.refusals("task", page("flag: true\nflag_set_by: %s" % actor)), [])

    def test_a_goal_and_a_topic_refuse_an_agent_set_flag_too(self):
        self.assertEqual(
            fr.refusals("goal", page("flag: 'true'\nflag_set_by: agent")),
            ["agent-set-flag"])
        self.assertEqual(
            fr.refusals("topic", page("flag: on\nflag_set_by: agent")),
            ["agent-set-flag"])

    def test_a_goal_and_a_topic_open_an_operator_set_flag(self):
        self.assertEqual(fr.refusals("goal", page("flag: 'true'")), [])
        self.assertEqual(fr.refusals("topic", page("flag: on")), [])

    def test_an_agent_set_flag_reaches_the_consumer_as_a_refusal_not_a_skip(self):
        """⚠️ The regression every other test in this class missed: they call
        `refusals()` in isolation, so a gate that swallowed the case stayed green.

        `row_for()` builds the row for a SET flag and lets `refusals()` label it. A gate
        answering `not flagged` for an agent-set flag returned a skip reason instead, so
        the `REFUSED | … | flag_set_by=<who>` line vanished and an agent-set flag read as
        an unflagged row — the mislabel `flag_provenance.flag_refusal()` exists to stop,
        and the token SC2's evidence is read from.
        """
        root = tempfile.mkdtemp()
        tasks = os.path.join(root, "25 Tasks")
        write(os.path.join(tasks, "A Task.md"),
              page("page_type: task\nflag: true\nstatus: in_progress\nrole: agent",
                   provenance="agent"))
        cfg = {"tasks_dir": "25 Tasks", "path": root, "worker_launcher": "/opt/l/cc-v"}
        ctx = {"today": "2026-11-12", "role_map": {}, "template_root": None,
               "no_liveness": True, "managers": set(), "topic_map": {},
               "mode_default": "interactive", "normalised": [],
               "subjects": {}, "liveness_cache": {}}
        row, reason = fr.row_for("v", cfg, "task", os.path.join(tasks, "A Task.md"), ctx)
        self.assertIsNone(reason, "a SET flag must not be reported as a skip")
        self.assertEqual(row["refused"], ["agent-set-flag"])

    def test_the_two_tokens_can_coexist_on_one_row(self):
        # Provenance and the manager-row rule are independent producers.
        ctx = {"topic_map": {}, "subjects": {}, "no_liveness": False,
               "managers": {"X"}, "liveness_cache": {}}
        block = page("flag: true\nflag_set_by: agent\nrole: manager")
        branch, refused = fr.resolve_branch("task", "manager", None, "LIVE", "9",
                                            True, block)
        self.assertEqual(branch, "skip")
        self.assertEqual(fr.refusals("task", block) + refused,
                         ["agent-set-flag", "manager-row"])


class BlockedBy(unittest.TestCase):
    """SC4: an open blocker is emitted; an empty one is `[]`, never null."""

    def test_an_inline_blocker_is_emitted(self):
        self.assertEqual(fr.blocked_by_of(page("blocked_by: '[[A Blocker]]'")),
                         ["[[A Blocker]]"])

    def test_an_empty_inline_list_is_empty(self):
        self.assertEqual(fr.blocked_by_of(page("blocked_by: []")), [])

    def test_a_list_blocker_is_emitted(self):
        self.assertEqual(
            fr.blocked_by_of(page("blocked_by:\n    - '[[One]]'\n    - '[[Two]]'")),
            ["[[One]]", "[[Two]]"])

    def test_an_absent_field_is_empty(self):
        self.assertEqual(fr.blocked_by_of(page("flag: true")), [])

    def test_a_completed_blocker_is_resolved_and_an_open_or_missing_one_stays(self):
        # 2026-11-15: every listed blocker counted forever, so a flagged task whose
        # prerequisites had all completed was skipped on every tick.
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d)
        for name, status in (("Done One", "completed"), ("Open One", "in_progress")):
            with open(os.path.join(d, name + ".md"), "w", encoding="utf-8") as h:
                h.write("---\nstatus: %s\n---\n" % status)
        block = page("blocked_by:\n    - '[[Done One]]'\n    - '[[Open One]]'\n    - '[[Missing One]]'")
        self.assertEqual(fr.blocked_by_of(block, d), ["[[Open One]]", "[[Missing One]]"])
        self.assertEqual(fr.blocked_by_of(page("blocked_by: '[[done one]]'"), d), [])


class RegistryProbe(unittest.TestCase):
    """A failed endpoint read is not an empty fleet, and a `⚙ `-prefixed label is not absent.

    Both defects produce the same wrong branch — `open-manager` against a live manager
    — which is a duplicate-spawn hazard, so each direction is pinned separately.
    """

    def _endpoint(self, body):
        """Patch the wrapper's liveness route with a stub serving `body`."""
        resp = unittest.mock.MagicMock()
        resp.status = 200
        resp.read.return_value = json.dumps(body).encode("utf-8")
        resp.__enter__ = lambda s: s
        resp.__exit__ = lambda *a: False
        return unittest.mock.patch.object(
            fr.urllib.request, "urlopen", return_value=resp)

    def _reset_cache(self):
        fr._liveness_cache.update({"read": False, "rows": None})

    def setUp(self):
        self._reset_cache()

    def test_a_failed_read_returns_none_not_an_empty_set(self):
        with unittest.mock.patch.object(
                fr.urllib.request, "urlopen", side_effect=fr.urllib.error.URLError("x")):
            self.assertIsNone(fr.live_sessions())

    def test_a_successful_empty_read_returns_an_empty_dict(self):
        with self._endpoint([]):
            self.assertEqual(fr.live_sessions(), {})

    def test_a_live_verdict_carries_its_name(self):
        with self._endpoint([
            {"session_id": "sid1", "liveness": "live", "name": "A Goal Manager"},
            {"session_id": "sid2", "liveness": "live", "name": "A Topic Manager"},
            {"session_id": "sid3", "liveness": "not-live", "name": "Dead"},
        ]):
            self.assertEqual(fr.live_sessions(),
                             {"sid1": "A Goal Manager", "sid2": "A Topic Manager"})

    def test_a_glyph_prefixed_manager_name_matches_its_subject(self):
        names = fr.live_session_names({"s": "⚙ A Goal Manager"})
        self.assertIn("A Goal", names)
        self.assertIn("A Goal Manager", names)

    def test_an_unreadable_registry_withholds_open_manager(self):
        ctx = {"topic_map": {}, "subjects": {}, "no_liveness": False,
               "managers": None, "liveness_cache": {}}
        # Selection keeps the subject as owner rather than collapsing to the fleet.
        self.assertEqual(fr.resolve_owner("goal", "G", page(""), ctx, "v"),
                         {"kind": "goal", "name": "G"})
        self.assertIsNone(fr.subject_live(ctx, "v", "goal", "G"))
        # And the branch does not open a second manager off an unanswerable probe.
        self.assertNotEqual(fr.resolve_branch("goal", "agent", None, "ABSENT", None,
                                              None, page(""))[0], "open-manager")

    def test_an_unreadable_registry_cannot_fire_open_manager(self):
        self.assertNotEqual(fr.resolve_branch("topic", "agent", None, "ABSENT", None,
                                              None, page(""))[0], "open-manager")


class TaskFlagScalar(unittest.TestCase):
    """The task predicate mirrors a YAML parse, not the literal lowercase token."""

    def test_unquoted_yaml_booleans_are_admitted(self):
        for scalar in ("true", "True", "TRUE", "yes", "on", "On"):
            self.assertTrue(fr.flag_admits(page("flag: %s" % scalar), "task"), scalar)

    def test_quoted_scalars_are_strings_and_stay_rejected(self):
        for scalar in ('"true"', "'true'", '"yes"'):
            self.assertFalse(fr.flag_admits(page("flag: %s" % scalar), "task"), scalar)

    def test_a_non_boolean_scalar_stays_rejected(self):
        self.assertFalse(fr.flag_admits(page("flag: 1"), "task"))


class PaneBatch(unittest.TestCase):
    """Pane resolution is batched: one registry read and one pane list per run.

    Measured 2026-11-16: `pane_of` spawned a fresh interpreter per session id — 17
    rows, 40.7 s of a 51 s run — because each spawn importlib-loaded
    `who-needs-me.py` only to call `pane_for`, which itself re-reads the registry and
    `wezterm cli list`. Both halves are gone: the module loads in-process once, and
    the three inputs are read once and shared.
    """

    SID = "11111111-aaaa-4aaa-8aaa-aaaaaaaaaaaa"

    def setUp(self):
        self.registry = {self.SID: "Session A"}
        self.panes = {"204": {"pane_id": 204, "title": "✳ Session A"}}
        self.records = [{"session_id": self.SID, "pane": "204", "kind": "idle", "ts": 1}]
        self.reads = {"registry": 0, "panes": 0, "records": 0}
        self.wnm = fr._who_needs_me()

    def fake(self):
        test = self

        class Fake:
            def read_registry(_self):
                test.reads["registry"] += 1
                return None if test.registry is None else dict(test.registry)

            def wezterm_panes(_self):
                test.reads["panes"] += 1
                return None if test.panes is None else dict(test.panes)

            def load(_self, _suffix):
                test.reads["records"] += 1
                return list(test.records)

            def pane_for_maps(_self, sid, registry, pmap, records):
                return test.wnm.pane_for_maps(sid, registry, pmap, records)

        return Fake()

    def test_the_three_inputs_are_read_once_per_run(self):
        """Three DIFFERENT sids still cost one read each — batching, not caching."""
        ctx = {"liveness_cache": {}}
        with unittest.mock.patch.object(fr, "_who_needs_me", self.fake):
            for sid in (self.SID[:4], self.SID[:8], self.SID):
                self.assertEqual("204", fr.pane_of(ctx, sid))
        self.assertEqual({"registry": 1, "panes": 1, "records": 1}, self.reads)

    def test_no_interpreter_is_spawned_for_a_pane(self):
        with unittest.mock.patch.object(fr.subprocess, "run") as run, \
             unittest.mock.patch.object(fr, "_who_needs_me", self.fake):
            self.assertEqual("204", fr.pane_of({"liveness_cache": {}}, self.SID))
        run.assert_not_called()

    def test_a_repeated_session_id_reads_nothing_again(self):
        ctx = {"liveness_cache": {}}
        with unittest.mock.patch.object(fr, "_who_needs_me", self.fake):
            self.assertEqual("204", fr.pane_of(ctx, self.SID))
            self.assertEqual("204", fr.pane_of(ctx, self.SID))
        self.assertEqual(1, self.reads["panes"])


class PaneRefusals(unittest.TestCase):
    """Every `pane_for` refusal survives the batch path, text included.

    `pane_of` delegates to `pane_for_maps`, which `pane_for` itself wraps, so these
    assert the CLI's own reason strings rather than a second copy of them. A refusal
    that silently became a `None` pane would read to the operator as "no pane", which
    is the dead end `pane_for`'s docstring exists to prevent.
    """

    SID = "11111111-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    OTHER = "11111111-bbbb-4bbb-8bbb-bbbbbbbbbbbb"

    def setUp(self):
        self.wnm = fr._who_needs_me()
        self.registry = {self.SID: "Session A"}
        self.panes = {"204": {"pane_id": 204, "title": "✳ Session A"}}
        self.records = [{"session_id": self.SID, "pane": "204", "kind": "idle", "ts": 1}]

    def resolve(self, sid, registry="same", pmap="same"):
        return self.wnm.pane_for_maps(
            sid,
            self.registry if registry == "same" else registry,
            self.panes if pmap == "same" else pmap,
            self.records)

    def test_an_empty_session_id(self):
        self.assertEqual((None, "pane-for: no session id given"), self.resolve(""))

    def test_an_unreadable_registry(self):
        pane, reason = self.resolve(self.SID, registry=None)
        self.assertIsNone(pane)
        self.assertEqual(
            "pane-for: session registry unreadable — cannot decide liveness", reason)

    def test_an_unknown_session_id(self):
        self.assertEqual(
            (None, "pane-for: no live session id matches deadbeef"), self.resolve("deadbeef"))

    def test_an_ambiguous_prefix(self):
        self.registry[self.OTHER] = "Session B"
        pane, reason = self.resolve(self.SID[:8])
        self.assertIsNone(pane)
        self.assertEqual(
            "pane-for: 2 live session ids match 11111111 — pass the full id", reason)

    def test_an_unreadable_pane_list(self):
        pane, reason = self.resolve(self.SID, pmap=None)
        self.assertIsNone(pane)
        self.assertTrue(reason.startswith("pane-for: "), reason)
        self.assertNotIn("None", reason)

    def test_a_nameless_session_with_no_recorded_pane(self):
        self.registry[self.SID] = ""
        self.records[0]["pane"] = None
        pane, reason = self.resolve(self.SID)
        self.assertIsNone(pane)
        self.assertEqual(
            "pane-for: session 11111111 is live but carries no name, and its recorded "
            "pane (none) is gone", reason)

    def test_no_title_matches(self):
        self.records[0]["pane"] = "85"
        self.panes = {"205": {"pane_id": 205, "title": "✳ Other"}}
        pane, reason = self.resolve(self.SID)
        self.assertIsNone(pane)
        self.assertEqual(
            "pane-for: session 11111111 is live but no pane resolves — recorded pane 85 "
            "is gone and no WezTerm pane is titled 'Session A'", reason)

    def test_two_panes_sharing_a_title(self):
        self.records[0]["pane"] = "85"
        self.panes = {
            "204": {"pane_id": 204, "title": "✳ Session A"},
            "205": {"pane_id": 205, "title": "✳ Session A"},
        }
        pane, reason = self.resolve(self.SID)
        self.assertIsNone(pane)
        self.assertEqual(
            "pane-for: session 11111111 is live but 2 WezTerm panes are titled "
            "'Session A' — ambiguous", reason)

    def test_a_stale_recorded_pane_falls_back_to_the_current_name(self):
        self.records[0]["pane"] = "85"
        self.panes = {"1391": {"pane_id": 1391, "title": "✳ Session A"}}
        self.assertEqual(("1391", None), self.resolve(self.SID))


class SubjectLivenessPrecedence(unittest.TestCase):
    """`subject_liveness` feeds `owner_live`, so its read order is load-bearing."""

    def test_a_mixed_page_resolves_claude_session_id_first(self):
        # A re-armed legacy page carries both keys; the surviving one is the live
        # manager. Reading the retired key first would probe the stale id and mark
        # a healthy subject's owner dead.
        with tempfile.TemporaryDirectory() as tmp:
            page = os.path.join(tmp, "A Goal.md")
            with open(page, "w", encoding="utf-8") as fh:
                fh.write(
                    "---\nmanager_session_id: old-sid\nclaude_session_id: new-sid\n---\n"
                )
            ctx = {"liveness_cache": {}, "no_liveness": False}
            seen = []

            def live(sid):
                seen.append(sid)
                return "LIVE"

            with unittest.mock.patch.object(fr, "subject_page", return_value=page), \
                 unittest.mock.patch.object(fr, "liveness_of", side_effect=live):
                verdict = fr.subject_liveness(ctx, "/v", "goal", "A Goal")

        self.assertEqual(["new-sid"], seen)
        self.assertTrue(verdict)

    def test_a_page_carrying_only_the_retired_key_reads_as_no_recorded_manager(self):
        # The retired key is gone from every page in both vaults and nothing writes it,
        # so a page carrying only that key has no recorded manager — and probing it
        # would resurrect an id no writer produces.
        with tempfile.TemporaryDirectory() as tmp:
            page = os.path.join(tmp, "Old Goal.md")
            with open(page, "w", encoding="utf-8") as fh:
                fh.write("---\nmanager_session_id: old-sid\n---\n")
            ctx = {"liveness_cache": {}, "no_liveness": False}
            seen = []

            def live(sid):
                seen.append(sid)
                return "LIVE"

            with unittest.mock.patch.object(fr, "subject_page", return_value=page), \
                 unittest.mock.patch.object(fr, "liveness_of", side_effect=live):
                verdict = fr.subject_liveness(ctx, "/v", "goal", "Old Goal")

        self.assertEqual([], seen)
        self.assertFalse(verdict)

    def test_the_row_builder_ignores_the_retired_key_too(self):
        # `row_for` carried the second copy of the removed fallback. Pinning it here keeps
        # the two sites from drifting apart again — the pair disagreeing on one page is
        # what the pre-migration review caught.
        root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        tasks = os.path.join(root, "25 Tasks")
        write(os.path.join(tasks, "Old Task.md"),
              page("page_type: task\nflag: true\nstatus: in_progress\nrole: agent\n"
                   "manager_session_id: old-sid"))
        cfg = {"tasks_dir": "25 Tasks", "path": root, "worker_launcher": "/opt/l/cc-v"}
        ctx = {"today": "2026-10-31", "role_map": {}, "template_root": None,
               "no_liveness": True, "managers": set(), "topic_map": {},
               "mode_default": "interactive", "normalised": [],
               "subjects": {}, "liveness_cache": {}}
        row, reason = fr.row_for("v", cfg, "task", os.path.join(tasks, "Old Task.md"), ctx)
        self.assertIsNone(reason)
        self.assertIsNone(row["session_id"])


class ProbeTimeoutTest(unittest.TestCase):
    """A helper subprocess that never answers must cost one row's field, not the tick.

    Measured 2026-11-16: an unbounded `pane_for` helper held the predicate in `poll()`
    for 11m49s until the daemon's 25-minute bound SIGKILLed it, and the tick opened
    nothing. The liveness read is now one bounded endpoint call per run
    (`LIVENESS_TIMEOUT`), and an unreadable endpoint reads UNKNOWN.
    """

    SID = "616a52dd-833b-475b-889f-ee1580c73c37"

    def test_a_hung_liveness_probe_reads_unknown(self):
        with unittest.mock.patch.object(fr, "read_liveness", return_value=None):
            self.assertEqual(fr.liveness_of(self.SID), "UNKNOWN")

    def test_a_hung_pane_list_reads_no_pane_and_is_read_once(self):
        """A wedged mux now costs one read per run, not one spawn per row.

        The per-sid spawn is gone, so the bound lives in `wezterm_panes()`'s own
        PANE_LIST_TIMEOUT/retry. What this pins is the consequence: an unreadable
        pane list yields no pane for every row, and is read ONCE for the run.
        """
        ctx = {"liveness_cache": {}}
        wnm = fr._who_needs_me()
        reads = []
        sid = self.SID

        class Fake:
            def read_registry(_self):
                return {sid: "Session A"}

            def wezterm_panes(_self):
                reads.append(1)
                return None

            def load(_self, _suffix):
                return []

            def pane_for_maps(_self, session_id, registry, pmap, records):
                return wnm.pane_for_maps(session_id, registry, pmap, records)

        with unittest.mock.patch.object(fr, "_who_needs_me", Fake):
            self.assertIsNone(fr.pane_of(ctx, self.SID))
            self.assertIsNone(fr.pane_of(ctx, self.SID))
        self.assertEqual(1, len(reads))

    def _live_row(self, no_pane):
        root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        tasks = os.path.join(root, "25 Tasks")
        write(os.path.join(tasks, "Live Task.md"),
              page("page_type: task\nflag: true\nstatus: in_progress\nrole: agent\n"
                   "claude_session_id: %s" % self.SID))
        cfg = {"tasks_dir": "25 Tasks", "path": root, "worker_launcher": "/opt/l/cc-v"}
        ctx = {"today": "2026-10-31", "role_map": {}, "template_root": None,
               "no_liveness": False, "no_pane": no_pane, "managers": set(),
               "topic_map": {}, "mode_default": "interactive", "normalised": [],
               "subjects": {}, "liveness_cache": {}, "fleet_sid": None}
        with unittest.mock.patch.object(fr, "liveness_of", return_value="LIVE"), \
             unittest.mock.patch.object(fr, "pane_of", return_value="42") as pane_of:
            row, reason = fr.row_for("v", cfg, "task",
                                     os.path.join(tasks, "Live Task.md"), ctx)
        self.assertIsNone(reason)
        return row, pane_of

    def test_no_pane_never_probes_a_live_rows_pane(self):
        # The daemon passes --no-pane: its tick skips every non-ABSENT row before it
        # reads `jump`, so the probe is pure cost — and it is the helper that hung.
        row, pane_of = self._live_row(no_pane=True)
        pane_of.assert_not_called()
        self.assertIsNone(row["pane"])
        self.assertEqual(row["liveness"], "LIVE")
        self.assertEqual(row["branch"], "skip")

    def test_without_no_pane_a_live_row_still_resolves_its_pane(self):
        # /supervisor:open still needs the pane to jump; the default must not change.
        row, pane_of = self._live_row(no_pane=False)
        pane_of.assert_called_once()
        self.assertEqual(row["pane"], "42")
        self.assertEqual(row["branch"], "jump")

    def test_the_bound_is_far_below_the_daemon_read_bound(self):
        # The daemon kills a read at 25 minutes. A per-read bound near that would let
        # one slow endpoint reproduce the very kill this guard exists to stop.
        self.assertLessEqual(fr.LIVENESS_TIMEOUT, 60)


class ParallelProbeTest(unittest.TestCase):
    """One run's liveness probes: each distinct sid once, concurrently, under one deadline."""

    SID = "616a52dd-833b-475b-889f-ee1580c73c37"

    def test_each_distinct_sid_is_probed_exactly_once(self):
        seen = []

        def live(sid):
            seen.append(sid)
            return "LIVE"

        # The same sid as row, owner and subject reaches the pre-pass three times.
        with unittest.mock.patch.object(fr, "liveness_of", side_effect=live):
            out = fr.probe_all(["a", "a", "b", None, "a", "b"])
        self.assertEqual(["a", "b"], sorted(seen))
        self.assertEqual({"a": "LIVE", "b": "LIVE"}, out)

    def test_row_owner_and_subject_read_the_shared_result_without_probing(self):
        ctx = {"liveness_cache": {}, "no_liveness": False, "fleet_sid": self.SID,
               "sid_verdicts": {self.SID: "ABSENT"}}
        with tempfile.TemporaryDirectory() as tmp:
            page_path = os.path.join(tmp, "A Goal.md")
            write(page_path, "---\nclaude_session_id: %s\n---\n" % self.SID)
            with unittest.mock.patch.object(fr, "liveness_of",
                                            side_effect=AssertionError("probed")), \
                 unittest.mock.patch.object(fr, "subject_page", return_value=page_path):
                self.assertEqual("ABSENT", fr.row_liveness(ctx, self.SID))
                self.assertFalse(fr.subject_liveness(ctx, "v", "goal", "A Goal"))
                self.assertFalse(fr.fleet_liveness(ctx))

    def test_probes_overlap_in_time_and_respect_the_worker_bound(self):
        import threading
        import time
        lock = threading.Lock()
        state = {"now": 0, "peak": 0}

        def live(sid):
            with lock:
                state["now"] += 1
                state["peak"] = max(state["peak"], state["now"])
            time.sleep(0.05)
            with lock:
                state["now"] -= 1
            return "LIVE"

        with unittest.mock.patch.object(fr, "liveness_of", side_effect=live):
            out = fr.probe_all(["s%d" % i for i in range(20)], workers=4)
        self.assertEqual(20, len(out))
        self.assertGreater(state["peak"], 1)
        self.assertLessEqual(state["peak"], 4)

    def test_the_total_deadline_yields_unknown_never_absent(self):
        import time

        def live(sid):
            time.sleep(0.5)
            return "ABSENT"

        with unittest.mock.patch.object(fr, "liveness_of", side_effect=live):
            out = fr.probe_all(["a", "b", "c", "d"], workers=1, deadline=0.1)
        self.assertEqual({"a", "b", "c", "d"}, set(out))
        self.assertNotIn("ABSENT", out.values())
        self.assertEqual({"UNKNOWN"}, set(out.values()))

    def test_row_output_is_unchanged_by_prefetching(self):
        root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        tasks = os.path.join(root, "25 Tasks")
        write(os.path.join(tasks, "Live Task.md"),
              page("page_type: task\nflag: true\nstatus: in_progress\nrole: agent\n"
                   "claude_session_id: %s" % self.SID))
        cfg = {"tasks_dir": "25 Tasks", "path": root, "worker_launcher": "/opt/l/cc-v"}

        def ctx(extra):
            base = {"today": "2026-10-31", "role_map": {}, "template_root": None,
                    "no_liveness": False, "no_pane": True, "managers": set(),
                    "topic_map": {}, "mode_default": "interactive", "normalised": [],
                    "subjects": {}, "liveness_cache": {}, "fleet_sid": None}
            base.update(extra)
            return base

        path = os.path.join(tasks, "Live Task.md")
        with unittest.mock.patch.object(fr, "liveness_of", return_value="LIVE"):
            sequential, _ = fr.row_for("v", cfg, "task", path, ctx({}))
            c = ctx({})
            pending, _ = fr.row_prepare("v", cfg, "task", path, c)
            c["sid_verdicts"] = fr.probe_all(pending.sids())
            prefetched = pending.finish()
        self.assertEqual(sequential, prefetched)
        self.assertIn(self.SID, c["sid_verdicts"])


class PageCacheTest(unittest.TestCase):
    """The mtime page cache: a warm run parses only what moved, and never serves a
    stale or ghost row."""

    FLAGGED = ("---\nflag: true\nflag_set_by: operator\nstatus: in_progress\n"
               "role: agent\nmode: headless\n---\nbody\n")
    UNFLAGGED = "---\nstatus: in_progress\n---\nbody\n"

    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.root)
        self.tasks = os.path.join(self.root, "vault", "25 Tasks")
        os.makedirs(self.tasks)
        self.cache = os.path.join(self.root, "cache.json")
        for i in range(20):
            self.write("Quiet %02d" % i, self.UNFLAGGED)
        self.write("Flagged", self.FLAGGED)
        cfg = {"v": {"path": os.path.join(self.root, "vault"),
                     "tasks_dir": "25 Tasks", "goals_dir": "24 Goals",
                     "topics_dir": "23 Topics", "worker_launcher": "cc-x"}}
        patcher = unittest.mock.patch.object(fr, "vault_configs", return_value=cfg)
        patcher.start()
        self.addCleanup(patcher.stop)

    def write(self, name, text):
        path = os.path.join(self.tasks, name + ".md")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(text)
        return path

    def run_once(self, **extra):
        args = fr.argparse.Namespace(json=True, vault=None,
                                     role_map=os.path.join(self.root, "none"),
                                     template_root=None, no_liveness=True, no_pane=False,
                                     cache=self.cache, no_cache=False)
        for key, value in extra.items():
            setattr(args, key, value)
        rows, skipped, _, _, stats = fr.collect(args)
        return sorted(r["name"] for r in rows), skipped, stats

    def entries(self):
        return fr.load_cache(self.cache)

    def test_warm_run_parses_only_uncached_pages(self):
        cold_rows, cold_skipped, cold = self.run_once()
        warm_rows, warm_skipped, warm = self.run_once()
        self.assertEqual(cold["parsed"], 21)
        self.assertEqual(cold["hits"], 0)
        # The 20 unflagged pages are served from the cache; the eligible one re-reads.
        self.assertEqual(warm["hits"], 20)
        self.assertEqual(warm["parsed"], 1)
        self.assertEqual((cold_rows, cold_skipped), (warm_rows, warm_skipped))
        self.assertEqual(warm_rows, ["Flagged"])

    def test_changed_page_is_picked_up_next_run(self):
        self.run_once()
        path = self.write("Quiet 03", self.FLAGGED)
        # Force a distinct mtime even on a coarse-clock filesystem.
        st = os.stat(path)
        os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000_000))
        rows, _, stats = self.run_once()
        self.assertEqual(rows, ["Flagged", "Quiet 03"])
        self.assertEqual(stats["hits"], 19)

    def test_unflagging_an_eligible_page_drops_its_row(self):
        self.run_once()
        path = self.write("Flagged", self.UNFLAGGED)
        st = os.stat(path)
        os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000_000))
        rows, skipped, _ = self.run_once()
        self.assertEqual(rows, [])
        self.assertEqual(skipped.get("not flagged"), 21)

    def test_deleted_page_leaves_no_row_and_no_cache_key(self):
        self.run_once()
        gone = os.path.join(self.tasks, "Quiet 07.md")
        self.assertIn(gone, self.entries())
        os.unlink(gone)
        flagged = os.path.join(self.tasks, "Flagged.md")
        os.rename(flagged, os.path.join(self.tasks, "Renamed.md"))
        rows, _, stats = self.run_once()
        self.assertEqual(rows, ["Renamed"])
        self.assertNotIn(gone, self.entries())
        self.assertEqual(stats["pruned"], 1)
        self.assertEqual(stats["entries"], 19)

    def test_deferred_page_is_never_cached(self):
        """A defer that comes due must surface without the file changing."""
        self.write("Later", self.FLAGGED.replace(
            "status: in_progress\n", "status: in_progress\ndefer_date: \"2999-01-01\"\n"))
        self.run_once()
        self.assertNotIn(os.path.join(self.tasks, "Later.md"), self.entries())

    def test_no_cache_neither_reads_nor_writes(self):
        _, _, stats = self.run_once(no_cache=True)
        self.assertIsNone(stats)
        self.assertFalse(os.path.exists(self.cache))

    def test_corrupt_or_foreign_cache_is_a_cold_start(self):
        with open(self.cache, "w", encoding="utf-8") as handle:
            handle.write("{not json")
        _, _, stats = self.run_once()
        self.assertEqual(stats["hits"], 0)
        with open(self.cache, "w", encoding="utf-8") as handle:
            json.dump({"version": 0, "entries": {}}, handle)
        _, _, stats = self.run_once()
        self.assertEqual(stats["hits"], 0)

    def test_vault_narrowed_run_keeps_other_vaults_entries(self):
        with open(self.cache, "w", encoding="utf-8") as handle:
            json.dump({"version": fr.CACHE_VERSION, "entries": {
                "/elsewhere/x.md": {"m": 1, "s": 1, "k": "task", "v": "other",
                                    "r": "not flagged"}}}, handle)
        self.run_once(vault="v")
        self.assertIn("/elsewhere/x.md", self.entries())


class FollowTheEndpoint(unittest.TestCase):
    """The consumer's output must FLIP when the endpoint's verdict flips.

    This is the SC2 probe: a consumer that derives liveness itself (an `os.kill`, a
    registry read) passes every "a call site exists" test and fails this one. Two
    sessions are used, and the second's verdict CONTRADICTS its pid — a consumer that
    follows the endpoint for some sessions and self-derives for others cannot pass on
    the first alone.
    """

    LIVE_SID = "11111111-1111-4111-8111-111111111111"
    CONTRADICTED_SID = "22222222-2222-4222-8222-222222222222"

    def setUp(self):
        fr._liveness_cache.update({"read": False, "rows": None})

    def _serve(self, verdicts):
        resp = unittest.mock.MagicMock()
        resp.status = 200
        resp.read.return_value = json.dumps(verdicts).encode("utf-8")
        resp.__enter__ = lambda s: s
        resp.__exit__ = lambda *a: False
        return unittest.mock.patch.object(
            fr.urllib.request, "urlopen", return_value=resp)

    def _reset(self):
        fr._liveness_cache.update({"read": False, "rows": None})

    def test_liveness_of_follows_the_endpoint_for_both_sessions(self):
        # Round 1: both live.
        with self._serve([
            {"session_id": self.LIVE_SID, "liveness": "live", "name": "A Manager"},
            {"session_id": self.CONTRADICTED_SID, "liveness": "live", "name": "B Manager"},
        ]):
            self.assertEqual(fr.liveness_of(self.LIVE_SID), "LIVE")
            self.assertEqual(fr.liveness_of(self.CONTRADICTED_SID), "LIVE")
        self._reset()
        # Round 2: the SECOND session's verdict flips to not-live while its pid is
        # unchanged — a self-deriving consumer would still answer LIVE here.
        with self._serve([
            {"session_id": self.LIVE_SID, "liveness": "live", "name": "A Manager"},
            {"session_id": self.CONTRADICTED_SID, "liveness": "not-live", "name": "B Manager"},
        ]):
            self.assertEqual(fr.liveness_of(self.LIVE_SID), "LIVE")
            self.assertEqual(fr.liveness_of(self.CONTRADICTED_SID), "ABSENT")

    def test_live_sessions_follows_the_endpoint(self):
        with self._serve([
            {"session_id": self.LIVE_SID, "liveness": "live", "name": "A Manager"},
            {"session_id": self.CONTRADICTED_SID, "liveness": "live", "name": "B Manager"},
        ]):
            self.assertEqual(fr.live_sessions(),
                             {self.LIVE_SID: "A Manager", self.CONTRADICTED_SID: "B Manager"})
        self._reset()
        with self._serve([
            {"session_id": self.LIVE_SID, "liveness": "live", "name": "A Manager"},
            {"session_id": self.CONTRADICTED_SID, "liveness": "not-live", "name": "B Manager"},
        ]):
            self.assertEqual(fr.live_sessions(), {self.LIVE_SID: "A Manager"})

    def test_an_unlisted_session_reads_unknown_not_absent(self):
        # ABSENT licenses a second spawn. A session the daemon never spawned (a tab,
        # a cluster manager) is one it cannot prove dead, so it must not read ABSENT.
        with self._serve([
            {"session_id": self.LIVE_SID, "liveness": "live", "name": "A Manager"},
        ]):
            self.assertEqual(fr.liveness_of("33333333-3333-4333-8333-333333333333"),
                             "UNKNOWN")

    def test_a_failed_read_reads_unknown_not_absent(self):
        with unittest.mock.patch.object(
                fr.urllib.request, "urlopen", side_effect=fr.urllib.error.URLError("down")):
            self.assertEqual(fr.liveness_of(self.LIVE_SID), "UNKNOWN")
            self.assertIsNone(fr.live_sessions())

    def test_an_unrecognised_verdict_reads_unknown_not_absent(self):
        # The endpoint's answer is binary, but a value that is neither is malformed —
        # and ABSENT licenses a second spawn, so malformed must not reach it.
        for bogus in ("", "unknown", "ambiguous", "malformed", "LIVE"):
            self._reset()
            with self._serve([{"session_id": self.LIVE_SID, "liveness": bogus,
                               "name": "A Manager"}]):
                self.assertEqual(fr.liveness_of(self.LIVE_SID), "UNKNOWN",
                                 "liveness=%r must not read ABSENT" % bogus)

    def test_a_daemon_without_names_withholds_the_identity_map(self):
        # A daemon predating claude-wrapper#131 serves verdicts with no `name`. The
        # identity map would then hold an empty name for every live session, which
        # empties `managers` and makes `open-manager` fire against live managers —
        # the duplicate-spawn hazard. An answer missing the field is unreadable.
        with self._serve([
            {"session_id": self.LIVE_SID, "liveness": "live"},
            {"session_id": self.CONTRADICTED_SID, "liveness": "live"},
        ]):
            self.assertIsNone(fr.live_sessions())


if __name__ == "__main__":
    unittest.main()
