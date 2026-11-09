#!/usr/bin/env python3
"""Tests for `write_answer_marker` in scripts/attention-answer.py.

The marker is the record that says WHO answered a worker's question. Four things are
load-bearing, and each is tested against its negative:

  * the hook's kind vocabulary — a kind the hook ignores is a write that can never be
    read, so it is refused here rather than discovered as a silent gap.
  * `ts` in SECONDS. The hook parses it with `float()` and compares against `time.time()`;
    milliseconds read as future-dated and are rejected, silently, on both sides.
  * the operator answer's empty session surviving as `""`. It is what separates "the
    operator answered, unnamed" from "nothing is known" — the distinction a later count
    depends on.
  * the producer id validated as a UUID before it reaches a filesystem path. It is external
    store data on an unauthenticated localhost endpoint, and an absolute value or a `..`
    segment would write outside the directory the hook reads.
"""

import importlib.util
import json
import os
import tempfile
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_SCRIPT = os.path.join(os.path.dirname(_HERE), "attention-answer.py")

_spec = importlib.util.spec_from_file_location("attention_answer_marker", _SCRIPT)
aa = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(aa)

SESSION = "11111111-1111-1111-1111-111111111111"


class WriteAnswerMarkerTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self._orig = aa.ATTENTION_STATE_DIR
        aa.ATTENTION_STATE_DIR = self.dir

    def tearDown(self):
        aa.ATTENTION_STATE_DIR = self._orig

    def _read(self, sid=SESSION):
        with open(os.path.join(self.dir, f"{sid}.answer.json"), encoding="utf-8") as f:
            return json.load(f)

    def test_writes_a_record_the_hook_can_read(self):
        self.assertTrue(aa.write_answer_marker(SESSION, "mgr", "manager"))
        rec = self._read()
        self.assertEqual(sorted(rec), ["answerer_kind", "answerer_session", "ts"])
        self.assertEqual(rec["answerer_session"], "mgr")
        self.assertEqual(rec["answerer_kind"], "manager")

    def test_refuses_a_kind_the_hook_would_ignore(self):
        for kind in ("robot", "", None):
            self.assertFalse(aa.write_answer_marker(SESSION, "x", kind))
        self.assertFalse(os.path.exists(os.path.join(self.dir, f"{SESSION}.answer.json")))

    def test_ts_is_epoch_seconds(self):
        aa.write_answer_marker(SESSION, "mgr", "manager")
        # A millisecond value is ~1000x larger. Assert the magnitude, because the hook's
        # failure mode (future-dated => ignored) is silent on both sides.
        self.assertLess(self._read()["ts"], 1e11)

    def test_operator_answer_keeps_an_empty_session(self):
        self.assertTrue(aa.write_answer_marker(SESSION, "", "operator"))
        rec = self._read()
        self.assertEqual(rec["answerer_session"], "")
        self.assertEqual(rec["answerer_kind"], "operator")

    def test_refuses_a_producer_id_that_is_not_a_uuid(self):
        # Path traversal and absolute values must never reach os.path.join.
        for bad in ("p1", "", "../../etc/passwd", "/tmp/evil", SESSION + "/x"):
            self.assertFalse(aa.write_answer_marker(bad, "mgr", "manager"))

    def test_never_raises_when_the_directory_cannot_be_created(self):
        blocker = os.path.join(self.dir, "blocked")
        with open(blocker, "w", encoding="utf-8") as f:
            f.write("not a directory")
        aa.ATTENTION_STATE_DIR = blocker
        self.assertFalse(aa.write_answer_marker(SESSION, "mgr", "manager"))


if __name__ == "__main__":
    unittest.main()
