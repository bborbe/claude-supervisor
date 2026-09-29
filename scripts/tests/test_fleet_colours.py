#!/usr/bin/env python3
"""Tests for scripts/fleet-colours.py.

Covers the logic that decides what colour a session is reported as. Each case
guards a defect the census could actually ship:

  * first-record-wins -- `agent-color` is re-emitted every turn carrying the
    CURRENT colour, so only the last record is live. Reading the first reports
    the spawn colour forever and silently defeats the "colour set after spawn"
    requirement the census exists to satisfy.
  * default/unknown collapse -- a transcript that exists but carries no record
    (colour never set) is not the same as no transcript at all (nothing to
    read). Collapsing them reports a guess as a measurement.
  * cwd-keyed pane join -- every session in one vault shares a cwd, so joining
    panes on cwd assigns one pane to many sessions and inflates the paned
    count. The registry's pid -> tty is the only join that discriminates.
  * backlog folding -- green/blue/cyan is the backlog; `default` and `purple`
    are not. Folding either in reports unconverted work that does not exist.

Run: python3 -m unittest discover -s scripts/tests -v
"""

import contextlib
import importlib.util
import io
import json
import os
import tempfile
import unittest
from pathlib import Path

_HERE = os.path.dirname(os.path.abspath(__file__))
_SCRIPT = os.path.join(os.path.dirname(_HERE), "fleet-colours.py")

_spec = importlib.util.spec_from_file_location("fleet_colours", _SCRIPT)
fc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fc)


def transcript(directory, sid, colours):
    """Write a transcript whose agent-color records carry `colours` in order."""
    path = Path(directory) / f"{sid}.jsonl"
    with path.open("w", encoding="utf-8") as fh:
        fh.write(json.dumps({"type": "user", "message": "hello"}) + "\n")
        for c in colours:
            fh.write(json.dumps({"type": "agent-color", "agentColor": c, "sessionId": sid}) + "\n")
    return path


class LastRecordWins(unittest.TestCase):
    """The last agent-color record is the live colour, not the first."""

    def test_last_record_wins(self):
        with tempfile.TemporaryDirectory() as d:
            p = transcript(d, "s1", ["green", "green", "purple"])
            self.assertEqual(fc.last_colour(p), "purple")

    def test_single_record(self):
        with tempfile.TemporaryDirectory() as d:
            p = transcript(d, "s1", ["cyan"])
            self.assertEqual(fc.last_colour(p), "cyan")

    def test_no_record_is_none_not_a_colour(self):
        with tempfile.TemporaryDirectory() as d:
            p = transcript(d, "s1", [])
            self.assertIsNone(fc.last_colour(p))

    def test_malformed_line_does_not_lose_the_last_good_record(self):
        """A truncated write mid-transcript must not blank the colour."""
        with tempfile.TemporaryDirectory() as d:
            p = transcript(d, "s1", ["orange"])
            with p.open("a", encoding="utf-8") as fh:
                fh.write('{"type":"agent-color","agentColor":"brok\n')
            self.assertEqual(fc.last_colour(p), "orange")

    def test_compact_and_spaced_serialization_both_read(self):
        """Guards the pre-filter: matching `"type":"agent-color"` verbatim
        assumes compact separators, so a serialization change upstream would
        report the whole fleet as `default` with no error."""
        with tempfile.TemporaryDirectory() as d:
            compact = Path(d) / "compact.jsonl"
            compact.write_text('{"type":"agent-color","agentColor":"green"}\n', encoding="utf-8")
            spaced = Path(d) / "spaced.jsonl"
            spaced.write_text('{"type": "agent-color", "agentColor": "green"}\n', encoding="utf-8")
            self.assertEqual(fc.last_colour(compact), "green")
            self.assertEqual(fc.last_colour(spaced), "green")


class DefaultIsNotUnknown(unittest.TestCase):
    """No record and no transcript are different states, both non-guesses."""

    def test_present_transcript_without_record_is_default(self):
        with tempfile.TemporaryDirectory() as d:
            p = transcript(d, "s1", [])
            colour = fc.UNKNOWN if p is None else (fc.last_colour(p) or fc.DEFAULT)
            self.assertEqual(colour, fc.DEFAULT)

    def test_absent_transcript_is_unknown(self):
        with tempfile.TemporaryDirectory() as d:
            saved = fc.PROJECTS
            fc.PROJECTS = Path(d)
            try:
                self.assertIsNone(fc.find_transcript("nope"))
            finally:
                fc.PROJECTS = saved

    def test_the_two_states_are_distinct_tokens(self):
        self.assertNotEqual(fc.DEFAULT, fc.UNKNOWN)


