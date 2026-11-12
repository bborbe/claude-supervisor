#!/usr/bin/env python3
"""A deliberate stand-down removes its subject from the auto-reopen set, at every tier.

`/supervisor:stop` writes `<branch>--<slug>.stopped` in the sweep-gate state dir. The
MANAGER tier has always read it (`manager-liveness.py --state` reports STOPPED). The
AUTO-OPEN path did not: `open-queue.py` knew only the task tier's `stopped:` page field, so
every flagged task under a stood-down goal or topic was still selected and opened, and
`adopt-orphans.py` handed such a subject's stranded workers to a new owner.

Each case below is a probe with a NEGATIVE CONTROL, because every claim here is an
absence, and an absence passes on a leg that opens nothing:

  * **SC1 — the goal tier.** A stopped goal suppresses its own task and leaves an unstopped
    goal's task alone; clearing the marker restores it.
  * **SC2 — the topic tier**, through the `## Goals` BODY path (`fleet-board.py`'s
    population), never the `topics:` task frontmatter.
  * **SC3 — the task tier still works** (regression): the page field still suppresses, and
    an unmarked control row is still selected.
  * **SC5 — the keepalive restart path still leaves a stood-down subject alone**
    (regression). It passes before this change: the guard is TRANSITIVE, through
    `topic-keepalive.py` → `owner.py` → `manager-liveness.py --state`. It is here so the
    new goal/topic reads cannot break it while every other box goes green.
  * **SC6 — the marker read is ONE resolver.** Two copies would drift from `stop()`'s
    writer, and the drift would be silent.
  * **SC7 — same-titled tasks in different vaults do not collide.** The marker is keyed by
    the subject slug, never by the task title; a title-keyed shortcut would pass SC1 and
    fail this.

⚠️ **Every open-queue probe goes through `collect()` → `select(live=0, target=10)`, never
the bare CLI.** `selected` derives from `opened`, which `capacity()` truncates by the live
fleet's worker count — on a fleet at its cap the CLI returns `selected: []` whatever the
markers say, and the probe would be blind exactly where this code runs.

⚠️ **No probe may read the operator's real `~/.claude/state/sweep-gate`.** A marker found
there would suppress a fixture row for the wrong reason, and a marker written there would
stand a live manager down. `MANAGER_LIVENESS_STATE_DIR` is pointed at a fixture dir, and
the in-process module attribute is repointed with it (the module reads `STATE_DIR` at
import, so the env var alone cannot move an already-loaded module).

Run: python3 -m unittest discover -s scripts/tests -v
"""

import glob
import importlib.util
import io
import os
import pathlib
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout

_HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.dirname(_HERE)

_oq_spec = importlib.util.spec_from_file_location(
    "open_queue_standdown", os.path.join(SCRIPTS, "open-queue.py")
)
oq = importlib.util.module_from_spec(_oq_spec)
_oq_spec.loader.exec_module(oq)

TODAY = "2026-10-30"


def task_page(directory, name, frontmatter, body=""):
    """A task page carrying operator provenance on its flag (the admitted shape)."""
    path = os.path.join(directory, name + ".md")
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("---\n%s\n---\n%s\n" % (frontmatter, body))


def topic_page(directory, name, goals):
    """A topic page whose body `## Goals` section lists `goals` — the population SC2 pins."""
    lines = ["---", "page_type: topic", "---", "", "## Goals", ""]
    lines += ["- [[%s]]" % goal for goal in goals]
    path = os.path.join(directory, name + ".md")
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + "\n")


def approved(names):
    """The shape `approved-not-started.py --json` emits."""
    return {
        "rows": [
            {
                "name": name,
                "approved_at": "2026-10-0%dT09:00:00+00:00" % (index % 9 + 1),
                "age_seconds": 60,
                "manager": None,
            }
            for index, name in enumerate(names)
        ],
        "tick_minutes": 30,
        "count": len(names),
    }


