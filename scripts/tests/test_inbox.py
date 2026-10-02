#!/usr/bin/env python3
"""Tests for scripts/inbox.py.

The inbox is a FILTER, so every test here is about what it must NOT admit as
much as what it must. Two properties carry the whole surface:

1. **A dated one-off is not recurring.** The recurring exclusion keys on an
   ISO-week token (`2026W35`). A plain ISO DATE in a title looks like a cadence
   marker and is not one — `Fix Failed CI Build claude-supervisor 2026-09-30` is
   a one-off. A date-shaped test would drop every such row from the inbox, and
   the loss is silent: the operator sees a shorter list and no error. Both
   directions are asserted, because a test that only proves "the week token is
   caught" passes just as well against a matcher that catches everything.

2. **A vault that cannot answer is skipped, not fatal.** One configured vault
   returns literal `null` rather than a task list. A scan that raised on it
   would abort mid-loop and silently truncate every vault after it in iteration
   order — producing a SHORT list that reads as a complete one. The control here
   is a scan whose second vault is broken: the first and third must still land.

The `why_it_matters` and grouping tests are ordinary shape checks, included so a
future edit to the render path has a failing test rather than a silently shorter
line.

Run: python3 -m unittest discover -s scripts/tests -v
"""
import contextlib
import importlib.util
import io
import os
import re
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location(
    "inbox", os.path.join(HERE, os.pardir, "inbox.py"))
inbox = importlib.util.module_from_spec(spec)
sys.modules["inbox"] = inbox
spec.loader.exec_module(inbox)

TASK = """---
phase: {phase}
status: {status}
{extra}---
{body}
"""


def write(d, name, phase="todo", status="next", extra="", body=""):
    with open(os.path.join(d, name + ".md"), "w", encoding="utf-8") as fh:
        fh.write(TASK.format(phase=phase, status=status, extra=extra, body=body))
    return name + ".md"


class RecurringTest(unittest.TestCase):
    """Property 1 — the cadence discriminator."""

    def test_week_token_is_recurring(self):
        for title in ("a recurring task - 2026W35-sat", "sweep 2026W01"):
            self.assertTrue(inbox.is_recurring({}, title), title)

    def test_recurring_frontmatter_key_is_recurring(self):
        self.assertTrue(inbox.is_recurring({"recurring": "weekly"}, "plain title"))

    def test_dated_one_off_is_not_recurring(self):
        # The negative control. A date-shaped matcher fails exactly here, and it
        # fails silently — the row just vanishes from the operator's list.
        for title in ("Fix Failed CI Build claude-supervisor 2026-09-30",
                      "Review the 2026-09-27 incident",
                      "A Criterion Authored at a Distance Is Audited by Someone"):
            self.assertFalse(inbox.is_recurring({}, title), title)


class ScanTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = self.tmp.name

    def vault(self, name, tasks_dir="25 Tasks", topics_dir=False):
        d = os.path.join(self.root, name, tasks_dir)
        os.makedirs(d, exist_ok=True)
        return (name, os.path.join(self.root, name), tasks_dir, topics_dir), d

    def test_live_row_is_admitted(self):
        v, d = self.vault("v")
        write(d, "An unapproved task")
        rows, skipped = inbox.scan([v])
        self.assertEqual([r["title"] for _, r in rows], ["An unapproved task"])
        self.assertEqual(skipped, [])

    def test_terminal_and_parked_statuses_are_excluded(self):
        v, d = self.vault("v")
        for status in ("completed", "aborted", "backlog", "hold"):
            write(d, f"row {status}", status=status)
        write(d, "row live", status="in_progress")
        rows, _ = inbox.scan([v])
        self.assertEqual([r["title"] for _, r in rows], ["row live"])

    def test_non_todo_phase_is_excluded(self):
        v, d = self.vault("v")
        write(d, "already approved", phase="planning")
        write(d, "no phase", phase="")
        rows, _ = inbox.scan([v])
        self.assertEqual(rows, [])

    def test_recurring_instance_is_excluded(self):
        v, d = self.vault("v")
        write(d, "routine - 2026W35-sat")
        write(d, "carries the key", extra="recurring: weekly\n")
        write(d, "a real one")
        rows, _ = inbox.scan([v])
        self.assertEqual([r["title"] for _, r in rows], ["a real one"])

    def test_unreadable_vault_is_skipped_and_never_fatal(self):
        """Property 2 — the scan must not truncate on a bad vault."""
        a, da = self.vault("a")
        b = ("b", os.path.join(self.root, "b"), "25 Tasks", False)  # never created
        c, dc = self.vault("c")
        write(da, "from a")
        write(dc, "from c")
        rows, skipped = inbox.scan([a, b, c])
        self.assertEqual(sorted(r["title"] for _, r in rows), ["from a", "from c"])
        self.assertEqual(len(skipped), 1)
        self.assertIn("b", skipped[0])

    def test_only_narrows_to_one_vault(self):
        a, da = self.vault("a")
        c, dc = self.vault("c")
        write(da, "from a")
        write(dc, "from c")
        rows, _ = inbox.scan([a, c], only="c")
        self.assertEqual([r["title"] for _, r in rows], ["from c"])


