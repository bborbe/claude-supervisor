#!/usr/bin/env python3
"""Tests for scripts/restart-precheck.py.

Three properties carry this script, and each is asserted here because each one was
either measured or broken during its first day:

  1. **A dirty worktree refuses only when the dirt is the target's to own.** The
     discriminator is *sharing*, not dirt: measured 2026-09-25, 19 of 25 live
     interactive sessions sat in a dirty worktree and all 17 Personal-vault sessions
     were dirty by the same three tracked files. A refusal on dirt alone would have
     blocked 76% of the fleet on somebody else's work, so the sole-occupant rule is
     pinned from both directions — solo+dirty refuses, shared+dirty does not.
  2. **Untracked paths never refuse.** Real worker trees always carry scratch; a probe
     that refused on them would refuse nearly every target.
  3. **An absent transcript is `undetermined`, not `mid-work`.** "The file is not
     there" must never be read as "the file says nothing" — the same
     blind-is-not-unreadable error the runbook records for the headless liveness probe.
     It also crashed the report (`int(inf)`) before it was fixed, so the CLI case is
     asserted rather than the helper alone.

Run: python3 -m unittest discover -s scripts/tests -v
"""

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_SCRIPTS = os.path.dirname(_HERE)
_spec = importlib.util.spec_from_file_location(
    "restart_precheck", os.path.join(_SCRIPTS, "restart-precheck.py")
)
rp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rp)

SCRIPT = os.path.join(_SCRIPTS, "restart-precheck.py")
SID = "aaaaaaaa-1111-4111-8111-111111111111"
OTHER = "bbbbbbbb-2222-4222-8222-222222222222"


def entry(sid, **over):
    """A registry record with the fields the script reads."""
    rec = {
        "sessionId": sid,
        "pid": 4242,
        "status": "idle",
        "kind": "interactive",
        "name": "fixture",
        "cwd": "/tmp",
    }
    rec.update(over)
    return rec


class FakeWhoNeedsMe:
    """Stands in for the imported sibling: only the four names `classify` uses."""

    LIVE_WINDOW = 300

    def __init__(self, age=1.0, closer=""):
        self._age = age
        self._closer = closer

    def session_transcript_age(self, sid):
        return self._age

    def closer_from_transcript(self, rec):
        return self._closer

    def is_parked_verb(self, detail):
        return (detail or "").startswith("later (on ")


class TestClassify(unittest.TestCase):
    def test_absent_transcript_is_undetermined(self):
        """`inf` age wins over any closer text — an absent file is not an empty one."""
        branch, closer = rp.classify(entry(SID), FakeWhoNeedsMe(age=float("inf"), closer="pick 1"))
        self.assertEqual(branch, "undetermined")
        self.assertEqual(closer, "(no transcript)")

    def test_no_closer_is_mid_work(self):
        branch, _ = rp.classify(entry(SID), FakeWhoNeedsMe(closer=""))
        self.assertEqual(branch, "mid-work")

    def test_nothing_closer_is_mid_work(self):
        branch, _ = rp.classify(entry(SID), FakeWhoNeedsMe(closer="nothing"))
        self.assertEqual(branch, "mid-work")

    def test_parked_closer_is_mid_work(self):
        """A `later (on …)` wait is not a gate — nothing is waiting on a human."""
        branch, _ = rp.classify(
            entry(SID), FakeWhoNeedsMe(closer="later (on 2026-09-25T14:13:38Z): approve: /x")
        )
        self.assertEqual(branch, "mid-work")

    def test_real_closer_is_gate_held(self):
        branch, closer = rp.classify(entry(SID), FakeWhoNeedsMe(closer="pick — 1. X · 2. Y"))
        self.assertEqual(branch, "gate-held")
        self.assertIn("pick", closer)


class TestOtherOccupants(unittest.TestCase):
    def test_excludes_self(self):
        entries = [("/a", entry(SID, cwd="/repo"))]
        self.assertEqual(rp.other_occupants(entries, "/repo", SID), 0)

    def test_counts_other_live_pid_in_same_cwd(self):
        entries = [("/a", entry(SID, cwd="/repo")), ("/b", entry(OTHER, cwd="/repo", pid=os.getpid()))]
        self.assertEqual(rp.other_occupants(entries, "/repo", SID), 1)

    def test_dead_pid_does_not_count(self):
        """A stale registry entry must not buy a refusal exemption."""
        entries = [
            ("/a", entry(SID, cwd="/repo")),
            ("/b", entry(OTHER, cwd="/repo", pid=2**22 + 12345)),
        ]
        self.assertEqual(rp.other_occupants(entries, "/repo", SID), 0)

    def test_different_cwd_does_not_count(self):
        entries = [
            ("/a", entry(SID, cwd="/repo")),
            ("/b", entry(OTHER, cwd="/elsewhere", pid=os.getpid())),
        ]
        self.assertEqual(rp.other_occupants(entries, "/repo", SID), 0)

    def test_no_cwd_is_zero(self):
        self.assertEqual(rp.other_occupants([("/a", entry(SID))], "", SID), 0)


