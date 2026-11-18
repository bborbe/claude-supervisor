"""round-context-delta.py: a round's context cost, measured from the transcript.

The script exists because "≤ 3 KB per round" was measured once by hand and never
reproduced. The load-bearing cases are the ones that would make a measurement
lie:

  - a **sidechain** record must not count toward the manager's round (its text
    belongs to a sub-agent's transcript, and its output reaches the manager as
    one hand-back message, which does count);
  - text **before the first marker** is setup, not a round;
  - a transcript with **no markers** is an error, never a zero;
  - a fire whose prompt does not name the round is not a round boundary.
"""
import importlib.util
import json
import pathlib
import subprocess
import sys
import tempfile
import unittest

SCRIPT = pathlib.Path(__file__).resolve().parent.parent / "round-context-delta.py"
spec = importlib.util.spec_from_file_location("round_context_delta", SCRIPT)
rcd = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rcd)


def fire(text="/supervisor:fleet-drive --interval 15m", ts="2026-11-17T10:00:00Z"):
    return {"type": "user", "scheduledFireId": "fire-" + ts, "timestamp": ts,
            "message": {"content": text}}


def assistant(text, sidechain=False):
    rec = {"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}}
    if sidechain:
        rec["isSidechain"] = True
    return rec


class TextBytesTest(unittest.TestCase):
    def test_string_content(self):
        self.assertEqual(rcd.text_bytes("abcd"), 4)

    def test_block_list_sums_text(self):
        blocks = [{"type": "text", "text": "ab"}, {"type": "text", "text": "cde"}]
        self.assertEqual(rcd.text_bytes(blocks), 5)

    def test_tool_input_is_counted_as_its_serialized_form(self):
        block = {"type": "tool_use", "input": {"path": "x"}}
        self.assertEqual(rcd.text_bytes(block), len(json.dumps({"path": "x"}).encode()))

    def test_none_and_numbers_are_zero(self):
        self.assertEqual(rcd.text_bytes(None), 0)
        self.assertEqual(rcd.text_bytes(7), 0)


class SegmentTest(unittest.TestCase):
    def test_segments_at_markers_and_sums_bytes(self):
        records = [fire(ts="t1"), assistant("aaaa"), assistant("bb"),
                   fire(ts="t2"), assistant("ccc")]
        rounds = rcd.segment(records, "/supervisor:fleet-drive")
        self.assertEqual(len(rounds), 2)
        # the marker's own prompt text counts toward its round
        self.assertEqual(rounds[0]["bytes"], len("/supervisor:fleet-drive --interval 15m") + 6)
        self.assertEqual(rounds[1]["bytes"], len("/supervisor:fleet-drive --interval 15m") + 3)

    def test_sidechain_records_are_excluded(self):
        records = [fire(), assistant("main"), assistant("side", sidechain=True)]
        rounds = rcd.segment(records, "/supervisor:fleet-drive")
        self.assertEqual(rounds[0]["bytes"], len("/supervisor:fleet-drive --interval 15m") + 4)

    def test_text_before_the_first_marker_is_not_a_round(self):
        records = [assistant("setup text"), fire(), assistant("work")]
        rounds = rcd.segment(records, "/supervisor:fleet-drive")
        self.assertEqual(len(rounds), 1)
        self.assertNotIn(len("setup text"), [r["bytes"] for r in rounds])

    def test_a_fire_naming_something_else_is_not_a_round(self):
        records = [fire(text="/supervisor:goal-drive --interval 15m"), assistant("x")]
        self.assertEqual(rcd.segment(records, "/supervisor:fleet-drive"), [])


class CliTest(unittest.TestCase):
    def run_cli(self, transcript_text, *args):
        with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False) as handle:
            handle.write(transcript_text)
            path = handle.name
        self.addCleanup(pathlib.Path(path).unlink)
        return subprocess.run(
            [sys.executable, str(SCRIPT), "--transcript", path, *args],
            capture_output=True, text=True,
        )

    def test_reports_each_closed_round_and_a_median(self):
        # Three fires = two CLOSED rounds; the trailing segment is dropped.
        lines = [json.dumps(fire(ts="t1")), json.dumps(assistant("a" * 2048)),
                 json.dumps(fire(ts="t2")), json.dumps(assistant("b" * 1024)),
                 json.dumps(fire(ts="t3")), json.dumps(assistant("c" * 512))]
        result = self.run_cli("\n".join(lines))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("round  1", result.stdout)
        self.assertIn("2 round(s)", result.stdout)
        self.assertIn("median", result.stdout)

    def test_the_trailing_open_round_is_dropped(self):
        """Nothing closed it, so its bytes include everything after its marker."""
        lines = [json.dumps(fire(ts="t1")), json.dumps(assistant("a" * 100)),
                 json.dumps(fire(ts="t2")), json.dumps(assistant("b" * 100))]
        result = self.run_cli("\n".join(lines))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("1 round(s)", result.stdout)

    def test_include_open_reports_it(self):
        lines = [json.dumps(fire(ts="t1")), json.dumps(assistant("a" * 100)),
                 json.dumps(fire(ts="t2")), json.dumps(assistant("b" * 100))]
        result = self.run_cli("\n".join(lines), "--include-open")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("2 round(s)", result.stdout)

    def test_a_single_marker_is_an_error_not_an_8mb_round(self):
        # Measured 2026-11-18 on cfaf1455: one marker produced an 8508 KB "round".
        lines = [json.dumps(fire(ts="t1")), json.dumps(assistant("a" * 100))]
        result = self.run_cli("\n".join(lines))
        self.assertEqual(result.returncode, 2)
        self.assertIn("no CLOSED round", result.stderr)

    def test_json_shape(self):
        lines = [json.dumps(fire(ts="t1")), json.dumps(assistant("a" * 100)),
                 json.dumps(fire(ts="t2")), json.dumps(assistant("b" * 50))]
        result = self.run_cli("\n".join(lines), "--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["count"], 1)
        self.assertIn("median_kb", payload)

    def test_no_markers_is_an_error_never_a_zero(self):
        result = self.run_cli(json.dumps(assistant("hello")))
        self.assertEqual(result.returncode, 2)
        self.assertIn("no round markers", result.stderr)

    def test_missing_transcript_is_an_error(self):
        result = subprocess.run(
            [sys.executable, str(SCRIPT), "--transcript", "/nonexistent/x.jsonl"],
            capture_output=True, text=True)
        self.assertEqual(result.returncode, 2)
        self.assertIn("transcript not found", result.stderr)


if __name__ == "__main__":
    unittest.main()
