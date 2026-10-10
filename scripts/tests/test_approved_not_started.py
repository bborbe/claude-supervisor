#!/usr/bin/env python3
"""Tests for scripts/approved-not-started.py.

Each case guards a defect the line has actually shipped or was filed against:

  * frontmatter-only reading -- a whole-file scan matches keys quoted in task-body
    prose and approves a task on the strength of a sentence *about* approval.
  * the 30-minute tick, asserted in BOTH directions -- a row 31 minutes old is listed
    and a row 29 minutes old is not, because a one-sided fixture passes on a script
    that lists everything.
  * `unknown` on an unreadable registry, never `0` -- the `None`-not-`0` rule from
    `docs/pane-reads.md`. An empty fleet and a failed read must not render the same,
    and only one of them is a fact about the fleet.

Run: python3 -m unittest discover -s scripts/tests -v
"""

import contextlib
import importlib.util
import io
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

# Side effect only: points the start-time cache at an isolated per-run store, in one shared
# home so five suites cannot each assign the same key and leave only the last standing.
import start_cache_isolation  # noqa: E402,F401
from endpoint_fixture import FixtureEndpoint, row as heartbeat_row  # noqa: E402

_HERE = os.path.dirname(os.path.abspath(__file__))
_SCRIPT = os.path.join(os.path.dirname(_HERE), "approved-not-started.py")

_spec = importlib.util.spec_from_file_location("approved_not_started", _SCRIPT)
ans = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ans)

NOW = datetime(2026, 10, 1, 22, 0, 0, tzinfo=timezone.utc)


def task_file(directory, name, frontmatter, body=""):
    path = os.path.join(directory, name + ".md")
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("---\n%s\n---\n%s\n" % (frontmatter, body))
    return path


def row(name, age_minutes, manager=None):
    return {
        "name": name,
        "approved_at": (NOW - timedelta(minutes=age_minutes)).isoformat(),
        "age_seconds": age_minutes * 60,
        "manager": manager,
    }


class Frontmatter(unittest.TestCase):
    def test_extracts_only_the_frontmatter_block(self):
        text = "---\nstatus: in_progress\n---\n\nstatus: completed\n"
        self.assertEqual(ans.frontmatter(text), "status: in_progress")

    def test_body_prose_does_not_leak_in(self):
        """The bug this guards: a body line quoting a key read as a real claim."""
        text = (
            "---\nstatus: next\n---\n\n"
            "This task is about `approved_at` and why a row without one is not approved.\n"
        )
        self.assertNotIn("approved_at", ans.frontmatter(text))

    def test_missing_frontmatter_is_empty(self):
        self.assertEqual(ans.frontmatter("no frontmatter here\n"), "")


class ParseIso(unittest.TestCase):
    def test_offset_timestamp(self):
        parsed = ans.parse_iso("2026-10-01T21:45:08.137545+02:00")
        self.assertEqual(parsed.astimezone(timezone.utc).hour, 19)

    def test_zulu_suffix(self):
        self.assertEqual(ans.parse_iso("2026-09-28T00:00:00Z").tzinfo, timezone.utc)

    def test_date_only(self):
        self.assertEqual(ans.parse_iso("2026-09-28").date().isoformat(), "2026-09-28")

    def test_quoted_value(self):
        self.assertIsNotNone(ans.parse_iso('"2026-09-28"'))

    def test_garbage_is_none(self):
        self.assertIsNone(ans.parse_iso("not a date"))

    def test_absent_is_none(self):
        self.assertIsNone(ans.parse_iso(None))
        self.assertIsNone(ans.parse_iso(""))


class FormatAge(unittest.TestCase):
    def test_minutes(self):
        self.assertEqual(ans.format_age(45 * 60), "45m")

    def test_hours(self):
        self.assertEqual(ans.format_age((7 * 60 + 35) * 60), "7h35m")

    def test_days(self):
        self.assertEqual(ans.format_age((2 * 24 + 3) * 3600), "2d3h")

    def test_negative_clamps_to_zero(self):
        self.assertEqual(ans.format_age(-90), "0m")


class ManagerFor(unittest.TestCase):
    def test_exact_goal_title_match(self):
        live = {"manager layer": "sid-1"}
        self.assertEqual(ans.manager_for(["[[Manager Layer]]"], live), "Manager Layer")

    def test_manager_suffix_allowed(self):
        live = {"manager layer manager": "sid-1"}
        self.assertEqual(ans.manager_for(["[[Manager Layer]]"], live), "Manager Layer Manager")

    def test_no_live_manager_is_none(self):
        self.assertIsNone(ans.manager_for(["[[Manager Layer]]"], {}))

    def test_no_goals_is_none(self):
        self.assertIsNone(ans.manager_for([], {"manager layer": "sid-1"}))


