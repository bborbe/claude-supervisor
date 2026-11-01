#!/usr/bin/env python3
"""Tests for `scripts/flagged-rows.py`.

Every case is a fixture — none touches the live fleet, the role map or the heartbeat
store. The live behaviour is exercised by the task's DoD run, not here.
"""

import importlib.util
import json
import os
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


def page(front, body=""):
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


class BranchPartition(unittest.TestCase):
    """The ordered predicate list — first match wins, so the set is a partition."""

    def branch(self, kind="task", role="agent", sid=None, live="none", pane=None,
               chain_live=None, block=None):
        return fr.resolve_branch(kind, role, sid, live, pane, chain_live,
                                 block or page("role: agent"))

    def test_unknown_outranks_everything(self):
        for verdict in ("UNKNOWN", "AMBIGUOUS", "MALFORMED"):
            self.assertEqual(self.branch(live=verdict)[0], "unknown")

    def test_unknown_outranks_a_human_row(self):
        self.assertEqual(self.branch(role="human", live="UNKNOWN")[0], "unknown")

    def test_human_row_is_direct(self):
        self.assertEqual(self.branch(role="human", live="none")[0], "direct")

    def test_sessionless_human_is_direct_not_create(self):
        self.assertEqual(self.branch(role="human", live="none", sid=None)[0], "direct")

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
    """SC4: the provenance rule is not uniform, and applying it to goals refuses live work."""

    def test_an_agent_set_task_is_refused(self):
        self.assertEqual(
            fr.refusals("task", page("flag: true\nflag_set_by: agent")),
            ["agent-set-flag"])

    def test_an_absent_flag_set_by_refuses_a_task(self):
        # After the 2026-09-30 legacy disposition no live row should be missing it.
        self.assertEqual(fr.refusals("task", page("flag: true")), ["agent-set-flag"])

    def test_operator_and_legacy_open_a_task(self):
        for actor in ("operator", "legacy"):
            self.assertEqual(
                fr.refusals("task", page("flag: true\nflag_set_by: %s" % actor)), [])

    def test_a_goal_accepts_any_provenance(self):
        self.assertEqual(
            fr.refusals("goal", page("flag: 'true'\nflag_set_by: agent")), [])
        self.assertEqual(fr.refusals("topic", page("flag: on")), [])

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


class RegistryProbe(unittest.TestCase):
    """A failed `--list` is not an empty fleet, and a `⚙ `-prefixed label is not absent.

    Both defects produce the same wrong branch — `open-manager` against a live manager
    — which is a duplicate-spawn hazard, so each direction is pinned separately.
    """

    def test_a_failed_list_returns_none_not_an_empty_set(self):
        with unittest.mock.patch.object(fr.subprocess, "run") as run:
            run.return_value = unittest.mock.Mock(returncode=2, stdout="", stderr="x")
            self.assertIsNone(fr.live_sessions())

    def test_a_successful_empty_list_returns_an_empty_dict(self):
        with unittest.mock.patch.object(fr.subprocess, "run") as run:
            run.return_value = unittest.mock.Mock(returncode=0, stdout="", stderr="")
            self.assertEqual(fr.live_sessions(), {})

    def test_the_pid_and_age_field_forms_both_parse(self):
        with unittest.mock.patch.object(fr.subprocess, "run") as run:
            run.return_value = unittest.mock.Mock(
                returncode=0, stderr="",
                stdout="sid1  pid 42  A Goal Manager  [wezterm]\n"
                       "sid2  age 30s  A Topic Manager  [endpoint]\n")
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


class PaneCache(unittest.TestCase):
    """`pane_of` imports a 2300-line module per call, so duplicate sids share one."""

    def test_a_repeated_session_id_probes_once(self):
        ctx = {"liveness_cache": {}}
        with unittest.mock.patch.object(fr.subprocess, "run") as run:
            run.return_value = unittest.mock.Mock(returncode=0, stdout="12\n", stderr="")
            self.assertEqual(fr.pane_of(ctx, "sid"), "12")
            self.assertEqual(fr.pane_of(ctx, "sid"), "12")
            self.assertEqual(run.call_count, 1)

    def test_a_failed_probe_is_cached_as_none(self):
        ctx = {"liveness_cache": {}}
        with unittest.mock.patch.object(fr.subprocess, "run") as run:
            run.return_value = unittest.mock.Mock(returncode=1, stdout="", stderr="x")
            self.assertIsNone(fr.pane_of(ctx, "sid"))
            self.assertIsNone(fr.pane_of(ctx, "sid"))
            self.assertEqual(run.call_count, 1)


if __name__ == "__main__":
    unittest.main()
