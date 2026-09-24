#!/usr/bin/env python3
"""Tests for scripts/fleet-board.py.

Covers the two things the board must not get wrong, both of which fail silently
in production:

  * **Every bucket consults a signal the registry status cannot supply.** A stub
    that maps `busy`->running, `idle`->idle, else->problem passes a naive reading
    of the design and is wrong: it has no `needs-input` output at all, and it
    reports a stuck `busy` session as `running`. The fixtures below make both
    failures observable — `GATED` is `idle` and must still reach `needs-input`,
    and `STUCK` is `busy` and must reach `problem` rather than `running`.

  * **No row is silently dropped.** The board's whole reason to exist is that the
    previous join was done in prose and could miss a session without saying so.
    `coverage_errors()` is the positive control, and
    `test_coverage_control_fails_on_a_dropped_row` proves it can fail — a control
    that has never failed is indistinguishable from one that cannot. The tree adds
    a second place a row can vanish (`build_tree` returning fewer ids than it was
    given), so `tree_errors()` carries its own positive control.

  * **The grouping rules 1-4 put each session under the right parent.** The four
    rules are the whole point of the tree, and every one of them can be skipped by
    an implementation that still renders a plausible-looking board: drop the goal
    chain and everything lands under the root; drop the colour signal and every
    manager becomes a worker. The fixtures below make each rule observable.

The fixtures are `constructed`: hand-written to exercise the branches, carrying no
claim about the live fleet. The live claims belong to `# Results` on the task page,
which records a real run against the real registry.

Run: python3 -m unittest discover -s scripts/tests -v
"""

import importlib.util
import os
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_SCRIPT = os.path.join(os.path.dirname(_HERE), "fleet-board.py")

_spec = importlib.util.spec_from_file_location("fleet_board", _SCRIPT)
fb = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fb)

# The renderer, imported the same way, so the no-wrap assertion is made against
# the thing that actually draws the box rather than a second width model that
# could disagree with it.
_BSpec = importlib.util.spec_from_file_location(
    "box_table", os.path.join(os.path.dirname(_HERE), "box-table.py")
)
bt = importlib.util.module_from_spec(_BSpec)
_BSpec.loader.exec_module(bt)

CWD = "/Users/x/Documents/Obsidian/Personal"


def _sid(n):
    return f"aaaabbbb-0000-0000-0000-00000000000{n}"


# `constructed` — one session of each kind, plus the two collision cases.
REGISTRY = {
    _sid(1): {"status": "busy", "name": "Busy One", "cwd": CWD},
    _sid(2): {"status": "idle", "name": "Idle One", "cwd": CWD},
    _sid(3): {"status": "idle", "name": "Gated One", "cwd": CWD},
    _sid(4): {"status": "busy", "name": "Stuck One", "cwd": CWD},
    _sid(5): {"status": "waiting", "name": "Waiting One", "cwd": CWD},
    _sid(6): {"status": "busy", "name": "Busy And Gated", "cwd": CWD},
}
GATES = {_sid(3), _sid(6)}
STUCK = {_sid(4)}
AGES = {sid: 5.0 for sid in REGISTRY}
TITLES = {_sid(1): ["Some Task"]}


def buckets(registry=None, gates=None, stuck=None):
    rows, _ = fb.build_rows(
        registry if registry is not None else REGISTRY,
        GATES if gates is None else gates,
        STUCK if stuck is None else stuck,
        TITLES,
        AGES,
    )
    return {r["session_id"]: r["bucket"] for r in rows}