class Collect(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def _collect(self, live):
        return ans.collect(self.dir, live, NOW)

    def test_approved_without_a_session_is_included(self):
        task_file(
            self.dir,
            "Build claude-interactive",
            "approved_at: 2026-10-01T14:06:25.41417+02:00\nstatus: next\nphase: planning",
        )
        rows = self._collect([])
        self.assertEqual([r["name"] for r in rows], ["Build claude-interactive"])

    def test_approved_with_a_live_session_is_excluded(self):
        """A recorded id is not an owner -- it must be probed, not trusted."""
        task_file(
            self.dir,
            "Started",
            "approved_at: 2026-10-01T21:00:00+00:00\nstatus: in_progress\n"
            "claude_session_id: abc-123",
        )
        rows = self._collect([{"sessionId": "abc-123", "name": "Started", "alive": True}])
        self.assertEqual(rows, [])

    def test_approved_with_a_dead_session_is_included(self):
        task_file(
            self.dir,
            "Died",
            "approved_at: 2026-10-01T21:00:00+00:00\nstatus: in_progress\n"
            "claude_session_id: dead-456",
        )
        rows = self._collect([{"sessionId": "live-789", "name": "Other", "alive": True}])
        self.assertEqual([r["name"] for r in rows], ["Died"])

    def test_terminal_statuses_are_excluded(self):
        for i, status in enumerate(("completed", "aborted", "hold", "backlog")):
            task_file(
                self.dir,
                "Terminal %d" % i,
                "approved_at: 2026-09-01T00:00:00Z\nstatus: %s" % status,
            )
        self.assertEqual(self._collect([]), [])

    def test_no_approved_at_is_excluded(self):
        task_file(self.dir, "Unapproved", "status: next\nphase: todo")
        self.assertEqual(self._collect([]), [])

    def test_unparseable_approved_at_is_excluded(self):
        task_file(self.dir, "Bad date", "approved_at: someday\nstatus: next")
        self.assertEqual(self._collect([]), [])

    def test_no_frontmatter_is_excluded(self):
        with open(os.path.join(self.dir, "Bare.md"), "w", encoding="utf-8") as handle:
            handle.write("just a body\n")
        self.assertEqual(self._collect([]), [])

    def test_body_prose_naming_approved_at_does_not_approve(self):
        task_file(
            self.dir,
            "Prose only",
            "status: next",
            body="This row mentions approved_at but carries none.",
        )
        self.assertEqual(self._collect([]), [])

    def test_oldest_first(self):
        task_file(self.dir, "Newer", "approved_at: 2026-10-01T21:00:00Z\nstatus: next")
        task_file(self.dir, "Older", "approved_at: 2026-09-01T00:00:00Z\nstatus: next")
        self.assertEqual([r["name"] for r in self._collect([])], ["Older", "Newer"])

    def test_manager_joined_from_goals(self):
        task_file(
            self.dir,
            "Owned",
            "approved_at: 2026-10-01T21:00:00Z\nstatus: next\n"
            "goals:\n    - '[[Manager Layer]]'",
        )
        rows = self._collect([{"sessionId": "sid", "name": "Manager Layer", "alive": True}])
        self.assertEqual(rows[0]["manager"], "Manager Layer")


class Render(unittest.TestCase):
    def test_empty_is_a_zero_line(self):
        self.assertEqual(ans.render([], ans.TICK_MINUTES), "approved, not started: 0")

    def test_head_line_shape(self):
        """The head is the first line — the row is also over the tick, so it repeats below."""
        line = ans.render([row("Build claude-interactive", 7 * 60 + 35)], ans.TICK_MINUTES)
        self.assertEqual(
            line.splitlines()[0],
            "approved, not started: 1 · oldest 7h35m (Build claude-interactive)",
        )

    def test_row_over_the_tick_is_listed(self):
        line = ans.render([row("Slow", 31)], ans.TICK_MINUTES)
        self.assertIn("Slow", line)
        self.assertIn("over one tick (30m)", line)

    def test_row_under_the_tick_is_not_listed(self):
        """The other half of the fixture -- a one-sided test passes on a list-everything bug."""
        line = ans.render([row("Fresh", 29)], ans.TICK_MINUTES)
        self.assertIn("Fresh", line)  # it is the oldest, so it heads the line
        self.assertNotIn("over one tick", line)

    def test_both_sides_in_one_render(self):
        line = ans.render([row("Old", 31), row("Fresh", 29)], ans.TICK_MINUTES)
        self.assertIn("Old", line)
        self.assertNotIn("Fresh", line)

    def test_unmanaged_is_named_not_hidden(self):
        line = ans.render([row("Orphan", 45)], ans.TICK_MINUTES)
        self.assertIn("manager unmanaged", line)

    def test_tick_is_thirty_minutes(self):
        self.assertEqual(ans.TICK_MINUTES, 30)


class UnreadableRegistry(unittest.TestCase):
    """SC3 -- the `None`-not-`0` rule, probed by pointing the registry at nothing."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        task_file(self.dir, "Waiting", "approved_at: 2026-10-01T21:00:00Z\nstatus: next")
        # A readable identity registry holding one session, plus a fixture endpoint holding one
        # LIVE row — `session-liveness.py --list` reports a list only when BOTH the endpoint
        # (liveness) and the registry (names) were read. ⚠️ **The endpoint row is not decoration:
        # an EMPTY endpoint no longer requires the registry** — `session-liveness.py` decides the
        # empty case before it reads the registry, so a readable-but-empty store returns a clean
        # `[]` whatever the registry does. The registry-unreadable path this class probes is only
        # reachable while the endpoint actually holds a live row, which is why the fixture carries
        # one. The endpoint is a fixture on an ephemeral port pointed at through
        # `$ATTENTION_STORE_URL`, so the real store on this machine cannot turn the clean-run
        # assertion below into a failure when the store is down.
        self.registry = tempfile.mkdtemp()
        with open(os.path.join(self.registry, "%d.json" % os.getpid()), "w", encoding="utf-8") as handle:
            json.dump({"pid": os.getpid(), "sessionId": "live-session-1", "name": "Live"}, handle)
        self.heartbeat = tempfile.mkdtemp()
        self._endpoint = FixtureEndpoint([heartbeat_row("live-session-1")])
        self._endpoint.__enter__()
        self._prior_url = os.environ.get("ATTENTION_STORE_URL")
        os.environ["ATTENTION_STORE_URL"] = self._endpoint.url

    def tearDown(self):
        if self._prior_url is None:
            os.environ.pop("ATTENTION_STORE_URL", None)
        else:
            os.environ["ATTENTION_STORE_URL"] = self._prior_url
        self._endpoint.__exit__(None, None, None)

    def test_read_live_returns_none_not_an_empty_list(self):
        self.assertIsNone(ans.read_live("/nonexistent-registry-dir-xyz"))

    def test_main_prints_unknown_and_exits_non_zero(self):
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = ans.main(["--tasks-dir", self.dir, "--registry-dir", "/nonexistent-registry-dir-xyz"])
        self.assertNotEqual(code, 0)
        # The line reaches stdout so a caller that captured only stdout renders a failed
        # read rather than a clean round; the explanation lands on stderr. It is never a count.
        self.assertEqual(stdout.getvalue().strip(), "approved, not started: unknown")
        self.assertIn("unreadable", stderr.getvalue())
        self.assertNotIn("approved, not started: 0", stdout.getvalue() + stderr.getvalue())

    def test_a_clean_run_still_prints_the_count_on_stdout(self):
        """The negative control — the stdout line must not be `unknown` when the read worked."""
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            code = ans.main(
                [
                    "--tasks-dir", self.dir,
                    "--registry-dir", self.registry,
                    "--heartbeat-dir", self.heartbeat,
                ]
            )
        self.assertEqual(code, 0)
        self.assertNotIn("unknown", stdout.getvalue())
        # The headline is the FIRST line, not the whole stream: `render` appends the per-row
        # delta section beneath it, so anchoring on the full output fails on a render that is
        # working exactly as intended. Measured 2026-10-01 → 2026-10-03: this assertion was
        # red on master for two days, because `0e8b9ec` added that section and only this test
        # still expected one line.
        #
        # Shape, not an exact age: this run reads the real clock, so pinning the duration
        # would make the test fail the moment the fixture date passes.
        headline = stdout.getvalue().strip().splitlines()[0]
        self.assertRegex(
            headline,
            r"^approved, not started: 1 · oldest \d+[mhd]+\d*[mh]? \(Waiting\)$",
        )
        # And the section beneath it actually renders the row, so this stays a positive
        # control: a headline-only assertion passes on a render that dropped every row.
        self.assertIn("manager unmanaged", stdout.getvalue())

    def test_missing_tasks_dir_is_a_usage_error(self):
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            code = ans.main(["--tasks-dir", "/nonexistent-tasks-dir-xyz"])
        self.assertEqual(code, 1)


if __name__ == "__main__":
    unittest.main()
