#!/usr/bin/env python3
"""Tests for scripts/propose-managers.py.

Each case guards a defect the line has actually shipped or was filed against:

  * the threshold is asserted in BOTH directions — a goal with exactly the threshold
    qualifies and one below it does not, because a one-sided fixture passes on a script
    that proposes every goal.
  * the slot gate emits at most `free` cards and emits ZERO at the cap — the operator's
    own acceptance criterion, and the half a "it posted something" test cannot see.
  * **`free is None` is UNLIMITED, not "none"** — the operator's `spawn.maxManagers: 0`
    off switch resolves `hardLimit: null`, and reading that as "no free slot" turned the
    off switch into permanent silence. Asserted through `main()`, because the bug lived in
    the wiring between the resolver and the slice, not in either.
  * **a manager named for an abbreviated title still owns its goal** — the live manager
    for the goal this change was written for is `Read CDB Data Through Weldall Manager`
    against a goal titled `Read CDB Data Through Weldall (MVP)`. An equality match listed
    it as unmanaged with its manager running (measured 2026-10-12).
  * **a goal whose manager is named for its TOPIC is managed too** — the two-hop chain.
  * **a missing tasks dir REFUSES, never reads as a clean round** — a typo'd path and an
    empty vault must not render the same.
  * **two same-titled goals in two vaults do not share a card key** — the key carries the
    goal's name and no vault, so the collision is refused rather than silently swallowed.
  * the dedup key carries no timestamp — a key that moves on a re-post of an unchanged
    goal defeats the store's open-scoped suppression and asks the operator twice
    (measured 2026-10-02 on the sibling under-target card).
  * an unreadable board is UNKNOWN, never zero — the `None`-not-`0` rule from
    `docs/pane-reads.md`. An empty fleet and a failed read must not render the same, and
    only one of them posts past a limit.
  * frontmatter-only reading — a whole-file scan matches keys quoted in task-body prose
    and qualifies a goal on the strength of a sentence *about* approval.
  * `post()` returns the PARSED item id, never `attention-ask.py`'s whole stdout.

Run: python3 -m unittest discover -s scripts/tests -v
"""

import importlib.util
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout

_HERE = os.path.dirname(os.path.abspath(__file__))
_SCRIPT = os.path.join(os.path.dirname(_HERE), "propose-managers.py")

_spec = importlib.util.spec_from_file_location("propose_managers", _SCRIPT)
pm = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pm)

# The real board module — `managed()` is delegated to it, so the fixture is the live rule.
BOARD = pm._board()

VAULT = {"name": "private-personal", "tasks_dir": "/nonexistent/tasks",
         "goals_dir": None, "topics_dir": None}
OTHER = {"name": "seibert-brogrammers", "tasks_dir": "/nonexistent/tasks",
         "goals_dir": None, "topics_dir": None}


def row(goal, vault=VAULT, title="a task", phase="planning"):
    return {"goal": goal, "vault": vault, "title": title,
            "phase": phase, "startability": pm.classify(phase)}


def task(phase="planning", status="in_progress", approved="2026-10-12T09:00:00+02:00",
         goals=("[[Some Goal]]",), body=""):
    """One task file's text, in the vault's frontmatter shape."""
    lines = ["---"]
    if approved:
        lines.append(f"approved_at: {approved}")
    lines.append(f"phase: {phase}")
    lines.append(f"status: {status}")
    lines.append("goals:")
    for goal in goals:
        lines.append(f"    - '{goal}'")
    lines.append("---")
    lines.append("Tags: [[Task]]")
    lines.append("")
    lines.append(body)
    return "\n".join(lines)


