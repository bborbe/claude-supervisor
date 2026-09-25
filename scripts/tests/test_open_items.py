#!/usr/bin/env python3
"""Tests for scripts/open-items.py.

Covers the two ways an open-items entry made a claim nothing ever checked, so a
finished item was indistinguishable from one never looked at:

  * an unresolvable `--task` target -- the entry reads `resolves on: task file
    status: completed` while naming a task that does not exist, so its close
    condition can never fire and nothing says so. Measured 2026-09-19: entry
    301b4032 sat open over a day pointing at a task file that was never created.
  * a path-bearing title sanitised on disk -- `/` cannot appear in a filename, so
    the vault writes `.` and an exact-title lookup returns "no such file" for a
    task sitting right there. Entry c7ec7e29 was `status: completed` the whole
    time and only semantic search found it.

Both are the same shape: a failed lookup and a true absence producing identical
output. The negative-control cases below (a resolvable target renders clean; an
entry naming no task is never flagged) exist because a check that flags
everything satisfies the positive case while catching nothing.

Run: python3 -m unittest discover -s scripts/tests -v
"""

import contextlib
import importlib.util
import io
import json
import os
import sys
import tempfile
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_SCRIPT = os.path.join(os.path.dirname(_HERE), "open-items.py")

_spec = importlib.util.spec_from_file_location("open_items", _SCRIPT)
oi = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(oi)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.tasks = os.path.join(self.tmp, "25 Tasks")
        os.makedirs(self.tasks)
        # ROOT is redirected so no test can touch the real ledger, and _TASK_DIRS
        # is cleared so a cached config read cannot leak between cases.
        self._root = oi.ROOT
        oi.ROOT = os.path.join(self.tmp, "state")
        oi._TASK_DIRS = None
        self.addCleanup(self._restore)

    def _restore(self):
        oi.ROOT = self._root
        oi._TASK_DIRS = None

    def task_file(self, stem, status="completed"):
        path = os.path.join(self.tasks, stem + ".md")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("---\nstatus: %s\n---\n" % status)
        return path

    def _run(self, argv, tasks_dir=None, unsearchable=False):
        """Invoke the real CLI. Global flags must precede the subcommand.

        `unsearchable` simulates vault-cli being absent or its config unreadable —
        the case where the check cannot run at all, which must not be reported as
        "no such task". `tasks_dir` overrides the default temp task dir.
        """
        if unsearchable:
            oi._TASK_DIRS = []
        out, err = io.StringIO(), io.StringIO()
        saved = sys.argv
        globals_ = ["open-items.py", "--session", "s1"]
        if not unsearchable:
            globals_ += ["--tasks-dir", tasks_dir or self.tasks]
        sys.argv = globals_ + argv
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                try:
                    code = oi.main()
                except SystemExit as exc:
                    code = exc.code
        finally:
            sys.argv = saved
        return code, out.getvalue(), err.getvalue()

    def run_cli(self, argv):
        return self._run(argv)

    def run_cli_unsearchable(self, argv):
        return self._run(argv, unsearchable=True)

    def add(self, *argv):
        return self.run_cli(["add"] + list(argv))

    def listing(self, *argv):
        return self.run_cli(["list"] + list(argv))

    def ledger(self):
        code, out, _ = self.run_cli(["list", "--format", "json"])
        self.assertEqual(code, 0)
        return json.loads(out)["items"]


