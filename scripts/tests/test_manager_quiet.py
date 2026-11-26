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
6. **A wake-counted subject page is never offered `sleep-until`.** The marker
   `quiet_check_exempt` turns the date-gated branch into `armed`, because a sleep
   suppresses the very wakes the criterion counts. The negative control — the same
   fixture with the marker removed — still reaches `sleep-until`.

Run: python3 -m unittest discover -s scripts/tests -v
"""

import contextlib
import importlib.util
import io
import json
import os
import tempfile
import time
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_SCRIPTS = os.path.dirname(_HERE)
_SCRIPT = os.path.join(_SCRIPTS, "manager-quiet.py")
_LIVENESS = os.path.join(_SCRIPTS, "manager-liveness.py")

TODAY = (2026, 10, 15)

GOAL = "---\npage_type: goal\nstatus: in_progress\n---\n\nA goal.\n"

TASK = """---
status: {status}
phase: {phase}
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

    def task(self, name, status="in_progress", phase="execution", extra=""):
        self.write(
            "25 Tasks/%s.md" % name,
            TASK.format(status=status, phase=phase, extra=extra),
        )

    def arm(self, subject="AGoal"):
        """Simulate the manager's head-of-round arm — which is what marks a NEW tick.

        A round arms before it reads, so `<slug>.cadence`'s mtime is the tick identity.
        Without advancing it, a second `tick()` call is a *re-read of the same tick* —
        exactly what the counter must not count twice. The mtime is stepped explicitly
        because the key is whole seconds and two arms in one second would collide.
        """
        self._seq = getattr(self, "_seq", 0) + 1
        p = os.path.join(self.state, "%s.cadence" % self.m.slug(subject))
        with open(p, "w", encoding="utf-8") as fh:
            fh.write("900\n")
        t = time.time() + self._seq
        os.utime(p, (t, t))

    def tick(self, subject="AGoal", arm=True):
        if arm:
            self.arm(subject)
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


class TestSleepUntil(Base):
    """A date-gated subject sleeps to its earliest `defer_date` — SC1's fixture.

    Two future-dated rows and zero runnable ones: the subject is not finished and not
    blocked, it is waiting on a date it already knows, so standing it down is the defect
    (a human re-arms it every morning). The verdict word AND the named instant are both
    asserted — a build that returned the right word with a wrong or absent instant would
    arm the wakeup for the wrong moment, which is worse than not sleeping at all.
    """

    def setUp(self):
        super().setUp()
        self.task("Later", extra='defer_date: "2026-10-20T11:00:00+02:00"\n')
        self.task("Sooner", extra='defer_date: "2026-10-18T08:00:00+02:00"\n')

    def test_two_future_dated_rows_sleep_until_the_earliest(self):
        self.assertEqual(self.tick()["verdict"], "armed")
        r = self.tick()
        self.assertEqual(r["verdict"], "sleep-until")
        self.assertEqual(r["trigger"], "2026-10-18T08:00:00+02:00")

    def test_a_sleeping_subject_never_reads_stood_down(self):
        self.assertNotIn("stood-down", self.verdicts(4))

    def test_the_boundary_is_the_same_as_the_stand_down(self):
        """One quiet tick is still ordinary — sleeping does not start a tick early."""
        self.assertEqual(self.verdicts(1), ["armed"])

    def test_a_runnable_row_defeats_the_sleep(self):
        """The negative control: live work must never be put to sleep."""
        self.task("Live")
        self.assertEqual(self.verdicts(3), ["armed", "armed", "armed"])

    def test_a_blocked_row_defeats_the_sleep(self):
        """A blocker reopens on a human's action, not on a clock — still stands down."""
        self.task("Blocked", extra="blocked_by:\n    - '[[Missing]]'\n")
        self.assertEqual(self.verdicts(2), ["armed", "stood-down"])


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


