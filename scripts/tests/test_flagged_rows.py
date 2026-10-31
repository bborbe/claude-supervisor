#!/usr/bin/env python3
"""Tests for `scripts/flagged-rows.py`.

Every case is a fixture — none touches the live fleet, the role map or the heartbeat
store. The live behaviour is exercised by the task's DoD run, not here.
"""

import importlib.util
import os
import tempfile
import unittest

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
        write(os.path.join(self.root, "start-day.yaml"),
              "spec:\n  name: start-day\nfrontmatter:\n  role: human\n  mode: interactive\n")

    def test_template_wins_over_a_disagreeing_on_disk_role(self):
        # The live Start Day case: a writer flipped the instance to `agent`.
        block = page("role: agent",
                     "# Source\n\nGenerated from `bborbe/nuke/task/"
                     "recurring-schedules/prod/start-day.yaml`.\n")
        self.assertEqual(fr.resolve_role(block, self.root), ("human", "template"))

    def test_on_disk_role_when_no_template(self):
        self.assertEqual(fr.resolve_role(page("role: agent"), self.root),
                         ("agent", "on-disk"))

    def test_body_when_neither(self):
        self.assertEqual(fr.resolve_role(page("status: in_progress"), self.root),
                         ("agent", "body"))

    def test_missing_template_falls_back_to_on_disk(self):
        block = page("role: agent",
                     "# Source\n\n`bborbe/nuke/task/recurring-schedules/prod/absent.yaml`\n")
        self.assertEqual(fr.resolve_role(block, self.root), ("agent", "on-disk"))


class BranchPartition(unittest.TestCase):
    """The ordered predicate list — first match wins, so the set is a partition."""

    def branch(self, kind="task", role="agent", sid=None, live="none", pane=None,
               owner=None, owner_live=None, block=None):
        return fr.resolve_branch(kind, role, sid, live, pane,
                                 owner or fr.FLEET, owner_live, block or page("role: agent"))

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
        owner = {"kind": "topic", "name": "Some Topic"}
        self.assertEqual(self.branch(kind="topic", live="none", owner=owner,
                                     owner_live=None)[0], "open-manager")

    def test_live_with_pane_jumps(self):
        self.assertEqual(self.branch(live="LIVE", sid="s1", pane="p1")[0], "jump")

    def test_live_without_pane_skips(self):
        self.assertEqual(self.branch(live="LIVE", sid="s1", pane=None)[0], "skip")

    def test_absent_with_id_resumes(self):
        self.assertEqual(self.branch(live="ABSENT", sid="s1")[0], "resume")

    def test_absent_without_id_creates(self):
        self.assertEqual(self.branch(live="ABSENT", sid=None)[0], "create")


class OwnerResolution(unittest.TestCase):
    """`docs/fleet-surface.md` § Which manager owns a subject, via manager_on_chain."""

    def test_managed_by_outranks_goals(self):
        block = page("managed_by: '[[A Topic]]'\ngoals:\n    - '[[A Goal]]'")
        owner, _ = fr.resolve_owner("task", "T", block, {}, {"A Goal"}, False)
        self.assertEqual(owner, {"kind": "goal", "name": "A Topic"})

    def test_second_goal_wins_when_it_has_the_manager(self):
        block = page("goals:\n    - '[[First]]'\n    - '[[Second]]'")
        owner, live = fr.resolve_owner("task", "T", block, {}, {"Second"}, False)
        self.assertEqual(owner, {"kind": "goal", "name": "Second"})
        self.assertTrue(live)

    def test_no_rung_falls_to_the_fleet_sentinel(self):
        owner, live = fr.resolve_owner("task", "T", page("status: in_progress"), {}, set(), False)
        self.assertEqual(owner, fr.FLEET)
        self.assertIsNone(live)

    def test_goal_uses_the_topic_page_index(self):
        owner, _ = fr.resolve_owner("goal", "G", page(""), {"G": "A Topic"}, set(), False)
        self.assertEqual(owner, {"kind": "topic", "name": "A Topic"})

    def test_goal_absent_from_every_index_falls_to_the_fleet(self):
        owner, _ = fr.resolve_owner("goal", "G", page(""), {}, set(), False)
        self.assertEqual(owner, fr.FLEET)

    def test_topic_row_owns_itself(self):
        owner, live = fr.resolve_owner("topic", "T", page(""), {}, {"T"}, True)
        self.assertEqual(owner, {"kind": "topic", "name": "T"})
        self.assertTrue(live)


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
        cfg = {"tasks_dir": "25 Tasks", "path": root}
        ctx = {"today": "2026-10-31", "role_map": {}, "template_root": None,
               "no_liveness": True, "managers": set(), "topic_map": {}}
        row, reason = fr.row_for("v", cfg, "task", os.path.join(tasks, "A Task.md"), ctx)
        self.assertIsNone(reason)
        self.assertEqual(set(row), set(self.FIELDS))
        self.assertEqual(row["name"], "A Task")
        self.assertEqual(row["kind"], "task")


if __name__ == "__main__":
    unittest.main()