class GroupingTest(unittest.TestCase):
    def test_topic_from_frontmatter_wins(self):
        self.assertEqual(
            inbox.topic_of({"topics": "[[Manager Layer]]"}, "", True),
            "Manager Layer")

    def test_topic_from_body_line(self):
        self.assertEqual(
            inbox.topic_of({}, "Topic: [[Work Approval]] — topic-direct", True),
            "Work Approval")

    def test_no_topic_when_vault_declares_none(self):
        # A vault without a topics_dir groups by vault, so this must be None
        # even when the row happens to carry a Topic: line.
        self.assertIsNone(
            inbox.topic_of({}, "Topic: [[Work Approval]]", False))


class WhyTest(unittest.TestCase):
    def test_impact_first_paragraph(self):
        body = "# Impact\n\nAgents filed 59 tasks.\n\nSecond para.\n\n# Tasks\n\n- [ ] x\n"
        self.assertEqual(inbox.why_it_matters(body), "Agents filed 59 tasks.")

    def test_absent_impact_falls_back_to_first_paragraph(self):
        self.assertEqual(inbox.why_it_matters("Just a line.\n\nMore."),
                         "Just a line.")

    def test_empty_body_is_empty_not_an_error(self):
        self.assertEqual(inbox.why_it_matters(""), "")

    def test_long_line_is_truncated(self):
        out = inbox.why_it_matters("x" * 400)
        self.assertEqual(len(out), inbox.WHY_WIDTH + 1)
        self.assertTrue(out.endswith("…"))