class PaneJoinIsPidKeyed(unittest.TestCase):
    """Panes join on pid -> tty, never on cwd (which collides within a vault)."""

    def test_pid_ttys_parses_ps_rows(self):
        """`??` (a headless process) must be dropped, not mapped to a tty."""
        real = fc.subprocess.run

        class R:
            stdout = "  101 ttys001 claude\n  102 ?? claude\n  103 ttys002 node\n  104 ttys003 claude\n"

        fc.subprocess.run = lambda *a, **k: R()
        try:
            self.assertEqual(fc.pid_ttys(), {101: "/dev/ttys001", 104: "/dev/ttys003"})
        finally:
            fc.subprocess.run = real

    def test_two_sessions_sharing_a_cwd_are_distinguishable(self):
        """The defect this guards: cwd-keyed joins collapse vault sessions."""
        rows = [
            {"session_id": "a", "cwd": "/Users/x/Documents/Obsidian/my-vault"},
            {"session_id": "b", "cwd": "/Users/x/Documents/Obsidian/my-vault"},
        ]
        self.assertEqual(fc.project_label(rows[0]["cwd"]), fc.project_label(rows[1]["cwd"]))
        self.assertNotEqual(rows[0]["session_id"], rows[1]["session_id"])


class BacklogExcludesNonBacklog(unittest.TestCase):
    """green/blue/cyan is the backlog; default and purple are reported apart."""

    def test_backlog_counts_only_the_three(self):
        rows = [{"colour": c} for c in
                ["green", "cyan", "blue", "default", "purple", "pink", "orange", "yellow"]]
        counts = fc.counts_of(rows)
        backlog = sum(counts.get(c, 0) for c in fc.BACKLOG)
        self.assertEqual(backlog, 3)
        self.assertEqual(counts[fc.DEFAULT], 1)
        self.assertEqual(counts["purple"], 1)

    def test_purple_is_not_in_the_backlog_tuple(self):
        self.assertNotIn("purple", fc.BACKLOG)
        self.assertNotIn(fc.DEFAULT, fc.BACKLOG)


class _Proc:
    """Stand-in for `subprocess.run`'s result: the two fields the scripts read."""

    def __init__(self, stdout, returncode=0):
        self.stdout = stdout
        self.returncode = returncode


