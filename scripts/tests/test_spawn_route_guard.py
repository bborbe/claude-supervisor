"""Unit tests for `scripts/spawn-route-guard.py`.

The guard is the backstop for a session that hand-rolls a `wezterm cli spawn` instead of
routing the open through the wrapper's endpoint. It has two arms — an agent row resumed as
a tab (matrix Rule 2) and any spawn into the role map's Hold window — and it must fail
open on everything else.

Every fixture here is a temp tree; the guard's module-level paths are repointed per test so
no real vault or role map is read.
"""
import importlib.util
import json
import os
import pathlib
import tempfile
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _load():
    spec = importlib.util.spec_from_file_location(
        "spawn_route_guard", os.path.join(_ROOT, "scripts", "spawn-route-guard.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class SpawnRouteGuard(unittest.TestCase):
    def setUp(self):
        self.guard = _load()
        self._tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self._tmp.name)
        (self.root / "v1" / "25 Tasks").mkdir(parents=True)
        role_map = self.root / "role-map.json"
        role_map.write_text(json.dumps({
            "Managers": {"window_id": 3},
            "Direct": {"window_id": 21},
            "Hold": {"window_id": 4},
        }), encoding="utf-8")
        state = self.root / "state"
        self.guard.VAULT_ROOT = str(self.root)
        self.guard.ROLE_MAP = str(role_map)
        self.guard.STATE_DIR = str(state)

    def tearDown(self):
        self._tmp.cleanup()

    def _page(self, name, body):
        (self.root / "v1" / "25 Tasks" / (name + ".md")).write_text(body, encoding="utf-8")

    def _verdict(self, command):
        return self.guard.verdict(command, "sid")

    # --- arm 1: an agent row never opens a tab -------------------------------------

    def test_an_explicit_agent_resume_is_refused(self):
        self._page("A", "---\nclaude_session_id: aaaaaaaa-1111-2222-3333-444444444444\nrole: agent\n---\n")
        decision, reason = self._verdict("wezterm cli spawn --window-id 3 --resume aaaaaaaa-1111-2222-3333-444444444444 -- x")
        self.assertEqual("deny", decision)
        self.assertTrue(reason.startswith("agent-tab"), reason)

    def test_a_roless_task_is_an_agent_and_is_refused(self):
        """Matrix Rule 1: an absent `role:` is an agent, so it must be refused, not allowed."""
        self._page("B", "---\nclaude_session_id: bbbbbbbb-1111-2222-3333-444444444444\n---\n")
        decision, reason = self._verdict("wezterm cli spawn --window-id 3 --resume bbbbbbbb-1111-2222-3333-444444444444 -- x")
        self.assertEqual("deny", decision)
        self.assertTrue(reason.startswith("agent-tab"), reason)

    def test_a_human_resume_into_direct_is_allowed(self):
        self._page("C", "---\nclaude_session_id: cccccccc-1111-2222-3333-444444444444\nrole: human\n---\n")
        decision, _ = self._verdict("wezterm cli spawn --window-id 21 --resume cccccccc-1111-2222-3333-444444444444 -- x")
        self.assertEqual("allow", decision)

    def test_an_unknown_session_is_allowed(self):
        decision, reason = self._verdict("wezterm cli spawn --window-id 3 --resume dddddddd-1111-2222-3333-444444444444 -- x")
        self.assertEqual("allow", decision)
        self.assertEqual("no-arm-fired", reason)

    # --- arm 2: the Hold window ----------------------------------------------------

    def test_a_spawn_into_the_hold_window_is_refused(self):
        decision, reason = self._verdict("wezterm cli spawn --window-id 4 -- echo hi")
        self.assertEqual("deny", decision)
        self.assertEqual("hold-window:4", reason)

    def test_a_spawn_into_another_window_is_allowed(self):
        decision, _ = self._verdict("wezterm cli spawn --window-id 3 -- echo hi")
        self.assertEqual("allow", decision)

    # --- fail-open -----------------------------------------------------------------

    def test_a_non_spawn_command_is_allowed(self):
        decision, reason = self._verdict("git status")
        self.assertEqual("allow", decision)
        self.assertEqual("not-a-spawn", reason)

    def test_an_unreadable_role_map_does_not_refuse(self):
        self.guard.ROLE_MAP = str(self.root / "absent.json")
        decision, _ = self._verdict("wezterm cli spawn --window-id 4 -- echo hi")
        self.assertEqual("allow", decision)


if __name__ == "__main__":
    unittest.main()