class ResolveTest(unittest.TestCase):
    """The verb path's vault lookup — it must refuse, never guess.

    Titles are not namespaced across vaults, so a same-named live row in two
    vaults is a real state rather than a hypothetical. A resolver that took the
    first match would move the wrong row in the wrong vault, and the operator
    would see a successful approve on a task they never named.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = self.tmp.name

    def vault(self, name):
        d = os.path.join(self.root, name, "25 Tasks")
        os.makedirs(d, exist_ok=True)
        return (name, os.path.join(self.root, name), "25 Tasks", False), d

    def run_resolve(self, title):
        with mock.patch.object(inbox, "load_vaults", lambda: self.vaults):
            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                rc = inbox.main(["--resolve", title])
        return rc, out.getvalue().strip(), err.getvalue().strip()

    def test_unique_row_resolves_to_its_vault(self):
        a, da = self.vault("a")
        self.vaults = [a]
        write(da, "Only here")
        rc, out, _ = self.run_resolve("Only here")
        self.assertEqual((rc, out), (0, "a"))

    def test_unknown_row_refuses(self):
        a, da = self.vault("a")
        self.vaults = [a]
        write(da, "Something else")
        rc, _, err = self.run_resolve("Not a task")
        self.assertEqual(rc, 3)
        self.assertIn("no live row", err)

    def test_same_title_in_two_vaults_refuses(self):
        a, da = self.vault("a")
        b, db = self.vault("b")
        self.vaults = [a, b]
        write(da, "Shared title")
        write(db, "Shared title")
        rc, _, err = self.run_resolve("Shared title")
        self.assertEqual(rc, 4)
        self.assertIn("more than one vault", err)

    def test_settled_row_does_not_resolve(self):
        # Already approved is not live, so a second verb cannot re-decide it.
        a, da = self.vault("a")
        self.vaults = [a]
        write(da, "Already approved", phase="planning")
        rc, _, _ = self.run_resolve("Already approved")
        self.assertEqual(rc, 3)


def row(title, goals=(), blocked_by=()):
    return {"title": title, "vault": "v", "why": "", "goals": list(goals),
            "blocked_by": list(blocked_by)}


class ParseBlockListTest(unittest.TestCase):
    """A YAML block list (`topics:` then `  - '[[X]]'`) was read as empty, so a
    marked row fell to its vault. Both shapes must yield the same topic."""

    def test_block_and_inline_topics_agree(self):
        block, _ = inbox.parse("---\ntopics:\n    - '[[Work Approval]]'\n---\n")
        inline, _ = inbox.parse("---\ntopics: ['[[Work Approval]]']\n---\n")
        self.assertEqual(inbox.topic_of(block, "", True), "Work Approval")
        self.assertEqual(inbox.topic_of(inline, "", True), "Work Approval")

    def test_empty_list_is_no_topic(self):
        fm, _ = inbox.parse("---\ntopics: []\n---\n")
        self.assertIsNone(inbox.topic_of(fm, "", True))


class ScopeTest(unittest.TestCase):
    """SC1 + SC2 — the session's subject, else the residual against LIVE managers."""

    ROWS = [("Work Approval", row("a")), ("Manager Layer", row("b")),
            ("vault-x", row("c", goals=["Some Goal"])), ("vault-x", row("d"))]

    def test_subject_view_is_exactly_the_subject(self):
        got = inbox.scope(self.ROWS, "Work Approval", set())
        self.assertEqual({g for g, _ in got}, {"Work Approval"})

    def test_subject_matches_a_goal(self):
        got = inbox.scope(self.ROWS, "Some Goal", set())
        self.assertEqual([r["title"] for _, r in got], ["c"])

    def test_residual_drops_live_keeps_stale(self):
        got = inbox.scope(self.ROWS, None, {"work-approval"})
        titles = [r["title"] for _, r in got]
        self.assertNotIn("a", titles)       # live manager owns it
        self.assertIn("b", titles)          # no live manager: residual

    def test_session_record_selects_scope(self):
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "s1.json"), "w") as fh:
                fh.write('{"subject": "Work Approval"}')
            with mock.patch.object(inbox, "MANAGER_DIR", d):
                self.assertEqual(inbox.session_subject("s1"), "Work Approval")
                self.assertIsNone(inbox.session_subject("absent"))
                self.assertIsNone(inbox.session_subject(None))

    def test_live_slugs_reads_cadence_age_not_records(self):
        with tempfile.TemporaryDirectory() as d:
            now = 1_000_000.0
            for name, age in (("fresh", 10), ("stale", 10_000)):
                p = os.path.join(d, name + ".cadence")
                with open(p, "w") as fh:
                    fh.write("300\n")
                os.utime(p, (now - age, now - age))
            with mock.patch.dict(os.environ, {"MANAGER_LIVENESS_STATE_DIR": d}):
                live = inbox.live_slugs(now)
            self.assertEqual(live, {"fresh"})
            # Read-only: --check would have written liveness-notified.json.
            self.assertFalse(os.path.exists(os.path.join(d, "liveness-notified.json")))


class RankTest(unittest.TestCase):
    """SC3 + SC4 — clause (7)'s order and cap, and nothing singled out."""

    def test_order_goal_then_unblocker_then_score_then_unscored(self):
        rows = [("g", row("z-unscored")), ("g", row("low")), ("g", row("high")),
                ("g", row("unblocker")), ("g", row("waiter", blocked_by=["unblocker"])),
                ("g", row("goal-row", goals=["G"]))]
        scores = {"low": 5, "high": 9, "waiter": 6}
        got = [r["title"] for _, r in inbox.rank(rows, scores)]
        self.assertEqual(got, ["goal-row", "unblocker", "high", "waiter", "low",
                               "z-unscored"])

    def test_cap_is_read_from_clause_7(self):
        with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False) as fh:
            fh.write("and recommend **at most 3**, in this order")
        try:
            self.assertEqual(inbox.clause7_cap(fh.name), 3)
        finally:
            os.unlink(fh.name)

    def test_real_clause_7_carries_a_cap(self):
        self.assertGreater(inbox.clause7_cap(), 0)

    def test_render_caps_and_marks_no_row(self):
        rows = [("g", row(f"t{i}")) for i in range(8)]
        buf = io.StringIO()
        inbox.render_ranked(rows, 5, "residual", [], {}, out=buf)
        out = buf.getvalue()
        self.assertEqual(sum(1 for l in out.splitlines() if re.match(r"\d+\. ", l)), 5)
        self.assertIn("3 more not shown", out)
        self.assertNotIn("recommend", out.lower())


if __name__ == "__main__":
    unittest.main()