class CensusTransportTest(unittest.TestCase):
    """fleet-colours.py's three pane-read states, end to end through `main()`.

    `pane_titles()` returned `{}` for both a failed `wezterm cli list` and a
    reachable WezTerm holding no panes, so `census()` turned a broken transport into
    a per-row claim: every session's `pane` became `None`, `render()` printed
    `N headless`, the `--json` branch emitted `"paned": 0`, and the process exited 0.
    Measured 2026-09-26 against origin/master with the mux socket unreachable: the
    human and `--json` outputs were byte-identical to the reachable-but-empty run --
    a broken transport reported as a measured zero, certified by the success code.

    All three states are asserted together because any two of them can be satisfied
    by a wrong implementation: warning unconditionally passes the broken case and
    fails both healthy ones; never warning passes the healthy cases and fails the
    broken one.
    """

    PANES = [{"pane_id": 5, "tab_id": 1, "window_id": 0, "title": "Session",
              "tty_name": "/dev/ttys001"}]

    def setUp(self):
        self._run = fc.subprocess.run
        self._sessions, self._projects = fc.SESSIONS, fc.PROJECTS
        self.addCleanup(self._restore)
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        fc.SESSIONS = Path(d.name)
        fc.PROJECTS = Path(d.name) / "projects"
        fc.PROJECTS.mkdir(parents=True, exist_ok=True)
        (fc.SESSIONS / "s1.json").write_text(
            json.dumps({"sessionId": "s1", "name": "S1", "cwd": "/tmp", "pid": 101}),
            encoding="utf-8")

    def _restore(self):
        fc.subprocess.run = self._run
        fc.SESSIONS, fc.PROJECTS = self._sessions, self._projects

    def run_census(self, panes, argv=()):
        """Run `main()` against a stubbed transport; (rc, stdout, stderr)."""
        def fake_run(args, **_kwargs):
            if args[0] == "ps":
                return _Proc("  101 ttys001 claude\n")
            if args[0] == "wezterm":
                # A non-zero exit is how a broken transport actually presents: empty
                # stdout, rc 1. Passing `None` here would test a shape wezterm never
                # emits, so the broken state is built from the real one.
                return _Proc("", returncode=1) if panes is None else _Proc(json.dumps(panes))
            raise AssertionError(args)

        fc.subprocess.run = fake_run
        saved_argv = fc.sys.argv
        fc.sys.argv = ["fleet-colours.py", *argv]
        out, err = io.StringIO(), io.StringIO()
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                rc = fc.main()
        finally:
            fc.sys.argv = saved_argv
        return rc, out.getvalue(), err.getvalue()

    def test_unreadable_transport_withholds_the_headless_count(self):
        """`None` from the transport must not be rendered as `N headless`."""
        rc, out, err = self.run_census(None)
        self.assertEqual(0, rc)
        self.assertIn("pane membership withheld", out)
        self.assertNotRegex(out, r"\d+ headless")

    def test_unreadable_transport_withholds_the_paned_count_in_json(self):
        """`"paned": 0` is the false claim; the key must not carry a number."""
        rc, out, err = self.run_census(None, ("--json",))
        self.assertEqual(0, rc)
        self.assertNotRegex(out, r'"paned":\s*\d')
        doc = json.loads(out)
        self.assertIsNone(doc["paned"])
        self.assertFalse(doc["panes_read"])

    def test_healthy_transport_with_panes_is_unchanged(self):
        """The healthy path is untouched: a real paned count, unchanged count line.

        Asserted verbatim, so this passes against the pre-fix script too -- that is
        what makes it a positive control rather than a restatement of the fix.
        """
        rc, out, err = self.run_census(self.PANES)
        self.assertEqual(0, rc, err)
        self.assertIn("1 live sessions · 1 in a wezterm pane · 0 headless", out)
        rc, out, err = self.run_census(self.PANES, ("--json",))
        self.assertEqual(1, json.loads(out)["paned"])

    def test_empty_but_reachable_transport_still_reads_as_empty(self):
        """A reachable WezTerm with no panes is a measured zero, not a failure.

        The control for the `is None` test: warning on `not panes` would pass the
        broken case above and wrongly fail this one. Passes pre-fix too, for the
        same reason as the healthy-with-panes case.
        """
        rc, out, err = self.run_census([])
        self.assertEqual(0, rc, err)
        self.assertIn("1 live sessions · 0 in a wezterm pane · 1 headless", out)
        # The marker, not the bare word: the transcript column legitimately prints
        # "(no transcript — colour unreadable)", which is a different unreadable.
        self.assertNotIn("pane membership withheld", out)
        rc, out, err = self.run_census([], ("--json",))
        self.assertEqual(0, json.loads(out)["paned"])

    def test_panes_read_is_true_whenever_the_query_succeeded(self):
        """The field the broken case sets false is true on both healthy states."""
        for panes in (self.PANES, []):
            with self.subTest(panes=panes):
                _rc, out, _err = self.run_census(panes, ("--json",))
                self.assertTrue(json.loads(out)["panes_read"])

    def test_session_json_carries_panes_read_too(self):
        """The `--session` document must not drop the flag the census form carries.

        A bare row array here would carry `"pane": null` with no `panes_read`
        anywhere, which is the ambiguity this change exists to remove — on the one
        output the census form does not cover.
        """
        _rc, out, _err = self.run_census(None, ("--session", "s1", "--json"))
        self.assertFalse(json.loads(out)["panes_read"])
        _rc, out, _err = self.run_census(self.PANES, ("--session", "s1", "--json"))
        self.assertTrue(json.loads(out)["panes_read"])

    def test_broken_and_empty_are_distinguishable(self):
        """The defect itself: the two states must not answer identically."""
        self.assertNotEqual(self.run_census(None)[1], self.run_census([])[1])
        self.assertNotEqual(self.run_census(None, ("--json",))[1],
                            self.run_census([], ("--json",))[1])


if __name__ == "__main__":
    unittest.main()