def flagged(goals=None, extra=""):
    """Frontmatter for an approved, operator-flagged task, optionally linked to goals."""
    lines = ["status: in_progress", "priority: 1", "flag: true", "flag_set_by: operator"]
    if goals:
        lines.append("goals:")
        lines.extend("  - '[[%s]]'" % goal for goal in goals)
    if extra:
        lines.append(extra)
    return "\n".join(lines)


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = self._tmp.name
        self.tasks = os.path.join(self.root, "25 Tasks")
        self.topics = os.path.join(self.root, "23 Topics")
        self.state = os.path.join(self.root, "sweep-gate")
        for path in (self.tasks, self.topics, self.state):
            os.makedirs(path)
        # ⚠️ Never the operator's real store: see the module docstring.
        self._saved_env = os.environ.get("MANAGER_LIVENESS_STATE_DIR")
        os.environ["MANAGER_LIVENESS_STATE_DIR"] = self.state
        self._saved_state = oq.manager_liveness.STATE_DIR
        oq.manager_liveness.STATE_DIR = self.state
        self.addCleanup(self._restore)

    def _restore(self):
        oq.manager_liveness.STATE_DIR = self._saved_state
        if self._saved_env is None:
            os.environ.pop("MANAGER_LIVENESS_STATE_DIR", None)
        else:
            os.environ["MANAGER_LIVENESS_STATE_DIR"] = self._saved_env

    def marker(self, name):
        with open(os.path.join(self.state, name), "w", encoding="utf-8") as handle:
            handle.write("2026-11-12T20:45:58+0100\n")

    def clear(self, name):
        os.remove(os.path.join(self.state, name))

    def run_collect(self, names, tasks_dir=None, topics_dir=None):
        """`collect()` → `select(live=0, target=10)`: the seam every SC probe uses."""
        rows, skipped, err = oq.collect(
            tasks_dir or self.tasks,
            TODAY,
            approved_json=approved(names),
            topics_dir=topics_dir or self.topics,
        )
        self.assertIsNone(err)
        opened, select_skipped = oq.select(rows, live=0, target=10)
        return opened, skipped + select_skipped


class GoalTierTest(Base):
    """SC1 — a stopped goal suppresses its own tasks, and only its tasks."""

    def setUp(self):
        super().setUp()
        task_page(self.tasks, "under-stopped", flagged(["Stopped Goal"]))
        task_page(self.tasks, "under-open", flagged(["Open Goal"]))

    def test_stopped_goal_suppresses_only_its_own_task(self):
        self.marker("goal--stopped-goal.stopped")

        opened, skipped = self.run_collect(["under-stopped", "under-open"])

        self.assertEqual([row["name"] for row in opened], ["under-open"])
        reasons = {row["name"]: row["reason"] for row in skipped}
        self.assertEqual(reasons.get("under-stopped"), "subject stood down")

    def test_clearing_the_marker_restores_the_row(self):
        """The other direction — without it, a resolver that suppresses everything passes."""
        self.marker("goal--stopped-goal.stopped")
        self.clear("goal--stopped-goal.stopped")

        opened, skipped = self.run_collect(["under-stopped", "under-open"])

        self.assertEqual(sorted(row["name"] for row in opened),
                         ["under-open", "under-stopped"])
        self.assertEqual(skipped, [])


class TopicTierTest(Base):
    """SC2 — a stopped topic suppresses through the body `## Goals` path."""

    def setUp(self):
        super().setUp()
        topic_page(self.topics, "Stopped Topic", ["A Goal"])
        topic_page(self.topics, "Open Topic", ["Other Goal"])
        task_page(self.tasks, "topic-task", flagged(["A Goal"]))
        task_page(self.tasks, "other-task", flagged(["Other Goal"]))

    def test_stopped_topic_suppresses_through_the_goals_body(self):
        self.marker("topic--stopped-topic.stopped")

        opened, skipped = self.run_collect(["topic-task", "other-task"])

        self.assertEqual([row["name"] for row in opened], ["other-task"])
        self.assertEqual(
            {row["name"]: row["reason"] for row in skipped}.get("topic-task"),
            "subject stood down",
        )

    def test_clearing_the_topic_marker_restores_the_row(self):
        self.marker("topic--stopped-topic.stopped")
        self.clear("topic--stopped-topic.stopped")

        opened, _ = self.run_collect(["topic-task", "other-task"])

        self.assertEqual(sorted(row["name"] for row in opened), ["other-task", "topic-task"])

    def test_a_task_named_directly_under_a_topic_is_suppressed(self):
        """A `## Goals` entry that IS a task title — the nearest-ancestor case."""
        topic_page(self.topics, "Stopped Topic", ["direct-task"])
        task_page(self.tasks, "direct-task", flagged())
        self.marker("topic--stopped-topic.stopped")

        opened, skipped = self.run_collect(["direct-task"])

        self.assertEqual(opened, [])
        self.assertEqual([row["reason"] for row in skipped], ["subject stood down"])


class TaskTierTest(Base):
    """SC3 — the task tier's own page field still suppresses (regression)."""

    def setUp(self):
        super().setUp()
        task_page(self.tasks, "stopped-row", flagged(extra="stopped: true"))
        task_page(self.tasks, "control-row", flagged())

    def test_task_tier_still_suppresses_and_the_control_still_opens(self):
        opened, skipped = self.run_collect(["stopped-row", "control-row"])

        self.assertEqual([row["name"] for row in opened], ["control-row"])
        self.assertEqual({row["name"]: row["reason"] for row in skipped}.get("stopped-row"),
                         "stopped")

    def test_clearing_the_field_restores_the_row(self):
        task_page(self.tasks, "stopped-row", flagged())

        opened, _ = self.run_collect(["stopped-row", "control-row"])

        self.assertEqual(sorted(row["name"] for row in opened),
                         ["control-row", "stopped-row"])


