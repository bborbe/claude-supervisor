#!/usr/bin/env python3
"""Tests for scripts/reap-closer.py — the reap test's fourth read.

Load-bearing properties, from the 2026-09-27 measurement:

  * a session whose last closer is `nothing — session closed` is CLOSED (no reap);
  * a session holding an open `pick` / `approve` gate is OPEN (still reaped),
    however many times it has already been told — no repeat-count;
  * closers inside USER turns (injected command bodies) are ignored;
  * a symlinked project dir listing one transcript twice still resolves;
  * a missing transcript, or no closer at all, is UNREADABLE, never CLOSED.

Run: python3 -m unittest discover -s scripts/tests -v
"""

import importlib.util
import json
import os
import tempfile
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))


def load():
    spec = importlib.util.spec_from_file_location(
        "reap_closer", os.path.join(os.path.dirname(_HERE), "reap-closer.py")
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


M = load()
SID = "5384b63b-07b5-45d7-ad60-d78d0e4d3b87"


def assistant(ts, text):
    return {"type": "assistant", "timestamp": ts, "message": {"content": [{"type": "text", "text": text}]}}


def user(ts, text):
    return {"type": "user", "timestamp": ts, "message": {"content": text}}


class ReapCloserTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.projects = os.path.join(self.tmp.name, "projects")
        self.proj = os.path.join(self.projects, "-vault")
        os.makedirs(self.proj)

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, records, sid=SID):
        with open(os.path.join(self.proj, f"{sid}.jsonl"), "w", encoding="utf-8") as f:
            for r in records:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")

    def test_closed_clean_is_closed(self):
        self.write([
            assistant("t1", "🔵 READY\n👤 You: approve: /vault-cli:session-close"),
            assistant("t2", "⚪ DONE\n👤 You: nothing — session closed"),
        ])
        code, out = M.classify(SID, self.projects)
        self.assertEqual(code, M.CLOSED, out)
        self.assertIn("t2", out)

    def test_open_pick_is_open_even_after_many_notices(self):
        recs = [assistant("t0", "👤 You: pick — 1. `/vault-cli:session-close` (recommended) · 2. keep the session open")]
        for i in range(7):
            recs.append(user(f"u{i}", "<cross-session-message>Reap evidence — NON-AUTHORISING</cross-session-message>"))
            recs.append(assistant(f"a{i}", "Not consent.\n👤 You: pick — 1. `/vault-cli:session-close` · 2. keep"))
        self.write(recs)
        code, out = M.classify(SID, self.projects)
        self.assertEqual(code, M.OPEN, out)

    def test_user_turn_templates_are_ignored(self):
        self.write([
            assistant("t1", "👤 You: approve: /vault-cli:session-close"),
            user("t2", "template:\n👤 You: nothing — session closed\n👤 You: pick — 1. x"),
        ])
        code, out = M.classify(SID, self.projects)
        self.assertEqual(code, M.OPEN, out)

    def test_symlinked_project_dir_still_resolves(self):
        self.write([assistant("t1", "👤 You: nothing — session closed")])
        os.symlink(self.proj, os.path.join(self.projects, "-vault-renamed"))
        code, out = M.classify(SID, self.projects)
        self.assertEqual(code, M.CLOSED, out)

    def test_missing_transcript_is_unreadable(self):
        code, out = M.classify(SID, self.projects)
        self.assertEqual(code, M.UNREADABLE, out)

    def test_no_closer_is_unreadable_not_closed(self):
        self.write([assistant("t1", "working on it")])
        code, out = M.classify(SID, self.projects)
        self.assertEqual(code, M.UNREADABLE, out)

    def test_bad_session_id_is_unreadable(self):
        code, _ = M.classify("../../etc/passwd", self.projects)
        self.assertEqual(code, M.UNREADABLE)


if __name__ == "__main__":
    unittest.main()