class TestReadOnly(Base):
    """`record=False` answers without writing — the wake's read must not be a tick.

    ⚠️ Every ordinary `tick()` records and moves the counter, so a wake that classified a
    subject would be an extra quiet tick for it; enough wakes would stand a live manager
    down, which is the silent-loss direction this module's docstring calls out.
    """

    def test_a_read_only_call_reports_the_stored_count(self):
        # No tasks: a quiet subject, so an ordinary tick moves the counter.
        self.assertEqual(self.tick()["consecutive_quiet"], 1)
        r = self.m.tick("AGoal", self.vault, TODAY, self.state, record=False)
        self.assertEqual(r["consecutive_quiet"], 1, "the stored count, never an increment")
        # The store is untouched, so the next real tick still moves 1 -> 2.
        self.assertEqual(self.tick()["consecutive_quiet"], 2)

    def test_a_read_only_call_writes_nothing(self):
        path = os.path.join(self.state, "%s.quiet" % self.m.slug("AGoal"))
        self.assertFalse(os.path.exists(path))
        self.m.tick("AGoal", self.vault, TODAY, self.state, record=False)
        self.assertFalse(os.path.exists(path), "a read must not create the quiet state")


class TestQuietShapes(Base):
    """Each quiet shape is read from a field — none is inferred."""

    def test_unsurfaced_statuses_are_quiet(self):
        for i, status in enumerate(("backlog", "hold")):
            self.task("Row%d" % i, status=status)
        r = self.tick()
        self.assertEqual(r["runnable"], [])
        self.assertEqual(sorted(r["quiet"]), ["Row0", "Row1"])

    def test_an_approved_next_row_is_runnable(self):
        """`vault-cli task approve` leaves `status: next`; the row is approved, not queued.

        Counting it quiet here made a subject full of approved rows read as empty, so
        the stand-down counter retired the manager that had just approved them.
        """
        self.task("Approved", status="next", phase="planning")
        r = self.tick()
        self.assertEqual(r["runnable"], ["Approved"])
        self.assertEqual(r["quiet"], [])

    def test_a_next_row_with_no_phase_key_is_runnable(self):
        """`sweep-gate.py` keeps the `status == "next"` arm for exactly this shape.

        Rows filed before the phase field existed carry no `phase` key at all; a
        `phase`-only predicate would drop every one of them out of the offer silently.
        """
        self.write(
            "25 Tasks/Unphased.md",
            "---\nstatus: next\ngoals:\n    - '[[AGoal]]'\n---\n\n# Tasks\n\n- [ ] x\n",
        )
        r = self.tick()
        self.assertEqual(r["runnable"], ["Unphased"])

    def test_a_todo_phase_is_quiet_even_at_status_next(self):
        """The approval boundary is `phase: todo`, the field `task approve` moves.

        Without this guard, admitting `status: next` would surface the operator's whole
        unapproved inbox as runnable work.
        """
        self.task("Unapproved", status="next", phase="todo")
        r = self.tick()
        self.assertEqual(r["runnable"], [])
        self.assertEqual(r["quiet"], ["Unapproved"])

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


class TestReopenTrigger(Base):
    """SC3 — the line must name the ACTUAL trigger, not merely carry a prefix.

    The criterion's probe asserts the trigger's *content* for exactly this reason: a
    probe matching only `reopen on` would be satisfied by an empty tail, which is also
    what a broken computation prints.
    """

    def test_the_earliest_defer_date_wins(self):
        self.task("Late", extra='defer_date: "2026-11-30"\n')
        self.task("Soon", extra='defer_date: "2026-10-22"\n')
        self.assertEqual(self.tick()["trigger"], "2026-10-22")

    def test_a_defer_date_beats_a_blocker(self):
        # A date the subject reaches on its own outranks an edge someone else must cut.
        self.task("Blocker")
        self.task("Waiting", extra="blocked_by:\n    - '[[Blocker]]'\n")
        self.task("Later", extra='defer_date: "2026-10-22"\n')
        self.assertEqual(self.tick()["trigger"], "2026-10-22")

    def test_a_blocked_row_names_the_blocker_not_the_blocked_row(self):
        self.task("Blocker")
        self.task("Waiting", extra="blocked_by:\n    - '[[Blocker]]'\n")
        self.assertEqual(self.tick()["trigger"], "blocked by Blocker")

    def test_several_blockers_are_all_named_and_deduped(self):
        self.task("B1")
        self.task("B2")
        self.task("W1", extra="blocked_by:\n    - '[[B1]]'\n")
        self.task("W2", extra="blocked_by:\n    - '[[B1]]'\n    - '[[B2]]'\n")
        self.assertEqual(self.tick()["trigger"], "blocked by B1, B2")

    def test_no_quiet_row_names_no_trigger_explicitly(self):
        self.task("Done", status="completed")
        self.assertEqual(self.tick()["trigger"], "no named trigger")

    def _argv(self):
        return ["--subject", "AGoal", "--vault", self.vault,
                "--today", "2026-10-15", "--state-dir", self.state]

    def test_the_cli_prints_the_sc3_line_on_stand_down(self):
        self.task("Done", status="completed")
        self.tick()                                   # tick 1 — armed
        self.arm()                                    # the next round arms before it reads
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.m.main(self._argv())                 # tick 2 — stood down
        self.assertIn(
            "Stood down: zero runnable tasks; reopen on no named trigger",
            out.getvalue(),
        )

    def test_the_cli_does_not_print_the_sc3_line_while_armed(self):
        self.task("Live")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.m.main(self._argv())
        self.assertNotIn("Stood down:", out.getvalue())


