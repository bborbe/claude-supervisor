#!/usr/bin/env python3
"""`scripts/decision-park-resume.py` — one test per defect the prose version shipped.

The procedure was prose for six review rounds and each round found a different real defect in it.
Each of those is a test here, named for the defect, so the seventh cannot come back:

1. kind (b) resumed with no prompt          → `test_kind_b_carries_the_answer_as_the_prompt`
2. kind (a) read a field the population already guarantees
                                            → `test_kind_a_requires_the_approval_to_postdate_the_turn`
3. kind (d) treated an empty `blocked_by` as cleared
                                            → `test_an_empty_blocked_by_is_not_a_cleared_wait`
4. no consumption bound at all              → `test_the_cap_is_spent_at_three`
5. the id recorded was not the one produced → `test_the_emitted_id_is_the_sessions_own`
6. the cap was armed but never read         → `test_crash_loop_fires_for_a_row_worker_keepalive_would_jump`
"""
import importlib.util
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.dirname(HERE)
sys.path.insert(0, SCRIPTS)

_spec = importlib.util.spec_from_file_location(
    "decision_park_resume", os.path.join(SCRIPTS, "decision-park-resume.py"))
dpr = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(dpr)

NOW = 1_800_000_000.0


class FakeKeepalive:
    """The seams: liveness, and the resume store the cap is read from."""

    def __init__(self, verdicts, stamps=None):
        self.verdicts = verdicts
        self.stamps = stamps or []

    def session_verdict(self, sid):
        return self.verdicts.get(sid, "ABSENT")

    def state_path(self, vault, subject, branch="goal"):
        return os.path.join(vault, f"{subject}.{branch}.resumes")

    def load_resumes(self, path):
        try:
            with open(path, encoding="utf-8") as handle:
                return [float(x) for x in handle.read().split()]
        except OSError:
            return list(self.stamps)

    def recent(self, stamps, now):
        return [s for s in stamps if now - s <= dpr.RESUME_WINDOW]


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.vault = self.dir
        self.tasks = os.path.join(self.dir, "tasks")
        os.makedirs(self.tasks, exist_ok=True)
        self.verdicts = {}
        self.fake = FakeKeepalive(self.verdicts)
        self._orig_keepalive = dpr.keepalive
        dpr.keepalive = lambda: self.fake
        self._orig_last_turn = dpr.last_turn
        self._orig_answered = dpr.answered_card
        dpr.last_turn = lambda sid: dpr.parse_iso("2026-11-05T00:00:00Z")
        dpr.answered_card = lambda sid: ""

    def tearDown(self):
        dpr.keepalive = self._orig_keepalive
        dpr.last_turn = self._orig_last_turn
        dpr.answered_card = self._orig_answered

    def task(self, name, **fm):
        body = "---\n" + "".join(f"{k}: {v}\n" for k, v in fm.items()) + "---\n\n# Impact\n"
        with open(os.path.join(self.tasks, name + ".md"), "w", encoding="utf-8") as handle:
            handle.write(body)

    def run_it(self):
        out = io.StringIO()
        with redirect_stdout(out):
            rc = dpr.main(["--vault", self.vault, "--tasks-dir", self.tasks,
                           "--now", str(NOW), "--json"])
        return rc, json.loads(out.getvalue())

    def parked(self, name="A Row", sid="sid-1", **extra):
        fm = {"status": "in_progress", "claude_session_id": sid}
        fm.update(extra)
        self.task(name, **fm)
        self.verdicts[sid] = "ABSENT"
        return name