class TestBuckets(unittest.TestCase):
    def test_each_bucket_is_reachable(self):
        """One session of each kind lands in its own bucket."""
        b = buckets()
        self.assertEqual(b[_sid(1)], "running")
        self.assertEqual(b[_sid(2)], "idle")
        self.assertEqual(b[_sid(3)], "needs-input")
        self.assertEqual(b[_sid(4)], "problem")
        self.assertEqual(sorted(set(b.values())), ["idle", "needs-input", "problem", "running"])

    def test_problem_outranks_running(self):
        """A `busy` session past --stuck-min is `problem`, never `running`.

        The collision case. Without it the precedence rule is prose, and a stub
        that reads only `status` reports a wedged session as working.
        """
        self.assertEqual(buckets()[_sid(4)], "problem")

    def test_needs_input_outranks_running(self):
        """A `busy` session holding an open gate is `needs-input` — a gate blocks
        whatever the status field says."""
        self.assertEqual(buckets()[_sid(6)], "needs-input")

    def test_a_gate_on_an_idle_session_is_needs_input(self):
        """The second signal is independent of status: `idle` alone would say
        nothing is wanted, and the gate says otherwise."""
        b = buckets(gates=set())
        self.assertEqual(b[_sid(3)], "idle", "without a gate the same session is idle")
        self.assertEqual(buckets()[_sid(3)], "needs-input")

    def test_waiting_falls_through_to_idle(self):
        """`waiting` is transient and explicitly NOT blocked-on-a-human, so it
        must not invent a fifth bucket — the classification has to be total."""
        self.assertEqual(buckets()[_sid(5)], "idle")

    def test_a_status_only_stub_cannot_pass(self):
        """The lazy implementation, run against the same fixtures, gets two rows
        wrong — so the suite fails it rather than blessing it."""

        def stub(status):
            return "running" if status in ("busy", "shell") else "idle"

        real = buckets()
        lazy = {sid: stub(rec["status"]) for sid, rec in REGISTRY.items()}
        self.assertNotEqual(real, lazy)
        self.assertEqual(lazy[_sid(4)], "running", "the stub calls the stuck session running")
        self.assertEqual(lazy[_sid(3)], "idle", "the stub misses the gate entirely")

    def test_every_registry_entry_gets_exactly_one_row(self):
        rows, _ = fb.build_rows(REGISTRY, GATES, STUCK, TITLES, AGES)
        self.assertEqual(len(rows), len(REGISTRY))
        self.assertEqual(len({r["session_id"] for r in rows}), len(REGISTRY))

    def test_an_absent_transcript_renders_as_absent(self):
        """`session_transcript_age()` answers `inf` for no transcript, which is not
        a duration — it must not reach `human_age()` and must not print `inf`."""
        rows, _ = fb.build_rows(REGISTRY, GATES, STUCK, TITLES, {_sid(1): float("inf")})
        cell = next(r for r in rows if r["session_id"] == _sid(1))["cells"][-1]
        self.assertEqual(cell, "—")

    def test_rows_render_through_box_table(self):
        """The document is valid box-table input, and no cell overruns its width."""
        rows, _ = fb.build_rows(REGISTRY, GATES, STUCK, TITLES, AGES)
        for r in rows:
            self.assertEqual(len(r["cells"]), len(fb.HEADER))
            for cell, w in zip(r["cells"], fb.WIDTHS):
                self.assertLessEqual(_dwidth(cell), w, f"cell {cell!r} overruns its column")


def _dwidth(s):
    """Display width, mirroring box-table.py — emoji occupy two cells."""
    import unicodedata

    total = 0
    for i, ch in enumerate(s):
        w = 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
        if w == 1 and i + 1 < len(s) and s[i + 1] == "️":
            w = 2
        total += w
    return total


class TestCoverageControl(unittest.TestCase):
    """SC3 — the assertion that makes a silently-dropped row impossible."""

    def setUp(self):
        self.registry_ids = set(REGISTRY)
        self.row_ids = list(self.registry_ids)
        self.fresh = {_sid(1), _sid(2)}

    def test_a_complete_board_passes(self):
        self.assertEqual(fb.coverage_errors(self.row_ids, self.registry_ids, self.fresh), [])

    def test_a_dropped_row_fails(self):
        """The positive control. If this cannot fail, the assertion proves nothing."""
        errors = fb.coverage_errors(self.row_ids[:-1], self.registry_ids, self.fresh)
        self.assertTrue(errors, "a dropped registry entry must be an error")

    def test_a_dropped_transcript_fresh_row_fails(self):
        """(b): the independently-derived half. A fresh session the registry
        carries but the rows omit is exactly the silent-drop failure."""
        errors = fb.coverage_errors(
            [i for i in self.row_ids if i != _sid(1)], self.registry_ids, self.fresh
        )
        self.assertTrue(any("transcript-fresh" in e for e in errors))

    def test_a_duplicated_row_fails(self):
        errors = fb.coverage_errors(self.row_ids + [_sid(1)], self.registry_ids, self.fresh)
        self.assertTrue(any("duplicate" in e for e in errors))

    def test_a_residual_is_not_an_error(self):
        """(c): a transcript-fresh session holding no registry entry is a real
        population — a headless worker holds no entry at all — so it is reported,
        never treated as a coverage failure and never asserted equal."""
        fresh = self.fresh | {"ccccdddd-0000-0000-0000-000000000099"}
        self.assertEqual(fb.coverage_errors(self.row_ids, self.registry_ids, fresh), [])


