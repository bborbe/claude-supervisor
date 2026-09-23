#!/usr/bin/env python3
"""Tests for scripts/fleet-sessions.py — the spawn-ledger join.

Covers the logic that decides what a row says about WHERE a session came from.
Each case guards a defect the view could actually ship:

  * hardcoded ledger path -- the writer resolves its directory from
    `SUPERVISOR_LEDGER_DIR` and only then falls back to the XDG state dir. A
    reader that hardcodes either one drifts from the writer silently: it keeps
    printing `unknown` for every row while looking like a working join.
  * inferred mode -- `interactive` and `headless` exist only in the ledger. A
    view that guesses from argv or transcript recency reports a mode it never
    measured, which is worse than `unknown` because it reads as a fact.
  * blank instead of unknown -- a session with no record was never *recorded*,
    which is a different claim from *not spawned*. A blank cell collapses the
    two and reads as a value the ledger supplied.
  * constant spawn counts -- the count is derived from `spawned_at` at render
    time. A constant satisfies any point equality on the day it is checked and
    diverges the next; only a state transition forces derivation. The two
    buckets (UTC and local) additionally cannot both be satisfied by one value.

Run: python3 -m unittest discover -s scripts/tests -v
"""

import contextlib
import importlib.util
import io
import json
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

_HERE = os.path.dirname(os.path.abspath(__file__))
_SCRIPT = os.path.join(os.path.dirname(_HERE), "fleet-sessions.py")

_spec = importlib.util.spec_from_file_location("fleet_sessions", _SCRIPT)
fs = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fs)

SID = "11111111-2222-3333-4444-555555555555"


def write_record(directory, session_id, **fields):
    """Write one ledger record the way the writer does — `<session_id>.json`."""
    record = {"session_id": session_id, "mode": None, "spawned_at": None, **fields}
    path = Path(directory) / f"{session_id}.json"
    path.write_text(json.dumps(record), encoding="utf-8")
    return path


class LedgerDirResolution(unittest.TestCase):
    """The reader resolves the directory exactly as the writer does."""

    def test_env_override_wins(self):
        with mock.patch.dict(os.environ, {"SUPERVISOR_LEDGER_DIR": "/tmp/custom-ledger"}):
            self.assertEqual(fs.ledger_dir(), Path("/tmp/custom-ledger"))

    def test_xdg_state_home_is_honoured(self):
        env = {k: v for k, v in os.environ.items() if k != "SUPERVISOR_LEDGER_DIR"}
        env["XDG_STATE_HOME"] = "/tmp/xdg-state"
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertEqual(fs.ledger_dir(), Path("/tmp/xdg-state/claude-supervisor/sessions"))

    def test_falls_back_to_local_state(self):
        env = {
            k: v
            for k, v in os.environ.items()
            if k not in ("SUPERVISOR_LEDGER_DIR", "XDG_STATE_HOME")
        }
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertEqual(
                fs.ledger_dir(), Path.home() / ".local" / "state" / "claude-supervisor" / "sessions"
            )

    def test_sessions_dir_is_not_the_ledger(self):
        """`SUPERVISOR_SESSIONS_DIR` names the live registry, a different store."""
        with mock.patch.dict(
            os.environ,
            {"SUPERVISOR_SESSIONS_DIR": "/tmp/registry", "XDG_STATE_HOME": "/tmp/xdg-state"},
            clear=True,
        ):
            self.assertNotIn("registry", str(fs.ledger_dir()))


class ReadLedger(unittest.TestCase):
    """A missing or unreadable store degrades the columns; it never aborts."""

    def test_missing_directory_reads_empty(self):
        with mock.patch.dict(os.environ, {"SUPERVISOR_LEDGER_DIR": "/tmp/does-not-exist-xyz"}):
            self.assertEqual(fs.read_ledger(), {})

    def test_records_are_keyed_by_session_id(self):
        with tempfile.TemporaryDirectory() as d:
            write_record(d, SID, mode="headless", label="worker")
            with mock.patch.dict(os.environ, {"SUPERVISOR_LEDGER_DIR": d}):
                ledger = fs.read_ledger()
        self.assertIn(SID, ledger)
        self.assertEqual(ledger[SID]["mode"], "headless")

    def test_a_corrupt_record_does_not_abort_the_rest(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "broken.json").write_text("{not json", encoding="utf-8")
            write_record(d, SID, mode="interactive")
            with mock.patch.dict(os.environ, {"SUPERVISOR_LEDGER_DIR": d}):
                ledger = fs.read_ledger()
        self.assertIn(SID, ledger)


