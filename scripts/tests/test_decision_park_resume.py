#!/usr/bin/env python3
"""`scripts/decision-park-resume.py` — one test per defect, driven through the REAL parsers.

Round seven's finding was that the previous suite mocked `answered_card` and `last_turn` and
fabricated a store file, so it was green while three defects were live. Nothing here stands in for
the code under test: `last_turn` reads a real transcript, `--record` writes the real store through
`topic-keepalive.py`, and `frontmatter` / `blocked_by` / `parse_iso` are the production functions.

The one seam patched is `session_verdict` — liveness is an external instrument, not this script's.
"""
import importlib.util
import io
import json
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.dirname(HERE)

_spec = importlib.util.spec_from_file_location(
    "decision_park_resume", os.path.join(SCRIPTS, "decision-park-resume.py"))
dpr = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(dpr)

NOW = 1_800_000_000.0
TURN = "2026-11-05T00:00:00Z"
APPROVED_AFTER = "2026-11-06T00:00:00Z"
APPROVED_BEFORE = "2026-11-04T00:00:00Z"


class Base(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.vault = os.path.join(self.root, "vault")
        self.tasks = os.path.join(self.vault, "25 Tasks")
        self.projects = os.path.join(self.root, "projects")
        os.makedirs(self.tasks, exist_ok=True)
        os.makedirs(self.projects, exist_ok=True)
        self.verdicts = {}

        self._orig_projects = dpr.PROJECTS_DIR
        dpr.PROJECTS_DIR = self.projects
        self.k = dpr.keepalive()
        self._orig_verdict = self.k.session_verdict
        self.k.session_verdict = lambda sid: self.verdicts.get(sid, "ABSENT")
        # The store's LOCATION is global (keyed off the vault), so a suite that used it
        # unisolated would accumulate across runs and across tests. Only the location is
        # redirected — `record`, `load_resumes` and `recent` stay the production functions.
        self._orig_store_path = dpr.store_path
        self.store = os.path.join(self.root, "store")
        os.makedirs(self.store, exist_ok=True)
        dpr.store_path = lambda vault, task: os.path.join(self.store, task.replace(" ", "-") + ".json")

    def tearDown(self):
        dpr.PROJECTS_DIR = self._orig_projects
        self.k.session_verdict = self._orig_verdict
        dpr.store_path = self._orig_store_path
        shutil.rmtree(self.root, ignore_errors=True)

    def transcript(self, sid, last_message=TURN):
        """A real transcript file, in the shape `last_turn` parses."""
        project = os.path.join(self.projects, "-Users-someone-vault")
        os.makedirs(project, exist_ok=True)
        with open(os.path.join(project, sid + ".jsonl"), "w", encoding="utf-8") as handle:
            handle.write(json.dumps({"type": "user", "timestamp": "2026-11-01T00:00:00Z"}) + "\n")
            handle.write(json.dumps({"type": "assistant", "timestamp": last_message}) + "\n")

    def task(self, name, **fm):
        body = "---\n" + "".join(f"{k}: {v}\n" for k, v in fm.items()) + "---\n\n# Impact\n"
        with open(os.path.join(self.tasks, name + ".md"), "w", encoding="utf-8") as handle:
            handle.write(body)

    def parked(self, name="A Row", sid="sid-1", **extra):
        fm = {"status": "in_progress", "claude_session_id": sid}
        fm.update(extra)
        self.task(name, **fm)
        self.verdicts[sid] = "ABSENT"
        self.transcript(sid)
        return name

    def decide(self):
        out = io.StringIO()
        with redirect_stdout(out):
            rc = dpr.main(["--vault", self.vault, "--tasks-dir", self.tasks,
                           "--now", str(NOW), "--json"])
        return rc, json.loads(out.getvalue())

    def resumed(self, result):
        return [r for r in result["rows"] if r["verdict"] == "resume"]


class TestTheWaits(Base):
    def test_kind_a_requires_the_approval_to_postdate_the_real_transcript(self):
        self.parked(approved_at=APPROVED_BEFORE)
        rc, out = self.decide()
        self.assertEqual(0, rc)
        self.assertFalse(self.resumed(out), "an approval older than the last turn is not a wait")

    def test_kind_a_fires_when_the_approval_is_later(self):
        self.parked(approved_at=APPROVED_AFTER)
        rc, out = self.decide()
        self.assertEqual("a", self.resumed(out)[0]["kind"])

    def test_an_empty_blocked_by_is_not_a_cleared_wait(self):
        self.parked()
        rc, out = self.decide()
        self.assertFalse(self.resumed(out), "'every name is terminal' is vacuously true of {}")

    def test_a_non_empty_blocked_by_all_terminal_fires(self):
        self.task("Dep Done", status="completed", claude_session_id="")
        self.parked(blocked_by="'[[Dep Done]]'")
        rc, out = self.decide()
        self.assertEqual("d", self.resumed(out)[0]["kind"])

    def test_a_held_dependency_does_not_fire(self):
        """`hold` is an ACTIVE block, not a finished dependency — the fleet's own
        `blocked_by_verdict` counts only `completed`."""
        self.task("Dep Held", status="hold", claude_session_id="")
        self.parked(blocked_by="'[[Dep Held]]'")
        rc, out = self.decide()
        self.assertFalse(self.resumed(out), "a held blocker must not resume the worker")

    def test_an_aborted_dependency_does_not_fire(self):
        self.task("Dep Aborted", status="aborted", claude_session_id="")
        self.parked(blocked_by="'[[Dep Aborted]]'")
        rc, out = self.decide()
        self.assertFalse(self.resumed(out), "aborted is not completed")

    def test_a_non_terminal_dependency_does_not_fire(self):
        self.task("Dep Open", status="in_progress", claude_session_id="")
        self.parked(blocked_by="'[[Dep Open]]'")
        rc, out = self.decide()
        self.assertFalse(self.resumed(out))

    def test_one_unreadable_wait_does_not_strand_the_other(self):
        """Round seven's all-or-nothing rule: (a) unreadable must not suppress a cleared (d)."""
        self.task("Dep Done", status="completed", claude_session_id="")
        self.parked(approved_at="not-a-date", blocked_by="'[[Dep Done]]'")
        rc, out = self.decide()
        self.assertEqual("d", self.resumed(out)[0]["kind"])


class TestTheCap(Base):
    def test_the_cap_is_read_from_the_store_the_record_writes(self):
        name = self.parked(approved_at=APPROVED_AFTER)
        for _ in range(dpr.RESUME_CAP):
            dpr.record(self.vault, name, "sid-1", NOW - 60)
        self.assertEqual(dpr.RESUME_CAP, dpr.spent(self.vault, name, NOW))
        rc, out = self.decide()
        self.assertEqual("crash-loop", out["rows"][0]["verdict"])

    def test_record_writes_the_path_spent_reads(self):
        """Round seven's defect in one assertion: a counter nothing writes is not a bound."""
        name = self.parked(approved_at=APPROVED_AFTER)
        path = dpr.store_path(self.vault, name)
        self.assertEqual(0, dpr.spent(self.vault, name, NOW))
        dpr.record(self.vault, name, "sid-1", NOW - 10)
        self.assertTrue(os.path.exists(path))
        self.assertEqual(1, dpr.spent(self.vault, name, NOW))

    def test_a_resume_under_the_cap_is_emitted(self):
        self.parked(approved_at=APPROVED_AFTER)
        rc, out = self.decide()
        self.assertEqual("resume", out["rows"][0]["verdict"])
        self.assertIn("attempt 1 of", out["rows"][0]["detail"])


class TestUnread(Base):
    def test_a_live_session_is_not_a_candidate(self):
        self.parked(approved_at=APPROVED_AFTER)
        self.verdicts["sid-1"] = "LIVE"
        rc, out = self.decide()
        self.assertEqual(0, out["candidates"])

    def test_an_undecidable_probe_is_unread_not_zero(self):
        self.parked(approved_at=APPROVED_AFTER)
        self.verdicts["sid-1"] = "UNKNOWN"
        rc, out = self.decide()
        self.assertEqual(1, len(out["unread"]))
        self.assertEqual(0, out["candidates"])

    def test_a_terminal_task_is_not_a_candidate(self):
        self.task("Done Row", status="completed", claude_session_id="sid-9",
                  approved_at=APPROVED_AFTER)
        self.verdicts["sid-9"] = "ABSENT"
        rc, out = self.decide()
        self.assertEqual(0, out["candidates"])

    def test_an_unreadable_tasks_dir_still_carries_the_counts(self):
        out = io.StringIO()
        with redirect_stdout(out):
            rc = dpr.main(["--vault", self.vault, "--tasks-dir", os.path.join(self.root, "nope"),
                           "--now", str(NOW)])
        self.assertEqual(1, rc)
        self.assertIn("decision-candidates=0", out.getvalue())

    def test_a_non_dict_json_line_in_the_transcript_does_not_crash(self):
        project = os.path.join(self.projects, "-Users-someone-vault")
        os.makedirs(project, exist_ok=True)
        with open(os.path.join(project, "sid-1.jsonl"), "w", encoding="utf-8") as handle:
            handle.write('"just a string"\n')
            handle.write(json.dumps({"timestamp": TURN}) + "\n")
        self.task("A Row", status="in_progress", claude_session_id="sid-1",
                  approved_at=APPROVED_AFTER)
        self.verdicts["sid-1"] = "ABSENT"
        rc, out = self.decide()
        self.assertEqual(0, rc, "a non-dict line must not take the round down")
        self.assertEqual("a", self.resumed(out)[0]["kind"])


if __name__ == "__main__":
    unittest.main()
