#!/usr/bin/env python3
"""Tests for scripts/next-focus.py.

Each case guards a claim the fleet round makes to the operator, so each is written to
fail on a plausible wrong implementation rather than on a broken one:

  * **the tier order is asserted as a full list, not as a membership check.** A set
    comparison passes on a script that sorts alphabetically; only sequence equality
    catches the ordering the operator asked for (due-today, then weekly, monthly,
    quarterly, yearly).
  * **the weekly citation is asserted against the `### 1.` heading line itself.** A
    hardcoded five-row block with plausible-looking citations is the laziest way to
    satisfy a "cites a plan line" criterion, and resolving the line number back to the
    heading is what it cannot fake.
  * **a `phase: todo` row carries NO `action` key at all** — not an empty one. `todo` is
    the operator's approval inbox: a row there is reported and opened by nobody. An
    empty `action` reads as "acted on and found nothing", which is the opposite claim.
  * **a task in no plan never appears as a candidate.** The rule is about absence, and an
    absence assertion over a population the script could satisfy by listing everything
    needs the positive half too — so the same fixture asserts it IS proposed.
  * **four unplanned `priority: 1` rows yield exactly three proposals with distinct
    dedup keys.** 38 such rows exist in the live vault; an unbounded rule posts 38 cards
    in one round. Distinct keys are asserted because the store's suppression is keyed,
    and two items sharing a key would answer one another.
  * **every alert failure renders `unread`, never `0 firing`** — three shapes (non-zero
    exit, unparseable output, absent script). A failed fetch and a quiet cluster must
    not look the same.
  * **at or above the target the block is held, not omitted.** A suppressed round and a
    round that found nothing must never render the same.
  * **an unreadable capacity store is `unknown`, never `0`.** A fleet that cannot count
    itself is not an idle fleet.
"""
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest

SCRIPT = pathlib.Path(__file__).resolve().parent.parent / "next-focus.py"

WEEK = "2026-W43"
MONTH = "2026-10"
QUARTER = "2026-Q4"
YEAR = "2026"
DAY = "2026-10-21"

WEEKLY = """# Fixture week

# Tasks

### 1. Weekly first item

**Task:** [[Weekly Task]]

**Priority:** 🔴 HIGH

### 2. Weekly second item

**Task:** [[Todo Task]]
"""

MONTHLY = """# Plan for this month

## Must / Should / Could

**Must**
1. **Monthly item** — [[Monthly Goal]]

**Should**
2. **Monthly second** — [[Monthly Goal]]
"""

QUARTERLY = """# Plan for this quarter

**🚀 Q4 Objectives (projects):**

1. **[[Quarterly Objective]]** (P1, theme [[Some Theme]]) — text

**🔁 Standing commitments:**
- 👨‍👩‍👧 **Family** — [[Some Theme]]
"""

YEARLY = """# Plan for 2026

## 2026 Targets

### 💰 Financial (70%)

- reach [[Yearly Topic]]

### 💪 Health (10%)

- run [[Yearly Topic]]
"""


def task(name, *, phase="planning", status="in_progress", priority="2",
         defer=None, due=None, flag=None, blocked=None):
    lines = ["---", f"status: {status}", f"phase: {phase}", f"priority: {priority}"]
    if defer:
        lines.append(f'defer_date: "{defer}"')
    if due:
        lines.append(f'due_date: "{due}"')
    if flag:
        lines.append("flag: true")
        lines.append(f"flag_set_by: {flag}")
    if blocked:
        lines.append(f"blocked_by: {blocked}")
    lines += ["---", "", f"# {name}", ""]
    return "\n".join(lines)


class NextFocusTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.vault = pathlib.Path(self.dir.name)
        self.make_vault()

    def write(self, rel, text):
        path = self.vault / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def make_vault(self):
        self.write(f"60 Periodic Notes/Weekly/{WEEK}.md", WEEKLY)
        self.write(f"60 Periodic Notes/Monthly/{MONTH}.md", MONTHLY)
        self.write(f"60 Periodic Notes/Quarterly/{QUARTER}.md", QUARTERLY)
        self.write(f"60 Periodic Notes/Yearly/{YEAR}.md", YEARLY)

        self.write("25 Tasks/Weekly Task.md", task("Weekly Task"))
        self.write("25 Tasks/Todo Task.md", task("Todo Task", phase="todo", priority="3"))
        self.write("25 Tasks/Due Task.md", task("Due Task", defer=DAY))
        self.write("24 Goals/Monthly Goal.md", task("Monthly Goal", status="in_progress"))
        self.write("22 Objectives/Quarterly Objective.md", task("Quarterly Objective"))
        self.write("23 Topics/Yearly Topic.md", task("Yearly Topic", status="in_progress"))

    def add_p1(self, count):
        for i in range(count):
            self.write(f"25 Tasks/Unplanned P1 {i}.md", task(f"Unplanned P1 {i}", priority="1"))

    def add_alerts(self, body, name="prometheus-alerts.sh"):
        path = self.vault / ".claude" / "scripts" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
        return path

    def run_script(self, *args, live="3", target="30", expect=0):
        cmd = [sys.executable, str(SCRIPT), "--vault", str(self.vault),
               "--tasks-dir", "25 Tasks", "--now", DAY, "--json"]
        if live is not None:
            cmd += ["--live", str(live)]
        if target is not None:
            cmd += ["--target", str(target)]
        cmd += list(args)
        proc = subprocess.run(cmd, capture_output=True, text=True, cwd=str(self.vault.parent))
        self.assertEqual(proc.returncode, expect, msg=proc.stderr)
        return json.loads(proc.stdout) if proc.stdout.strip() else None

    # -- ranking ------------------------------------------------------------

    def test_order_is_the_four_tiers_after_due_today(self):
        """Full-sequence equality: a set comparison passes on an alphabetical sort."""
        names = [row["name"] for row in self.run_script()["candidates"]]
        self.assertEqual(names, ["Due Task", "Weekly Task", "Todo Task",
                                 "Monthly Goal", "Quarterly Objective"],
                         "due-today, then weekly, monthly, quarterly — in that order")

    def test_yearly_is_the_last_tier(self):
        """Positive assertion: with the higher tiers empty, yearly still ranks.

        The earlier form asserted only `NotIn` against a full cap, so an implementation
        that dropped the yearly tier entirely passed it.
        """
        for period, key in (("Weekly", WEEK), ("Monthly", MONTH), ("Quarterly", QUARTER)):
            self.write(f"60 Periodic Notes/{period}/{key}.md", "# Empty plan\n")
        self.write("25 Tasks/Due Task.md", task("Due Task", status="completed"))
        names = [r["name"] for r in self.run_script()["candidates"]]
        self.assertEqual(names, ["Yearly Topic"], "yearly must still rank when it is all there is")

    def test_weekly_citation_is_the_numbered_heading_line(self):
        """The citation must land on the `### 1.` heading, not on a plausible line."""
        result = self.run_script()
        weekly = [r for r in result["candidates"] if r["name"] == "Weekly Task"][0]
        src = weekly["source"]
        self.assertEqual(src["period"], "weekly")
        text = (self.vault / f"60 Periodic Notes/Weekly/{WEEK}.md").read_text(encoding="utf-8")
        line = text.splitlines()[src["line"] - 1]
        self.assertTrue(line.startswith("### 1. "),
                        f"line {src['line']} of the weekly plan is {line!r}")

    def test_every_plan_citation_resolves_to_its_heading(self):
        """Citation fidelity across every tier — a fabricated line fails here."""
        result = self.run_script()
        for row in result["candidates"]:
            src = row["source"]
            if src["period"] == "due-today":
                continue
            text = pathlib.Path(src["file"]).read_text(encoding="utf-8")
            line = text.splitlines()[src["line"] - 1]
            self.assertTrue(line.lstrip().startswith(("#", "1.", "2.", "**")),
                            f'{row["name"]}: line {src["line"]} is {line!r}')

    # -- the approval boundary ---------------------------------------------

    def test_todo_row_carries_no_action_at_all(self):
        """Not an empty action — the key's ABSENCE is the rule."""
        todo = [r for r in self.run_script()["candidates"] if r["name"] == "Todo Task"][0]
        self.assertTrue(todo["needs_approval"])
        self.assertNotIn("action", todo, "a phase: todo row must carry no action key")
        self.assertNotIn("/supervisor:open", json.dumps(todo),
                         "a phase: todo row must not name the open verb anywhere")

    def test_approved_row_carries_its_action(self):
        """The positive half: a non-todo row DOES name the open verb."""
        weekly = [r for r in self.run_script()["candidates"] if r["name"] == "Weekly Task"][0]
        self.assertFalse(weekly["needs_approval"])
        self.assertIn("/supervisor:open", weekly["action"])

    # -- proposals ----------------------------------------------------------

    def test_plan_absent_task_is_proposed_never_a_candidate(self):
        self.add_p1(1)
        result = self.run_script()
        self.assertNotIn("Unplanned P1 0", [r["name"] for r in result["candidates"]],
                         "a task in no plan is never a planned candidate")
        self.assertIn("Unplanned P1 0", [r["name"] for r in result["proposed"]],
                      "...and it must still surface as a proposal")

    def test_proposal_cap_is_three_with_distinct_keys(self):
        """38 live rows would post 38 cards; the cap is what stops that."""
        self.add_p1(4)
        result = self.run_script()
        self.assertEqual(len(result["proposed"]), 3)
        self.assertEqual(len(result["proposed_overflow"]), 1,
                         "the fourth row must be reported, not silently dropped")
        keys = [row["card_key"] for row in result["proposed"]]
        self.assertEqual(len(set(keys)), 3, "two items sharing a key would answer one another")

    def test_card_key_is_stable_across_runs(self):
        """A key that moves on a re-post asks the operator the same question twice."""
        self.add_p1(1)
        first = self.run_script()["proposed"][0]["card_key"]
        second = self.run_script()["proposed"][0]["card_key"]
        self.assertEqual(first, second)

    def test_blocked_goal_is_proposed(self):
        self.write("24 Goals/Blocked Goal.md",
                   task("Blocked Goal", status="hold", blocked="Some Bug Task"))
        result = self.run_script()
        self.assertIn("Blocked Goal", [r["name"] for r in result["proposed"]])

    def test_goal_at_hold_without_blocked_by_is_not_proposed(self):
        self.write("24 Goals/Parked Goal.md", task("Parked Goal", status="hold"))
        result = self.run_script()
        self.assertNotIn("Parked Goal", [r["name"] for r in result["proposed"]])

    # -- the three-valued reads --------------------------------------------

    def test_alerts_unread_on_nonzero_exit(self):
        self.add_alerts("#!/bin/bash\nexit 1\n")
        self.assertEqual(self.run_script()["alerts"], "unread")

    def test_alerts_unread_on_unparseable_output(self):
        self.add_alerts("#!/bin/bash\necho 'connection reset by peer'\n")
        self.assertEqual(self.run_script()["alerts"], "unread")

    def test_alerts_unavailable_without_the_script(self):
        self.assertEqual(self.run_script()["alerts"], "unavailable")

    def test_alerts_clear_when_the_count_is_zero(self):
        self.add_alerts("#!/bin/bash\necho 'total active alerts: 0'\n")
        self.assertEqual(self.run_script()["alerts"], "clear")

    def test_unknown_capacity_is_not_zero(self):
        """A fleet that cannot count itself is not an idle fleet."""
        result = self.run_script("--plugin", "/nonexistent", live=None, target=None)
        self.assertIsNone(result["capacity"]["live"])
        self.assertIsNone(result["capacity"]["target"])

    def test_at_target_renders_held_not_empty(self):
        """A suppressed round and a round that found nothing must not look the same."""
        proc = subprocess.run(
            [sys.executable, str(SCRIPT), "--vault", str(self.vault),
             "--tasks-dir", "25 Tasks", "--now", DAY, "--live", "30", "--target", "30"],
            capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, msg=proc.stderr)
        self.assertTrue(proc.stdout.startswith("Next focus: held — at target (30/30)"),
                        proc.stdout.splitlines()[0])

    def test_missing_vault_refuses(self):
        proc = subprocess.run(
            [sys.executable, str(SCRIPT), "--vault", "/nonexistent-vault",
             "--tasks-dir", "25 Tasks", "--live", "1", "--target", "30"],
            capture_output=True, text=True)
        self.assertEqual(proc.returncode, 2)
        self.assertIn("REFUSED", proc.stderr)

    # -- regressions the review found --------------------------------------

    def test_bad_now_refuses_rather_than_emitting_nothing(self):
        """Exit 2 is a refusal; an empty block would read as "nothing to do"."""
        proc = subprocess.run(
            [sys.executable, str(SCRIPT), "--vault", str(self.vault),
             "--tasks-dir", "25 Tasks", "--now", "not-a-date",
             "--live", "1", "--target", "30"],
            capture_output=True, text=True)
        self.assertEqual(proc.returncode, 2)
        self.assertIn("REFUSED", proc.stderr)

    def test_overdue_task_ranks_and_says_so(self):
        """Exact-day equality buried a task that fell due yesterday and is still open."""
        self.write("25 Tasks/Overdue Task.md", task("Overdue Task", due="2026-10-19"))
        row = [r for r in self.run_script()["candidates"] if r["name"] == "Overdue Task"][0]
        self.assertEqual(row["source"]["period"], "due-today")
        self.assertIn("overdue since 2026-10-19", row["why"])

    def test_future_defer_date_parks_the_task(self):
        """`defer_date` is defer-UNTIL: a future one must not surface, however old the due."""
        self.write("25 Tasks/Parked Task.md",
                   task("Parked Task", defer="2027-01-01", due="2026-09-01"))
        names = [r["name"] for r in self.run_script()["candidates"]]
        self.assertNotIn("Parked Task", names,
                         "a task deferred into the future is parked, not overdue")

    def test_arrived_defer_date_surfaces_the_task(self):
        """The other half: once the defer date has passed the row is active again."""
        self.write("25 Tasks/Resumed Task.md",
                   task("Resumed Task", defer="2026-10-19", due="2027-05-01"))
        row = [r for r in self.run_script()["candidates"] if r["name"] == "Resumed Task"][0]
        self.assertIn("active since 2026-10-19", row["why"],
                      "a past defer with a future due is active, not overdue")

    def test_weekly_link_with_heading_resolves(self):
        """A `[[Page#Section]]` link is not a filename; unstripped it ranks nothing."""
        self.write(f"60 Periodic Notes/Weekly/{WEEK}.md",
                   WEEKLY.replace("[[Weekly Task]]", "[[Weekly Task#Some Section]]"))
        self.assertIn("Weekly Task", [r["name"] for r in self.run_script()["candidates"]])

    def test_link_escaping_the_board_dirs_resolves_to_nothing(self):
        """A plan page is content: a traversal link must not resolve outside the vault."""
        self.write(f"60 Periodic Notes/Weekly/{WEEK}.md",
                   WEEKLY.replace("[[Weekly Task]]", "[[../../etc/passwd]]"))
        result = self.run_script()
        self.assertNotIn("../../etc/passwd", [r["name"] for r in result["candidates"]])
        self.assertTrue(any("no page" in note for note in result["notes"]))

    def test_partial_live_override_is_kept(self):
        """A supplied --live must not be replaced because --target was absent."""
        result = self.run_script("--plugin", "/nonexistent", live="3", target=None)
        self.assertEqual(result["capacity"]["live"], 3)
        self.assertIsNone(result["capacity"]["target"])

    def test_zero_target_is_unlimited_not_at_target(self):
        """`target: 0` is the documented unlimited form, never "held"."""
        proc = subprocess.run(
            [sys.executable, str(SCRIPT), "--vault", str(self.vault),
             "--tasks-dir", "25 Tasks", "--now", DAY, "--live", "5", "--target", "0"],
            capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, msg=proc.stderr)
        self.assertTrue(proc.stdout.startswith("Next focus (5/unlimited)"),
                        proc.stdout.splitlines()[0])

    def test_alert_row_is_named_after_the_alert_not_the_count(self):
        """The `total active alerts: N` line is a summary, not an item to add to a plan."""
        self.add_alerts("#!/bin/bash\necho 'total active alerts: 2'\n"
                        "echo 'KafkaLagHigh - consumer group is 5000 behind'\n")
        result = self.run_script()
        self.assertEqual(result["alerts"], "firing")
        names = [r["name"] for r in result["proposed"]]
        self.assertIn("KafkaLagHigh - consumer group is 5000 behind", names)
        self.assertNotIn("total active alerts: 2", names)

    def test_firing_alert_survives_the_proposal_cap(self):
        """Ranked first: behind the P1 rows it fell into the overflow and surfaced nowhere.

        With >=3 unplanned P1 tasks the alert was always the 4th row, so it was in
        `proposed_overflow` — never a card, never a named overflow, and the render
        prints `alerts:` only for unread/unavailable. It vanished from the round.
        """
        self.add_p1(4)
        self.add_alerts("#!/bin/bash\necho 'total active alerts: 1'\n"
                        "echo 'KafkaLagHigh - consumer group is 5000 behind'\n")
        result = self.run_script()
        self.assertEqual(result["alerts"], "firing")
        self.assertEqual(len(result["proposed"]), 3)
        self.assertEqual(result["proposed"][0]["kind"], "alert",
                         "a firing prod alert outranks an unplanned P1 task")
        self.assertIn("KafkaLagHigh - consumer group is 5000 behind",
                      [r["name"] for r in result["proposed"]],
                      "a firing alert must never be capped out by P1 tasks")

    def test_render_names_the_proposals_and_the_overflow(self):
        """The render path is what the round pastes; JSON-only coverage misses it."""
        self.add_p1(4)
        proc = subprocess.run(
            [sys.executable, str(SCRIPT), "--vault", str(self.vault),
             "--tasks-dir", "25 Tasks", "--now", DAY, "--live", "3", "--target", "30"],
            capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, msg=proc.stderr)
        self.assertIn("Proposed for this week's plan:", proc.stdout)
        self.assertIn("left for the next round (cap 5)", proc.stdout)
        overflow = [ln for ln in proc.stdout.splitlines()
                    if ln.startswith("  left for the next round (cap 3)")]
        self.assertEqual(len(overflow), 1, proc.stdout)
        self.assertIn("Unplanned P1 3", overflow[0],
                      "the remainder must be NAMED, never a bare count")


if __name__ == "__main__":
    unittest.main()