class ModeIsLedgerSourced(unittest.TestCase):
    """Mode comes from the ledger, and its absence is `unknown`, never a guess."""

    def test_ledger_mode_is_rendered(self):
        self.assertEqual(fs.spawn_mode({"mode": "headless"}), "headless")
        self.assertEqual(fs.spawn_mode({"mode": "interactive"}), "interactive")

    def test_no_record_is_unknown(self):
        self.assertEqual(fs.spawn_mode(None), fs.UNKNOWN)

    def test_record_without_mode_is_unknown(self):
        """A record that exists but carries no mode is still not a measured mode."""
        self.assertEqual(fs.spawn_mode({"label": "worker"}), fs.UNKNOWN)


class Attribution(unittest.TestCase):
    """The manager and label the ledger recorded — or an explicit unknown."""

    def test_label_is_rendered_when_no_manager_is_recorded(self):
        """`parent_session` is null in every live record; the label still names the work."""
        self.assertEqual(fs.attribution({"label": "audit the parser"}), "audit the parser")

    def test_manager_and_label_render_together(self):
        rec = {"parent_session": SID, "label": "audit the parser"}
        self.assertEqual(fs.attribution(rec), f"{SID[:8]} audit the parser")

    def test_manager_alone_still_renders(self):
        self.assertEqual(fs.attribution({"parent_session": SID}), SID[:8])

    def test_agent_id_is_the_last_resort(self):
        self.assertEqual(fs.attribution({"agent_id": "agent_7"}), "agent_7")

    def test_no_record_is_unknown(self):
        self.assertEqual(fs.attribution(None), fs.UNKNOWN)

    def test_empty_record_is_unknown_not_blank(self):
        """An explicit marker, never a blank and never a guessed default."""
        self.assertEqual(fs.attribution({}), fs.UNKNOWN)


class SpawnCounts(unittest.TestCase):
    """Counts are derived at render time, over two buckets, from `spawned_at`."""

    def test_records_on_different_days_are_bucketed_apart(self):
        """UTC-only `spawned_at` gives the day boundary two defensible readings."""
        # 2026-09-21T22:30Z is still 2026-09-21 in UTC but already 2026-09-22 in CEST.
        records = {
            "a": {"spawned_at": "2026-09-21T22:30:00.000Z"},
            "b": {"spawned_at": "2026-09-21T10:00:00.000Z"},
        }
        now = datetime(2026, 9, 21, 23, 0, tzinfo=timezone.utc).timestamp()
        utc_n, local_n = fs.spawn_counts(records, now)
        self.assertEqual(utc_n, 2, "both records are 2026-09-21 in UTC")
        self.assertGreaterEqual(local_n, 1, "the 22:30Z record may fall on the next local day")

    def test_counts_are_stable_across_a_rerun(self):
        """No spawn between runs → both counts unchanged. The stability half."""
        records = {"a": {"spawned_at": "2026-09-21T10:00:00.000Z"}}
        now = datetime(2026, 9, 21, 12, 0, tzinfo=timezone.utc).timestamp()
        self.assertEqual(fs.spawn_counts(records, now), fs.spawn_counts(records, now))

    def test_one_more_spawn_raises_both_counts_by_exactly_one(self):
        """The delta half: a constant, or an invocation counter, fails this."""
        now = datetime(2026, 9, 21, 12, 0, tzinfo=timezone.utc).timestamp()
        before = {"a": {"spawned_at": "2026-09-21T10:00:00.000Z"}}
        after = dict(before)
        after["b"] = {"spawned_at": "2026-09-21T11:00:00.000Z"}
        u0, l0 = fs.spawn_counts(before, now)
        u1, l1 = fs.spawn_counts(after, now)
        self.assertEqual((u1 - u0, l1 - l0), (1, 1))

    def test_a_previous_day_is_not_counted(self):
        records = {"a": {"spawned_at": "2026-09-20T10:00:00.000Z"}}
        now = datetime(2026, 9, 21, 12, 0, tzinfo=timezone.utc).timestamp()
        self.assertEqual(fs.spawn_counts(records, now), (0, 0))

    def test_unparseable_spawned_at_is_skipped(self):
        records = {"a": {"spawned_at": None}, "b": {"spawned_at": "not-a-date"}}
        now = datetime(2026, 9, 21, 12, 0, tzinfo=timezone.utc).timestamp()
        self.assertEqual(fs.spawn_counts(records, now), (0, 0))


