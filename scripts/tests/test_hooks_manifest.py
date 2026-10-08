"""hooks/hooks.json: the event -> state mapping, pinned.

This manifest carries the whole session-state contract — which Claude Code event means which
state — and nothing else in the repo reads it. The failure modes are both silent:

  * **Swapping two arguments inverts every session's reported state.** Putting `idle` on
    `UserPromptSubmit` and `busy` on `Stop` leaves the feature working in reverse and passes
    every other test in the suite, because the script's own tests pass the state directly and
    never consult the manifest.
  * **A typo in the script path disables all four hooks at once**, which is indistinguishable
    from a timer that never fired: no error, no record, nothing to see.

The repo's stated answer to a contract is a test beside the code — see the `resolve-task-file.py`
entry in CHANGELOG.md, which was extracted into a script for exactly this reason.
"""
import json
import pathlib
import unittest

REPO = pathlib.Path(__file__).resolve().parent.parent.parent
MANIFEST = REPO / "hooks" / "hooks.json"

# The mapping this file exists to pin. Each event's meaning is a decision, not an inference:
# a turn STARTING is `busy`, a turn ENDING is `idle`, an open prompt is `waiting-on-operator`,
# and a finished session clears its own record so the directory does not grow forever.
EXPECTED = {
    "UserPromptSubmit": "busy",
    "Stop": "idle",
    "Notification": "waiting-on-operator",
    "SessionEnd": "clear",
}

# Events that must NOT be wired to the state script: PermissionRequest belongs to the answer
# relay, and adding a state argument there would be a second writer for one record.
STATE_SCRIPT = "heartbeat-state.py"


class HooksManifestTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
        cls.hooks = cls.manifest["hooks"]

    def state_arg_for(self, event):
        """The state argument the manifest passes for an event, or None if it does not."""
        for group in self.hooks.get(event, []):
            for hook in group.get("hooks", []):
                args = hook.get("args", [])
                if any(STATE_SCRIPT in str(a) for a in args):
                    return args[-1]
        return None

    def test_every_event_maps_to_its_intended_state(self):
        for event, expected in EXPECTED.items():
            self.assertEqual(
                self.state_arg_for(event),
                expected,
                f"{event} must record {expected!r} — a swap here inverts every session's state",
            )

    def test_no_other_event_writes_a_state_record(self):
        # ⚠️ Two writers for one record is how a state becomes a coin flip. PermissionRequest
        # already owns a hook in this manifest, for a different job.
        for event in self.hooks:
            if event in EXPECTED:
                continue
            self.assertIsNone(
                self.state_arg_for(event),
                f"{event} must not write a state record",
            )

    def test_the_referenced_script_exists(self):
        # A typo in the path disables every hook silently — no error, no record, and it reads
        # exactly like a timer that never fired.
        for event in EXPECTED:
            for group in self.hooks[event]:
                for hook in group["hooks"]:
                    for arg in hook.get("args", []):
                        if STATE_SCRIPT in str(arg):
                            rel = str(arg).replace("${CLAUDE_PLUGIN_ROOT}/", "")
                            self.assertTrue(
                                (REPO / rel).is_file(),
                                f"{event} points at a script that does not exist: {rel}",
                            )

    def test_every_state_hook_carries_a_timeout(self):
        # ⚠️ These run inside the operator's turn. An unbounded hook is a hang with no
        # diagnosis, which is worse than a missed write.
        for event in EXPECTED:
            for group in self.hooks[event]:
                for hook in group["hooks"]:
                    self.assertIn("timeout", hook, f"{event} has no timeout")
                    self.assertLessEqual(hook["timeout"], 30, f"{event}'s timeout is too long")


if __name__ == "__main__":
    unittest.main()