class TestSlugAgreesWithLiveness(Base):
    """Property 5 — the two readers must name the same file."""

    def test_slug_matches_manager_liveness(self):
        live = load_liveness(self.state)
        for subject in (
            "AGoal",
            "Recurring Operator Gates Are Fixed Once, Not Answered Every Session",
            "Drive Rounds Report Success-Criterion Progress, Not Session Activity",
        ):
            self.assertEqual(self.m.slug(subject), live.base_slug(subject))

    def test_the_counter_sits_beside_the_liveness_files(self):
        self.assertEqual(
            os.path.dirname(self.m.quiet_path("agoal", self.state)), self.state
        )
        self.assertTrue(self.m.quiet_path("agoal", self.state).endswith(".quiet"))


if __name__ == "__main__":
    unittest.main()


class TestOneTickIsNotOneCall(Base):
    """A tick is identified by the ARM, not by the call.

    Found 2026-10-16 by a goal manager that read its subject twice in one tick and
    watched the counter go 1 -> 2 -> 3: it stood itself down a tick early. Early is the
    unsafe direction — a manager left armed costs a model call, while one stood down on
    live work stops driving it and nothing announces the loss.
    """

    def test_two_reads_in_one_tick_count_once(self):
        self.arm()
        first, second = self.tick(arm=False), self.tick(arm=False)
        self.assertEqual(first["consecutive_quiet"], 1)
        self.assertEqual(second["consecutive_quiet"], 1)
        self.assertEqual(second["verdict"], "armed")

    def test_reading_three_times_in_one_tick_still_does_not_stand_down(self):
        self.arm()
        r = None
        for _ in range(3):
            r = self.tick(arm=False)
        self.assertEqual(r["consecutive_quiet"], 1)
        self.assertEqual(r["verdict"], "armed")

    def test_a_new_arm_is_a_new_tick(self):
        self.arm()
        self.tick(arm=False)
        self.arm()
        r = self.tick(arm=False)
        self.assertEqual(r["consecutive_quiet"], 2)
        self.assertEqual(r["verdict"], "stood-down")

    def test_a_runnable_row_still_resets_within_one_tick(self):
        self.arm()
        self.tick(arm=False)
        self.task("Work")
        r = self.tick(arm=False)
        self.assertEqual(r["consecutive_quiet"], 0)
        self.assertEqual(r["verdict"], "armed")

    def test_a_legacy_bare_integer_counter_still_counts(self):
        # A counter written by an older version has no tick key, so it cannot tell a
        # re-read from a new tick — it must still count rather than be discarded.
        p = os.path.join(self.state, "%s.quiet" % self.m.slug("AGoal"))
        with open(p, "w", encoding="utf-8") as fh:
            fh.write("1\n")
        self.arm()
        r = self.tick(arm=False)
        self.assertEqual(r["consecutive_quiet"], 2)
        self.assertEqual(r["verdict"], "stood-down")


