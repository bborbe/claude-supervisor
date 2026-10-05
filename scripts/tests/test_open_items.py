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
        # `$WEZTERM_PANE` is read at `add` time to record an asked-of-you's origin, and the
        # test process inherits it from whatever pane ran the suite. Popped here so a test
        # that asserts on the origin controls the value rather than reading the runner's
        # pane — the same isolation ROOT gets, for the same reason.
        self._pane = os.environ.pop("WEZTERM_PANE", None)
        self.addCleanup(self._restore)

    def _restore(self):
        oi.ROOT = self._root
        oi._TASK_DIRS = None
        if self._pane is None:
            os.environ.pop("WEZTERM_PANE", None)
        else:
            os.environ["WEZTERM_PANE"] = self._pane

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

    def everything(self):
        """Every entry including closed ones — `ledger()` reads the default render, which
        excludes them by design, so it cannot see a closed entry's record."""
        code, out, _ = self.run_cli(
            ["list", "--state", "all", "--include-closed", "--format", "json"]
        )
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

    Measured 2026-09-21 in the `the topic phase model` manager session: entries
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


class OriginRecording(Base):
    """SC1: an asked-of-you records WHERE its gate was raised — raising pane + session.

    Captured automatically at write time, never via a flag: `add` refuses `--held-in-pane`
    on this kind outright, so the value cannot be supplied by the caller. The field exists
    because the SESSION cannot be the discriminator — every entry in a ledger carries the
    ledger's own session, which stays LIVE long after one of its panes is gone — so the
    PANE is what a later reader needs, and nothing stored it before this change.
    """

    def test_asked_of_you_records_the_raising_pane_and_session(self):
        os.environ["WEZTERM_PANE"] = "19"
        self.add("--kind", "asked-of-you", "--text", "should the PR carry the specs?")
        item = self.ledger()[0]
        self.assertEqual(item["origin_pane"], "19")
        self.assertEqual(item["origin_session"], "s1")

    def test_the_recorded_pane_follows_the_actual_pane_not_a_constant(self):
        """A constant would also read back, so the equality is the probe: change the pane
        and the recorded value must follow it."""
        os.environ["WEZTERM_PANE"] = "19"
        self.add("--kind", "asked-of-you", "--text", "one")
        os.environ["WEZTERM_PANE"] = "481"
        self.add("--kind", "asked-of-you", "--text", "two")
        self.assertEqual([i["origin_pane"] for i in self.ledger()], ["19", "481"])

    def test_asked_of_you_outside_wezterm_records_no_pane(self):
        os.environ.pop("WEZTERM_PANE", None)
        self.add("--kind", "asked-of-you", "--text", "headless?")
        item = self.ledger()[0]
        self.assertIsNone(item["origin_pane"])
        self.assertEqual(item["origin_session"], "s1")

    def test_other_kinds_do_not_record_an_origin(self):
        """Only `asked-of-you` needs it: the other kinds already carry `held_in_pane` as
        their pane provenance, and stamping this session's pane on them would assert an
        origin the ask may not have had — the instruction was given to a worker, not here."""
        os.environ["WEZTERM_PANE"] = "19"
        self.add("--kind", "pushed", "--text", "ship it")
        item = self.ledger()[0]
        self.assertIsNone(item["origin_pane"])
        self.assertIsNone(item["origin_session"])


class ClassifyOrigin(unittest.TestCase):
    """SC2: the classify path reads the STORED origin and returns two different verdicts
    for a gone pane and a live one — not a note, and not the entry's session."""

    def test_gone_when_the_recorded_pane_is_not_live(self):
        self.assertEqual(oi.classify_origin({"origin_pane": "19"}, {"20", "21"}), "gone")

    def test_live_when_the_recorded_pane_is_live(self):
        self.assertEqual(oi.classify_origin({"origin_pane": "19"}, {"19", "20"}), "live")

    def test_unknown_when_no_origin_is_recorded(self):
        """A legacy entry — or any non-asked-of-you — records no origin. `unknown` is not
        `gone`: "could not tell" must never read as "dead", the false-negative this whole
        ledger exists to remove."""
        self.assertEqual(oi.classify_origin({"origin_pane": None}, {"19"}), "unknown")
        self.assertEqual(oi.classify_origin({}, {"19"}), "unknown")

    def test_unknown_when_the_probe_could_not_run(self):
        self.assertEqual(oi.classify_origin({"origin_pane": "19"}, None), "unknown")