class ManagedTest(unittest.TestCase):
    """The name match is `fleet-board.py`'s, so these pin the delegation, not a copy."""

    def test_an_abbreviated_manager_label_still_owns_its_goal(self):
        """The reproduced case: `(MVP)` dropped from the goal title in the label."""
        self.assertTrue(pm.managed(
            ["Read CDB Data Through Weldall (MVP)"],
            ["Read CDB Data Through Weldall Manager"],
            BOARD,
        ))

    def test_the_registry_decoration_is_stripped(self):
        self.assertTrue(pm.managed(["Some Goal"], ["⚙ Some Goal Manager"], BOARD))

    def test_an_unrelated_manager_does_not_own_the_goal(self):
        self.assertFalse(pm.managed(
            ["Read CDB Data Through Weldall (MVP)"],
            ["Cluster Workers Manager"],
            BOARD,
        ))

    def test_a_short_coincidental_prefix_does_not_match(self):
        """The length floor: `Cluster Workers` must not claim `Cluster Workers and More`."""
        self.assertFalse(pm.managed(["Cluster Workers and More"], ["Cluster Workers Manager"], BOARD))

    def test_a_topic_that_lists_the_goal_manages_it(self):
        """The two-hop chain — a manager named for the topic owns the goal under it."""
        self.assertTrue(pm.managed(["CDB in Weldall"], ["CDB in Weldall Manager"], BOARD))

    def test_no_labels_means_unmanaged(self):
        self.assertFalse(pm.managed(["Some Goal"], [], BOARD))

    # ---- the registered subject: the path that covers an abbreviated label -------------

    def test_the_registered_subject_owns_the_goal_its_abbreviated_label_does_not(self):
        """The reproduced defect, both halves in one case.

        `Vault API Manager` is neither the title *Typed Vault REST API for Agents and
        Tools* nor a prefix of it, so the label rule cannot see it (first assertion —
        this is the defect, not an incidental). The registration states the subject the
        tier resolved, so it matches exactly (second assertion).
        """
        titles = ["Typed Vault REST API for Agents and Tools"]
        live = lambda _s, _b: True
        self.assertFalse(pm.managed(titles, ["Vault API Manager"], BOARD,
                                    vault="private-personal", live_ok=live))
        self.assertTrue(pm.managed(
            titles, ["Vault API Manager"], BOARD,
            registrations=[("Typed Vault REST API for Agents and Tools", "goal", "private-personal")],
            vault="private-personal", live_ok=live,
        ))

    def test_a_non_ok_registration_leaves_the_goal_qualifying(self):
        """STALE/STOPPED/UNARMED/INVALID must not suppress — hiding work is the silent direction.

        The registration directory is documented as never cleaned, so a registration
        outlives its session. Suppressing on one would hide a genuinely manager-less goal.

        ⚠️ `live_ok` is injected rather than left to the default. The default reads the
        real `~/.claude/state/sweep-gate`, so leaving it unset would make this case assert
        whatever the live fleet happens to be doing — a fixture that passes or fails on the
        machine rather than on the rule. `LivenessOkTest` exercises the real classifier.
        """
        titles = ["Typed Vault REST API for Agents and Tools"]
        self.assertFalse(pm.managed(
            titles, ["Vault API Manager"], BOARD,
            registrations=[("Typed Vault REST API for Agents and Tools", "goal", "private-personal")],
            vault="private-personal", live_ok=lambda _s, _b: False,
        ))

    def test_a_registration_for_another_vault_does_not_suppress(self):
        """A goal title is not namespaced across vaults — a title match elsewhere proves nothing."""
        titles = ["Typed Vault REST API for Agents and Tools"]
        self.assertFalse(pm.managed(
            titles, [], BOARD,
            registrations=[("Typed Vault REST API for Agents and Tools", "goal", "seibert-brogrammers")],
            vault="private-personal", live_ok=lambda _s, _b: True,
        ))

    def test_a_candidate_with_no_vault_never_matches_a_registration(self):
        """Loud, never silent: an unpaired candidate cannot be suppressed by a title alone."""
        titles = ["Typed Vault REST API for Agents and Tools"]
        self.assertFalse(pm.managed(
            titles, [], BOARD,
            registrations=[("Typed Vault REST API for Agents and Tools", "goal", "")],
            vault=None, live_ok=lambda _s, _b: True,
        ))

    def test_the_vault_comparison_ignores_case_and_surrounding_space(self):
        titles = ["Some Goal"]
        self.assertTrue(pm.managed(
            titles, [], BOARD,
            registrations=[("Some Goal", "goal", "  Private-Personal ")],
            vault="private-personal", live_ok=lambda _s, _b: True,
        ))

    def test_a_registration_naming_another_goal_does_not_suppress(self):
        """The other direction: the registration path must not suppress on any registration."""
        self.assertFalse(pm.managed(
            ["Typed Vault REST API for Agents and Tools"], [], BOARD,
            registrations=[("Cluster Workers", "goal", "private-personal")],
            vault="private-personal", live_ok=lambda _s, _b: True,
        ))

    def test_the_three_measured_false_proposals_are_all_suppressed(self):
        """Measured 2026-10-25: three of five live managers were offered a manager they had.

        Each pair is a label the rule cannot see plus the registration that names the goal
        exactly. One card was actually printed (`Agent Namespace Manager`, rank 3).
        """
        pairs = [
            ("Agent Namespace Manager", "Agent Workloads Run in Dedicated Agent Namespaces"),
            ("Vault API Manager", "Typed Vault REST API for Agents and Tools"),
            ("PR Review Agent Fast Manager",
             "PR Reviews Return a Verdict Within Minutes, Not Hours"),
        ]
        for label, goal in pairs:
            with self.subTest(label=label):
                self.assertFalse(pm.managed([goal], [label], BOARD,
                                            vault="private-personal",
                                            live_ok=lambda _s, _b: True))
                self.assertTrue(pm.managed(
                    [goal], [label], BOARD,
                    registrations=[(goal, "goal", "private-personal")],
                    vault="private-personal", live_ok=lambda _s, _b: True,
                ))


