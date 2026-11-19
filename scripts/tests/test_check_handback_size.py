"""check-handback-size.py: the ≤ 1 KB cap is asserted, not just described.

`docs/handback-contract.md` § The shape says a leg returns ≤ 1 KB. Until 2026-11-17 nothing
measured it, so every leg could exceed the cap on every round and no check failed — the same
"a rule that lives only in prose" defect `test_check_handback_keys` covers one layer down for
keys.

The load-bearing cases:
  - a fence over the cap fails, naming the agent file;
  - the contract's declared **variable** lines (the truncation exception) are excluded, so a
    keepalive hand-back carrying many `crash-loop:` rows still passes;
  - a leg with no `**Hand-back**` fence is a failure, never a silent pass;
  - `supervisor:fleet-round` is registered — the round delegate's hand-back is the only
    channel for the counts the manager renders, so it must be measured like every other leg.

Fixtures are built from `check-handback-keys.LEGS` rather than hand-listed, so the two
checkers cannot drift apart.
"""
import importlib.util
import json
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest

SCRIPTS = pathlib.Path(__file__).resolve().parent.parent
KEYS_SCRIPT = SCRIPTS / "check-handback-keys.py"
SIZE_SCRIPT = SCRIPTS / "check-handback-size.py"
MAX = 1024


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def fence(keys):
    """A minimal `**Hand-back**` fence emitting every key — comfortably under the cap."""
    return (
        "**Hand-back**\n\n```text\n"
        + " · ".join(f"{k}=<n>" for k in keys)
        + "\n```\n</output_format>\n"
    )


class CheckHandbackSizeTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        (pathlib.Path(self.dir) / "scripts").mkdir()
        shutil.copy(KEYS_SCRIPT, pathlib.Path(self.dir) / "scripts" / "check-handback-keys.py")
        shutil.copy(SIZE_SCRIPT, pathlib.Path(self.dir) / "scripts" / "check-handback-size.py")
        self.legs = load(KEYS_SCRIPT, "check_handback_keys").LEGS
        for leg in self.legs.values():
            self.write(leg["agent"], fence(leg["required"]))

    def write(self, relpath, text):
        path = pathlib.Path(self.dir) / relpath
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)

    def run_check(self):
        return subprocess.run([sys.executable, "scripts/check-handback-size.py"], cwd=self.dir,
                              capture_output=True, text=True)

    def test_wired_tree_passes(self):
        result = self.run_check()
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_fleet_round_is_registered(self):
        """The round delegate must be measured — an unregistered leg is an unmeasured one."""
        self.assertIn("supervisor:fleet-round", self.legs)
        self.assertIn("agents/fleet-round.md",
                      [leg["agent"] for leg in self.legs.values()])

    def test_oversize_fence_fails(self):
        keys = self.legs["supervisor:fleet-round"]["required"]
        fat = " · ".join(f"{k}=" + "x" * 60 for k in keys)
        self.write("agents/fleet-round.md",
                   f"**Hand-back**\n\n```text\n{fat}\n```\n</output_format>\n")
        result = self.run_check()
        self.assertEqual(result.returncode, 1)
        self.assertIn("agents/fleet-round.md", result.stderr)
        self.assertIn("over its", result.stderr)

    def test_variable_lines_are_excluded(self):
        """A keepalive fence whose row lists run long still passes: the contract truncates them."""
        keys = self.legs["supervisor:fleet-keepalive-actor"]["required"]
        rows = "\n".join(f"crash-loop: subject-{i} · WORKER" for i in range(40))
        self.write(
            "agents/fleet-keepalive-actor.md",
            "**Hand-back**\n\n```text\n"
            + " · ".join(f"{k}=<n>" for k in keys)
            + "\n" + rows + "\n```\n</output_format>\n",
        )
        result = self.run_check()
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_missing_fence_fails(self):
        self.write("agents/fleet-round.md", "# no hand-back here\n")
        result = self.run_check()
        self.assertEqual(result.returncode, 1)
        self.assertIn("agents/fleet-round.md", result.stderr)
        self.assertIn("no `**Hand-back**`", result.stderr)

    def test_missing_agent_fails(self):
        pathlib.Path(self.dir, "agents/fleet-round.md").unlink()
        result = self.run_check()
        self.assertEqual(result.returncode, 1)
        self.assertIn("agents/fleet-round.md", result.stderr)
        self.assertIn("missing", result.stderr)


def transcript_line(text):
    return json.dumps({"type": "assistant",
                       "message": {"role": "assistant",
                                   "content": [{"type": "text", "text": text}]}})