class ClassifyCommand(Base):
    """The CLI read path: two entries whose origins differ get two verdicts, and the
    verdict tracks the stored field with NO note written or changed on either."""

    def setUp(self):
        super().setUp()
        self._panes = oi.live_panes
        self.addCleanup(lambda: setattr(oi, "live_panes", self._panes))

    def _stub_panes(self, value):
        oi.live_panes = lambda: value

    def _two_rows(self):
        os.environ["WEZTERM_PANE"] = "19"
        self.add("--kind", "asked-of-you", "--text", "gone one")
        os.environ["WEZTERM_PANE"] = "20"
        self.add("--kind", "asked-of-you", "--text", "live one")

    def test_two_entries_with_different_origins_get_two_verdicts(self):
        self._two_rows()
        self._stub_panes({"20"})
        code, out, _ = self.run_cli(["classify"])
        self.assertEqual(code, 0)
        self.assertIn("origin 19 · gone", out)
        self.assertIn("origin 20 · live", out)

    def test_classify_writes_no_note_on_either_entry(self):
        self._two_rows()
        self._stub_panes({"20"})
        self.run_cli(["classify"])
        for item in self.ledger():
            self.assertIsNone(item["note"])
            self.assertIsNone(item["noted_at"])

    def test_an_unreadable_probe_says_so_and_reports_unknown(self):
        os.environ["WEZTERM_PANE"] = "19"
        self.add("--kind", "asked-of-you", "--text", "x")
        self._stub_panes(None)
        code, out, err = self.run_cli(["classify"])
        self.assertEqual(code, 0)
        self.assertIn("origin 19 · unknown", out)
        self.assertIn("unreadable", err)

    def test_json_carries_the_verdict_and_a_panes_readable_flag(self):
        os.environ["WEZTERM_PANE"] = "19"
        self.add("--kind", "asked-of-you", "--text", "x")
        self._stub_panes({"20"})
        code, out, _ = self.run_cli(["classify", "--format", "json"])
        self.assertEqual(code, 0)
        payload = json.loads(out)
        self.assertTrue(payload["panes_readable"])
        self.assertEqual(payload["items"][0]["origin_verdict"], "gone")