def _group_index():
    """A `constructed` vault: two topics, two goals, one goal owned by a topic.

    `constructed` — hand-written to exercise the branches, carrying no claim about
    the live vault. The live claims belong to `# Progress` on the task page, which
    quotes a real run against the real vault.
    """
    return fb.VaultIndex(
        topics=["Manager Layer", "Attention Routing"],
        goals=[
            "A Manager Sweep Costs Nothing When Nothing Changed",
            "The Operator Never Opens a Worker Tab to Answer a Question",
        ],
        task_titles=["Some Task", "Deep Task", "Goal-Less Task"],
        task_goals={
            "some task": ["The Operator Never Opens a Worker Tab to Answer a Question"],
            "deep task": ["A Manager Sweep Costs Nothing When Nothing Changed"],
        },
        goal_topics={
            "the operator never opens a worker tab to answer a question": ["Manager Layer"],
            "a manager sweep costs nothing when nothing changed": ["Manager Layer"],
        },
    )


# `constructed` — one session per rule, plus the two specificity cases.
GROUP_REGISTRY = {
    _sid(9): {"status": "idle", "name": "Fleet Manager", "cwd": CWD},
    _sid(2): {"status": "idle", "name": "Manager Layer", "cwd": CWD},
    _sid(6): {"status": "idle", "name": "Orange One", "cwd": CWD},
    _sid(7): {"status": "idle", "name": "A Manager Sweep Costs Nothing When Nothing Changed", "cwd": CWD},
    _sid(3): {"status": "busy", "name": "Some Task", "cwd": CWD},
    _sid(8): {"status": "idle", "name": "Deep Task", "cwd": CWD},
    _sid(4): {"status": "idle", "name": "Goal-Less Task", "cwd": CWD},
    _sid(5): {"status": "idle", "name": "Unstamped Session", "cwd": CWD},
}
GROUP_TITLES = {_sid(3): ["Some Task"], _sid(8): ["Deep Task"], _sid(4): ["Goal-Less Task"]}
GROUP_COLOURS = {_sid(6): "orange"}
GROUP_AGES = {sid: 5.0 for sid in GROUP_REGISTRY}


def _grouping():
    return fb.build_grouping(GROUP_REGISTRY, _group_index(), GROUP_COLOURS, set(), GROUP_TITLES)


def _group_rows():
    g = _grouping()
    rows, _ = fb.build_rows(GROUP_REGISTRY, set(), set(), GROUP_TITLES, GROUP_AGES, grouping=g)
    return g, rows