class TranscriptModeTest(unittest.TestCase):
    """`--transcript`: the EMITTED hand-back, where absent is a failure and not 0 B.

    Measured 2026-11-18 on eight live `supervisor:fleet-round` delegates: seven emitted a
    54–111 B closing remark and no verdict at all, and the eighth emitted prose at 1 510 B.
    A literal "≤ 1 KB" test passes all seven, which is why absence must fail.
    """

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)

    def write(self, name, *texts):
        path = pathlib.Path(self.dir) / name
        path.write_text("\n".join(transcript_line(t) for t in texts), encoding="utf-8")
        return path

    def run_transcript(self, path):
        return subprocess.run([sys.executable, str(SIZE_SCRIPT), "--transcript", str(path)],
                              capture_output=True, text=True)

    def test_a_round_id_block_under_the_cap_passes(self):
        path = self.write("a.jsonl", "**Round 1795010883.8338208 — final verdict**\n\n- digest: x")
        result = self.run_transcript(path)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("(emitted)", result.stdout)

    def test_an_oversize_round_id_block_fails(self):
        path = self.write("b.jsonl", "**Round 1795010883 — final verdict**\n\n" + "- x\n" * 400)
        result = self.run_transcript(path)
        self.assertEqual(result.returncode, 1)
        self.assertIn("over its", result.stderr)

    def test_an_absent_hand_back_fails_rather_than_reading_zero(self):
        path = self.write("c.jsonl", "I'll start by reading the router and the step files.")
        result = self.run_transcript(path)
        self.assertEqual(result.returncode, 1)
        self.assertIn("no `**Hand-back**` fence and no round-id block", result.stderr)

    def test_a_fence_wrapped_in_prose_is_measured_as_the_whole_block(self):
        """The fence branch must not measure only the fenced body.

        A delegate using the authored shape still wraps it in a heading, a table and a
        trailing note; measuring the fence alone under-reported a 1 098 B message as 40 B
        and passed it 74 B over the cap.
        """
        keys = load(KEYS_SCRIPT, "check_handback_keys").LEGS["supervisor:fleet-round"]["required"]
        wrapped = (
            "The round is complete. Hand-back delivered.\n\n"
            "**Round 1795010883 — final verdict**\n\n"
            "**Hand-back**\n\n```text\n"
            + " · ".join(f"{k}=<n>" for k in keys)
            + "\n```\n</output_format>\n\n"
            + "- a payload bullet that the contract says belongs elsewhere\n" * 20
        )
        path = self.write("e.jsonl", wrapped)
        result = self.run_transcript(path)
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("over its", result.stderr)

    def test_a_missing_transcript_dir_fails(self):
        result = subprocess.run(
            [sys.executable, str(SIZE_SCRIPT), "--transcript-dir", str(pathlib.Path(self.dir) / "nope")],
            capture_output=True, text=True)
        self.assertEqual(result.returncode, 1)
        self.assertIn("not a directory", result.stderr)

    def test_a_dir_matching_no_agent_type_fails(self):
        """A typo'd agent type must not print `ok: 0` — an empty set is a failure."""
        self.write("f.jsonl", "**Round 1795010883 — final verdict**\n")
        (pathlib.Path(self.dir) / "f.meta.json").write_text(
            json.dumps({"agentType": "supervisor:other-leg"}), encoding="utf-8")
        result = subprocess.run(
            [sys.executable, str(SIZE_SCRIPT), "--transcript-dir", self.dir],
            capture_output=True, text=True)
        self.assertEqual(result.returncode, 1)
        self.assertIn("no transcript matched", result.stderr)

    def test_dir_mode_measures_matching_transcripts(self):
        self.write("g.jsonl", "**Round 1795010883 — final verdict**\n")
        self.write("h.jsonl", "I'll start by reading the router.")
        result = subprocess.run(
            [sys.executable, str(SIZE_SCRIPT), "--transcript-dir", self.dir],
            capture_output=True, text=True)
        self.assertEqual(result.returncode, 1)
        self.assertIn("h.jsonl", result.stderr)   # the absent one is named
        self.assertNotIn("g.jsonl", result.stderr)  # the present one is not

    def test_the_authored_fence_shape_also_counts(self):
        keys = load(KEYS_SCRIPT, "check_handback_keys").LEGS["supervisor:fleet-round"]["required"]
        path = self.write("d.jsonl",
                          "**Hand-back**\n\n```text\n"
                          + " · ".join(f"{k}=<n>" for k in keys)
                          + "\n```\n</output_format>")
        result = self.run_transcript(path)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