class RegistrationTest(unittest.TestCase):
    """The registration reader — `answer-path-check.py`'s convention, the same guards."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.original = pm.WORKER_MANAGER_DIR
        pm.WORKER_MANAGER_DIR = self.tmp.name

    def tearDown(self):
        pm.WORKER_MANAGER_DIR = self.original
        self.tmp.cleanup()

    def write(self, session_id, payload):
        with open(os.path.join(self.tmp.name, f"{session_id}.json"), "w", encoding="utf-8") as fh:
            fh.write(payload if isinstance(payload, str) else json.dumps(payload))

    def test_a_full_registration_reads_subject_branch_and_vault(self):
        self.write("sid", {"subject": "A Goal", "branch": "goal", "vault": "private-personal"})
        self.assertEqual(pm._registration("sid"), ("A Goal", "goal", "private-personal"))

    def test_an_absent_vault_reads_empty_not_none(self):
        self.write("sid", {"subject": "A Goal", "branch": "goal"})
        self.assertEqual(pm._registration("sid"), ("A Goal", "goal", ""))

    def test_a_path_escaping_or_empty_id_is_refused(self):
        for bad in ("../secrets", "a/b", "a\\b", ".", "..", "", None, 7):
            self.assertIsNone(pm._registration(bad), bad)

    def test_a_non_object_registration_is_none_not_a_traceback(self):
        for payload in ("null", "[]", '"a string"', "42"):
            self.write("odd", payload)
            self.assertIsNone(pm._registration("odd"), payload)

    def test_a_non_string_subject_or_branch_is_none_not_a_traceback(self):
        for payload in ({"subject": 123, "branch": "goal"},
                        {"subject": "A Goal", "branch": None},
                        {"subject": "", "branch": "goal"}):
            self.write("typed", payload)
            self.assertIsNone(pm._registration("typed"), payload)

    def test_no_registration_file_is_none(self):
        self.assertIsNone(pm._registration("missing"))


class LivenessOkTest(unittest.TestCase):
    """Only `OK` suppresses — exercised against the real `manager-liveness.py` classifier."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.module = pm._liveness()
        self.original = self.module.STATE_DIR
        self.module.STATE_DIR = self.tmp.name

    def tearDown(self):
        self.module.STATE_DIR = self.original
        self.tmp.cleanup()

    def write(self, name, ext, body):
        path = os.path.join(self.tmp.name, f"{name}.{ext}")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(body)
        return path

    def test_a_fresh_cadence_reads_ok_and_suppresses(self):
        self.write(self.module.slug("A Goal", "goal"), "cadence", "300")
        self.assertTrue(pm._liveness_ok("A Goal", "goal"))

    def test_no_record_reads_unarmed_and_does_not_suppress(self):
        self.assertFalse(pm._liveness_ok("A Goal", "goal"))

    def test_an_old_record_reads_stale_and_does_not_suppress(self):
        path = self.write(self.module.slug("A Goal", "goal"), "cadence", "300")
        old = 1_000_000
        os.utime(path, (old, old))
        self.assertFalse(pm._liveness_ok("A Goal", "goal"))

    def test_a_stopped_marker_does_not_suppress(self):
        self.write(self.module.slug("A Goal", "goal"), "cadence", "300")
        self.write(self.module.slug("A Goal", "goal"), "stopped", "")
        self.assertFalse(pm._liveness_ok("A Goal", "goal"))

    def test_an_unknown_branch_is_a_refusal_not_a_traceback(self):
        self.assertFalse(pm._liveness_ok("A Goal", "not-a-branch"))


