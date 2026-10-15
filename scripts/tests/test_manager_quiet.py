#!/usr/bin/env python3
"""Tests for scripts/manager-quiet.py.

The load-bearing properties, in the order the criterion grades them:

1. **One quiet tick is not a stand-down; two consecutive ones are.** A single quiet
   tick is ordinary — a round may land between two writes — so the boundary is the
   whole point of the counter, and a build that stood down on the first would retire a
   manager every time it caught a subject between ticks.
2. **The negative control**: a subject with one runnable task NEVER stands down,
   however many ticks pass. Property 1 alone is satisfied by a build that stands down
   unconditionally on a quiet read; positive without negative is a probe that cannot
   fail, so both are asserted here.
3. **A runnable tick RESETS the count**, so "consecutive" means consecutive. A counter
   that only ever incremented would stand a subject down two ticks after its last
   quiet one, however much work arrived in between.
4. **It fails toward ARMED.** An unparseable `defer_date`, an unresolved subject and a
   vault that is not a vault root all leave the loop armed and the counter untouched.
5. **The slug agrees with `manager-liveness.py`'s** — a stand-down recorded under a
   slug nothing else reads is a stand-down nothing else can see.

Run: python3 -m unittest discover -s scripts/tests -v
"""

import importlib.util
import os
import tempfile
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_SCRIPTS = os.path.dirname(_HERE)
_SCRIPT = os.path.join(_SCRIPTS, "manager-quiet.py")
_LIVENESS = os.path.join(_SCRIPTS, "manager-liveness.py")

TODAY = (2026, 10, 15)

GOAL = "---\npage_type: goal\nstatus: in_progress\n---\n\nA goal.\n"

TASK = """---
status: {status}
phase: execution
{extra}goals:
    - '[[AGoal]]'
---

# Success Criteria

- [ ] SC1: something

# Tasks

- [ ] do the thing
"""


