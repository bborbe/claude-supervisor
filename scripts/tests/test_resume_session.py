"""resume-session.py: the resumer for a session with no registry entry.

`restart-worker.py` is kill-first and refuses `unknown-session-id` before any other
gate, so it can never resume the sessions the reopen rule selects — every one of them
is registry-absent by construction. This script skips the kill and goes straight to the
resume recipe, recovering the cwd from the transcript.

What is load-bearing here:

- **`already-live` is checked first.** A resume onto a live session is the one mistake
  that loses work rather than time, so it is a refusal before any other step, and
  `UNKNOWN` / `AMBIGUOUS` / `MALFORMED` are refusals too — never "not live".
- **The cwd comes from the transcript's `cwd` field, never from the directory name.**
  The directory encoding is lossy: a real `-` in a path component is indistinguishable
  from a `/`. The fixture therefore puts a `cwd` containing a dash in the JSON and a
  differently-spelled directory, so a name-decoding implementation fails the assertion.
- **The two cwd failures are distinct tokens.** A missing transcript is
  `unknown-transcript`; a transcript that parses but carries no `cwd` is `no-cwd`.
  Collapsing them would tell an operator to look for a file that exists.

The probe and both siblings are stubbed by copying the script beside fakes in a temp
`scripts/` dir, with `HOME` redirected so `~/.claude/projects` is the fixture's.
"""
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest

SCRIPT = pathlib.Path(__file__).resolve().parents[1] / "resume-session.py"

FAKE_LIVENESS = '''#!/usr/bin/env python3
import json, os, sys
args = sys.argv[1:]
table = json.loads(os.environ.get("FAKE_LIVENESS") or "{}")
token = table.get(args[1] if len(args) > 1 else "", "UNKNOWN")
if token == "LIVE":
    print("LIVE — session is live  heartbeat age 3s  source heartbeat")
    sys.exit(0)
print("%s — not live" % token, file=sys.stderr)
sys.exit({"ABSENT": 1, "UNKNOWN": 2, "AMBIGUOUS": 3, "MALFORMED": 4}.get(token, 2))
'''

FAKE_WORKER = '''#!/usr/bin/env python3
def resolve_launcher(sid, cwd, fb, subject=None):
    return "/fake/launcher", "fixture"

def resume_command(sid, title, chip, cwd, launcher):
    return "resume %s in %s via %s" % (sid, cwd, launcher)
'''

FAKE_BOARD = '''#!/usr/bin/env python3
MANAGER_COLOUR = "orange"
'''

SID = "f9792011-e6c2-4126-86de-660d8f8a6cc9"


class Harness(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="resume-session-")
        self.addCleanup(shutil.rmtree, self.root, True)
        scripts = os.path.join(self.root, "scripts")
        os.makedirs(scripts)
        shutil.copy(SCRIPT, os.path.join(scripts, "resume-session.py"))
        for name, body in (
            ("session-liveness.py", FAKE_LIVENESS),
            ("restart-worker.py", FAKE_WORKER),
            ("fleet-board.py", FAKE_BOARD),
        ):
            with open(os.path.join(scripts, name), "w", encoding="utf-8") as handle:
                handle.write(body)
        self.script = os.path.join(scripts, "resume-session.py")
        self.projects = os.path.join(self.root, ".claude", "projects")

    def write_transcript(self, directory="-wrong-dir-name", records=None):
        folder = os.path.join(self.projects, directory)
        os.makedirs(folder, exist_ok=True)
        path = os.path.join(folder, f"{SID}.jsonl")
        with open(path, "w", encoding="utf-8") as handle:
            for record in records if records is not None else [
                {"type": "summary"},
                {"type": "user", "cwd": "/Users/someone/Documents/Obsidian/private-personal"},
            ]:
                handle.write(json.dumps(record) + "\n")
        return path

    def run_script(self, *args, liveness=None):
        env = dict(os.environ)
        env["HOME"] = self.root
        env["FAKE_LIVENESS"] = json.dumps(liveness if liveness is not None else {SID: "ABSENT"})
        proc = subprocess.run(
            [sys.executable, self.script, *args],
            capture_output=True, text=True, env=env, timeout=60,
        )
        return proc.returncode, (proc.stdout or "").splitlines()


class TestRefusals(Harness):
    def test_malformed_id(self):
        code, out = self.run_script("--session", "not-an-id")
        self.assertEqual(out[0], "malformed-id")
        self.assertEqual(code, 1)

    def test_live_session_is_refused_first(self):
        """A resume onto a live session is a second writer -- refused before any lookup."""
        self.write_transcript()
        code, out = self.run_script("--session", SID, liveness={SID: "LIVE"})
        self.assertEqual(out[0], "already-live")
        self.assertEqual(code, 1)

    def test_unknown_liveness_is_cannot_tell_not_already_live(self):
        """A probe that could not run must stay RETRYABLE -- folding it into already-live
        would let one transient failure become a terminal refusal the caller records."""
        self.write_transcript()
        code, out = self.run_script("--session", SID, liveness={SID: "UNKNOWN"})
        self.assertEqual(out[0], "cannot-tell")
        self.assertEqual(code, 1)

    def test_unknown_transcript(self):
        code, out = self.run_script("--session", SID)
        self.assertEqual(out[0], "unknown-transcript")

    def test_no_cwd_is_distinct_from_unknown_transcript(self):
        self.write_transcript(records=[{"type": "summary"}])
        code, out = self.run_script("--session", SID)
        self.assertEqual(out[0], "no-cwd")


class TestResume(Harness):
    def test_dry_run_recovers_cwd_from_the_field_not_the_dirname(self):
        """The directory name is deliberately wrong; only the `cwd` field may be used."""
        self.write_transcript(directory="-wrong-dir-name")
        code, out = self.run_script("--session", SID, "--dry-run")
        self.assertEqual(out[0], "resumed")
        self.assertEqual(code, 0)
        cwd_line = next(line for line in out if line.strip().startswith("cwd "))
        self.assertIn("/Users/someone/Documents/Obsidian/private-personal", cwd_line)
        self.assertNotIn("-wrong-dir-name", cwd_line.split("(from")[0])
        self.assertIn("/fake/launcher", "\n".join(out))

    def test_dry_run_does_not_spawn(self):
        self.write_transcript()
        code, out = self.run_script("--session", SID, "--dry-run")
        self.assertEqual(out[0], "resumed")
        self.assertTrue(any("command" in line for line in out))


if __name__ == "__main__":
    unittest.main()
