"""reopen-candidate.py: the rule that replaces the drive leg's CLOSERS gate.

The defect this guards, measured 2026-10-27 (fleet rounds 76-80): 79-83 orphans every
round and `revive=0`, because step 4a required an observed `CLOSERS` row and a headless
worker ends its turn at READY and prints none. The gate was built for a 2026-09-23
failure that was a **live** session with an uncaptured closer -- so the rule this suite
covers must fire on a dead session and must never fire on a live one.

What is load-bearing here, and each is a way the suite could be plausibly wrong:

- **`cannot-tell` is not `ABSENT`.** A probe that errored must not license a resume; the
  whole double-writer hazard is one fold of UNKNOWN into "not live". Two tests assert it
  (UNKNOWN from the probe, and an unreadable state store).

- **The cap counts only resumes that took.** `--record-resume` against a non-LIVE id
  writes nothing, so a resume that never landed cannot arm the cap against its own retry.

- **Progress is the only reset.** Three recorded resumes at 5 open boxes, then the task
  at 3 boxes, must read `resume` again -- a box was ticked. The same three at the SAME
  box count must read `capped`, which is the eleven-planning-pass case.

- **The state store is per task.** The cap is keyed on the vault directory name and the
  task's slug, so two tasks cannot spend each other's budget.

The probe is stubbed, not mocked in-process: the script under test is copied beside a
fake `session-liveness.py` in a temp `scripts/` dir, so `SCRIPTS` resolves there. `HOME`
is redirected to the same temp dir so the state store never touches the real one.
"""
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest

SCRIPT = pathlib.Path(__file__).resolve().parents[1] / "reopen-candidate.py"

FAKE_LIVENESS = '''#!/usr/bin/env python3
"""Stand-in for session-liveness.py: read the verdict from $FAKE_LIVENESS (a JSON map)."""
import json, os, sys

args = sys.argv[1:]
if not args or args[0] != "--check" or len(args) < 2:
    print("MALFORMED")
    sys.exit(4)
table = json.loads(os.environ.get("FAKE_LIVENESS") or "{}")
token = table.get(args[1], "UNKNOWN")
print(token)
sys.exit({"LIVE": 0, "ABSENT": 1, "UNKNOWN": 2, "AMBIGUOUS": 3, "MALFORMED": 4}.get(token, 2))
'''

TASK = """---
status: {status}
phase: execution
claude_session_id: {sid}
---
Tags: [[Task]]

---

# Success Criteria

- [ ] {box1}
- [ ] {box2}
- [ ] {box3}
"""

SID = "4f1695bd-0000-4000-8000-000000000001"


class Harness(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="reopen-candidate-")
        self.addCleanup(shutil.rmtree, self.root, True)
        scripts = os.path.join(self.root, "scripts")
        os.makedirs(scripts)
        shutil.copy(SCRIPT, os.path.join(scripts, "reopen-candidate.py"))
        with open(os.path.join(scripts, "session-liveness.py"), "w", encoding="utf-8") as handle:
            handle.write(FAKE_LIVENESS)
        self.script = os.path.join(scripts, "reopen-candidate.py")
        vault = os.path.join(self.root, "vault")
        tasks = os.path.join(vault, "25 Tasks")
        os.makedirs(tasks)
        self.task = os.path.join(tasks, "A Disappeared Worker.md")
        self.write_task()

    def write_task(self, status="in_progress", sid=SID, boxes=3):
        body = TASK.format(
            status=status,
            sid=sid,
            box1="one" if boxes >= 1 else "done",
            box2="two" if boxes >= 2 else "done",
            box3="three" if boxes >= 3 else "done",
        )
        if boxes < 3:
            body = body.replace("- [ ] done", "- [x] done")
        with open(self.task, "w", encoding="utf-8") as handle:
            handle.write(body)

    def run_script(self, *args, liveness=None):
        env = dict(os.environ)
        env["HOME"] = self.root
        env["FAKE_LIVENESS"] = json.dumps(liveness or {SID: "ABSENT"})
        proc = subprocess.run(
            [sys.executable, self.script, "--task", self.task, *args],
            capture_output=True,
            text=True,
            env=env,
            timeout=60,
        )
        return proc.returncode, (proc.stdout or "").splitlines()

    def state_file(self):
        return os.path.join(self.root, ".claude", "state", "fleet-resumes", "vault", "a-disappeared-worker.json")

    def write_state(self, resumes):
        path = self.state_file()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            json.dump({"resumes": resumes}, handle)

    def read_state(self):
        with open(self.state_file(), encoding="utf-8") as handle:
            return json.load(handle)