class TestDeferredTodoRowNamesItsDate(Base):
    """A `phase: todo` row with a FUTURE `defer_date` must name that date.

    Found 2026-11-07 on the goal `Automatic Weekly AI Knowledge Sharing Task in Seibert
    Personal`: the subject's only quiet row carried `defer_date: "2026-11-14"` and the
    stand-down line read `reopen on no named trigger` — the one case `round-quiet.md`
    says a reader cannot act on. `classify()` returned on the unapproved phase before it
    read the defer, so the only reason a reader could act on was dropped.

    The row must stay QUIET either way: naming the trigger must not smuggle an
    unapproved row into the runnable set, and a date that has passed must not be
    claimed as a reopen.
    """

    def _todo_with_defer(self, date):
        self.task("Unapproved", phase="todo", extra='defer_date: "%s"\n' % date)

    def test_a_future_defer_on_a_todo_row_names_the_date(self):
        self._todo_with_defer("2026-10-22")
        r = self.tick()
        self.assertEqual(r["runnable"], [])
        self.assertEqual(r["trigger"], "2026-10-22")

    def test_the_row_stays_quiet_and_never_becomes_runnable(self):
        self._todo_with_defer("2026-10-22")
        for _ in range(3):
            self.assertEqual(self.tick()["runnable"], [])

    def test_a_past_defer_on_a_todo_row_still_names_no_trigger(self):
        self._todo_with_defer("2026-10-01")
        self.assertEqual(self.tick()["trigger"], "no named trigger")

    def test_a_todo_row_with_no_defer_still_names_no_trigger(self):
        self.task("Unapproved", phase="todo")
        self.assertEqual(self.tick()["trigger"], "no named trigger")

    def _argv(self):
        return ["--subject", "AGoal", "--vault", self.vault,
                "--today", "2026-10-15", "--state-dir", self.state]

    def test_the_cli_line_names_the_date_on_stand_down(self):
        self._todo_with_defer("2026-10-22")
        self.tick()                                   # tick 1 — armed
        self.arm()                                    # the next round arms before it reads
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.m.main(self._argv())                 # tick 2 — stood down
        self.assertIn(
            "Stood down: zero runnable tasks; reopen on 2026-10-22",
            out.getvalue(),
        )
        self.assertNotIn("no named trigger", out.getvalue())


class TestDeferParsersAreNotTheFault(Base):
    """The quoted-scalar form parses, so the defect is the branch order, not the parser.

    The defect fixture carries `defer_date: "2026-11-14"` (quoted). `defer-date.py` was
    already proven to parse that exact string, so pinning it here stops a future reader
    from "fixing" the parser instead of `classify()`.
    """

    def test_the_quoted_scalar_parses_states_and_displays(self):
        parsed, reason = self.m.defer_date.parse_defer_date('"2026-11-14"')
        self.assertIsNone(reason)
        self.assertIsNotNone(parsed)
        state, detail = self.m.defer_date.defer_state('"2026-11-14"', (2026, 10, 15))
        self.assertEqual(state, "deferred")
        self.assertEqual(detail, parsed)
        self.assertEqual(self.m.defer_date.display('"2026-11-14"'), "2026-11-14")