class TickSummaryGuard(Base):
    """A manager tick summary can no longer enter the ledger as an operator ask.

    The positive case alone is satisfied by a guard that refuses everything, so the
    negative controls carry the weight: the identical text on `asked-of-me` must be
    ACCEPTED (that kind holds the operator's words verbatim, and refusing there could
    block a genuine instruction that quotes a tick), and a `pushed` entry whose text is
    the task it filed must be accepted too.
    """

    TICK = (
        "Manager-loop tick 9 (2026-10-06 09:00, probe). Drive leg returned reaped 0 / "
        "nudged 0 / to resume 0 / to open 0."
    )

    def test_refuses_a_tick_summary_as_a_pushed_entry(self):
        code, _, err = self.add("--kind", "pushed", "--text", self.TICK)
        # The refusal must be the RULE's refusal, not argparse's, and the two are
        # distinguishable here rather than by "non-zero exit": argparse exits with the
        # integer 2 and writes "unrecognized arguments" to stderr, while the rule exits
        # with the message itself — `sys.exit(str)` carries it as the exit code, and the
        # harness catches SystemExit before the interpreter can print it.
        self.assertIsInstance(code, str)
        self.assertNotIn("unrecognized arguments", code)
        self.assertIn("tick summary", code)
        self.assertIn("--kind pushed", code)
        self.assertEqual(err, "")

    def test_writes_nothing_when_it_refuses(self):
        """A refusal that still recorded the entry would be the defect wearing a
        non-zero exit code."""
        self.add("--kind", "pushed", "--text", self.TICK)
        self.assertEqual(self.ledger(), [])

    def test_accepts_the_same_text_as_an_asked_of_me(self):
        code, _, _ = self.add("--kind", "asked-of-me", "--text", self.TICK)
        self.assertEqual(code, 0)
        self.assertEqual(len(self.ledger()), 1)

    def test_accepts_a_pushed_entry_whose_text_is_the_task(self):
        code, _, _ = self.add(
            "--kind",
            "pushed",
            "--text",
            "Ship the ledger fix",
            "--task",
            "Ship the ledger fix",
        )
        self.assertEqual(code, 0)
        self.assertEqual(len(self.ledger()), 1)

    def test_refuses_every_real_tick_shape_from_the_ledger(self):
        """The shape is taken from the six entries that motivated the guard, not invented:
        ticks 2, 4, 5, 6, 7 and 8, in the date form the live ledger actually carries."""
        for n in (2, 4, 5, 6, 7, 8):
            code, _, _ = self.add(
                "--kind", "pushed", "--text", "Manager-loop tick %d (2026-09-29 09:41)" % n
            )
            self.assertNotEqual(code, 0, "tick %d was accepted" % n)
        self.assertEqual(self.ledger(), [])

    def test_refuses_a_tick_summary_with_leading_whitespace(self):
        """The pattern stays `^`-anchored and the caller normalises, so a leading space is
        not a silent bypass of the guard."""
        for pad in (" ", "  ", "\n", "\t"):
            code, _, _ = self.add("--kind", "pushed", "--text", pad + self.TICK)
            self.assertNotEqual(code, 0, "accepted with pad %r" % pad)
        self.assertEqual(self.ledger(), [])

    def test_refuses_a_tick_summary_copied_out_of_a_list_or_quote(self):
        """Every form the text actually arrives in: a tick summary is copied out of a sweep's
        own output, where it renders as a bullet, a quoted block, a task-list checkbox line
        or an ordered item. A guard that refused only the unprefixed form would admit all of
        them — and the checkbox and ordinal shapes are the ones a rendered task list emits."""
        for pad in (
            "- ", "* ", "+ ", "> ", "  - ", "> > ",
            "- [ ] ", "- [x] ", "[ ] ", "1. ", "2) ", "  1. ", "– ", "— ",
        ):
            code, _, _ = self.add("--kind", "pushed", "--text", pad + self.TICK)
            self.assertNotEqual(code, 0, "accepted with pad %r" % pad)
        self.assertEqual(self.ledger(), [])

    def test_still_accepts_a_pushed_text_that_merely_starts_with_punctuation(self):
        """Negative control for the marker strip: only the REMAINDER has to match, so
        stripping a leading marker or ordinal must not turn real work into a refusal."""
        for text in (
            "- Added the retry guard",
            "* Ship the ledger fix",
            "> see the PR",
            "1) Fix the thing",
            "1.5 million rows is the wrong figure",
        ):
            code, _, _ = self.add("--kind", "pushed", "--text", text)
            self.assertEqual(code, 0, "refused %r" % text)

    def test_does_not_refuse_a_merely_similar_text(self):
        """A guard that refused anything mentioning a tick would refuse real work."""
        for text in (
            "Manager-loop tick summary handling is broken",
            "Fix the Manager-loop tick (2026-09-29) filing path",
            "the manager-loop tick logs are filed into the ledger",
        ):
            code, _, _ = self.add("--kind", "pushed", "--text", text)
            self.assertEqual(code, 0, "refused %r" % text)


