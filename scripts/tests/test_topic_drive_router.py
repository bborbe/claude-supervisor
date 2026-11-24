#!/usr/bin/env python3
"""The topic round must DISPATCH its act leg — a router row, not prose.

Measured 2026-11-24: `commands/topic-drive.md` pointed at `agents/topic-drive.md` /
`agents/goal-drive.md` in prose but its router had no step that dispatched either, so the
topic round drove no goal at all. The router row is the only thing that makes a round read
the dispatch, and the step file is the only thing that names the sub-agent — both are pinned.
"""

import os
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(os.path.dirname(_HERE))
_CMD = os.path.join(_ROOT, "commands", "topic-drive.md")
_STEP = os.path.join(_ROOT, "commands", "steps", "topic-drive-act.md")


def read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


class TopicRoundDispatchesItsActLeg(unittest.TestCase):
    def test_the_router_names_the_drive_step(self):
        text = read(_CMD)
        rows = [line for line in text.splitlines() if line.startswith("| 5 |")]
        self.assertEqual(1, len(rows), "the router has no single row 5: %r" % rows)
        self.assertIn("topic-drive-act.md", rows[0])

    def test_the_drive_step_dispatches_the_topic_drive_agent(self):
        text = read(_STEP)
        self.assertIn('subagent_type: "supervisor:topic-drive"', text)

    def test_the_router_no_longer_runs_a_goal_keep_alive(self):
        text = read(_CMD)
        self.assertNotIn("--receipt", text,
                         "the router still runs the retired goal-manager receipt")
        self.assertNotIn("topic-keepalive.py \\\n  --vault", text,
                         "the router still invokes the retired per-goal keeper")


if __name__ == "__main__":
    unittest.main()