class UnresolvableTarget(Base):
    """Defect 1: the entry names a task file that does not exist."""

    def test_flags_an_open_entry_whose_task_target_backs_no_file(self):
        self.add(
            "--kind",
            "asked-of-me",
            "--text",
            "stop the vault UI 500s",
            "--task",
            "Vault UI 500s on a Stale Vault Name Instead of Reloading Its Vault List",
        )
        code, out, _ = self.listing()
        self.assertEqual(code, 0)
        self.assertIn("UNRESOLVABLE", out)
        self.assertIn("(no file)", out)

    def test_add_warns_but_still_records_the_entry(self):
        """The ledger exists to record an ask BEFORE its task exists — so an
        unresolvable --task must warn, never refuse."""
        code, out, err = self.add(
            "--kind",
            "asked-of-me",
            "--text",
            "file a task for this",
            "--task",
            "A Task That Does Not Exist Yet",
        )
        self.assertEqual(code, 0)
        self.assertIn("added", out)
        self.assertIn("UNRESOLVABLE", err)
        self.assertEqual(len(self.ledger()), 1)

    def test_add_is_silent_when_the_target_resolves(self):
        self.task_file("Ship the ledger fix")
        _, _, err = self.add(
            "--kind", "pushed", "--text", "ship it", "--task", "Ship the ledger fix"
        )
        self.assertNotIn("UNRESOLVABLE", err)

    def test_add_records_the_resolved_path(self):
        path = self.task_file("Ship the ledger fix")
        self.add(
            "--kind", "pushed", "--text", "ship it", "--task", "Ship the ledger fix"
        )
        self.assertEqual(self.ledger()[0]["task_path"], path)


class PathBearingTitle(Base):
    """Defect 2: the title contains a path, so the filename was sanitised."""

    def test_resolves_a_path_bearing_title_against_the_sanitised_filename(self):
        self.task_file(
            "Take Ownership of ~.claude.commands.open.md — Land the Entangled Hunks"
        )
        self.add(
            "--kind",
            "pushed",
            "--text",
            "own open.md",
            "--task",
            "Take Ownership of ~/.claude/commands/open.md — Land the Entangled Hunks",
        )
        code, out, _ = self.listing()
        self.assertEqual(code, 0)
        self.assertNotIn("UNRESOLVABLE", out)
        self.assertNotIn("(no file)", out)

    def test_candidates_lead_with_the_measured_on_disk_form(self):
        """The real file for this title is `Own ~.claude.commands.open.md` — the `/.`
        collapses to one dot, it does not double to `..`. Both readings are emitted,
        the measured one first."""
        title = "Own ~/.claude/commands/open.md"
        candidates = list(oi.filename_candidates(title))
        self.assertEqual(candidates[0], title)
        self.assertEqual(candidates[1], "Own ~.claude.commands.open.md")
        self.assertIn("Own ~..claude.commands.open.md", candidates)
        self.assertEqual(len(set(candidates)), len(candidates))

    def test_candidates_are_a_single_entry_when_no_path_is_present(self):
        self.assertEqual(list(oi.filename_candidates("Plain Title")), ["Plain Title"])


class NegativeControls(Base):
    """A check that flags everything satisfies the positive case and catches
    nothing — these are the cases that make the flag mean something."""

    def test_a_resolvable_target_renders_with_no_marker(self):
        self.task_file("Ship the ledger fix")
        self.add(
            "--kind", "pushed", "--text", "ship it", "--task", "Ship the ledger fix"
        )
        code, out, _ = self.listing()
        self.assertEqual(code, 0)
        self.assertNotIn("UNRESOLVABLE", out)
        self.assertNotIn("(no file)", out)

    def test_an_entry_naming_no_task_is_never_flagged(self):
        """An asked-of-you resolves on the operator's answer — it makes no file
        claim, so there is nothing to falsify."""
        self.add("--kind", "asked-of-you", "--text", "should I ship it?")
        code, out, _ = self.listing()
        self.assertEqual(code, 0)
        self.assertNotIn("UNRESOLVABLE", out)
        self.assertEqual(self.ledger()[0]["task_state"], "none")

    def test_a_closed_entry_is_not_flagged(self):
        """A closed entry is terminal: its close condition gates nothing, so an
        unresolvable target on it is history. Flagging it would make the very
        entries this fix explains look broken after they were closed correctly."""
        self.add(
            "--kind", "asked-of-me", "--text", "stop the 500s", "--task", "Never Filed"
        )
        self.run_cli(["close", "--id", self.ledger()[0]["id"], "--evidence", "shipped"])
        code, out, _ = self.listing("--state", "closed")
        self.assertEqual(code, 0)
        self.assertIn("Never Filed", out)
        self.assertNotIn("UNRESOLVABLE", out)

    def test_an_entry_flagged_open_then_backed_by_a_file_clears(self):
        """The flag tracks disk, not a stamp written at add time: filing the task
        afterwards must clear it without re-adding the entry."""
        self.add(
            "--kind", "asked-of-me", "--text", "stop the 500s", "--task", "Late Task"
        )
        _, before, _ = self.listing()
        self.assertIn("UNRESOLVABLE", before)
        self.task_file("Late Task")
        _, after, _ = self.listing()
        self.assertNotIn("UNRESOLVABLE", after)


