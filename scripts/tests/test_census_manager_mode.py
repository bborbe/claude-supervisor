"""census-manager-mode.py: a topic or goal manager must never be running headless.

The load-bearing case is `test_fires_on_a_headless_manager` — a detector that cannot fire is
indistinguishable from a clean fleet, which is exactly the blindness this script exists to
remove. Its twin, `test_silent_on_a_tab_manager`, is what makes the first one discriminating:
the same vault and the same live session, differing only in `entrypoint`, must flip the verdict.
"""
import json
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest

SCRIPT = pathlib.Path(__file__).resolve().parent.parent / "census-manager-mode.py"
ROOT = SCRIPT.parent.parent

TOPIC = (
    "---\n"
    "page_type: topic\n"
    "status: in_progress\n"
    "phase: execution\n"
    "claude_session_id: 6f14c290-e936-4c7e-90f3-fef3b4d8d54d\n"
    "---\n"
    "\nTags: [[Topic]]\n"
)

TASK_PAGE = (
    "---\n"
    "page_type: task\n"
    "status: in_progress\n"
    "phase: execution\n"
    "claude_session_id: 11111111-2222-3333-4444-555555555555\n"
    "---\n"
    "\nTags: [[Task]]\n"
)

SID = "6f14c290-e936-4c7e-90f3-fef3b4d8d54d"


class CensusManagerModeTest(unittest.TestCase):
    def setUp(self):
        self.dir = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, str(self.dir), ignore_errors=True)
        self.vault = self.dir / "vault"
        self.registry = self.dir / "sessions"
        (self.vault / "23 Topics").mkdir(parents=True)
        (self.vault / "25 Tasks").mkdir(parents=True)
        self.registry.mkdir()
        (self.vault / "23 Topics" / "Development Pipeline Throughput.md").write_text(TOPIC, encoding="utf-8")
        (self.vault / "25 Tasks" / "Some Worker.md").write_text(TASK_PAGE, encoding="utf-8")
        self.live = self.dir / "live.json"

    def write_registry(self, sid, entrypoint):
        (self.registry / "12345.json").write_text(
            json.dumps({"sessionId": sid, "entrypoint": entrypoint}), encoding="utf-8"
        )

    def write_live(self, *sids):
        self.live.write_text(json.dumps([{"sessionId": s} for s in sids]), encoding="utf-8")

    def run_census(self, *extra):
        return subprocess.run(
            [
                sys.executable,
                str(SCRIPT),
                "--vault",
                str(self.vault),
                "--sessions-dir",
                str(self.registry),
                "--live-json",
                str(self.live),
                *extra,
            ],
            capture_output=True,
            text=True,
        )

    def test_fires_on_a_headless_manager(self):
        """A live sdk-cli session on a topic page is the defect, and must be named."""
        self.write_registry(SID, "sdk-cli")
        self.write_live(SID)
        result = self.run_census()
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn(f"{SID} Development Pipeline Throughput DEFECT manager-running-headless", result.stdout)

    def test_silent_on_a_tab_manager(self):
        """The same page and session, differing only in entrypoint, must pass.

        Without this case the first test proves only that the script prints when a session
        exists, not that it discriminates on the entrypoint the rule turns on.
        """
        self.write_registry(SID, "cli")
        self.write_live(SID)
        result = self.run_census()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("DEFECT", result.stdout)

    def test_headless_worker_is_not_a_defect(self):
        """A worker is headless by design — only topic and goal pages carry a manager."""
        worker = "11111111-2222-3333-4444-555555555555"
        self.write_registry(worker, "sdk-cli")
        self.write_live(worker)
        result = self.run_census()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("DEFECT", result.stdout)

    def test_not_live_is_not_a_defect(self):
        """A headless page whose session is not live is the keep-alive population, not this one."""
        self.write_registry(SID, "sdk-cli")
        self.write_live()
        result = self.run_census()
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_unreadable_registry_refuses(self):
        """An unreadable registry must refuse, never render as a clean census."""
        self.write_live(SID)
        shutil.rmtree(self.registry)
        result = self.run_census()
        self.assertEqual(result.returncode, 2)
        self.assertIn("REFUSED", result.stderr)

    def test_unreadable_live_file_refuses(self):
        self.write_registry(SID, "sdk-cli")
        self.live.write_text("not json", encoding="utf-8")
        result = self.run_census()
        self.assertEqual(result.returncode, 2)
        self.assertIn("REFUSED", result.stderr)

    def test_missing_vault_refuses(self):
        self.write_registry(SID, "sdk-cli")
        self.write_live(SID)
        result = subprocess.run(
            [sys.executable, str(SCRIPT), "--vault", str(self.dir / "nope"),
             "--sessions-dir", str(self.registry), "--live-json", str(self.live)],
            capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("REFUSED", result.stderr)

    def test_unreadable_layout_refuses(self):
        """A vault with no page directory must refuse, never read as a clean fleet.

        `scan()` returns empty for a layout it does not recognise, so without the `page_dirs`
        guard every live session reads as owning nothing and the census prints a clean fleet
        from a wrong path — the refusal/clean conflation the exit codes exist to prevent.
        """
        self.write_registry(SID, "sdk-cli")
        self.write_live(SID)
        shutil.rmtree(self.vault / "23 Topics")
        shutil.rmtree(self.vault / "25 Tasks")
        result = self.run_census()
        self.assertEqual(result.returncode, 2)
        self.assertIn("REFUSED", result.stderr)
        self.assertIn("no topic, goal or task page directory", result.stderr)


if __name__ == "__main__":
    unittest.main()
