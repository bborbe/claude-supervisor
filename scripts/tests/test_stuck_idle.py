"""Tests for scripts/stuck-idle.py — the fleet round's idle-with-no-wait probe.

The probe is three conjuncts read from disk, so it is tested as a table plus
fixture files: a transcript with and without a pending tool call, a task whose
tail does and does not declare a wait, and the 2-round count carried in from the
drive ledger.
"""
import importlib.util
import json
import os
import tempfile
import unittest

SCRIPTS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SID = "e4fd1919-b082-4bb4-aebb-d7185cd61502"


def load(name, filename):
    path = os.path.join(SCRIPTS, filename)
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


probe = load("stuck_idle", "stuck-idle.py")


class DecideTest(unittest.TestCase):
    """The verdict table. Every conjunct is required, and unreadable is not clean."""

    def test_one_unchanged_round_is_not_stuck(self):
        self.assertEqual(probe.decide(100, 99, None, False), "not-stuck")

    def test_no_previous_reading_is_not_stuck(self):
        # A first observation can never satisfy a 2-round count.
        self.assertEqual(probe.decide(100, None, None, False), "not-stuck")

    def test_two_unchanged_rounds_idle_and_no_wait_is_stuck(self):
        self.assertEqual(probe.decide(100, 100, None, False), "stuck")

    def test_in_flight_tool_is_never_stuck(self):
        self.assertEqual(probe.decide(100, 100, "Bash", False), "not-stuck")

    def test_declared_wait_is_never_stuck(self):
        self.assertEqual(probe.decide(100, 100, None, True), "not-stuck")

    def test_unreadable_transcript_is_unverifiable_not_not_stuck(self):
        self.assertEqual(probe.decide(None, None, None, False), "unverifiable")


class InFlightTest(unittest.TestCase):
    """`in flight` is the LAST call, never any unmatched one."""

    def _write(self, records):
        handle = tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False)
        for rec in records:
            handle.write(json.dumps(rec) + "\n")
        handle.close()
        self.addCleanup(os.unlink, handle.name)
        return handle.name

    def test_trailing_tool_use_without_result_is_in_flight(self):
        path = self._write([
            {"message": {"content": [{"type": "tool_use", "name": "Bash"}]}},
        ])
        self.assertEqual(probe.in_flight_tool(path), "Bash")

    def test_matched_tool_use_is_not_in_flight(self):
        path = self._write([
            {"message": {"content": [{"type": "tool_use", "name": "Bash"}]}},
            {"message": {"content": [{"type": "tool_result"}]}},
        ])
        self.assertIsNone(probe.in_flight_tool(path))

    def test_abandoned_earlier_call_does_not_count(self):
        # An interrupted call leaves an unmatched tool_use behind; the conversation
        # carried on, so the last call is what "in flight" means.
        path = self._write([
            {"message": {"content": [{"type": "tool_use", "name": "Bash"}]}},
            {"message": {"content": [{"type": "text", "text": "carried on"}]}},
        ])
        self.assertEqual(probe.in_flight_tool(path), "Bash")
        # ... and a later completed call clears it.
        path = self._write([
            {"message": {"content": [{"type": "tool_use", "name": "Bash"}]}},
            {"message": {"content": [{"type": "tool_use", "name": "Read"}]}},
            {"message": {"content": [{"type": "tool_result"}]}},
        ])
        self.assertIsNone(probe.in_flight_tool(path))


class DeclaredWaitTest(unittest.TestCase):

    def _write(self, text):
        handle = tempfile.NamedTemporaryFile("w", suffix=".md", delete=False, encoding="utf-8")
        handle.write(text)
        handle.close()
        self.addCleanup(os.unlink, handle.name)
        return handle.name

    def test_waiting_panel_declares_a_wait(self):
        path = self._write("# Progress\n\n- today: working\n\n\U0001f7e1 WAITING\n")
        self.assertTrue(probe.declared_wait(path))

    def test_ends_line_declares_a_wait(self):
        path = self._write("# Progress\n\n- today: ⏰ Ends: PR merged\n")
        self.assertTrue(probe.declared_wait(path))

    def test_plain_progress_declares_nothing(self):
        path = self._write("# Progress\n\n- today: implemented the probe\n")
        self.assertFalse(probe.declared_wait(path))

    def test_a_wait_outside_the_tail_is_not_read(self):
        # A park the session already left must not keep reading as live.
        body = "# Progress\n\n\U0001f7e1 WAITING\n" + ("- filler\n" * 60)
        self.assertFalse(probe.declared_wait(self._write(body)))


class TranscriptResolutionTest(unittest.TestCase):
    """The id join is exact — a prefix would pick a sibling."""

    def test_exact_basename_resolves(self):
        with tempfile.TemporaryDirectory() as root:
            os.makedirs(os.path.join(root, "project-a"))
            wanted = os.path.join(root, "project-a", f"{SID}.jsonl")
            open(wanted, "w").close()
            open(os.path.join(root, "project-a", f"{SID}-other.jsonl"), "w").close()
            self.assertEqual(probe.resolve_transcript(SID, root), wanted)

    def test_missing_transcript_resolves_to_none(self):
        with tempfile.TemporaryDirectory() as root:
            self.assertIsNone(probe.resolve_transcript(SID, root))


if __name__ == "__main__":
    unittest.main()