class TestVerdict(Harness):
    def test_resume_on_absent_open_work(self):
        code, out = self.run_script()
        self.assertEqual(out[0], "resume")
        self.assertEqual(code, 0)

    def test_live_session_is_not_open_work(self):
        """A live session holds its own gate -- the 2026-09-23 case must never fire."""
        code, out = self.run_script(liveness={SID: "LIVE"})
        self.assertEqual(out[0], "not-open-work")
        self.assertEqual(code, 1)

    def test_terminal_task_is_not_open_work(self):
        self.write_task(status="completed")
        code, out = self.run_script()
        self.assertEqual(out[0], "not-open-work")

    def test_zero_open_boxes_is_not_open_work(self):
        self.write_task(boxes=0)
        code, out = self.run_script()
        self.assertEqual(out[0], "not-open-work")

    def test_no_session_id_is_not_open_work(self):
        """Never a fresh spawn while an id exists -- and never a reopen without one."""
        self.write_task(sid="")
        code, out = self.run_script()
        self.assertEqual(out[0], "not-open-work")

    def test_unknown_liveness_is_cannot_tell(self):
        code, out = self.run_script(liveness={SID: "UNKNOWN"})
        self.assertEqual(out[0], "cannot-tell")
        self.assertEqual(code, 2)

    def test_ambiguous_liveness_is_cannot_tell(self):
        code, out = self.run_script(liveness={SID: "AMBIGUOUS"})
        self.assertEqual(out[0], "cannot-tell")

    def test_unreadable_state_store_is_cannot_tell(self):
        path = self.state_file()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("{not json")
        code, out = self.run_script()
        self.assertEqual(out[0], "cannot-tell")

    def test_malformed_state_entries_are_cannot_tell(self):
        """A `resumes` list holding non-objects must not crash into an AttributeError."""
        self.write_state([1, 2, 3])
        code, out = self.run_script()
        self.assertEqual(out[0], "cannot-tell")
        self.assertEqual(code, 2)

    def test_session_override_decides_for_that_id(self):
        """The documented `--session` override must drive the probe, not the frontmatter."""
        other = "aaaaaaaa-0000-4000-8000-000000000002"
        code, out = self.run_script("--session", other, liveness={other: "ABSENT"})
        self.assertEqual(out[0], "resume")

    def test_session_override_live_blocks_the_resume(self):
        other = "aaaaaaaa-0000-4000-8000-000000000002"
        code, out = self.run_script("--session", other, liveness={other: "LIVE"})
        self.assertEqual(out[0], "not-open-work")


class TestCap(Harness):
    def resume_entry(self, boxes, at="2026-10-27T12:00:00+00:00"):
        return {"at": at, "session_id": SID, "open_boxes": boxes}

    def test_capped_at_three(self):
        self.write_state([self.resume_entry(3) for _ in range(3)])
        code, out = self.run_script()
        self.assertEqual(out[0], "capped")
        self.assertEqual(code, 1)

    def test_two_resumes_still_allows_one(self):
        self.write_state([self.resume_entry(3) for _ in range(2)])
        code, out = self.run_script()
        self.assertEqual(out[0], "resume")

    def test_progress_resets_the_cap(self):
        """Three resumes at 5 boxes, task now at 3 -- a box was ticked, so the cap resets."""
        self.write_state([self.resume_entry(5) for _ in range(3)])
        code, out = self.run_script()
        self.assertEqual(out[0], "resume")

    def test_no_progress_does_not_reset(self):
        self.write_state([self.resume_entry(3) for _ in range(3)])
        code, out = self.run_script()
        self.assertEqual(out[0], "capped")

    def test_capped_without_a_box_count_renders_unknown(self):
        """A `None` box count must not read as a measurement."""
        self.write_state([{"at": "x", "session_id": SID} for _ in range(3)])
        code, out = self.run_script()
        self.assertEqual(out[0], "capped")
        self.assertTrue(any("unknown ->" in line for line in out), out)


class TestRecord(Harness):
    def test_record_writes_when_live(self):
        code, out = self.run_script("--record-resume", SID, liveness={SID: "LIVE"})
        self.assertEqual(out[0], "recorded")
        self.assertEqual(code, 0)
        self.assertEqual(len(self.read_state()["resumes"]), 1)

    def test_record_refuses_when_not_live(self):
        """A resume that never landed must not arm the cap against its own retry."""
        code, out = self.run_script("--record-resume", SID, liveness={SID: "ABSENT"})
        self.assertEqual(out[0], "not-recorded")
        self.assertEqual(code, 1)
        self.assertFalse(os.path.exists(self.state_file()))

    def test_record_accumulates(self):
        for _ in range(2):
            self.run_script("--record-resume", SID, liveness={SID: "LIVE"})
        self.assertEqual(len(self.read_state()["resumes"]), 2)

    def test_record_refuses_a_foreign_id(self):
        """A foreign id must not spend this task's budget."""
        other = "aaaaaaaa-0000-4000-8000-000000000002"
        code, out = self.run_script("--record-resume", other, liveness={other: "LIVE"})
        self.assertEqual(out[0], "not-recorded")
        self.assertFalse(os.path.exists(self.state_file()))

    def test_record_refuses_a_terminal_task(self):
        self.write_task(status="completed")
        code, out = self.run_script("--record-resume", SID, liveness={SID: "LIVE"})
        self.assertEqual(out[0], "not-recorded")
        self.assertFalse(os.path.exists(self.state_file()))

    def test_record_leaves_no_temp_file_behind(self):
        self.run_script("--record-resume", SID, liveness={SID: "LIVE"})
        leftovers = [n for n in os.listdir(os.path.dirname(self.state_file())) if n.endswith(".tmp")]
        self.assertEqual(leftovers, [])


class TestStateIsolation(Harness):
    def test_other_task_has_its_own_budget(self):
        self.write_state([{"at": "x", "session_id": SID, "open_boxes": 3} for _ in range(3)])
        other = os.path.join(os.path.dirname(self.task), "Another Task.md")
        shutil.copy(self.task, other)
        env = dict(os.environ)
        env["HOME"] = self.root
        env["FAKE_LIVENESS"] = json.dumps({SID: "ABSENT"})
        proc = subprocess.run(
            [sys.executable, self.script, "--task", other],
            capture_output=True,
            text=True,
            env=env,
            timeout=60,
        )
        self.assertEqual((proc.stdout or "").splitlines()[0], "resume")


if __name__ == "__main__":
    unittest.main()
