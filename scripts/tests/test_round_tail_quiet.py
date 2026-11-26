#!/usr/bin/env python3
"""Tests for the wait-then-close verdict in manager-quiet.py.

`verdict_for` gains a `self-close` arm: every tracked task terminal (no runnable, no
quiet-but-blocked/unapproved/deferred row) AND no live worker of this session. A live or
unreadable worker count keeps the loop ARMED — never a close. A blocked quiet row stands
down (dormant), never self-closes.

Run: python3 -m unittest discover -s scripts/tests -v
"""

import importlib.util
import os
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_SCRIPTS = os.path.dirname(_HERE)
_spec = importlib.util.spec_from_file_location(
    "manager_quiet", os.path.join(_SCRIPTS, "manager-quiet.py")
)
mq = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mq)


def seen(runnable=None, quiet=None, terminal=None, defer=None, unapproved=None,
         exempt=None, live_workers=0):
    return {
        "runnable": runnable or [],
        "quiet": quiet or [],
        "terminal": terminal or [],
        "defer": defer or {},
        "blocked": {},
        "unapproved": unapproved or [],
        "exempt": exempt or {},
        "live_workers": live_workers,
    }


class TestSelfCloseVerdict(unittest.TestCase):
    def test_all_terminal_and_no_workers_self_closes(self):
        self.assertEqual(
            mq.verdict_for(seen(terminal=["t1", "t2"], live_workers=0), 2),
            "self-close",
        )

    def test_a_live_worker_keeps_the_loop_armed(self):
        self.assertEqual(
            mq.verdict_for(seen(terminal=["t1"], live_workers=1), 2),
            "armed",
        )

    def test_unreadable_live_workers_stand_down(self):
        # Cannot confirm the worker count -> keep the existing stand-down (a marker; the
        # session stays alive), never a close.
        self.assertEqual(
            mq.verdict_for(seen(terminal=["t1"], live_workers=None), 2),
            "stood-down",
        )

    def test_a_blocked_quiet_row_stands_down_not_self_closes(self):
        # A blocked row needs a human; it is not "done", so it stands down (dormant),
        # never self-closes.
        self.assertEqual(
            mq.verdict_for(seen(quiet=["blocked"], terminal=[], live_workers=0), 2),
            "stood-down",
        )

    def test_below_the_threshold_is_armed(self):
        self.assertEqual(
            mq.verdict_for(seen(terminal=["t1"], live_workers=0), 1),
            "armed",
        )

    def test_zero_tracked_tasks_is_not_self_close(self):
        # No tasks at all is not "every task terminal" — stay stood-down, not a close.
        self.assertEqual(
            mq.verdict_for(seen(terminal=[], quiet=[], live_workers=0), 2),
            "stood-down",
        )


if __name__ == "__main__":
    unittest.main()