class Render(unittest.TestCase):
    """End to end: the row carries both new columns from the ledger."""

    def _render(self, ledger_dir_path):
        out = io.StringIO()
        with mock.patch.dict(os.environ, {"SUPERVISOR_LEDGER_DIR": ledger_dir_path}):
            with contextlib.redirect_stdout(out):
                fs.main()
        return out.getvalue()

    def test_columns_are_appended_after_the_existing_five(self):
        """Callers parse the first five columns; the new pair must not displace them."""
        with tempfile.TemporaryDirectory() as d:
            header = self._render(d).splitlines()[0]
        self.assertTrue(header.startswith("LAST-ACTIVE"))
        self.assertLess(header.index("SESSION"), header.index("SPAWN MODE"))
        self.assertLess(header.index("WORKING ON"), header.index("SPAWN MODE"))
        self.assertLess(header.index("SPAWN MODE"), header.index("ATTRIBUTION"))

    def test_both_counts_are_printed_and_labelled(self):
        with tempfile.TemporaryDirectory() as d:
            output = self._render(d)
        self.assertIn("spawned today (UTC): ", output)
        self.assertIn("spawned today (local): ", output)

    def test_ledger_known_session_renders_its_mode_and_label(self):
        """A row with a record shows what the ledger says, not `unknown`."""
        with tempfile.TemporaryDirectory() as d:
            write_record(d, SID, mode="headless", label="audit the parser")
            # The roster is built from transcripts; a session with no transcript
            # simply does not appear, so this asserts on the record plumbing.
            with mock.patch.dict(os.environ, {"SUPERVISOR_LEDGER_DIR": d}):
                ledger = fs.read_ledger()
        self.assertEqual(fs.spawn_mode(ledger[SID]), "headless")
        self.assertEqual(fs.attribution(ledger[SID]), "audit the parser")

    def test_a_session_with_no_record_renders_unknown_not_blank(self):
        """An explicit marker in BOTH new cells — never a blank, never a guess."""
        row = fs.render_row(12.0, "Personal", SID, "some task", False, None)
        self.assertRegex(row, r"unknown\s+unknown\s*$")
        self.assertNotIn("interactive", row)
        self.assertNotIn("headless", row)

    def test_a_ledger_known_row_renders_its_mode_and_attribution(self):
        row = fs.render_row(
            12.0, "Personal", SID, "some task", False,
            {"mode": "headless", "label": "audit the parser"},
        )
        self.assertRegex(row, r"headless\s+audit the parser\s*$")

    def test_the_original_five_columns_keep_their_positions(self):
        """`fleet-status` / `fleet-loop` parse SESSION and WORKING ON by name."""
        row = fs.render_row(12.0, "Personal", SID, "some task", True, None)
        self.assertLess(row.index(SID[:8]), row.index("some task"))
        self.assertLess(row.index("some task"), row.index("unknown"))
        self.assertIn("●", row)


class RetiredFlags(unittest.TestCase):
    """`--all`, `--minutes N` and `--vault NAME` stay accepted-and-ignored.

    ⚠️ A byte-identical whole-output comparison is the obvious probe and it does
    NOT work here: the roster is live, so ages advance and rows reorder between
    two runs seconds apart. The claim is about *scoping*, so it is tested as one
    — the session set, which is stable across a re-run, plus the structural fact
    that argv is never consulted.
    """

    def test_retired_flags_do_not_change_the_row_set(self):
        """The SESSION column is the scoping artifact; ages legitimately move."""
        def sessions(argv):
            out = io.StringIO()
            with mock.patch("sys.argv", argv), contextlib.redirect_stdout(out):
                fs.main()
            rows = out.getvalue().splitlines()[1:]
            return {ln.split()[3] for ln in rows if len(ln.split()) > 3 and ":" not in ln.split()[0]}

        plain = sessions(["fleet-sessions.py"])
        flagged = sessions(["fleet-sessions.py", "--all", "--minutes", "5", "--vault", "Personal"])
        self.assertEqual(plain, flagged)
        self.assertTrue(plain, "the roster must not be empty — an empty set compares equal vacuously")

    def test_argv_is_never_consulted(self):
        """No flag can narrow anything, because no flag is ever read."""
        source = Path(_SCRIPT).read_text(encoding="utf-8")
        self.assertNotIn("sys.argv", source)
        self.assertNotIn("argparse", source)

    def test_no_scope_reaches_the_roster(self):
        """The roster is always every project — no cwd/vault narrowing survived."""
        source = Path(_SCRIPT).read_text(encoding="utf-8")
        self.assertIn('PROJECTS.glob("*/*.jsonl")', source)


class CountsRestOnSpawnedAt(unittest.TestCase):
    """No count and no column rests on the never-closing `status` / `ended_at`."""

    def test_status_and_ended_at_never_reach_the_render_path(self):
        source = Path(_SCRIPT).read_text(encoding="utf-8")
        for field in ("status", "ended_at"):
            self.assertNotIn(f"rec.get(\"{field}\")", source)
            self.assertNotIn(f"record[\"{field}\"]", source)

    def test_counts_grep_to_spawned_at(self):
        source = Path(_SCRIPT).read_text(encoding="utf-8")
        count_body = source[source.index("def spawn_counts") : source.index("# An absent ledger record")]
        self.assertIn("spawned_at", count_body)
        self.assertNotIn("status", count_body)
        self.assertNotIn("ended_at", count_body)


if __name__ == "__main__":
    unittest.main()
