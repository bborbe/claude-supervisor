#!/usr/bin/env python3
"""Tests for scripts/close-idle-tab.py.

The script's claim is narrow and load-bearing, so the tests pin exactly it:

  1. **Every guard refuses without closing.** A refusal that closed anyway would be worse
     than no guard — the target is a live worker.
  2. **The task gate is inverted on purpose.** `close-worker.py` refuses a non-terminal
     task; this verb refuses a *terminal* one, because a finished session is
     `close-worker.py`'s population.
  3. **Only `entrypoint: cli` reaches the pane path.** A headless worker belongs to
     `reap-idle-headless.py`; closing its pid here would leave a pane behind.

The gate is a pure function over injected readings, so the whole predicate is exercised
without a live worker, a live pane or a live registry.

Run: python3 -m unittest discover -s scripts/tests -v
"""

import importlib.util
import os
import unittest
from unittest import mock

_HERE = os.path.dirname(os.path.abspath(__file__))
_SCRIPTS = os.path.dirname(_HERE)
_spec = importlib.util.spec_from_file_location(
    "close_idle_tab", os.path.join(_SCRIPTS, "close-idle-tab.py")
)
close_idle_tab = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(close_idle_tab)

SID = "dddddddd-4444-4444-8444-444444444444"
IDLE = close_idle_tab.IDLE_FLOOR + 60


def tab_record(**over):
    rec = {
        "sessionId": SID,
        "pid": 4242,
        "entrypoint": "cli",
        "status": "idle",
        "cwd": "/tmp/worktree",
    }
    rec.update(over)
    return rec


class TestTabGate(unittest.TestCase):
    def gate(self, rec=None, idle=IDLE, cards=(), readable=True, children=(), held=None):
        rec = tab_record() if rec is None else rec
        with mock.patch.object(close_idle_tab.precheck, "pid_is_alive", return_value=True):
            return close_idle_tab.tab_gate(rec, idle, list(cards), readable, list(children), held)

    def test_clear_gate_passes(self):
        self.assertIsNone(self.gate())

    def test_hold_outranks_everything(self):
        token, _ = self.gate(held="operator is reviewing", cards=[{"item_id": "x"}])
        self.assertEqual(token, "held-session")

    def test_non_integer_pid_refuses(self):
        token, _ = self.gate(rec=tab_record(pid="nope"))
        self.assertEqual(token, "ambiguous-pid")

    def test_dead_pid_refuses(self):
        with mock.patch.object(close_idle_tab.precheck, "pid_is_alive", return_value=False):
            token, _ = close_idle_tab.tab_gate(tab_record(), IDLE, [], True, [])
        self.assertEqual(token, "already-gone")

    def test_headless_entrypoint_is_not_this_verbs(self):
        token, _ = self.gate(rec=tab_record(entrypoint="sdk-cli"))
        self.assertEqual(token, "not-tab")

    def test_missing_entrypoint_is_not_tab_either(self):
        token, _ = self.gate(rec=tab_record(entrypoint=""))
        self.assertEqual(token, "not-tab")

    def test_busy_target_refuses(self):
        token, _ = self.gate(rec=tab_record(status="busy"))
        self.assertEqual(token, "busy-target")

    def test_unknown_idle_reading_refuses(self):
        token, _ = self.gate(idle=None)
        self.assertEqual(token, "not-idle-long-enough")

    def test_short_idle_refuses(self):
        token, _ = self.gate(idle=close_idle_tab.IDLE_FLOOR - 1)
        self.assertEqual(token, "not-idle-long-enough")

    def test_unreadable_store_fails_closed(self):
        token, _ = self.gate(readable=False)
        self.assertEqual(token, "gate-held")

    def test_open_card_refuses(self):
        token, detail = self.gate(cards=[{"item_id": "abc"}])
        self.assertEqual(token, "gate-held")
        self.assertIn("abc", detail)

    def test_unreadable_process_tree_refuses(self):
        with mock.patch.object(close_idle_tab.precheck, "pid_is_alive", return_value=True):
            token, _ = close_idle_tab.tab_gate(tab_record(), IDLE, [], True, None)
        self.assertEqual(token, "watcher-alive")

    def test_surviving_child_refuses(self):
        token, detail = self.gate(children=["999"])
        self.assertEqual(token, "watcher-alive")
        self.assertIn("999", detail)


class TestTaskGate(unittest.TestCase):
    def setUp(self):
        self.today = close_idle_tab.close_worker.defer_date.as_now()

    def gate(self, fm, handed_off=False):
        return close_idle_tab.task_gate(("25 Tasks/x.md", fm, handed_off), self.today)

    def test_no_task_refuses(self):
        token, _ = close_idle_tab.task_gate(None, self.today)
        self.assertEqual(token, "task-unresolved")

    def test_parked_task_is_this_verbs_population(self):
        self.assertIsNone(self.gate({"status": "in_progress"}))

    def test_terminal_task_belongs_to_close_worker(self):
        for status in ("completed", "aborted", "hold"):
            token, _ = self.gate({"status": status})
            self.assertEqual(token, "task-terminal", status)

    def test_handed_off_task_belongs_to_close_worker(self):
        token, _ = self.gate({"status": "in_progress"}, handed_off=True)
        self.assertEqual(token, "task-terminal")

    def test_future_deferred_task_belongs_to_close_worker(self):
        token, _ = self.gate({"status": "in_progress", "defer_date": "2099-01-01"})
        self.assertEqual(token, "task-terminal")


if __name__ == "__main__":
    unittest.main()
