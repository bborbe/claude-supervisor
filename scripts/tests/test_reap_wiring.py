#!/usr/bin/env python3
"""Lockstep: the drive leg's documented reap invocation must be executable.

`agents/goal-drive.md`'s fifth read tells the leg to route the reap verdict
through `scripts/reap-line.py`. Prose cannot be executed, so a wiring that only
mentions the script has no signal of its own — measured 2026-10-08, the leg's
behaviour tracked only an explicit instruction in the dispatch, which is the
reason the producer exists at all.

This file is the behavioural probe SC4 pairs with its greps: it reads the
documented invocation out of the agent file, asserts it names the three flags
the script takes, and executes the script with exactly those flags. A doc/script
interface drift therefore fails inside `make test` instead of silently, and a
producer that dropped every reap fails on the live control.

Run: python3 -m unittest discover -s scripts/tests -v
"""

import os
import subprocess
import sys
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.dirname(os.path.dirname(_HERE))
_AGENT = os.path.join(_REPO, "agents", "goal-drive.md")
_SCRIPT = os.path.join(_REPO, "scripts", "reap-line.py")

# The tick-86 target and a live control, so the probe pins BOTH directions.
ABSENT_ID = "99058101-0000-4000-8000-000000000001"
LIVE_ID = "5384b63b-07b5-45d7-ad60-d78d0e4d3b87"
TASK = "T"
MOOT = "reap moot — no live recipient"
FLAGS = ("--task", "--session", "--liveness")


def agent_text():
    with open(_AGENT, encoding="utf-8") as fh:
        return fh.read()


def documented_invocation():
    """The one line in the agent file that runs the producer, or None.

    All three flags must sit on the same line: a prose line that merely names
    `--liveness` beside the script is not an invocation, and matching it would
    make this lockstep pass on a file that documents no runnable command.
    """
    for line in agent_text().splitlines():
        if "scripts/reap-line.py" in line and all(flag in line for flag in FLAGS):
            return line
    return None


def run_reap(liveness, session):
    return subprocess.run(
        [sys.executable, _SCRIPT,
         "--task", TASK, "--session", session, "--liveness", liveness],
        capture_output=True,
        text=True,
    )


class ReapWiringTest(unittest.TestCase):
    def test_agent_file_documents_the_producer(self):
        text = agent_text()
        self.assertIn("scripts/reap-line.py", text)
        self.assertIn(MOOT, text)

    def test_documented_invocation_names_the_scripts_flags(self):
        line = documented_invocation()
        self.assertIsNotNone(
            line, "no reap-line.py invocation documented in agents/goal-drive.md"
        )
        for flag in FLAGS:
            self.assertIn(flag, line, flag)

    def test_documented_invocation_is_executable_absent(self):
        # The documented flags are the ones executed: if the script renames one
        # and the agent file is not updated, this fails.
        self.assertIsNotNone(documented_invocation())
        result = run_reap("absent", ABSENT_ID)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), MOOT)
        self.assertNotIn("SendMessage", result.stdout)

    def test_documented_invocation_is_executable_live(self):
        # The live control: a producer that dropped every reap would pass the
        # absent probe and fail here.
        self.assertIsNotNone(documented_invocation())
        result = run_reap("live", LIVE_ID)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("SendMessage", result.stdout)
        self.assertIn(LIVE_ID, result.stdout)


if __name__ == "__main__":
    unittest.main()