class WithdrawVerb(Base):
    """The exit SC2 chose: an entry that was never an ask reaches a terminal state."""

    def withdraw(self, item, reason="filed in error"):
        return self.run_cli(["withdraw", "--id", item["id"], "--reason", reason])

    def test_closes_a_pushed_entry_naming_no_task(self):
        self.add("--kind", "pushed", "--text", "a log, not an ask")
        code, _, _ = self.withdraw(self.ledger()[0])
        self.assertEqual(code, 0)
        closed = self.everything()[0]
        self.assertEqual(closed["state"], "closed")
        self.assertEqual(
            closed["closed_evidence"], "operator withdrew it: filed in error"
        )
        self.assertIsNotNone(closed["withdrawn_at"])

    def test_closes_an_asked_of_me_entry_naming_no_task(self):
        self.add("--kind", "asked-of-me", "--text", "stop the 500s")
        code, _, _ = self.withdraw(self.ledger()[0], "superseded")
        self.assertEqual(code, 0)
        self.assertEqual(self.everything()[0]["state"], "closed")

    def test_refuses_an_asked_of_you_and_writes_nothing(self):
        """Its only close path is `answer` — a withdrawal here would stamp an operator
        act onto a question they may never have seen."""
        self.add("--kind", "asked-of-you", "--text", "should I ship it?")
        item = self.ledger()[0]
        code, _, err = self.withdraw(item)
        # The rule's refusal carries the message as the exit code, not on stderr — see
        # `test_refuses_a_tick_summary_as_a_pushed_entry` for why the two are told apart.
        self.assertIsInstance(code, str)
        self.assertNotIn("unrecognized arguments", code)
        self.assertIn("answer", code)
        self.assertEqual(err, "")
        still = self.everything()[0]
        self.assertEqual(still["state"], "open")
        self.assertIsNone(still["withdrawn_at"])

    def test_refuses_an_already_closed_entry_and_preserves_its_record(self):
        """Without the state guard the unconditional write replaces an evidence-close with a
        withdrawal claim — recording a resolved ask as one that was never real, which is the
        indistinguishability this verb exists to preserve."""
        self.task_file("Ship the ledger fix")
        self.add("--kind", "pushed", "--text", "ship it", "--task", "Ship the ledger fix")
        item = self.ledger()[0]
        self.run_cli(
            ["close", "--id", item["id"], "--evidence", "task reads status: completed"]
        )
        code, _, err = self.withdraw(item)
        self.assertIsInstance(code, str)
        self.assertIn("already closed", code)
        self.assertEqual(err, "")
        after = self.everything()[0]
        self.assertEqual(after["state"], "closed")
        self.assertEqual(after["closed_evidence"], "task reads status: completed")
        self.assertIsNone(after["withdrawn_at"])

    def test_refuses_an_empty_reason(self):
        """A blank reason records `operator withdrew it: ` — a withdrawal carrying zero
        operator words, which is the attribution forgery this verb exists to prevent,
        reached by omission. `cmd_close` guards the same field the same way."""
        self.add("--kind", "pushed", "--text", "a log, not an ask")
        item = self.ledger()[0]
        for reason in ("", "   "):
            code, _, err = self.withdraw(item, reason)
            self.assertIsInstance(code, str)
            self.assertIn("non-empty --reason", code)
            self.assertEqual(err, "")
        after = self.ledger()[0]
        self.assertEqual(after["state"], "open")
        self.assertIsNone(after["closed_evidence"])

    def test_does_not_forge_an_operator_answer(self):
        """`answer` / `answered_at` assert the operator REPLIED. A withdrawal is a
        different claim, and writing either here would be the forgery the `answer` rule
        exists to prevent — reached through a second verb."""
        self.add("--kind", "pushed", "--text", "a log, not an ask")
        self.withdraw(self.ledger()[0])
        closed = self.everything()[0]
        self.assertIsNone(closed["answer"])
        self.assertIsNone(closed["answered_at"])

    def test_a_withdrawn_entry_leaves_the_default_render(self):
        """The render rule prints every OPEN entry; a terminal one must not print."""
        self.add("--kind", "pushed", "--text", "a log, not an ask")
        self.withdraw(self.ledger()[0])
        code, out, _ = self.listing()
        self.assertEqual(code, 0)
        self.assertIn("(none open)", out)


