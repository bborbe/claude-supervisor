"""resume-check.py: the three readings it exists to tell apart.

The regression it pins: a resumed headless worker whose conversation never ran a turn is
indistinguishable from a productive one at the call -- `spawn_agent` answers `running` with a
wrapper id either way. Measured 2026-11-05 on agent_12 / agent_13 (`num_turns: 0`, empty
result) against agent_14, which worked. The transcripts of that batch are the fixture shape
below: a `<task-notification>` enqueue, then the caller's prompt, and no assistant record.
"""

import importlib.util
import json
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, "..", "resume-check.py")


def load_module():
    spec = importlib.util.spec_from_file_location("resume_check", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def write_transcript(root, session_id, records):
    project = os.path.join(root, "project")
    os.makedirs(project, exist_ok=True)
    path = os.path.join(project, f"{session_id}.jsonl")
    with open(path, "w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record) + "\n")
    return path


SYNTHETIC = {
    "type": "user",
    "timestamp": "2026-11-05T21:09:56.768Z",
    "message": {"role": "user", "content": "<task-notification>\n<task-id>x</task-id>\n</task-notification>"},
}
ZERO_RESULT = {
    "type": "result",
    "timestamp": "2026-11-05T21:09:57.000Z",
    "subtype": "success",
    "num_turns": 0,
    "result": "",
}
ASSISTANT = {
    "type": "assistant",
    "timestamp": "2026-11-05T21:11:00.000Z",
    "message": {"role": "assistant", "content": [{"type": "text", "text": "working"}]},
}
REAL_RESULT = {
    "type": "result",
    "timestamp": "2026-11-05T21:12:00.000Z",
    "subtype": "success",
    "num_turns": 4,
    "result": "did it",
}


class ResumeCheckTest(unittest.TestCase):
    def setUp(self):
        self.module = load_module()
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def test_synthetic_input_without_a_turn_is_zero_turn(self):
        write_transcript(self.root, "s1", [SYNTHETIC, ZERO_RESULT])
        result = self.module.measure("s1", None, self.root)
        self.assertEqual(result["verdict"], "zero-turn")
        self.assertEqual(result["turns"], 0)
        self.assertEqual(result["synthetic"], 2)

    def test_a_real_turn_is_productive(self):
        write_transcript(self.root, "s2", [SYNTHETIC, ASSISTANT, REAL_RESULT])
        result = self.module.measure("s2", None, self.root)
        self.assertEqual(result["verdict"], "productive")
        self.assertGreater(result["turns"], 0)

    def test_a_missing_transcript_is_no_evidence(self):
        result = self.module.measure("missing", None, self.root)
        self.assertEqual(result["verdict"], "no-evidence")

    def test_the_window_is_bounded_above_so_later_work_is_not_credited(self):
        # The incident shape: nothing at resume time, real work only much later.
        write_transcript(self.root, "s3", [SYNTHETIC, ZERO_RESULT, ASSISTANT, REAL_RESULT])
        since = self.module.parse_time("2026-11-05T21:09:00Z")
        until = self.module.parse_time("2026-11-05T21:10:00Z")
        bounded = self.module.measure("s3", since, self.root, until)
        self.assertEqual(bounded["verdict"], "zero-turn")
        unbounded = self.module.measure("s3", since, self.root)
        self.assertEqual(unbounded["verdict"], "productive")

    def test_fail_on_zero_turn_is_opt_in(self):
        write_transcript(self.root, "s4", [SYNTHETIC, ZERO_RESULT])
        base = ["--session", "s4", "--projects-root", self.root]
        self.assertEqual(self.module.main(base), 0)
        self.assertEqual(self.module.main(base + ["--fail-on-zero-turn"]), 1)


if __name__ == "__main__":
    unittest.main()