class TestGroupingRules(unittest.TestCase):
    """Design rules 1-4 — each one observable, so none can be silently skipped."""

    def test_rule_1_the_root_is_the_fleet_manager(self):
        g = _grouping()
        self.assertEqual(g.role_of(_sid(9)), "manager")
        self.assertIsNone(g.parent_of(_sid(9)), "the root answers to nobody")

    def test_rule_1_a_name_resolving_to_a_topic_page_is_a_manager(self):
        self.assertEqual(_grouping().role_of(_sid(2)), "manager")

    def test_rule_1_colour_alone_makes_a_manager(self):
        """The signal that catches a manager whose subject cannot be resolved.

        Without it every subject-less manager reads as a worker — the whole class
        the tree exists to make visible.
        """
        g = _grouping()
        self.assertEqual(g.role_of(_sid(6)), "manager")
        self.assertIsNone(g.subject_of(_sid(6)), "orange proves a manager, not its scope")

    def test_rule_2_the_goal_chain_nests_a_worker_under_its_manager(self):
        """goal -> topic listing that goal -> the live manager for that topic."""
        g = _grouping()
        self.assertEqual(g.role_of(_sid(3)), "worker")
        self.assertEqual(g.parent_of(_sid(3)), _sid(2))

    def test_rule_2_a_manager_whose_subject_is_the_goal_beats_the_topic(self):
        """Both a goal-subject manager and its topic's manager exist; the goal is
        the more specific parent and must win."""
        g = _grouping()
        self.assertEqual(g.parent_of(_sid(8)), _sid(7))

    def test_rule_3_a_goal_less_worker_goes_under_the_root(self):
        g = _grouping()
        self.assertEqual(g.role_of(_sid(4)), "worker")
        self.assertEqual(g.parent_of(_sid(4)), _sid(9))

    def test_rule_3_a_goal_with_no_live_manager_goes_under_the_root(self):
        """The chain resolves but no manager is live to own it, so the fleet layer
        does — a worker is never left parentless."""
        without = {k: v for k, v in GROUP_REGISTRY.items() if k != _sid(2)}
        g = fb.build_grouping(without, _group_index(), GROUP_COLOURS, set(), GROUP_TITLES)
        self.assertEqual(g.role_of(_sid(3)), "worker")
        self.assertEqual(g.parent_of(_sid(3)), _sid(9))

    def test_rule_4_no_task_file_is_unmanaged(self):
        g = _grouping()
        self.assertEqual(g.role_of(_sid(5)), "unmanaged")
        self.assertEqual(g.parent_of(_sid(5)), "unmanaged")

    def test_rule_4_does_not_swallow_a_manager(self):
        """A manager carries no task stamp either; detection runs first, so the
        root and every orange manager stay out of `Unmanaged`."""
        g = _grouping()
        for sid in (_sid(9), _sid(2), _sid(6), _sid(7)):
            self.assertEqual(g.role_of(sid), "manager", sid)


class TestManagerSubject(unittest.TestCase):
    """The two sources, in order — and the one signal that is banned."""

    def test_source_one_resolves_through_the_loop_record(self):
        """A name that matches no page still resolves when the loop's slug does."""
        self.assertEqual(
            fb.manager_subject("Manager-Layer", _group_index(), {"manager-layer"}),
            ("topic", "Manager Layer"),
        )

    def test_source_two_matches_the_name_exactly_and_case_insensitively(self):
        self.assertEqual(
            fb.manager_subject("manager layer", _group_index(), set()),
            ("topic", "Manager Layer"),
        )

    def test_source_two_allows_a_manager_suffix(self):
        self.assertEqual(
            fb.manager_subject("Manager Layer Manager", _group_index(), set()),
            ("topic", "Manager Layer"),
        )

    def test_an_unresolvable_manager_answers_none_rather_than_guessing(self):
        self.assertIsNone(fb.manager_subject("Vuln Fix Agent", _group_index(), set()))


