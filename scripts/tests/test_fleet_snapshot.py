#!/usr/bin/env python3
"""Tests for scripts/fleet-snapshot.py.

Covers the way an empty session list destroyed snapshot continuity:

  * an empty payload was written as a valid snapshot and exited 0. The script
    had no empty-guard, so `fleet-snapshot.py` overwrote a real snapshot with
    `{"sessions": {}}` and printed `snapshot written: … - 0 sessions` — success,
    by its own report. Measured 2026-09-23 (v0.39.1): a sweep whose roster read
    came back empty clobbered the snapshot, so the next round diffed against
    nothing, every session read first-seen, and `stall_count` reset.

The defect is the same shape as the ledger misread this release also fixes: a
failed read and a true absence producing identical output. The refusal is only
half the fix — the negative-control cases below (a non-empty payload still
writes; a bare dict and the `{"sessions": …}` wrapper both count as non-empty)
exist because a script that refuses everything satisfies the refusal case while
breaking the sweep entirely.

The probe is deliberately two-sided: the empty payload must exit non-zero AND
leave the existing snapshot byte-identical, and a non-empty payload must still
exit 0. A guard that only checked the exit code would pass on a script that
crashed before writing anything.

Run: python3 -m unittest discover -s scripts/tests -v
"""

import contextlib
import hashlib
import importlib.util
import io
import json
import os
import tempfile
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_SCRIPT = os.path.join(os.path.dirname(_HERE), "fleet-snapshot.py")

_spec = importlib.util.spec_from_file_location("fleet_snapshot", _SCRIPT)
fs = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fs)


def _digest(path):
    with open(path, "rb") as handle:
        return hashlib.sha256(handle.read()).hexdigest()


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.path = os.path.join(self.tmp, "state", "fleet-snapshot.json")

    def run_main(self, payload):
        """Invoke main() with a JSON payload on stdin; return (rc, stdout, stderr)."""
        stdin = io.StringIO(json.dumps(payload))
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = fs.main(stdin=stdin, path=self.path)
        return rc, out.getvalue(), err.getvalue()

    def write_existing(self, sessions=None):
        """Write a snapshot that must survive an empty-payload run."""
        sessions = sessions if sessions is not None else {"aaaa1111": {"name": "x"}}
        fs.write_snapshot(sessions, path=self.path, swept_at="2026-09-23T17:12:00Z")
        return _digest(self.path)


class Refusal(Base):
    def test_empty_sessions_wrapper_is_refused(self):
        before = self.write_existing()
        rc, out, err = self.run_main({"sessions": {}})
        self.assertEqual(rc, 1)
        self.assertEqual(_digest(self.path), before, "snapshot must be untouched")
        self.assertEqual(out, "", "nothing may be reported as written")
        self.assertIn("empty", err)

    def test_empty_bare_dict_is_refused(self):
        before = self.write_existing()
        rc, _, err = self.run_main({})
        self.assertEqual(rc, 1)
        self.assertEqual(_digest(self.path), before)
        self.assertIn("empty", err)

    def test_empty_list_is_refused(self):
        before = self.write_existing()
        rc, _, err = self.run_main([])
        self.assertEqual(rc, 1)
        self.assertEqual(_digest(self.path), before)
        self.assertIn("empty", err)

    def test_refusal_creates_no_file_when_none_exists(self):
        rc, _, _ = self.run_main({"sessions": {}})
        self.assertEqual(rc, 1)
        self.assertFalse(
            os.path.exists(self.path), "a refused run must not create a snapshot"
        )

    def test_refusal_is_the_scripts_own_not_a_crash(self):
        """A path error would also exit non-zero and leave the file alone."""
        before = self.write_existing()
        rc, _, err = self.run_main({"sessions": {}})
        self.assertEqual(rc, 1)
        self.assertEqual(_digest(self.path), before)
        self.assertNotIn("Traceback", err, "refusal must be deliberate, not a crash")
        self.assertIn("refusing to write an empty snapshot", err)


