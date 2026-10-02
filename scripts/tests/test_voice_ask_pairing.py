#!/usr/bin/env python3
"""Tests for scripts/voice-ask-pairing.py.

Fixtures are synthetic on purpose: the repo is public, so no transcript text is
committed. They mirror the real sample's shape — the question sits mid-text and
the say keeps talking after it.

Run: python3 -m unittest discover -s scripts/tests -v
"""

import importlib.util
import json
import os
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location(
    "voice_ask_pairing", os.path.join(HERE, "..", "voice-ask-pairing.py")
)
vap = importlib.util.module_from_spec(spec)
spec.loader.exec_module(vap)

QUESTIONS = [
    "The worker finished its audit. Want me to open the PR? It is ready on the branch.",
    "Two tasks share one session id. Should I split them, or leave both on it?",
    "The deploy is staged on dev. Can I promote it to prod now?",
]
STATEMENTS = [
    "The worker finished its audit and opened the PR.",
    "Recommended: approve the first option, the branch is green.",
    "Pick one when you are back; nothing is blocked meanwhile.",
]


def _say(ts, text):
    return {"timestamp": ts, "message": {"content": [
        {"type": "tool_use", "name": "mcp__tts__say", "input": {"text": text}}]}}


def _post(ts):
    return {"timestamp": ts, "message": {"content": [
        {"type": "tool_use", "name": "Bash",
         "input": {"command": "python3 scripts/attention-ask.py post --dedup-key k --payload p"}}]}}


class QuestionRule(unittest.TestCase):
    def test_flags_every_question(self):
        self.assertEqual([vap.is_question(t) for t in QUESTIONS], [True] * 3)

    def test_flags_no_statement(self):
        self.assertEqual([vap.is_question(t) for t in STATEMENTS], [False] * 3)


class Pairing(unittest.TestCase):
    def _transcript(self, records):
        fd, path = tempfile.mkstemp(suffix=".jsonl")
        with os.fdopen(fd, "w") as fh:
            for r in records:
                fh.write(json.dumps(r) + "\n")
        self.addCleanup(os.remove, path)
        return path

    def test_voice_only_ask_is_flagged(self):
        path = self._transcript([_say("2026-10-02T10:00:00Z", QUESTIONS[0])])
        questions, unpaired = vap.scan(path)
        self.assertEqual((len(questions), len(unpaired)), (1, 1))

    def test_post_within_window_pairs(self):
        path = self._transcript([_say("2026-10-02T10:00:00Z", QUESTIONS[0]),
                                 _post("2026-10-02T10:00:40Z")])
        self.assertEqual(len(vap.scan(path)[1]), 0)

    def test_post_before_say_pairs(self):
        path = self._transcript([_post("2026-10-02T09:59:30Z"),
                                 _say("2026-10-02T10:00:00Z", QUESTIONS[1])])
        self.assertEqual(len(vap.scan(path)[1]), 0)

    def test_post_outside_window_does_not_pair(self):
        path = self._transcript([_say("2026-10-02T10:00:00Z", QUESTIONS[2]),
                                 _post("2026-10-02T10:01:01Z")])
        self.assertEqual(len(vap.scan(path)[1]), 1)

    def test_statements_never_flag(self):
        path = self._transcript([_say("2026-10-02T10:00:00Z", t) for t in STATEMENTS])
        self.assertEqual(vap.scan(path), ([], []))


if __name__ == "__main__":
    unittest.main()