class TestTree(unittest.TestCase):
    """The drawn tree: every session row exactly once, under the right glyph."""

    def setUp(self):
        self.grouping, self.rows = _group_rows()
        self.tree, self.ordered = fb.build_tree(self.rows, self.grouping)

    def cell(self, sid):
        """The Session cell drawn for one session, whatever its depth."""
        label = next(x["label"] for x in self.rows if x["session_id"] == sid)
        return next(r[0] for r in self.tree if label in r[0])

    def test_the_tree_carries_every_session_row_exactly_once(self):
        self.assertEqual(sorted(self.ordered), sorted(GROUP_REGISTRY))
        self.assertEqual(len(self.ordered), len(set(self.ordered)))

    def test_a_group_header_is_not_a_session_row(self):
        """`Unmanaged` names a group; counting it as a session would make the
        coverage assertion fail on a correct board."""
        self.assertIn(["Unmanaged", "", "", "", ""], self.tree)
        self.assertEqual(len(self.tree), len(GROUP_REGISTRY) + 1)
        self.assertNotIn("Unmanaged", self.ordered)

    def test_the_root_is_flush_and_its_children_branch(self):
        self.assertEqual(self.tree[0][0], "Fleet Manager")
        self.assertTrue(self.tree[1][0].startswith("├ "), self.tree[1][0])

    def test_a_grandchild_carries_its_parents_continuation_bar(self):
        cell = self.cell(_sid(3))
        self.assertTrue(cell.startswith("│   └ "), cell)

    def test_the_last_child_uses_the_closing_glyph(self):
        cell = self.cell(_sid(5))
        self.assertTrue(cell.startswith("└ "), cell)

    def test_a_manager_row_carries_its_subject_kind(self):
        self.assertTrue(self.cell(_sid(2)).endswith(" (topic)"), self.cell(_sid(2)))

    def test_the_tree_control_can_fail(self):
        """The positive control. A row that is built but never drawn is the same
        silent drop `coverage_errors()` guards one step earlier."""
        ids = [r["session_id"] for r in self.rows]
        self.assertTrue(fb.tree_errors(self.ordered[:-1], ids), "a missing row must fail")
        self.assertTrue(fb.tree_errors(self.ordered + [self.ordered[0]], ids), "a duplicate must fail")
        self.assertTrue(fb.tree_errors(self.ordered + [_sid(1)], ids), "an unbacked row must fail")
        self.assertEqual(fb.tree_errors(self.ordered, ids), [])

    def test_every_row_fits_the_box_table_contract(self):
        for row in self.tree:
            self.assertEqual(len(row), len(fb.HEADER))

    def test_the_rendered_box_never_wraps(self):
        """SC1's no-wrap clause. The tree's glyphs and the `(topic)` suffix are
        truncated by the renderer, so the cell — not the row — is where the budget
        is spent, and only a rendered box can prove it."""
        doc = {"header": fb.HEADER, "rows": self.tree, "widths": fb.WIDTHS}
        for line in bt.render(doc, link=False).splitlines():
            self.assertLessEqual(bt.dwidth(line), 119, line)


class TestVaultParsing(unittest.TestCase):
    """The two parsers whose false positives were measured, not imagined."""

    def test_frontmatter_links_reads_a_list(self):
        fm = "goals:\n    - '[[Goal One]]'\n    - '[[Goal Two]]'\nstatus: in_progress\n"
        self.assertEqual(fb.frontmatter_links(fm, "goals"), ["Goal One", "Goal Two"])

    def test_frontmatter_links_treats_an_empty_list_as_no_link(self):
        """`goals: []` is the explicit "serves no goal" stamp, not a missing key."""
        self.assertEqual(fb.frontmatter_links("goals: []\nstatus: in_progress\n", "goals"), [])

    def test_frontmatter_links_stops_at_the_next_key(self):
        fm = "goals:\n    - '[[Goal One]]'\nthemes:\n    - '[[Theme]]'\n"
        self.assertEqual(fb.frontmatter_links(fm, "goals"), ["Goal One"])

    def test_goals_section_ignores_a_prose_mention(self):
        """The measured false positive: a note recording that a goal was REMOVED
        from the list names it, and reading whole lines claimed it as a member."""
        text = (
            "## Goals\n\n"
            "- [[Goal One]] — a real member\n\n"
            "⚠️ **Scope narrowed.** [[Goal Two]] was removed from this list.\n\n"
            "## Boundaries\n"
            "- [[Goal Three]]\n"
        )
        self.assertEqual(fb._goals_section_links(text), ["Goal One"])

    def test_goals_section_takes_one_link_per_entry(self):
        """An entry is `- [[Goal]] — annotation linking [[Other]]`; the entry
        admits one member and the rest is provenance."""
        text = "## Goals\n\n- [[Goal One]] — added 2026-09-20, resolving [[Something Else]]\n"
        self.assertEqual(fb._goals_section_links(text), ["Goal One"])

    def test_goals_section_stops_at_the_next_heading(self):
        text = "## Goals\n- [[Goal One]]\n\n## Boundaries\n- [[Goal Two]]\n"
        self.assertEqual(fb._goals_section_links(text), ["Goal One"])