class TestWakeCountedExemption(Base):
    """Property 6 — a wake-counted subject page is never offered `sleep-until`.

    Some subjects are advanced only by the manager's own wakes (a soak counted as "≥N
    wakable ticks"), and `pane-wake-guard.py` returns `skip:sleeping` for a `SLEEPING`
    subject — so the quiet check's own remedy suppresses the criterion. The marker
    `quiet_check_exempt` on the subject page turns the date-gated branch into `armed`.

    ⚠️ **The negative control is the same fixture with the marker removed.** It must reach
    `sleep-until` on the same code, or the positive half proves nothing. And the counter is
    pre-seeded to `{"tick": 0, "count": 1}`: `QUIET_TICKS_TO_STAND_DOWN` is 2, so a fresh
    fixture returns `armed` whatever the marker says and the `sleep-until` half would be
    unreachable on any code.
    """

    REASON = "Wire the Sweep Gate Verdict SC2 — the >=5 wakable-ticks floor"
    EXEMPT_GOAL = (
        "---\npage_type: goal\nstatus: in_progress\n"
        'quiet_check_exempt: "%s"\n'
        "---\n\nA goal.\n"
    ) % REASON

    def seed(self, count=1):
        """One tick short of the stand-down, so the NEXT read is what decides."""
        with open(os.path.join(self.state, "%s.quiet" % self.m.slug("AGoal")),
                  "w", encoding="utf-8") as fh:
            fh.write(json.dumps({"tick": 0, "count": count}) + "\n")

    def deferred(self):
        self.task("Later", extra='defer_date: "2099-01-01"\n')

    def _argv(self):
        return ["--subject", "AGoal", "--vault", self.vault,
                "--today", "2026-10-15", "--state-dir", self.state]

    def test_a_wake_counted_subject_stays_armed_instead_of_sleeping(self):
        self.deferred()
        self.write("24 Goals/AGoal.md", self.EXEMPT_GOAL)
        self.seed()
        r = self.tick()
        self.assertEqual(r["verdict"], "armed")
        self.assertEqual(r["exempt"]["reason"], self.REASON)

    def test_the_same_fixture_without_the_marker_sleeps(self):
        """The negative control: the marker flips the verdict, not the fixture."""
        self.deferred()
        self.seed()
        r = self.tick()
        self.assertEqual(r["verdict"], "sleep-until")
        self.assertEqual(r["exempt"]["reason"], "")

    def test_the_exemption_blocks_sleep_until_only_never_stood_down(self):
        self.task("Waiting", status="hold")
        self.write("24 Goals/AGoal.md", self.EXEMPT_GOAL)
        self.seed()
        self.assertEqual(self.tick()["verdict"], "stood-down")

    def test_an_absent_page_is_not_an_exemption(self):
        self.assertEqual(
            self.m.exemption(self.vault, ["Gone"], [{"goals": ["Gone"]}])["reason"],
            "",
        )

    def test_the_marker_is_reached_through_the_rows_not_the_subject_name(self):
        """A subject with no tracked rows has nothing to exempt."""
        self.write("24 Goals/AGoal.md", self.EXEMPT_GOAL)
        self.assertEqual(self.m.exemption(self.vault, ["AGoal"], [])["reason"], "")

    def test_the_cli_prints_the_exempt_line_naming_key_and_reason(self):
        self.deferred()
        self.write("24 Goals/AGoal.md", self.EXEMPT_GOAL)
        self.seed()
        self.arm()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = self.m.main(self._argv())
        self.assertEqual(rc, 0)
        lines = out.getvalue().splitlines()
        self.assertTrue(lines[0].startswith("armed\t"))
        self.assertIn("Exempt: quiet_check_exempt — " + self.REASON, lines)

    def test_the_exempt_line_never_displaces_the_stand_down_action_line(self):
        """The action line stays the SECOND line even when the subject is exempt.

        `commands/steps/round-quiet.md` calls the second line the action line and tells
        the round to print it verbatim, so an `Exempt:` line printed earlier would make a
        manager report the exemption instead of the trigger.
        """
        self.task("Waiting", status="hold")
        self.write("24 Goals/AGoal.md", self.EXEMPT_GOAL)
        self.seed()
        self.arm()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.m.main(self._argv())
        lines = out.getvalue().splitlines()
        self.assertTrue(lines[1].startswith("Stood down: zero runnable tasks"))
        self.assertEqual(lines[2], "Exempt: quiet_check_exempt — " + self.REASON)

    def test_several_marked_pages_pick_the_first_by_name_and_carry_all(self):
        self.write("24 Goals/AGoal.md", self.EXEMPT_GOAL)
        self.write("24 Goals/BGoal.md",
                   '---\npage_type: goal\nstatus: in_progress\n'
                   'quiet_check_exempt: "second reason"\n---\n')
        ex = self.m.exemption(
            self.vault, ["AGoal", "BGoal"],
            [{"goals": ["BGoal"]}, {"goals": ["AGoal"]}],
        )
        self.assertEqual(ex["pages"], ["AGoal", "BGoal"])
        self.assertEqual(ex["reason"], self.REASON)

    def test_an_unreadable_page_is_not_an_exemption(self):
        """An OSError on a page that exists reads as no marker, never as a granted one."""
        os.remove(os.path.join(self.vault, "24 Goals", "AGoal.md"))
        os.makedirs(os.path.join(self.vault, "24 Goals", "AGoal.md"))
        self.assertEqual(
            self.m.exemption(self.vault, ["AGoal"], [{"goals": ["AGoal"]}])["reason"],
            "",
        )

    def test_a_cross_listed_row_does_not_lend_another_goals_marker(self):
        self.write("24 Goals/OtherGoal.md", self.EXEMPT_GOAL)
        # Admitted because its NAME matches the member, but its only goal is unrelated.
        self.assertEqual(
            self.m.exemption(
                self.vault, ["AGoal"], [{"goals": ["OtherGoal"]}]
            )["reason"],
            "",
        )

    def test_a_marker_on_a_topic_member_goal_page_is_honoured(self):
        self.write("23 Topics/TGoal.md",
                   '---\npage_type: topic\n---\n\n## Goals\n\n- [[MGoal]]\n')
        self.write("24 Goals/MGoal.md", self.EXEMPT_GOAL)
        self.assertEqual(
            self.m.exemption(self.vault, ["MGoal"], [{"goals": ["MGoal"]}])["reason"],
            self.REASON,
        )