class PositiveControl(Base):
    """A guard that refuses everything satisfies Refusal while breaking the sweep."""

    def test_non_empty_wrapper_is_written(self):
        rc, out, err = self.run_main({"sessions": {"aaaa1111": {"name": "x"}}})
        self.assertEqual(rc, 0, err)
        self.assertTrue(os.path.exists(self.path))
        self.assertIn("1 sessions", out)

    def test_non_empty_bare_dict_is_written(self):
        rc, out, _ = self.run_main({"aaaa1111": {"name": "x"}, "bbbb2222": {"name": "y"}})
        self.assertEqual(rc, 0)
        self.assertIn("2 sessions", out)

    def test_written_snapshot_round_trips(self):
        self.run_main({"sessions": {"aaaa1111": {"name": "x", "status": "busy"}}})
        with open(self.path) as handle:
            data = json.load(handle)
        self.assertEqual(list(data["sessions"]), ["aaaa1111"])
        self.assertEqual(data["sessions"]["aaaa1111"]["status"], "busy")
        self.assertTrue(data["swept_at"].endswith("Z"))

    def test_non_empty_run_advances_an_existing_snapshot(self):
        before = self.write_existing({"aaaa1111": {"name": "x"}})
        rc, _, _ = self.run_main({"sessions": {"aaaa1111": {"name": "x"}, "cccc3333": {}}})
        self.assertEqual(rc, 0)
        self.assertNotEqual(_digest(self.path), before, "a real write must land")


class AtomicWrite(Base):
    """A half-written snapshot is the same loss as an empty one, by another route.

    The write was a plain `open(path, "w")`, which truncates first — so a crash
    or a serialisation error part-way through left a truncated or zero-byte
    snapshot where a valid one stood, and the next round diffed against nothing
    exactly as it would after an empty clobber. `open-items.py` already
    documented this script as using tmp+`os.replace`; it did not.
    """

    def test_failed_serialisation_leaves_the_snapshot_intact(self):
        before = self.write_existing()
        with self.assertRaises(TypeError):
            fs.write_snapshot({"aaaa1111": object()}, path=self.path)
        self.assertEqual(
            _digest(self.path), before, "a failed write must not truncate the target"
        )
        self.assertFalse(
            os.path.exists(self.path + ".tmp"), "no tmp file may survive a failure"
        )

    def test_failed_serialisation_creates_no_snapshot_when_none_exists(self):
        with self.assertRaises(TypeError):
            fs.write_snapshot({"aaaa1111": object()}, path=self.path)
        self.assertFalse(os.path.exists(self.path))
        self.assertFalse(os.path.exists(self.path + ".tmp"))

    def test_successful_write_leaves_no_tmp_file(self):
        fs.write_snapshot({"aaaa1111": {}}, path=self.path)
        self.assertTrue(os.path.exists(self.path))
        self.assertFalse(os.path.exists(self.path + ".tmp"))


class MalformedPayload(Base):
    """Non-empty is not the same as *came from a roster read*.

    A failed read that returns an error envelope rather than nothing is
    non-empty, so it passed the empty guard, was written as a one-session
    snapshot and exited 0 — the same collapse the guard exists to prevent,
    reached with a payload that merely looks like data.
    """

    def test_error_envelope_is_refused(self):
        before = self.write_existing()
        rc, out, err = self.run_main({"error": "roster read failed"})
        self.assertEqual(rc, 1)
        self.assertEqual(_digest(self.path), before, "snapshot must be untouched")
        self.assertEqual(out, "", "nothing may be reported as written")
        self.assertIn("malformed", err)

    def test_scalar_entry_is_refused(self):
        before = self.write_existing()
        rc, _, err = self.run_main({"foo": 1})
        self.assertEqual(rc, 1)
        self.assertEqual(_digest(self.path), before)
        self.assertIn("malformed", err)

    def test_string_entry_inside_the_wrapper_is_refused(self):
        rc, _, _ = self.run_main({"sessions": {"aaaa1111": "oops"}})
        self.assertEqual(rc, 1)
        self.assertFalse(os.path.exists(self.path))

    def test_list_with_a_non_object_entry_is_refused(self):
        rc, _, _ = self.run_main([{"name": "x"}, "oops"])
        self.assertEqual(rc, 1)

    def test_a_dup_suffixed_key_still_writes(self):
        """Key-shape validation would reject live data — 1 of 45 keys is `…dup`."""
        rc, out, err = self.run_main(
            {"3d3b7835-8c20-46e7-8ec5-0c0782d238d7dup": {"name": "x"}}
        )
        self.assertEqual(rc, 0, err)
        self.assertIn("1 sessions", out)


if __name__ == "__main__":
    unittest.main()
