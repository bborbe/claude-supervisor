"""The worker-drive `State:` header contract, and the lint that can fail.

`/supervisor:worker-drive` reports its session state as the first header line of
the §8 report, and the closer's icon must follow it:

    working 🟢 · external 🟡 · decision 🔵 · closed ⚪

A report that fits none of the four prints `unclassified — <reason>` and is
treated as `decision` (🔵). The rule lives in `docs/worker-session-states.md`.

⚠️ **A lint over samples the author wrote proves the samples, not the command.**
The negative control below is therefore part of the contract: it feeds the lint a
report whose `State:` and closer icon disagree and requires it to reject. Without
that test a lint that returned `None` for every input would pass this file.

The real post-deploy reports that satisfy the criterion's second half are recorded
in the task's `# Progress`, not here — these samples are the shape, not the
evidence.
"""
import pathlib
import re
import unittest

REPO = pathlib.Path(__file__).resolve().parent.parent.parent

STATES = ("working", "external", "decision", "closed", "unclassified")
EXPECTED_ICON = {
    "working": "🟢",
    "external": "🟡",
    "decision": "🔵",
    "closed": "⚪",
    # Unclassified is treated as a decision until a pane read says otherwise.
    "unclassified": "🔵",
}

_STATE_LINE = re.compile(r"^State:\s+(working|external|decision|closed|unclassified)\b")
_CLOSER_LINE = re.compile(r"^(🟢|🟡|🔴|🔵|⚪)\s+(ACTIVE|WAITING|BLOCKED|READY|DONE)\b")


def lint(report):
    """Return (state, icon) or raise AssertionError naming the defect."""
    state = None
    for line in report.splitlines():
        match = _STATE_LINE.match(line)
        if match:
            state = match.group(1)
            break
    if state is None:
        raise AssertionError("report carries no `State:` header line")

    icon = None
    for line in report.splitlines():
        match = _CLOSER_LINE.match(line)
        if match:
            icon = match.group(1)
            break
    if icon is None:
        raise AssertionError("report carries no closer state line")

    expected = EXPECTED_ICON[state]
    if icon != expected:
        raise AssertionError(f"State {state} expects {expected}, closer shows {icon}")
    return state, icon


SAMPLES = {
    "working": [
        """State: working 🟢 · a tool call in flight
🟢 ACTIVE · 1 running (make test) · nothing needed
👤 You: nothing
⏰ Next: the test run exits""",
        """State: working 🟢 · driving the next open box
🟢 ACTIVE · nothing running · nothing needed
👤 You: nothing
⏰ Next: next tool round""",
    ],
    "external": [
        """State: external 🟡 · /github-pr-watch armed on PR 412
🟡 WAITING · 1 running (github-pr-watch) · nothing needed
👤 You: nothing
⏰ Ends: the bot review lands or the watch times out (github-pr-watch, PR 412)""",
        """State: external 🟡 · ScheduleWakeup armed for the deploy check
🟡 WAITING · nothing running (clock-bound) · nothing needed
👤 You: nothing
⏰ Ends: the scheduled wakeup fires (ScheduleWakeup, 15 min)""",
    ],
    "decision": [
        """State: decision 🔵 · one card minted for the pick
🔵 READY · nothing running · you decide
👤 You: pick — 1. merge on green (recommended) · 2. hold
⏰ Next: your answer on the card""",
        """State: decision 🔵 · approve: line on its own card
🔵 READY · nothing running · you decide
👤 You: approve: gh pr merge 412 --merge
⏰ Next: the card is answered""",
    ],
    "closed": [
        """State: closed ⚪ · task completed, zero open boxes
⚪ DONE · nothing running · nothing needed
👤 You: nothing
⏰ Next: no mechanism is running""",
        """State: closed ⚪ · task aborted by the operator
⚪ DONE · nothing running · nothing needed
👤 You: nothing
⏰ Next: no mechanism is running""",
    ],
}


class WorkerDriveStateTest(unittest.TestCase):
    def test_two_samples_per_state(self):
        for state in STATES[:-1]:
            self.assertGreaterEqual(
                len(SAMPLES.get(state, [])), 2, f"{state} has fewer than 2 samples"
            )

    def test_samples_pass_the_lint(self):
        for state, reports in SAMPLES.items():
            for index, report in enumerate(reports, 1):
                with self.subTest(state=state, sample=index):
                    got_state, icon = lint(report)
                    self.assertEqual(got_state, state)
                    self.assertEqual(icon, EXPECTED_ICON[state])

    def test_mismatched_icon_is_rejected(self):
        """The negative control: a lint that cannot fail proves nothing."""
        broken = "State: external 🟡 · a wait\n🟢 ACTIVE · nothing running · nothing needed"
        with self.assertRaises(AssertionError):
            lint(broken)

    def test_missing_state_line_is_rejected(self):
        with self.assertRaises(AssertionError):
            lint("🟢 ACTIVE · nothing running · nothing needed")

    def test_unclassified_is_treated_as_decision(self):
        report = "State: unclassified — permission prompt\n🔵 READY · nothing running · you decide"
        self.assertEqual(lint(report), ("unclassified", "🔵"))

    def test_command_documents_the_header(self):
        command = (REPO / "commands" / "worker-drive.md").read_text(encoding="utf-8")
        self.assertIn("State:", command)
        self.assertIn("docs/worker-session-states.md", command)

    def test_rule_doc_exists(self):
        rule = REPO / "docs" / "worker-session-states.md"
        self.assertTrue(rule.exists(), "docs/worker-session-states.md is missing")
        text = rule.read_text(encoding="utf-8")
        for state in STATES[:-1]:
            self.assertIn(f"`{state}`", text)


if __name__ == "__main__":
    unittest.main()
