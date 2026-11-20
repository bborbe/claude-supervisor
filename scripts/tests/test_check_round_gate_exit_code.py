"""check-round-gate-exit-code.py: the gate must be able to fail.

`commands/steps/round-dispatch.md` tells the manager to run `check-handback-size.py` and branch
on its exit code. The one live invocation measured on 2026-11-20 was
`… | tail -5; echo "size_exit=$?"`, where `$?` is the pipe's last stage — a real exit 1 logged
as 0, and the gate never ran again.

The load-bearing cases:
  - an unpiped invocation with `gate_rc=$?` on the next line passes;
  - a piped invocation fails, naming the pipe;
  - an invocation with no capture fails;
  - an absent invocation fails (a deleted gate is not a passing gate);
  - a backslash-continued command is read as one logical line.
"""
import importlib.util
import pathlib
import unittest

SCRIPTS = pathlib.Path(__file__).resolve().parent.parent
SCRIPT = SCRIPTS / "check-round-gate-exit-code.py"


def load():
    spec = importlib.util.spec_from_file_location("check_round_gate_exit_code", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


GOOD = """\
Some prose.

```bash
python3 ${CLAUDE_PLUGIN_ROOT:-$HOME/.claude/plugins/marketplaces/claude-supervisor}/scripts/check-handback-size.py \\
  --transcript ~/.claude/projects/*/<this session id>/subagents/agent-<agentId>.jsonl
gate_rc=$?
```
"""

PIPED = """\
```bash
python3 "$P/check-handback-size.py" --transcript "$T" 2>&1 | tail -5; echo "size_exit=$?"
```
"""

NO_CAPTURE = """\
```bash
python3 "$P/check-handback-size.py" --transcript "$T"
```
"""

ABSENT = """\
```bash
python3 "$P/some-other-check.py"
```
"""


class CheckRoundGateExitCodeTest(unittest.TestCase):
    def setUp(self):
        self.mod = load()

    def test_clean_block_passes(self):
        self.assertEqual(self.mod.check(GOOD), [])

    def test_piped_invocation_fails(self):
        problems = self.mod.check(PIPED)
        self.assertEqual(len(problems), 1)
        self.assertIn("piped", problems[0])

    def test_missing_capture_fails(self):
        problems = self.mod.check(NO_CAPTURE)
        self.assertEqual(len(problems), 1)
        self.assertIn("gate_rc=$?", problems[0])

    def test_absent_gate_fails(self):
        problems = self.mod.check(ABSENT)
        self.assertEqual(len(problems), 1)
        self.assertIn("no `check-handback-size.py` invocation", problems[0])

    def test_continuation_is_one_logical_line(self):
        # The GOOD fixture's invocation spans two physical lines; it must be judged as one
        # command, or the pipe check would look at a fragment and pass a piped form.
        lines = self.mod.logical_lines(GOOD)
        gate = [ln for ln in lines if "check-handback-size.py" in ln]
        self.assertEqual(len(gate), 1)
        self.assertIn("--transcript", gate[0])


if __name__ == "__main__":
    unittest.main()