class NoTaskMarker(Base):
    """The render half: an entry whose close condition can never fire is visible."""

    def test_flags_an_open_pushed_entry_naming_no_task(self):
        self.add("--kind", "pushed", "--text", "a log, not an ask")
        code, out, _ = self.listing()
        self.assertEqual(code, 0)
        self.assertIn("NO TASK", out)

    def test_flags_an_open_asked_of_me_entry_naming_no_task(self):
        self.add("--kind", "asked-of-me", "--text", "stop the 500s")
        _, out, _ = self.listing()
        self.assertIn("NO TASK", out)

    def test_does_not_flag_an_asked_of_you(self):
        """It resolves on the operator's answer and legitimately names no task, so a
        marker here would fire on every question the ledger holds."""
        self.add("--kind", "asked-of-you", "--text", "should I ship it?")
        _, out, _ = self.listing()
        self.assertNotIn("NO TASK", out)

    def test_does_not_flag_an_asked_of_you_carrying_an_unresolvable_task(self):
        """The carve-out is by KIND, covering every state — not only `none`. A question whose
        task is context rather than a resolution path must not be flagged, and `set`, the
        repair verb the UNRESOLVABLE rule names, refuses that kind."""
        self.add(
            "--kind", "asked-of-you", "--text", "should I ship it?", "--task", "Never Filed"
        )
        _, out, _ = self.listing()
        self.assertNotIn("UNRESOLVABLE", out)
        self.assertNotIn("NO TASK", out)

    def test_does_not_flag_an_asked_of_you_when_no_task_dir_was_searchable(self):
        """The carve-out is by KIND, so it must cover all THREE `target_state` values —
        `unknown` is reached when no task dir was searchable, and it carries its own
        marker, so pinning only `none` and `unresolvable` would leave it live."""
        self.add(
            "--kind", "asked-of-you", "--text", "should I ship it?", "--task", "Never Filed"
        )
        code, out, _ = self.run_cli_unsearchable(["list"])
        self.assertEqual(code, 0)
        self.assertNotIn("UNCHECKED", out)
        self.assertNotIn("NO TASK", out)

    def test_does_not_flag_an_entry_whose_task_resolves(self):
        self.task_file("Ship the ledger fix")
        self.add("--kind", "pushed", "--text", "ship it", "--task", "Ship the ledger fix")
        _, out, _ = self.listing()
        self.assertNotIn("NO TASK", out)

    def test_does_not_flag_an_unresolvable_target(self):
        """A NAMED task backing no file is a different fact from no task at all, and it
        already has its own marker — collapsing the two would lose which one happened."""
        self.add("--kind", "asked-of-me", "--text", "stop the 500s", "--task", "Never Filed")
        _, out, _ = self.listing()
        self.assertIn("UNRESOLVABLE", out)
        self.assertNotIn("NO TASK", out)

    def test_does_not_flag_a_closed_entry(self):
        self.add("--kind", "asked-of-me", "--text", "stop the 500s")
        item = self.ledger()[0]
        self.run_cli(["withdraw", "--id", item["id"], "--reason", "superseded"])
        _, out, _ = self.listing("--state", "closed")
        self.assertIn("stop the 500s", out)
        self.assertNotIn("NO TASK", out)


