"""manager-wake.py — resolution, prompt validation, and the dry-run path.

Exercises the pure logic without a live pane: the manager-state resolution (which
must pick the newest registration and honour `--tier auto`), the `--interval`
validation that guards the typed prompt, and the dry-run branch that must never
call `wezterm`. The submit-confirmation path needs a live pane and is not driven
here — the blocking review finding it fixes is covered by the code's own comment
and by `scripts/send-delivery-drill.py`.
"""
import importlib.util
import json
import os
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.dirname(HERE)
PATH = os.path.join(SCRIPTS, "manager-wake.py")


def load():
    spec = importlib.util.spec_from_file_location("manager_wake_under_test", PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class ResolveManagerSession(unittest.TestCase):
    A = "11111111-1111-1111-1111-111111111111"
    B = "22222222-2222-2222-2222-222222222222"

    def setUp(self):
        self.mod = load()
        self.state = tempfile.mkdtemp(prefix="mw-state-")
        self.mod.MANAGER_STATE = self.state

    def _write(self, sid, subject, branch, resolved_at):
        with open(os.path.join(self.state, f"{sid}.json"), "w", encoding="utf-8") as fh:
            json.dump({"subject": subject, "branch": branch, "resolved_at": resolved_at}, fh)

    def test_newest_wins(self):
        self._write(self.A, "Goal X", "goal", "2026-01-01T00:00:00Z")
        self._write(self.B, "Goal X", "goal", "2026-02-01T00:00:00Z")
        self.assertEqual((self.B, "goal"), self.mod.resolve_manager_session("Goal X", "goal"))

    def test_auto_matches_any_branch(self):
        self._write(self.A, "Topic Y", "topic", "2026-01-01T00:00:00Z")
        self.assertEqual((self.A, "topic"), self.mod.resolve_manager_session("Topic Y", "auto"))

    def test_tier_mismatch_is_none(self):
        self._write(self.A, "Goal X", "goal", "2026-01-01T00:00:00Z")
        self.assertEqual((None, None), self.mod.resolve_manager_session("Goal X", "topic"))

    def test_subject_match_is_case_insensitive(self):
        self._write(self.A, "Goal X", "goal", "2026-01-01T00:00:00Z")
        self.assertEqual((self.A, "goal"), self.mod.resolve_manager_session("goal x", "auto"))

    def test_no_match_is_none(self):
        self.assertEqual((None, None), self.mod.resolve_manager_session("Nope", "auto"))


class MainValidation(unittest.TestCase):
    def setUp(self):
        self.mod = load()
        self.mod.MANAGER_STATE = tempfile.mkdtemp(prefix="mw-empty-")

    def test_non_numeric_interval_is_usage_error(self):
        self.assertEqual(
            2, self.mod.main(["--subject", "X", "--interval", "15m; rm -rf /", "--dry-run"])
        )

    def test_zero_interval_is_usage_error(self):
        self.assertEqual(2, self.mod.main(["--subject", "X", "--interval", "0", "--dry-run"]))

    def test_no_manager_exits_zero(self):
        self.assertEqual(0, self.mod.main(["--subject", "__nope__", "--dry-run"]))


class WakeDryRun(unittest.TestCase):
    def test_dry_run_types_nothing(self):
        out = load().wake(123, "/supervisor:goal-drive --interval 15m", dry_run=True)
        self.assertIn("WOULD-WAKE", out)
        self.assertIn("pane=123", out)


class NonSessionFilesIgnored(unittest.TestCase):
    """`last-<vault>.json` sits in the same dir; it must never be read as a session id."""

    def setUp(self):
        self.mod = load()
        self.state = tempfile.mkdtemp(prefix="mw-state-")
        self.mod.MANAGER_STATE = self.state

    def _write(self, name, subject, branch, resolved_at):
        with open(os.path.join(self.state, f"{name}.json"), "w", encoding="utf-8") as fh:
            json.dump({"subject": subject, "branch": branch, "resolved_at": resolved_at}, fh)

    def test_last_vault_file_is_ignored(self):
        # Newer, but not a session id — must lose to the UUID even on recency.
        self._write("last-private-personal", "Goal X", "goal", "2026-03-01T00:00:00Z")
        self._write("11111111-2222-3333-4444-555555555555", "Goal X", "goal", "2026-01-01T00:00:00Z")
        self.assertEqual(
            ("11111111-2222-3333-4444-555555555555", "goal"),
            self.mod.resolve_manager_session("Goal X", "auto"),
        )

    def test_only_last_vault_file_is_none(self):
        self._write("last-private-personal", "Goal X", "goal", "2026-03-01T00:00:00Z")
        self.assertEqual((None, None), self.mod.resolve_manager_session("Goal X", "auto"))


class WakeConfirmation(unittest.TestCase):
    """The submit-confirmation loop, driven with fake pane reads and a no-op send."""

    def setUp(self):
        import time as _time
        self.mod = load()
        self._real_sleep = _time.sleep
        self.mod.time.sleep = lambda _s: None

        class _R:
            returncode = 0
            stderr = ""
        self._real_run = self.mod.subprocess.run
        self.mod.subprocess.run = lambda *a, **k: _R()

    def tearDown(self):
        import time as _time
        _time.sleep = self._real_sleep
        self.mod.subprocess.run = self._real_run

    def _wake_with(self, reads):
        it = iter(reads)
        self.mod._pane_text = lambda _pane: next(it, reads[-1])
        return self.mod.wake(1, "/supervisor:goal-drive --interval 15m", dry_run=False)

    def test_swallowed_then_cleared_is_woken(self):
        out = self._wake_with([
            "❯ /supervisor:goal-drive --interval 15m",
            "❯ \n> /supervisor:goal-drive --interval 15m",
        ])
        self.assertIn("WOKEN", out)

    def test_never_landed_is_unverified(self):
        out = self._wake_with(["❯ ", "❯ "])
        self.assertIn("UNVERIFIED", out)

    def test_queued_is_woken(self):
        out = self._wake_with(["❯ press up to edit queued messages"])
        self.assertIn("WOKEN", out)
        self.assertIn("queued", out)

    def test_always_swallowed_is_failed(self):
        out = self._wake_with(["❯ /supervisor:goal-drive --interval 15m"] * 20)
        self.assertIn("FAILED", out)


if __name__ == "__main__":
    unittest.main()
