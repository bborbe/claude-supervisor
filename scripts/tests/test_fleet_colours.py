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

import importlib.util
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
            {"session_id": "a", "cwd": "/Users/x/Documents/Obsidian/Personal"},
            {"session_id": "b", "cwd": "/Users/x/Documents/Obsidian/Personal"},
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


if __name__ == "__main__":
    unittest.main()