def load(state_dir):
    os.environ["MANAGER_LIVENESS_STATE_DIR"] = state_dir
    spec = importlib.util.spec_from_file_location("manager_quiet", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def load_liveness(state_dir):
    os.environ["MANAGER_LIVENESS_STATE_DIR"] = state_dir
    spec = importlib.util.spec_from_file_location("manager_liveness", _LIVENESS)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.vault = os.path.join(self.tmp, "vault")
        for d in ("25 Tasks", "24 Goals", "23 Topics"):
            os.makedirs(os.path.join(self.vault, d))
        self.state = os.path.join(self.tmp, "state")
        os.makedirs(self.state)
        self.write("24 Goals/AGoal.md", GOAL)
        self.m = load(self.state)

    def write(self, rel, text):
        with open(os.path.join(self.vault, rel), "w", encoding="utf-8") as fh:
            fh.write(text)

    def task(self, name, status="in_progress", extra=""):
        self.write("25 Tasks/%s.md" % name, TASK.format(status=status, extra=extra))

    def tick(self, subject="AGoal"):
        return self.m.tick(subject, self.vault, TODAY, self.state)

    def verdicts(self, n, subject="AGoal"):
        return [self.tick(subject)["verdict"] for _ in range(n)]


class TestBoundary(Base):
    """Property 1 — the 1-vs-2-tick boundary the criterion names."""

    def test_a_subject_with_no_tasks_is_quiet(self):
        r = self.tick()
        self.assertEqual(r["verdict"], "armed")
        self.assertEqual(r["consecutive_quiet"], 1)

    def test_one_quiet_tick_does_not_stand_down(self):
        self.task("Done", status="completed")
        self.assertEqual(self.verdicts(1), ["armed"])

    def test_two_consecutive_quiet_ticks_stand_down(self):
        self.task("Done", status="completed")
        self.assertEqual(self.verdicts(2), ["armed", "stood-down"])

    def test_a_third_quiet_tick_stays_stood_down(self):
        self.task("Done", status="completed")
        self.assertEqual(self.verdicts(3), ["armed", "stood-down", "stood-down"])


class TestNegativeControl(Base):
    """Property 2 — one runnable task never stands the subject down."""

    def test_one_runnable_task_never_stands_down(self):
        self.task("Live")  # in_progress, no blocker, no defer_date
        self.assertEqual(self.verdicts(5), ["armed"] * 5)

    def test_the_counter_stays_zero_while_a_runnable_task_exists(self):
        self.task("Live")
        for _ in range(5):
            r = self.tick()
            self.assertEqual(r["consecutive_quiet"], 0)
        self.assertEqual(self.m.read_quiet_count("agoal", self.state), 0)

    def test_the_runnable_row_is_named(self):
        self.task("Live")
        self.assertEqual(self.tick()["runnable"], ["Live"])


class TestReset(Base):
    """Property 3 — the count is CONSECUTIVE."""

    def test_a_runnable_tick_resets_the_counter(self):
        self.task("Done", status="completed")
        self.assertEqual(self.tick()["consecutive_quiet"], 1)
        self.task("Live")                       # work arrives
        self.assertEqual(self.tick()["consecutive_quiet"], 0)
        os.remove(os.path.join(self.vault, "25 Tasks", "Live.md"))
        # Back to quiet, but the earlier quiet tick no longer counts.
        self.assertEqual(self.tick()["consecutive_quiet"], 1)
        self.assertEqual(self.tick()["verdict"], "stood-down")

    def test_reset_clears_the_counter(self):
        self.task("Done", status="completed")
        self.tick()
        self.m.write_quiet_count("agoal", 0, self.state)
        self.assertEqual(self.tick()["consecutive_quiet"], 1)


class TestQuietShapes(Base):
    """Each quiet shape is read from a field — none is inferred."""

    def test_unsurfaced_statuses_are_quiet(self):
        for i, status in enumerate(("next", "backlog", "hold")):
            self.task("Row%d" % i, status=status)
        r = self.tick()
        self.assertEqual(r["runnable"], [])
        self.assertEqual(sorted(r["quiet"]), ["Row0", "Row1", "Row2"])

    def test_terminal_tasks_are_excluded_not_quiet(self):
        self.task("Done", status="completed")
        self.task("Gone", status="aborted")
        r = self.tick()
        self.assertEqual(r["runnable"], [])
        self.assertEqual(r["quiet"], [])
        self.assertEqual(sorted(r["terminal"]), ["Done", "Gone"])

    def test_a_future_defer_date_is_quiet(self):
        self.task("Later", extra='defer_date: "2026-10-22"\n')
        self.assertEqual(self.tick()["runnable"], [])

    def test_a_past_defer_date_is_runnable(self):
        self.task("Overdue", extra='defer_date: "2026-10-01"\n')
        self.assertEqual(self.tick()["runnable"], ["Overdue"])

    def test_a_defer_date_of_today_is_runnable(self):
        # The date it becomes due is the day it must be driven — `>` not `>=`.
        self.task("Today", extra='defer_date: "2026-10-15"\n')
        self.assertEqual(self.tick()["runnable"], ["Today"])

    def test_an_rfc3339_defer_date_is_read(self):
        # The vault normalizes an UNQUOTED date scalar to RFC3339, so a reader that
        # accepts only the date-only spelling silently ignores half the vault.
        self.task("Later", extra="defer_date: 2026-10-22T00:00:00Z\n")
        self.assertEqual(self.tick()["runnable"], [])

    def test_an_unparseable_defer_date_is_runnable(self):
        # Fail toward armed: a date we cannot read is not a date we can defer on.
        self.task("Odd", extra="defer_date: someday\n")
        self.assertEqual(self.tick()["runnable"], ["Odd"])

    def test_a_blocked_by_open_task_is_quiet(self):
        self.task("Blocker")
        self.task("Waiting", extra="blocked_by:\n    - '[[Blocker]]'\n")
        self.assertEqual(self.tick()["runnable"], ["Blocker"])

    def test_a_blocked_by_completed_task_is_runnable(self):
        self.task("Blocker", status="completed")
        self.task("Free", extra="blocked_by:\n    - '[[Blocker]]'\n")
        self.assertEqual(self.tick()["runnable"], ["Free"])

    def test_an_unparseable_blocked_by_entry_is_quiet(self):
        # `blocked_by_verdict` reads a declared-but-unparsed entry as blocked, and a
        # blocker we cannot verify is one we cannot clear.
        self.task("Odd", extra="blocked_by:\n    - Blocker\n")
        self.assertEqual(self.tick()["runnable"], [])


class TestFailTowardArmed(Base):
    """Property 4 — a refusal never advances the counter."""

    def test_an_unresolvable_subject_errors_and_writes_nothing(self):
        r = self.tick("NoSuchSubject")
        self.assertIn("error", r)
        self.assertFalse(os.path.exists(self.m.quiet_path("nosuchsubject", self.state)))

    def test_a_relative_vault_is_refused(self):
        r = self.m.tick("AGoal", "vault", TODAY, self.state)
        self.assertIn("error", r)

    def test_a_vault_without_markers_is_refused(self):
        bare = os.path.join(self.tmp, "bare")
        os.makedirs(bare)
        r = self.m.tick("AGoal", bare, TODAY, self.state)
        self.assertIn("error", r)

    def test_a_corrupt_counter_reads_as_zero(self):
        with open(self.m.quiet_path("agoal", self.state), "w") as fh:
            fh.write("not-a-number\n")
        self.assertEqual(self.m.read_quiet_count("agoal", self.state), 0)


class TestLoaderMemoized(Base):
    """The sibling module must be loaded ONCE, not re-executed per call.

    `classify` calls `load_predispatch()` once per tracked task, and `exec_module`
    re-runs the module body on every call — unlike a normal `import`, which caches in
    `sys.modules`. Without the memo, a ten-row subject re-parsed
    `manager-predispatch.py` eleven times per tick and reset its module-level state
    between rows.
    """

    def test_load_predispatch_returns_the_same_module_object(self):
        self.assertIs(self.m.load_predispatch(), self.m.load_predispatch())

    def test_a_tick_shares_one_module_across_every_task(self):
        for i in range(5):
            self.task("Row%d" % i, status="next")
        seen = []
        real = self.m.load_predispatch

        def spy():
            mod = real()
            seen.append(id(mod))
            return mod

        self.m.load_predispatch = spy
        try:
            self.tick()
        finally:
            self.m.load_predispatch = real
        # It IS called per task (so the test cannot pass by the call sites vanishing)…
        self.assertGreater(len(seen), 1)
        # …but every call resolves to the one loaded object.
        self.assertEqual(len(set(seen)), 1)


class TestSlugAgreesWithLiveness(Base):
    """Property 5 — the two readers must name the same file."""

    def test_slug_matches_manager_liveness(self):
        live = load_liveness(self.state)
        for subject in (
            "AGoal",
            "Recurring Operator Gates Are Fixed Once, Not Answered Every Session",
            "Drive Rounds Report Success-Criterion Progress, Not Session Activity",
        ):
            self.assertEqual(self.m.slug(subject), live.slug(subject))

    def test_the_counter_sits_beside_the_liveness_files(self):
        self.assertEqual(
            os.path.dirname(self.m.quiet_path("agoal", self.state)), self.state
        )
        self.assertTrue(self.m.quiet_path("agoal", self.state).endswith(".quiet"))


if __name__ == "__main__":
    unittest.main()