class LiveRegistrationsTest(unittest.TestCase):
    """The board JSON → registrations half, including the sessions it must skip."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.original = pm.WORKER_MANAGER_DIR
        pm.WORKER_MANAGER_DIR = self.tmp.name

    def tearDown(self):
        pm.WORKER_MANAGER_DIR = self.original
        self.tmp.cleanup()

    def register(self, session_id, subject="A Goal", branch="goal", vault="private-personal"):
        with open(os.path.join(self.tmp.name, f"{session_id}.json"), "w", encoding="utf-8") as fh:
            json.dump({"subject": subject, "branch": branch, "vault": vault}, fh)

    @staticmethod
    def board(*sessions):
        return json.dumps({"sessions": list(sessions)})

    def test_a_manager_registration_is_returned(self):
        self.register("sid-1")
        self.assertEqual(
            pm.live_registrations(self.board({"role": "manager", "session_id": "sid-1"})),
            [("A Goal", "goal", "private-personal")],
        )

    def test_the_fleet_manager_contributes_nothing(self):
        """The root manages no goal, and its registration is not evidence about one."""
        self.register("root", subject="Fleet Manager", branch="fleet")
        self.assertEqual(
            pm.live_registrations(self.board(
                {"role": "manager", "session_id": "root", "label": "Fleet Manager"})),
            [],
        )

    def test_a_worker_row_contributes_nothing(self):
        self.register("sid-1")
        self.assertEqual(
            pm.live_registrations(self.board({"role": "worker", "session_id": "sid-1"})), [])

    def test_a_manager_with_no_registration_contributes_nothing(self):
        self.assertEqual(
            pm.live_registrations(self.board({"role": "manager", "session_id": "unknown"})), [])

    def test_an_unreadable_board_is_empty_not_a_raise(self):
        for bad in ("", "not json", "null", "[]", '{"sessions": 3}'):
            self.assertEqual(pm.live_registrations(bad), [], bad)

    def test_a_non_object_session_row_is_skipped(self):
        self.register("sid-1")
        self.assertEqual(
            pm.live_registrations(self.board("junk", {"role": "manager", "session_id": "sid-1"})),
            [("A Goal", "goal", "private-personal")],
        )


class QualifyTest(unittest.TestCase):
    def qualify(self, rows, labels=(), topics=None):
        return pm.qualify(rows, list(labels), topics or {}, BOARD)

    def test_goal_at_threshold_qualifies(self):
        rows = [row("G", title="t1"), row("G", title="t2")]
        self.assertEqual([c["goal"] for c in self.qualify(rows)], ["G"])

    def test_goal_below_threshold_does_not(self):
        """The other direction: one approved task is a row to open, not a subject."""
        self.assertEqual(self.qualify([row("G", title="t1")]), [])

    def test_goal_with_a_live_manager_does_not_qualify(self):
        rows = [row("G", title="t1"), row("G", title="t2")]
        self.assertEqual(self.qualify(rows, ["G"]), [])

    def test_a_goal_whose_abbreviated_manager_registered_does_not_qualify(self):
        """The reproduced defect end-to-end through `qualify`, not only through `managed`.

        The label is invisible to the rule; the registration names the goal exactly, so the
        goal must not be offered a second manager.
        """
        goal = "Typed Vault REST API for Agents and Tools"
        rows = [row(goal, title="t1"), row(goal, title="t2")]
        self.assertEqual([c["goal"] for c in self.qualify(rows, ["Vault API Manager"])], [goal])
        self.assertEqual(pm.qualify(
            rows, ["Vault API Manager"], {}, BOARD,
            registrations=[(goal, "goal", VAULT["name"])], live_ok=lambda _s, _b: True,
        ), [])

    def test_a_stale_registration_leaves_the_goal_qualifying_end_to_end(self):
        goal = "Typed Vault REST API for Agents and Tools"
        rows = [row(goal, title="t1"), row(goal, title="t2")]
        self.assertEqual([c["goal"] for c in pm.qualify(
            rows, [], {}, BOARD,
            registrations=[(goal, "goal", VAULT["name"])], live_ok=lambda _s, _b: False,
        )], [goal])

    def test_goal_reached_through_its_topic_is_managed(self):
        rows = [row("G", title="t1"), row("G", title="t2")]
        self.assertEqual(self.qualify(rows, ["T Manager"], {"G": {"T"}}), [])

    def test_same_title_in_two_vaults_is_two_goals(self):
        """Goal titles are not namespaced across vaults — they must never be summed."""
        rows = [row("G", vault=VAULT, title="t1"), row("G", vault=OTHER, title="t1")]
        self.assertEqual(self.qualify(rows), [])

    def test_ranking_is_priority_then_count(self):
        """Priority wins over count, so a script sorting on count alone fails here."""
        rows = [row("Low", title="a"), row("Low", title="b"),
                row("Low", title="c"), row("Low", title="d"),
                row("High", title="e"), row("High", title="f")]
        priorities = {"Low": 3, "High": 1}
        original = pm.goal_priority
        pm.goal_priority = lambda _dir, name: priorities.get(name)
        try:
            self.assertEqual([c["goal"] for c in self.qualify(rows)], ["High", "Low"])
        finally:
            pm.goal_priority = original

    def test_equal_priority_ranks_the_most_approved_work_first(self):
        rows = [row("Few", title="a"), row("Few", title="b"),
                row("Many", title="c"), row("Many", title="d"), row("Many", title="e")]
        original = pm.goal_priority
        pm.goal_priority = lambda _dir, _name: 2
        try:
            self.assertEqual([c["goal"] for c in self.qualify(rows)], ["Many", "Few"])
        finally:
            pm.goal_priority = original

    def test_unset_priority_ranks_last(self):
        rows = [row("Unset", title="a"), row("Unset", title="b"),
                row("Set", title="c"), row("Set", title="d")]
        original = pm.goal_priority
        pm.goal_priority = lambda _dir, name: 3 if name == "Set" else None
        try:
            self.assertEqual([c["goal"] for c in self.qualify(rows)], ["Set", "Unset"])
        finally:
            pm.goal_priority = original


class SlotGateTest(unittest.TestCase):
    """The operator's own acceptance criterion: at most `free` cards, zero at the cap."""

    def candidates(self, count):
        return [{"goal": f"G{n}", "vault": "v", "approved": 2, "tasks": [],
                 "priority": n} for n in range(count)]

    def test_three_qualifying_goals_and_two_free_slots_emit_exactly_two(self):
        cards = pm.cap_cards(self.candidates(3), 2)
        self.assertEqual([c["goal"] for c in cards], ["G0", "G1"])

    def test_rank_order_is_preserved_by_the_cap(self):
        """The cap is a slice, never a re-rank — the ranking is `qualify`'s alone."""
        self.assertEqual([c["goal"] for c in pm.cap_cards(self.candidates(5), 3)],
                         ["G0", "G1", "G2"])

    def test_at_the_cap_zero_cards(self):
        self.assertEqual(pm.cap_cards(self.candidates(4), 0), [])

    def test_negative_free_is_zero_cards_not_a_negative_slice(self):
        """A cap read below the live count must not slice from the end."""
        self.assertEqual(pm.cap_cards(self.candidates(4), -2), [])

    def test_more_slots_than_goals_emits_every_goal(self):
        self.assertEqual(len(pm.cap_cards(self.candidates(2), 9)), 2)

    def test_unlimited_posts_every_candidate(self):
        """`None` is the operator's `0` off switch resolved — never "no free slot"."""
        self.assertEqual(len(pm.cap_cards(self.candidates(7), None)), 7)

    def test_unlimited_with_no_candidates_is_still_empty(self):
        self.assertEqual(pm.cap_cards([], None), [])


class DedupKeyTest(unittest.TestCase):
    def test_key_is_the_goal_name_alone(self):
        self.assertEqual(pm.dedup_key("Some Goal"), "fleet-propose-manager:Some Goal")

    def test_key_carries_no_timestamp(self):
        """A key that moves on an unchanged goal re-asks the operator (2026-10-02)."""
        self.assertEqual(pm.dedup_key("Some Goal"), pm.dedup_key("Some Goal"))
        self.assertNotRegex(pm.dedup_key("Some Goal"), r"\d{4}-\d{2}-\d{2}")

    def test_two_same_titled_goals_collide(self):
        """The key carries no vault, so these would share one store row."""
        candidates = [{"goal": "G", "vault": "a"}, {"goal": "G", "vault": "b"}]
        self.assertEqual(pm.key_collisions(candidates), {"fleet-propose-manager:G": ["G", "G"]})

    def test_distinct_goals_do_not_collide(self):
        self.assertEqual(pm.key_collisions([{"goal": "A"}, {"goal": "B"}]), {})


class LiveManagersTest(unittest.TestCase):
    def board(self, sessions):
        return json.dumps({"sessions": sessions})

    def test_manager_role_counts_and_fleet_manager_is_excluded(self):
        labels = pm.live_manager_labels(self.board([
            {"label": "Fleet Manager", "role": "manager"},
            {"label": "Cluster Workers Manager", "role": "manager"},
            {"label": "some worker", "role": "worker"},
        ]))
        self.assertEqual(labels, ["Cluster Workers Manager"])

    def test_unreadable_board_is_unknown_not_empty(self):
        """`None`, never `[]` — an empty fleet is the answer that posts past a limit."""
        for bad in ("", "not json", "{}", '{"sessions": null}'):
            self.assertIsNone(pm.live_manager_labels(bad), bad)

    def test_a_readable_board_with_no_managers_is_empty_not_unknown(self):
        self.assertEqual(pm.live_manager_labels(self.board([])), [])