class TestGateAttribution(unittest.TestCase):
    """Each `needs-input` row must name ITS OWN pane or its own reason.

    Measured 2026-09-23: every `needs-input` detail read the same literal
    `"waiting on an open gate"`, 4 of 4 rows, so `/supervisor:fleet-verify`
    check 4 could not tie a bucket to a pane and reported UNKNOWN — which is
    exactly the shape that hid a real bucket/pane mismatch. A string identical
    across rows attributes nothing.
    """

    def gate(self, sid, pane, kind="permission"):
        return {"session_id": sid, "pane": pane, "kind": kind}

    def test_each_gated_session_carries_its_own_pane(self):
        panes = fb.gate_attribution(
            [self.gate(_sid(3), 1039), self.gate(_sid(6), 1040, "question")],
            {_sid(3), _sid(6)},
        )
        self.assertEqual(panes[_sid(3)], "pane 1039 — open permission gate")
        self.assertEqual(panes[_sid(6)], "pane 1040 — open question gate")

    def test_only_gated_sessions_are_attributed(self):
        """A record for a session outside `gate_ids` (answered, parked, reapable)
        is not an open gate and must not lend its pane to the detail."""
        panes = fb.gate_attribution([self.gate(_sid(2), 1041)], {_sid(3)})
        self.assertNotIn(_sid(2), panes)

    def test_an_idle_record_lends_nothing_to_a_gated_session(self):
        """Measured live 2026-09-25: a gated session also holds its `idle` record,
        and the first cut rendered `open idle, question gates`. `idle` is not a
        gate kind — it names neither the gate nor, reliably, its pane."""
        panes = fb.gate_attribution(
            [self.gate(_sid(3), 1737, "idle"), self.gate(_sid(3), 1737, "question")],
            {_sid(3)},
        )
        self.assertEqual(panes[_sid(3)], "pane 1737 — open question gate")

    def test_several_gates_on_one_session_list_every_pane(self):
        panes = fb.gate_attribution(
            [self.gate(_sid(3), 1040), self.gate(_sid(3), 1039, "question")],
            {_sid(3)},
        )
        self.assertEqual(panes[_sid(3)], "panes 1039, 1040 — open permission, question gates")

    def test_rows_carry_distinct_attribution(self):
        attribution = {_sid(3): "pane 1039 — open permission gate",
                       _sid(6): "pane 1040 — open question gate"}
        _, details = fb.build_rows(REGISTRY, GATES, STUCK, TITLES, AGES,
                                   gate_attribution=attribution)
        self.assertEqual(details[_sid(3)], attribution[_sid(3)])
        self.assertEqual(details[_sid(6)], attribution[_sid(6)])
        self.assertEqual(len(set(details.values())), len(details),
                         "a detail repeated across rows attributes nothing")

    def test_unattributed_gate_names_its_own_reason_not_a_stub(self):
        """A gate id with no attribution is a real, reportable situation — say
        which one, never fall back to one sentence shared by every row."""
        _, details = fb.build_rows(REGISTRY, GATES, STUCK, TITLES, AGES,
                                   gate_attribution={_sid(6): "pane 1040 — open question gate"})
        self.assertIn(_sid(3), details)
        self.assertIn("no pane", details[_sid(3)])
        self.assertIn(_sid(3)[:8], details[_sid(3)])
        self.assertNotEqual(details[_sid(3)], "waiting on an open gate")

    def test_attribution_parses_with_the_feeds_own_pane_pattern(self):
        """Check 4 joins a detail to a pane; the id must be readable by the same
        pattern `who-needs-me.py` uses for pane references."""
        panes = fb.gate_attribution([self.gate(_sid(3), 1039)], {_sid(3)})
        m = fb.wnm._PANE_REF.search(panes[_sid(3)])
        self.assertIsNotNone(m)
        self.assertEqual(m.group(1), "1039")


if __name__ == "__main__":
    unittest.main()