class TestWorktreeProbe(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        subprocess.run(["git", "init", "-q", self.tmp], check=True)
        with open(os.path.join(self.tmp, "a.txt"), "w", encoding="utf-8") as f:
            f.write("one\n")
        subprocess.run(["git", "-C", self.tmp, "add", "a.txt"], check=True)
        subprocess.run(
            ["git", "-C", self.tmp, "-c", "user.email=t@t", "-c", "user.name=t",
             "commit", "-qm", "init"],
            check=True,
        )

    def _write_untracked(self):
        with open(os.path.join(self.tmp, "scratch.txt"), "w", encoding="utf-8") as f:
            f.write("scratch\n")

    def test_clean(self):
        verdict, tracked, untracked = rp.worktree_probe(self.tmp)
        self.assertEqual((verdict, tracked, untracked), ("clean", [], 0))

    def test_untracked_only_is_clean(self):
        """Untracked scratch must never refuse — real worker trees always carry some."""
        self._write_untracked()
        verdict, tracked, untracked = rp.worktree_probe(self.tmp)
        self.assertEqual(verdict, "clean")
        self.assertEqual(tracked, [])
        self.assertEqual(untracked, 1)

    def test_tracked_modification_is_dirty(self):
        with open(os.path.join(self.tmp, "a.txt"), "a", encoding="utf-8") as f:
            f.write("two\n")
        verdict, tracked, _ = rp.worktree_probe(self.tmp)
        self.assertEqual(verdict, "dirty")
        self.assertEqual(len(tracked), 1)

    def test_tracked_plus_untracked_is_dirty_and_counts_both(self):
        with open(os.path.join(self.tmp, "a.txt"), "a", encoding="utf-8") as f:
            f.write("two\n")
        self._write_untracked()
        verdict, tracked, untracked = rp.worktree_probe(self.tmp)
        self.assertEqual(verdict, "dirty")
        self.assertEqual((len(tracked), untracked), (1, 1))

    def test_not_a_repo_is_not_dirty(self):
        verdict, _, _ = rp.worktree_probe(tempfile.mkdtemp())
        self.assertEqual(verdict, "not-a-repo")

    def test_missing_cwd_is_unknown(self):
        self.assertEqual(rp.worktree_probe("")[0], "unknown")


class TestCli(unittest.TestCase):
    """The exit code and first-line token are the caller's contract."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.sessions = os.path.join(self.tmp, "sessions")
        self.repo = os.path.join(self.tmp, "repo")
        os.makedirs(self.sessions)
        os.makedirs(self.repo)
        subprocess.run(["git", "init", "-q", self.repo], check=True)
        with open(os.path.join(self.repo, "a.txt"), "w", encoding="utf-8") as f:
            f.write("one\n")
        subprocess.run(["git", "-C", self.repo, "add", "a.txt"], check=True)
        subprocess.run(
            ["git", "-C", self.repo, "-c", "user.email=t@t", "-c", "user.name=t",
             "commit", "-qm", "init"],
            check=True,
        )

    def _register(self, sid, **over):
        rec = entry(sid, cwd=self.repo, **over)
        with open(os.path.join(self.sessions, f"{rec['pid']}.json"), "w", encoding="utf-8") as f:
            json.dump(rec, f)

    def _run(self, sid):
        return subprocess.run(
            [sys.executable, SCRIPT, "--sessions-dir", self.sessions, sid],
            capture_output=True, text=True, timeout=60,
        )

    def _dirty(self):
        with open(os.path.join(self.repo, "a.txt"), "a", encoding="utf-8") as f:
            f.write("two\n")

    def test_unknown_session_id(self):
        done = self._run(SID)
        self.assertEqual(done.returncode, 1)
        self.assertEqual(done.stdout.splitlines()[0], "unknown-session-id")

    def test_solo_dirty_worktree_refuses(self):
        self._register(SID)
        self._dirty()
        done = self._run(SID)
        self.assertEqual(done.returncode, 1)
        self.assertEqual(done.stdout.splitlines()[0], "dirty-worktree")

    def test_shared_dirty_worktree_does_not_refuse(self):
        """The measurement this rule exists for: 17 vault sessions shared one dirty tree."""
        self._register(SID)
        self._register(OTHER, pid=os.getpid())
        self._dirty()
        done = self._run(SID)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertIn("dirt noted, not refused", done.stdout)

    def test_clean_solo_worktree_proceeds(self):
        self._register(SID)
        done = self._run(SID)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)

    def test_absent_transcript_reports_undetermined_without_crashing(self):
        """`int(inf)` raised OverflowError here before the absent case was rendered."""
        self._register(SID)
        done = self._run(SID)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertIn("transcript: absent", done.stdout)
        self.assertIn("classification: undetermined", done.stdout)

    def test_ambiguous_pid_refuses(self):
        self._register(SID)
        self._register(SID, pid=os.getpid())
        done = self._run(SID)
        self.assertEqual(done.returncode, 1)
        self.assertEqual(done.stdout.splitlines()[0], "ambiguous-pid")


if __name__ == "__main__":
    unittest.main()