class ScanTest(unittest.TestCase):
    def scan_dir(self, files):
        """Write `{filename: text}` into a temp tasks dir and scan it."""
        with tempfile.TemporaryDirectory() as tmp:
            for name, text in files.items():
                with open(os.path.join(tmp, name), "w", encoding="utf-8") as handle:
                    handle.write(text)
            return pm.scan([{"name": "v", "tasks_dir": tmp, "goals_dir": None,
                             "topics_dir": None}])

    def test_approved_live_phase_task_is_a_row(self):
        rows = self.scan_dir({"t.md": task()})
        self.assertEqual([r["goal"] for r in rows], ["Some Goal"])

    def test_phase_todo_is_scanned_but_does_not_count_toward_the_threshold(self):
        """`scan` is the snapshot's broader read; the card's threshold is live-phase only.

        The row is kept so its `unstartable` bit can reach the declined-ask snapshot —
        excluding it here would leave the sibling's arms 3 and 4 with no row that could
        ever carry that bit.
        """
        rows = self.scan_dir({"t.md": task(phase="todo")})
        self.assertEqual([r["startability"] for r in rows], [pm.UNSTARTABLE])
        self.assertEqual(pm.qualify(rows, [], {}, BOARD), [])

    def test_terminal_status_is_not_approved_work(self):
        for status in ("completed", "aborted", "hold", "backlog"):
            self.assertEqual(self.scan_dir({"t.md": task(status=status)}), [], status)

    def test_missing_approved_at_is_not_approved(self):
        self.assertEqual(self.scan_dir({"t.md": task(approved=None)}), [])

    def test_prose_about_approval_does_not_approve_a_task(self):
        """Frontmatter only: a sentence *about* approval is not an approval record."""
        body = "This task was approved_at some point but the field is absent."
        self.assertEqual(self.scan_dir({"t.md": task(approved=None, body=body)}), [])

    def test_a_task_naming_two_goals_counts_for_both(self):
        rows = self.scan_dir({"t.md": task(goals=("[[A]]", "[[B]]"))})
        self.assertEqual(sorted(r["goal"] for r in rows), ["A", "B"])

    def test_goal_link_is_unwrapped(self):
        rows = self.scan_dir({"t.md": task(goals=("[[Some Goal]]",))})
        self.assertEqual(rows[0]["goal"], "Some Goal")


class TopicsTest(unittest.TestCase):
    def test_goals_are_read_from_the_goals_section_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            topics = os.path.join(tmp, "23 Topics")
            os.makedirs(topics)
            with open(os.path.join(topics, "A Topic.md"), "w", encoding="utf-8") as handle:
                handle.write("# Scope\n\n## Goals\n\n- [[In Scope Goal]] — active\n\n"
                             "## Non-goals\n\n- [[Not A Goal]]\n")
            self.assertEqual(pm.topics_by_goal({"topics_dir": topics}),
                             {"In Scope Goal": {"A Topic"}})

    def test_a_vault_with_no_topics_dir_contributes_nothing(self):
        self.assertEqual(pm.topics_by_goal({"topics_dir": None}), {})


class RootsTest(unittest.TestCase):
    def test_a_missing_tasks_dir_refuses(self):
        """A typo'd path and an empty vault must not render the same."""
        self.assertIsNone(pm.roots_from_args(["/tmp/definitely-not-a-tasks-dir"]))

    def test_an_existing_tasks_dir_resolves(self):
        with tempfile.TemporaryDirectory() as tmp:
            tasks = os.path.join(tmp, "25 Tasks")
            os.makedirs(tasks)
            roots = pm.roots_from_args([tasks])
            self.assertEqual(roots[0]["tasks_dir"], tasks)


class PostTest(unittest.TestCase):
    def test_the_item_id_is_parsed_out_of_stdout(self):
        """`attention-ask.py post` prints `ITEM_ID:` then `POLL:` — never return both."""
        original = pm.subprocess.run

        class Proc:
            returncode = 0
            stdout = "ITEM_ID: abc-123\nPOLL: attention-ask.py poll abc-123\n"
            stderr = ""

        pm.subprocess.run = lambda *a, **k: Proc()
        try:
            self.assertEqual(pm.post({"dedup_key": "k", "payload": "p", "context": "c",
                                      "options": ["a", "b"], "recommend": "a"}), "abc-123")
        finally:
            pm.subprocess.run = original

    def test_a_post_without_an_item_id_is_a_failure_not_a_blob(self):
        original = pm.subprocess.run

        class Proc:
            returncode = 0
            stdout = "something unexpected\n"
            stderr = ""

        pm.subprocess.run = lambda *a, **k: Proc()
        try:
            self.assertIsNone(pm.post({"dedup_key": "k", "payload": "p", "context": "c",
                                       "options": ["a"], "recommend": "a"}))
        finally:
            pm.subprocess.run = original