class SetTask(Base):
    """The act step: naming a covering task on an entry that already exists.

    `add` was the only writer of `task`, so the act rule's own trigger — an open entry
    with no task behind it — named an action no verb could perform.
    """

    def test_clears_the_marker_once_a_task_is_named(self):
        self.task_file("Covering Task")
        self.add("--kind", "asked-of-me", "--text", "stop the 500s")
        item = self.ledger()[0]
        _, before, _ = self.listing()
        self.assertIn("NO TASK", before)
        code, _, _ = self.run_cli(["set", "--id", item["id"], "--task", "Covering Task"])
        self.assertEqual(code, 0)
        _, after, _ = self.listing()
        self.assertNotIn("NO TASK", after)

    def test_a_named_task_backing_no_file_is_unresolvable_not_clean(self):
        """`set` must not be usable to fake a resolution: the target is re-resolved on
        every read, so a task that backs no file still reports the truth."""
        self.add("--kind", "asked-of-me", "--text", "stop the 500s")
        item = self.ledger()[0]
        code, _, err = self.run_cli(["set", "--id", item["id"], "--task", "Never Filed"])
        self.assertEqual(code, 0)
        self.assertIn("UNRESOLVABLE", err)
        _, out, _ = self.listing()
        self.assertIn("UNRESOLVABLE", out)
        self.assertNotIn("NO TASK", out)

    def test_refuses_an_asked_of_you(self):
        """Naming a task would move `target_state` off `none` — the value `marker_for`'s
        kind carve-out keys on — so an unresolvable name would render `UNRESOLVABLE` on a
        question whose real resolution is the operator's answer."""
        self.add("--kind", "asked-of-you", "--text", "should I ship it?")
        item = self.ledger()[0]
        code, _, err = self.run_cli(["set", "--id", item["id"], "--task", "Never Filed"])
        self.assertIsInstance(code, str)
        self.assertIn("asked-of-you", code)
        self.assertEqual(err, "")
        after = self.ledger()[0]
        self.assertIsNone(after["task"])
        self.assertEqual(after["task_state"], "none")
        _, out, _ = self.listing()
        self.assertNotIn("NO TASK", out)
        self.assertNotIn("UNRESOLVABLE", out)

    def test_refuses_a_closed_entry(self):
        self.task_file("Covering Task")
        self.add("--kind", "asked-of-me", "--text", "stop the 500s")
        item = self.ledger()[0]
        self.run_cli(["close", "--id", item["id"], "--evidence", "shipped"])
        code, _, err = self.run_cli(["set", "--id", item["id"], "--task", "Covering Task"])
        self.assertIsInstance(code, str)
        self.assertIn("already closed", code)
        self.assertEqual(err, "")
        self.assertIsNone(self.everything()[0]["task"])

    def test_warns_when_it_replaces_an_existing_task(self):
        """Re-pointing is the legitimate way to correct a wrong title, but a silent
        re-point rewrites a decision leaving no trace — so the prior title is named."""
        self.task_file("First Task")
        self.task_file("Second Task")
        self.add("--kind", "asked-of-me", "--text", "stop the 500s", "--task", "First Task")
        item = self.ledger()[0]
        code, _, err = self.run_cli(["set", "--id", item["id"], "--task", "Second Task"])
        self.assertEqual(code, 0)
        self.assertIn("First Task", err)
        self.assertEqual(self.ledger()[0]["task"], "Second Task")

    def test_does_not_warn_when_the_task_is_unchanged(self):
        """Negative control: re-setting the same title replaces nothing."""
        self.task_file("Covering Task")
        self.add("--kind", "asked-of-me", "--text", "stop the 500s")
        item = self.ledger()[0]
        self.run_cli(["set", "--id", item["id"], "--task", "Covering Task"])
        code, _, err = self.run_cli(["set", "--id", item["id"], "--task", "Covering Task"])
        self.assertEqual(code, 0)
        self.assertNotIn("replaced task", err)

    def test_refuses_an_empty_task(self):
        """`resolve_task("")` returns None, so an empty --task writes `task: ""` with no path
        and STRIPS a previously valid resolution path — the same empty-value hole --reason
        and --resolves-on are guarded against on this verb."""
        self.task_file("First Task")
        self.add("--kind", "asked-of-me", "--text", "stop the 500s", "--task", "First Task")
        item = self.ledger()[0]
        code, _, err = self.run_cli(["set", "--id", item["id"], "--task", "  "])
        self.assertIsInstance(code, str)
        self.assertIn("--task needs a non-empty value", code)
        self.assertEqual(err, "")
        after = self.ledger()[0]
        self.assertEqual(after["task"], "First Task")
        self.assertEqual(after["task_state"], "ok")

    def test_refuses_an_empty_resolves_on(self):
        """An empty close condition is one nothing can check — the defect the `NO TASK`
        marker exists to make visible, reintroduced through a flag."""
        self.add("--kind", "asked-of-me", "--text", "stop the 500s")
        item = self.ledger()[0]
        code, _, err = self.run_cli(
            ["set", "--id", item["id"], "--task", "Covering Task", "--resolves-on", "  "]
        )
        self.assertIsInstance(code, str)
        self.assertIn("non-empty", code)
        self.assertEqual(err, "")
        self.assertIsNone(self.ledger()[0]["task"])

    def test_records_the_resolved_path(self):
        self.task_file("Covering Task")
        self.add("--kind", "asked-of-me", "--text", "stop the 500s")
        item = self.ledger()[0]
        self.run_cli(["set", "--id", item["id"], "--task", "Covering Task"])
        row = self.ledger()[0]
        self.assertEqual(row["task"], "Covering Task")
        self.assertEqual(row["task_state"], "ok")


if __name__ == "__main__":
    unittest.main()