class CrossVaultCollisionTest(Base):
    """SC7 — the marker is keyed by subject slug, never by task title."""

    def setUp(self):
        super().setUp()
        self.other = os.path.join(self.root, "other-vault")
        self.other_tasks = os.path.join(self.other, "25 Tasks")
        self.other_topics = os.path.join(self.other, "23 Topics")
        for path in (self.other_tasks, self.other_topics):
            os.makedirs(path)
        task_page(self.tasks, "Shared Title", flagged(["Stopped Goal"]))
        task_page(self.other_tasks, "Shared Title", flagged(["Open Goal"]))

    def test_same_titled_task_in_the_other_vault_still_opens(self):
        self.marker("goal--stopped-goal.stopped")

        opened_a, skipped_a = self.run_collect(
            ["Shared Title"], tasks_dir=self.tasks, topics_dir=self.topics
        )
        opened_b, skipped_b = self.run_collect(
            ["Shared Title"], tasks_dir=self.other_tasks, topics_dir=self.other_topics
        )

        self.assertEqual(opened_a, [])
        self.assertEqual([row["reason"] for row in skipped_a], ["subject stood down"])
        self.assertEqual([row["name"] for row in opened_b], ["Shared Title"])
        self.assertEqual(skipped_b, [])


class OneResolverTest(unittest.TestCase):
    """SC6 — the marker read is one function, and both consumers call it."""

    def test_exactly_one_definition_and_both_consumers_reference_it(self):
        hits = []
        for path in glob.glob(os.path.join(SCRIPTS, "*.py")):
            with open(path, encoding="utf-8") as handle:
                if "def is_subject_stopped" in handle.read():
                    hits.append(os.path.basename(path))

        self.assertEqual(hits, ["manager-liveness.py"])

        for consumer in ("open-queue.py", "adopt-orphans.py"):
            with open(os.path.join(SCRIPTS, consumer), encoding="utf-8") as handle:
                self.assertIn("is_subject_stopped", handle.read(), consumer)


class KeepaliveStillLeavesTest(Base):
    """SC5 — the restart path keeps not restarting a stood-down subject (regression).

    ⚠️ **Asserted on the decision output, never by spawning a session.** The verdict is
    the guard; a real restart would touch the live fleet.

    The loop state is read for real, through `manager-liveness.py --state` against the
    fixture state dir, so the marker reaches this path exactly as it does in production.
    Only the SESSION probe is stubbed: a fabricated id cannot read LIVE, and a `leave` on
    a `restart`-shaped fixture would pass for the wrong reason.
    """

    GOAL_PAGE = """---
page_type: goal
status: in_progress
phase: execution
claude_session_id: sid-1
---
# Success Criteria
- [ ] one
"""

    def setUp(self):
        super().setUp()
        self.vault = pathlib.Path(self.root) / "vault"
        (self.vault / "24 Goals").mkdir(parents=True)
        (self.vault / "24 Goals" / "A Goal.md").write_text(self.GOAL_PAGE, encoding="utf-8")
        # A cadence record aged past the stale limit: the manager reads as LAPSED, which is
        # what makes `restart` the verdict once the marker is gone.
        cadence = os.path.join(self.state, "goal--a-goal.cadence")
        with open(cadence, "w", encoding="utf-8") as handle:
            handle.write("300\n")
        old = time.time() - 86_400
        os.utime(cadence, (old, old))

        spec = importlib.util.spec_from_file_location(
            "topic_keepalive_standdown", os.path.join(SCRIPTS, "topic-keepalive.py")
        )
        self.k = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.k)
        self._saved_verdict = self.k.session_verdict
        self.k.session_verdict = lambda sid: "LIVE"
        self.addCleanup(self._restore_keepalive)

    def _restore_keepalive(self):
        self.k.session_verdict = self._saved_verdict

    def verdict(self):
        out = io.StringIO()
        with redirect_stdout(out):
            rc = self.k.main(
                ["--vault", str(self.vault), "--subject", "A Goal", "--branch", "goal",
                 "--now", str(time.time())]
            )
        self.assertEqual(0, rc)
        return out.getvalue().splitlines()[0]

    def test_a_stood_down_subject_is_left_alone(self):
        self.marker("goal--a-goal.stopped")

        self.assertEqual("leave", self.verdict())

    def test_clearing_the_marker_lets_the_lapse_be_restarted(self):
        self.marker("goal--a-goal.stopped")
        self.clear("goal--a-goal.stopped")

        self.assertEqual("restart", self.verdict())


if __name__ == "__main__":
    unittest.main()