class MainTest(unittest.TestCase):
    """`main()`'s wiring — the layer the pure-helper tests cannot reach."""

    def run_main(self, argv, cap=(3, 5, "default", "default"), count=1, rows=(),
                 labels=(), roots=(VAULT,), state_dir=None, registrations=()):
        """Run `main()` with every I/O edge stubbed; returns `(exit_code, stdout)`.

        ⚠️ **The declined-ask state dir is always isolated.** Without `--state-dir` the run
        writes `~/.claude/state/manager-propose/`, so a second case reusing a goal name
        would suppress on state a previous case left behind — the suite would then pass or
        fail on its own history. `state_dir` is accepted so a two-run case can share one.

        ⚠️ **`count` is derived, not stubbed.** `main()` takes its live-manager count from
        the same single board read that yields the registrations, so the stub is
        `live_managers()` returning `(labels, registrations)`. `count=` still names how many
        live managers a case wants; when the case names no `labels`, placeholders fill the
        count so the cap arithmetic reads exactly as it did before the read was widened.
        """
        original = (pm.resolve_cap, pm.live_managers, pm.scan, pm.vaults, pm.post)
        posted = []
        pm.resolve_cap = lambda: cap
        if labels is None:
            live = None
        else:
            named = list(labels) or [f"Manager {n}" for n in range(count or 0)]
            live = (named, list(registrations))
        pm.live_managers = lambda: live
        pm.scan = lambda _roots: list(rows)
        pm.vaults = lambda: list(roots)
        pm.post = lambda card: posted.append(card) or f"id-{len(posted)}"
        buf = io.StringIO()
        tmp = None
        if state_dir is None:
            tmp = tempfile.TemporaryDirectory()
            state_dir = tmp.name
        try:
            with redirect_stdout(buf):
                code = pm.main(list(argv) + ["--state-dir", state_dir])
        finally:
            (pm.resolve_cap, pm.live_managers, pm.scan, pm.vaults, pm.post) = original
            if tmp is not None:
                tmp.cleanup()
        self.posted = posted
        return code, buf.getvalue()

    def test_no_candidates_exits_three(self):
        code, out = self.run_main([], rows=[])
        self.assertEqual(code, 3)
        self.assertIn("no card — no qualifying goal", out)

    def test_cards_in_a_dry_run_exit_zero_and_post_nothing(self):
        rows = [row("G", title="t1"), row("G", title="t2")]
        code, out = self.run_main([], rows=rows)
        self.assertEqual(code, 0)
        self.assertEqual(self.posted, [])
        self.assertIn("dry run", out)

    def test_post_writes_one_card_per_slot(self):
        rows = [row(f"G{n}", title="a") for n in range(5)]
        rows += [row(f"G{n}", title="b") for n in range(5)]
        code, _ = self.run_main(["--post"], cap=(3, 3, "default", "default"), count=1, rows=rows)
        self.assertEqual(code, 0)
        self.assertEqual(len(self.posted), 2)

    def test_the_dry_run_line_names_the_key_actually_posted(self):
        """The printed key must be the one `post()` receives, or the dry run lies.

        A goal in the approval inbox is posted under `fleet-approve-manager:<goal>`; a
        print that echoed the start-manager key would point the operator at a card the
        store never received. Measured 2026-10-20 — the line read `dedup_key()` while
        `post()` read `card_key()`, and only the posted body was right.
        """
        rows = [row("Queued", title="a"), row("Queued", title="b")]
        original = pm.goal_phase
        pm.goal_phase = lambda _dir, name: "todo" if name == "Queued" else None
        try:
            code, out = self.run_main([], rows=rows)
        finally:
            pm.goal_phase = original
        self.assertEqual(code, 0)
        self.assertIn("card: fleet-approve-manager:Queued", out)
        self.assertNotIn("card: fleet-propose-manager:Queued", out)

    def test_at_the_cap_posts_nothing_and_exits_three(self):
        rows = [row("G", title="a"), row("G", title="b")]
        code, out = self.run_main(["--post"], cap=(3, 3, "default", "default"), count=3, rows=rows)
        self.assertEqual(code, 3)
        self.assertEqual(self.posted, [])
        self.assertIn("at the manager cap", out)

    def test_the_off_switch_is_unlimited_and_posts_every_candidate(self):
        """The reproduced defect: `maxManagers: 0` resolved `hardLimit: null` and went silent."""
        rows = [row(f"G{n}", title="a") for n in range(4)]
        rows += [row(f"G{n}", title="b") for n in range(4)]
        code, out = self.run_main(["--post"], cap=(None, None, "config", "config"),
                                  count=9, rows=rows)
        self.assertEqual(code, 0)
        self.assertEqual(len(self.posted), 4)
        self.assertIn("unlimited", out)

    def test_an_unreadable_cap_refuses(self):
        code, _ = self.run_main([], cap=None)
        self.assertEqual(code, 2)

    def test_an_unreadable_board_refuses(self):
        code, _ = self.run_main([], count=None, labels=None)
        self.assertEqual(code, 2)

    def test_a_shared_dedup_key_refuses_rather_than_swallowing_one(self):
        rows = [row("G", vault=VAULT, title="a"), row("G", vault=VAULT, title="b"),
                row("G", vault=OTHER, title="a"), row("G", vault=OTHER, title="b")]
        code, _ = self.run_main(["--post"], rows=rows)
        self.assertEqual(code, 2)
        self.assertEqual(self.posted, [])

    def test_json_and_post_together_refuse(self):
        self.assertEqual(self.run_main(["--json", "--post"])[0], 2)