class UnsearchableDirs(Base):
    """A check that could not run must say so, not assert a negative.

    Reporting "UNRESOLVABLE" from a search that never executed is the same
    positive-claim-from-a-failed-lookup shape the whole check exists to remove —
    merely inverted — and it would flag every entry on a host without vault-cli.
    """

    def test_list_reports_unknown_not_unresolvable(self):
        self.run_cli_unsearchable(
            ["add", "--kind", "asked-of-me", "--text", "do a thing", "--task", "Any Task"]
        )
        code, out, _ = self.run_cli_unsearchable(["list"])
        self.assertEqual(code, 0)
        self.assertIn("UNCHECKED", out)
        self.assertNotIn("UNRESOLVABLE", out)

    def test_add_warns_that_it_did_not_check(self):
        code, _, err = self.run_cli_unsearchable(
            ["add", "--kind", "asked-of-me", "--text", "x", "--task", "Any Task"]
        )
        self.assertEqual(code, 0)
        self.assertIn("NOT checked", err)
        self.assertNotIn("resolves to no file", err)

    def test_json_carries_the_unknown_state(self):
        self.run_cli_unsearchable(
            ["add", "--kind", "asked-of-me", "--text", "x", "--task", "Any Task"]
        )
        code, out, _ = self.run_cli_unsearchable(["list", "--format", "json"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["items"][0]["task_state"], "unknown")

    def test_an_entry_naming_no_task_is_still_none(self):
        """`none` and `unknown` are different facts and must not merge: one entry
        makes no claim, the other's claim simply went unchecked."""
        self.run_cli_unsearchable(["add", "--kind", "asked-of-you", "--text", "why?"])
        code, out, _ = self.run_cli_unsearchable(["list", "--format", "json"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["items"][0]["task_state"], "none")

    def test_a_nonexistent_tasks_dir_is_unknown_not_unresolvable(self):
        """A typo'd --tasks-dir, or a vault that moved, is not searchable either. A
        non-empty dir list must not by itself count as a check that ran."""
        dead = "/nonexistent/vault/tasks"
        self._run(
            ["add", "--kind", "asked-of-me", "--text", "x", "--task", "Any Task"],
            tasks_dir=dead,
        )
        code, out, _ = self._run(["list"], tasks_dir=dead)
        self.assertEqual(code, 0)
        self.assertIn("UNCHECKED", out)
        self.assertNotIn("UNRESOLVABLE", out)


class JsonShape(Base):
    def test_json_carries_the_three_valued_task_state(self):
        self.task_file("Real Task")
        self.add("--kind", "pushed", "--text", "a", "--task", "Real Task")
        self.add("--kind", "pushed", "--text", "b", "--task", "Missing Task")
        self.add("--kind", "asked-of-you", "--text", "c")
        states = [item["task_state"] for item in self.ledger()]
        self.assertEqual(states, ["ok", "unresolvable", "none"])


class PaneHeldAskedOfYou(Base):
    """A gate the operator releases in a worker's own pane is not a ledger entry.

    `asked-of-you`'s only close path is `answer`, and `answer` needs THIS session to
    receive the operator's words. For a gate in a tab worker's pane the manager is
    forbidden from receiving them — "a relay never releases a gate" — so the entry is
    unclosable by construction: it sits open forever, indistinguishable from a question
    genuinely still outstanding, and the ledger answers "what is still asked of me"
    wrongly and permanently.

    Measured 2026-09-21 in the `Phase-Gated Topic Flow` manager session: entries
    `7fbcec86`, `d1161bc4` and `4f79f16e` were created for pane gates and not one could
    close on its own — all three were eventually cleared by a manual batch pick that
    stamped them `operator answered in session: y`, an attribution a later reader cannot
    tell from a genuine answer. `f9c512c1` is still open over a gate whose pane no longer
    exists.

    The flag asserts where the ask lives, so the refusal follows from a fact rather than
    a special case: on `asked-of-me` / `pushed` a pane origin is ordinary provenance,
    because those kinds close on their task file.
    """

    def test_refuses_an_asked_of_you_held_in_a_workers_pane(self):
        code, _, err = self.add(
            "--kind",
            "asked-of-you",
            "--text",
            "apply the auditor's two Critical fixes to prompt 2, then re-audit",
            "--held-in-pane",
            "766",
        )
        # The refusal must be the RULE's refusal, not argparse's, and the two are
        # distinguishable here rather than by "non-zero exit": argparse exits with the
        # integer 2 and writes "unrecognized arguments" to stderr, while the rule exits
        # with the message itself — `sys.exit(str)` carries it as the exit code, and the
        # harness catches SystemExit before the interpreter can print it. An assertion on
        # the exit code alone would pass against the pre-change script and pin nothing.
        self.assertIsInstance(code, str)
        self.assertNotIn("unrecognized arguments", code)
        self.assertIn("jump-link.py 766", code)
        self.assertIn("never releases a gate", code)
        self.assertEqual(err, "")
        self.assertEqual(self.ledger(), [])

    def test_an_asked_of_you_answered_here_still_adds(self):
        """Negative control: the guard must subtract only the pane-held case.

        A question this session can receive the answer to closes normally — a relayable
        non-gate question, or a headless worker's gate answered over the supervisor's
        permission channel. A check that refused everything would satisfy the positive
        case above while catching nothing.
        """
        code, out, _ = self.add(
            "--kind",
            "asked-of-you",
            "--text",
            "should the PR carry the daemon's regenerated specs?",
        )
        self.assertEqual(code, 0)
        self.assertIn("added", out)
        self.assertEqual(len(self.ledger()), 1)

    def test_a_pane_origin_is_recorded_on_the_kinds_that_close_on_their_task(self):
        self.task_file("Ship the ledger fix")
        code, _, _ = self.add(
            "--kind",
            "asked-of-me",
            "--text",
            "stop the vault UI 500s",
            "--task",
            "Ship the ledger fix",
            "--held-in-pane",
            "766",
        )
        self.assertEqual(code, 0)
        self.assertEqual(self.ledger()[0]["held_in_pane"], "766")
        _, out, _ = self.listing()
        self.assertIn("pane: 766", out)


class Resolution(unittest.TestCase):
    def test_resolve_task_returns_none_for_an_empty_title(self):
        self.assertIsNone(oi.resolve_task(None, []))
        self.assertIsNone(oi.resolve_task("", ["/nonexistent"]))

    def test_resolve_task_searches_every_directory(self):
        tmp = tempfile.mkdtemp()
        other = os.path.join(tmp, "other")
        os.makedirs(other)
        path = os.path.join(other, "Found.md")
        open(path, "w", encoding="utf-8").close()
        self.assertEqual(oi.resolve_task("Found", [os.path.join(tmp, "no"), other]), path)
        self.assertIsNone(oi.resolve_task("Absent", [os.path.join(tmp, "no"), other]))


if __name__ == "__main__":
    unittest.main()