class TestTheSixDefects(Base):
    # 1
    def test_kind_b_carries_the_answer_as_the_prompt(self):
        self.parked()
        dpr.answered_card = lambda sid: "run it"
        rc, out = self.run_it()
        self.assertEqual(0, rc)
        row = [r for r in out["rows"] if r["verdict"] == "resume"][0]
        self.assertEqual("b", row["kind"])
        self.assertEqual("run it", row["prompt"], "kind (b) must never resume bare")

    def test_a_skip_answer_is_not_an_answer(self):
        self.parked()
        # answered_card returns None for a skip, by construction — assert the contract here.
        dpr.answered_card = lambda sid: ""
        dpr.last_turn = lambda sid: None
        rc, out = self.run_it()
        self.assertEqual(0, rc)
        self.assertFalse([r for r in out["rows"] if r["verdict"] == "resume"])

    # 2
    def test_kind_a_requires_the_approval_to_postdate_the_turn(self):
        self.parked(approved_at="2026-11-01T00:00:00Z")
        # approval BEFORE the last turn → the worker was still running after approval
        dpr.last_turn = lambda sid: dpr.parse_iso("2026-11-02T00:00:00Z")
        rc, out = self.run_it()
        self.assertEqual(0, rc)
        self.assertFalse([r for r in out["rows"] if r["verdict"] == "resume"],
                         "an old approval is not a cleared wait")

    def test_kind_a_fires_when_the_approval_is_later(self):
        self.parked(approved_at="2026-11-03T00:00:00Z")
        dpr.last_turn = lambda sid: dpr.parse_iso("2026-11-02T00:00:00Z")
        rc, out = self.run_it()
        self.assertEqual(0, rc)
        row = [r for r in out["rows"] if r["verdict"] == "resume"][0]
        self.assertEqual("a", row["kind"])

    # 3
    def test_an_empty_blocked_by_is_not_a_cleared_wait(self):
        self.parked()  # no blocked_by at all
        dpr.last_turn = lambda sid: dpr.parse_iso("2026-11-05T00:00:00Z")
        rc, out = self.run_it()
        self.assertEqual(0, rc)
        self.assertFalse([r for r in out["rows"] if r["verdict"] == "resume"],
                         "an empty set satisfies 'every name is terminal' vacuously")

    def test_a_non_empty_blocked_by_all_terminal_fires(self):
        self.task("Dep Done", status="completed", claude_session_id="")
        self.parked(blocked_by="'[[Dep Done]]'")
        dpr.last_turn = lambda sid: dpr.parse_iso("2026-11-05T00:00:00Z")
        rc, out = self.run_it()
        self.assertEqual(0, rc)
        row = [r for r in out["rows"] if r["verdict"] == "resume"][0]
        self.assertEqual("d", row["kind"])

    # 4
    def test_the_cap_is_spent_at_three(self):
        name = self.parked()
        dpr.answered_card = lambda sid: "go"
        path = os.path.join(self.vault, f"{name}.decision.resumes")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(" ".join(str(NOW - 60) for _ in range(3)))
        rc, out = self.run_it()
        self.assertEqual(0, rc)
        self.assertEqual("crash-loop", out["rows"][0]["verdict"])

    # 5
    def test_the_emitted_id_is_the_sessions_own(self):
        self.parked(sid="sid-abc")
        dpr.answered_card = lambda sid: "go"
        rc, out = self.run_it()
        self.assertEqual("sid-abc", out["rows"][0]["sid"])

    # 6
    def test_crash_loop_fires_for_a_row_worker_keepalive_would_jump(self):
        """The cap this script reads is its own — worker-keepalive's verdict would `jump` a
        parked-phase row and never reach its crash-loop branch, which is why it cannot bound."""
        name = self.parked(phase="todo")
        dpr.answered_card = lambda sid: "go"
        path = os.path.join(self.vault, f"{name}.decision.resumes")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(" ".join(str(NOW - 60) for _ in range(3)))
        rc, out = self.run_it()
        self.assertEqual("crash-loop", out["rows"][0]["verdict"])


class TestUnread(Base):
    def test_a_live_session_is_not_a_candidate(self):
        self.parked()
        self.verdicts["sid-1"] = "LIVE"
        rc, out = self.run_it()
        self.assertEqual(0, out["candidates"])

    def test_an_undecidable_probe_is_unread_not_zero(self):
        self.parked()
        self.verdicts["sid-1"] = "UNKNOWN"
        rc, out = self.run_it()
        self.assertEqual(1, len(out["unread"]))
        self.assertEqual(0, out["candidates"])

    def test_an_unparseable_approval_is_unread(self):
        self.parked(approved_at="not-a-date")
        dpr.last_turn = lambda sid: NOW
        rc, out = self.run_it()
        self.assertEqual(1, len(out["unread"]))

    def test_a_terminal_task_is_not_a_candidate(self):
        self.task("Done Row", status="completed", claude_session_id="sid-9",
                  approved_at="2026-11-01T00:00:00Z")
        self.verdicts["sid-9"] = "ABSENT"
        rc, out = self.run_it()
        self.assertEqual(0, out["candidates"])


if __name__ == "__main__":
    unittest.main()