class PhaseGateTest(unittest.TestCase):
    """A goal in the approval inbox is asked for APPROVAL, never for a manager.

    The gate exists because `phase: todo` is the operator's inbox: a start-manager card on
    an unapproved goal would let the approval step be skipped by answering the wrong card.
    An ABSENT phase must keep the start card — absent is exempt, not unapproved, which is
    the contract `vault-cli goal approve` states.
    """

    def qualify(self, rows):
        return pm.qualify(rows, [], {}, BOARD)

    def card(self, goal, phase=None):
        candidate = {"goal": goal, "vault": "v", "approved": 2,
                     "tasks": ["a", "b"], "priority": 2}
        if phase is not None:
            candidate["phase"] = phase
        return pm.card_for(candidate, None, None)

    def test_a_todo_goal_gets_the_approve_card(self):
        card = self.card("Queued", phase="todo")
        self.assertEqual(card["options"], ["approve", "not now"])
        self.assertEqual(card["recommend"], "approve")
        self.assertEqual(card["dedup_key"], pm.approve_dedup_key("Queued"))
        self.assertIn('vault-cli goal approve "Queued"', card["context"])
        self.assertNotIn("Start a manager", card["payload"])

    def test_a_planning_goal_keeps_the_start_card(self):
        card = self.card("Ready", phase="planning")
        self.assertEqual(card["options"], ["start manager", "not now"])
        self.assertEqual(card["dedup_key"], pm.dedup_key("Ready"))

    def test_an_absent_phase_keeps_the_start_card(self):
        card = self.card("Unphased")
        self.assertEqual(card["options"], ["start manager", "not now"])

    def test_the_two_cards_never_share_a_dedup_key(self):
        """One key for both would let the store swallow whichever asked second."""
        self.assertNotEqual(self.card("G", phase="todo")["dedup_key"],
                            self.card("G", phase="planning")["dedup_key"])

    def test_goal_phase_reads_frontmatter_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, "G.md"), "w", encoding="utf-8") as handle:
                handle.write("---\nphase: todo\npriority: 2\n---\n\n"
                             "Prose mentioning phase: planning must not count.\n")
            self.assertEqual(pm.goal_phase(tmp, "G"), "todo")
            self.assertIsNone(pm.goal_phase(tmp, "Missing"))
            self.assertIsNone(pm.goal_phase(None, "G"))

    def test_qualify_carries_the_phase(self):
        rows = [row("Queued", title="a"), row("Queued", title="b"),
                row("Ready", title="c"), row("Ready", title="d")]
        original = pm.goal_phase
        pm.goal_phase = lambda _dir, name: "todo" if name == "Queued" else None
        try:
            by_goal = {c["goal"]: c["phase"] for c in self.qualify(rows)}
        finally:
            pm.goal_phase = original
        self.assertEqual(by_goal, {"Queued": "todo", "Ready": None})


class DeclinedAskTest(unittest.TestCase):
    """The declined-ask state — the re-ask loop this change exists to close.

    Each case pins one of the four `compare` arms, plus the two properties the state must
    have to be safe: it is isolated per goal, and a corrupt file refuses rather than
    reading as "nothing was declined".
    """

    def setUp(self):
        self.run = MainTest("test_no_candidates_exits_three")

    def two_runs(self, first, second, argv=("--post",)):
        with tempfile.TemporaryDirectory() as state:
            code1, out1 = self.run.run_main(list(argv), rows=first, state_dir=state)
            posted1 = list(self.run.posted)
            code2, out2 = self.run.run_main(list(argv), rows=second, state_dir=state)
            posted2 = list(self.run.posted)
        return (code1, out1, posted1), (code2, out2, posted2)

    def test_an_unchanged_ask_is_not_re_asked(self):
        """The reproduced defect: the second run must NOT write a second card."""
        rows = [row("G", title="a"), row("G", title="b")]
        first, second = self.two_runs(rows, rows)
        self.assertEqual(len(first[2]), 1)
        self.assertEqual(second[0], 0)
        self.assertEqual(second[2], [])
        self.assertIn("SUPPRESS:", second[1])
        self.assertIn("id-1", second[1])

    def test_a_new_approved_row_re_posts(self):
        """Arm 1 — a row not in the declined batch is a different ask."""
        first, second = self.two_runs(
            [row("G", title="a"), row("G", title="b")],
            [row("G", title="a"), row("G", title="b"), row("G", title="c")])
        self.assertEqual(len(first[2]), 1)
        self.assertEqual(len(second[2]), 1)
        self.assertIn("REPOST:", second[1])
        self.assertIn("not a subset", second[1])

    def test_an_unknown_row_re_posts(self):
        """Arm 2 — a blank phase is neither startable nor unstartable; unknown re-posts."""
        unknown = {"goal": "G", "vault": VAULT, "title": "c", "phase": "",
                   "startability": pm.UNKNOWN}
        first, second = self.two_runs(
            [row("G", title="a"), row("G", title="b"), row("G", title="c")],
            [row("G", title="a"), row("G", title="b"), unknown])
        self.assertEqual(len(first[2]), 1)
        self.assertEqual(len(second[2]), 1)
        self.assertIn("no readable startability", second[1])

    def test_a_row_that_became_unstartable_re_posts(self):
        """Arm 3 — a TRANSITION. The operator declined work they could start; parked now."""
        first, second = self.two_runs(
            [row("G", title="a"), row("G", title="b"), row("G", title="c")],
            [row("G", title="a"), row("G", title="b"), row("G", title="c", phase="todo")])
        self.assertEqual(len(first[2]), 1)
        self.assertEqual(len(second[2]), 1)
        self.assertIn("was startable in the declined snapshot", second[1])

    def test_a_permanently_parked_row_does_not_loop(self):
        """The loop guard: arm 3 must be a transition, never a state.

        This branch's `row_set` deliberately includes parked rows, so a state test
        ("unstartable this tick") would re-post every round for any goal holding one —
        the exact defect the declined-ask state exists to remove.
        """
        rows = [row("G", title="a"), row("G", title="b"),
                row("G", title="c", phase="todo")]
        first, second = self.two_runs(rows, rows)
        self.assertEqual(len(first[2]), 1)
        self.assertEqual(second[2], [])
        self.assertIn("SUPPRESS:", second[1])

    def test_a_row_that_became_startable_re_posts(self):
        """Arm 4 — work that was parked when declined and is actionable now is a change."""
        first, second = self.two_runs(
            [row("G", title="a"), row("G", title="b"), row("G", title="c", phase="todo")],
            [row("G", title="a"), row("G", title="b"), row("G", title="c")])
        self.assertEqual(len(first[2]), 1)
        self.assertEqual(len(second[2]), 1)
        self.assertIn("was unstartable in the declined snapshot", second[1])

    def test_a_fewer_row_ask_is_the_same_ask(self):
        """A completed row drops out of the set; that is not a new ask."""
        first, second = self.two_runs(
            [row("G", title="a"), row("G", title="b"), row("G", title="c")],
            [row("G", title="a"), row("G", title="b")])
        self.assertEqual(len(first[2]), 1)
        self.assertEqual(second[2], [])
        self.assertIn("SUPPRESS:", second[1])

    def test_the_snapshot_records_the_broader_row_set(self):
        """A parked row must be in the snapshot, or arms 3 and 4 could never fire."""
        with tempfile.TemporaryDirectory() as state:
            self.run.run_main(["--post"],
                              rows=[row("G", title="a"), row("G", title="b"),
                                    row("G", title="c", phase="todo")],
                              state_dir=state)
            files = os.listdir(state)
            self.assertEqual(len(files), 1)
            with open(os.path.join(state, files[0]), encoding="utf-8") as handle:
                data = json.load(handle)
        self.assertEqual(data["row_set"], ["a", "b", "c"])
        self.assertEqual(data["startability"], {"a": "startable", "b": "startable",
                                                "c": "unstartable"})
        self.assertEqual(data["card_item_id"], "id-1")

    def test_a_corrupt_state_refuses_rather_than_posting(self):
        with tempfile.TemporaryDirectory() as state:
            path = os.path.join(state, pm.key_slug(pm.dedup_key("G")) + ".json")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("not json")
            code, out = self.run.run_main(["--post"],
                                          rows=[row("G", title="a"), row("G", title="b")],
                                          state_dir=state)
        self.assertEqual(code, 2)
        self.assertEqual(self.run.posted, [])

    def test_clear_makes_the_goal_askable_again(self):
        """The `ANSWERED: start manager` path — a spent snapshot must not suppress for ever."""
        rows = [row("G", title="a"), row("G", title="b")]
        with tempfile.TemporaryDirectory() as state:
            self.run.run_main(["--post"], rows=rows, state_dir=state)
            code, out = self.run.run_main(["--clear", "--goal", "G"], state_dir=state)
            self.assertEqual(code, 0)
            self.assertIn("cleared G", out)
            code, _ = self.run.run_main(["--post"], rows=rows, state_dir=state)
        self.assertEqual(code, 0)
        self.assertEqual(len(self.run.posted), 1)

    def test_compare_exit_codes(self):
        """0 suppress · 10 re-post · 3 no snapshot · 2 refused — the step file's branch."""
        rows = [row("G", title="a"), row("G", title="b")]
        with tempfile.TemporaryDirectory() as state:
            self.assertEqual(self.run.run_main(["--compare"], rows=rows,
                                               state_dir=state)[0], 3)
            self.run.run_main(["--post"], rows=rows, state_dir=state)
            self.assertEqual(self.run.run_main(["--compare"], rows=rows,
                                               state_dir=state)[0], 0)
            self.assertEqual(self.run.run_main(
                ["--compare"], rows=rows + [row("G", title="c")], state_dir=state)[0], 10)
            with open(os.path.join(state, pm.key_slug(pm.dedup_key("G")) + ".json"), "w",
                      encoding="utf-8") as handle:
                handle.write("{]")
            self.assertEqual(self.run.run_main(["--compare"], rows=rows,
                                               state_dir=state)[0], 2)

    def test_the_state_is_keyed_per_goal_and_card_type(self):
        """Two goals never share a file, and neither do a goal's two card types."""
        with tempfile.TemporaryDirectory() as state:
            self.run.run_main(["--post"],
                              rows=[row("A", title="a"), row("A", title="b"),
                                    row("B", title="c"), row("B", title="d")],
                              state_dir=state)
            self.assertEqual(len(os.listdir(state)), 2)
            self.assertNotEqual(pm.key_slug(pm.dedup_key("A")), pm.key_slug(pm.dedup_key("B")))
            self.assertNotEqual(pm.key_slug(pm.dedup_key("A/B")), pm.key_slug(pm.dedup_key("A B")))
        self.assertNotEqual(pm.key_slug(pm.dedup_key("A")),
                            pm.key_slug(pm.approve_dedup_key("A")))

    def test_an_answered_approve_card_does_not_suppress_the_start_card(self):
        """The two cards ask different questions; the state must be keyed by the card's key.

        A goal-keyed snapshot would let an answered approve card suppress the
        start-manager card that the approval makes legitimate — a permanent silence in the
        direction the whole change exists to remove.
        """
        rows = [row("Queued", title="a"), row("Queued", title="b")]
        original = pm.goal_phase
        with tempfile.TemporaryDirectory() as state:
            pm.goal_phase = lambda _d, name: "todo" if name == "Queued" else None
            try:
                self.run.run_main(["--post"], rows=rows, state_dir=state)
                self.assertEqual(len(self.run.posted), 1)
                self.assertIn("fleet-approve-manager:Queued",
                              self.run.posted[0]["dedup_key"])
                pm.goal_phase = lambda _d, name: None
                code, out = self.run.run_main(["--post"], rows=rows, state_dir=state)
            finally:
                pm.goal_phase = original
        self.assertEqual(code, 0)
        self.assertEqual(len(self.run.posted), 1)
        self.assertIn("fleet-propose-manager:Queued", self.run.posted[0]["dedup_key"])
        self.assertNotIn("SUPPRESS", out)


if __name__ == "__main__":
    unittest.main()
