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

    # The no-page case splits in two exactly as `reap-idle-headless.py` splits it
    # (`:783-805`): a proven absence of anchor is a close, an absent SEARCH is a refusal.

    def _gate_unanchored(self, detail, dirs, unsearched=False, config_error=None):
        with mock.patch.object(
            close_idle_tab.close_worker,
            "resolution_refusal",
            return_value=("task-unresolved", detail),
        ), mock.patch.object(
            close_idle_tab.close_worker, "fleet_task_dirs", return_value=dirs
        ), mock.patch.object(
            close_idle_tab.close_worker,
            "own_vault_unsearched",
            return_value=unsearched,
        ), mock.patch.object(
            # ⚠️ Stubbed, not left real: `unanchored_verdict` consults it FIRST, so a host
            # with no readable `vault-cli` config (CI) would refuse every case here and the
            # positive test would assert a close the code never reaches.
            close_idle_tab.close_worker,
            "vault_config_error",
            return_value=config_error,
        ):
            return close_idle_tab.task_gate(None, self.today, SID, "/tmp/worktree")

    def test_unanchored_closes_when_every_vault_was_searched(self):
        token, detail = self._gate_unanchored(
            f"no page carries {SID}", ["/v/25 Tasks"]
        )
        self.assertEqual(token, "unanchored-closed")
        self.assertIn(SID, detail)

    def test_vault_not_searched_still_refuses(self):
        # The own-vault check is structural now (`own_vault_unsearched`), not a substring
        # test on the refusal's prose — a reworded message can no longer flip the close.
        token, _ = self._gate_unanchored(
            "searched /a; /b not among them", ["/a"], unsearched=True
        )
        self.assertEqual(token, "task-unresolved")

    def test_unreadable_config_still_refuses(self):
        # A config read that FAILED is not an empty config: with `{}` the own-vault check
        # is vacuous while the root probe keeps the dir list non-empty.
        token, _ = self._gate_unanchored(
            f"no page carries {SID}", ["/v/25 Tasks"],
            config_error="vault config unreadable",
        )
        self.assertEqual(token, "task-unresolved")

    def test_empty_task_dirs_still_refuses(self):
        # An empty dir list is an absent search, not an absent anchor: without this
        # guard an empty `vault-cli` config would close the whole fleet in one tick.
        token, _ = self._gate_unanchored(f"no page carries {SID}", [])
        self.assertEqual(token, "task-unresolved")

    def test_no_page_without_a_session_id_still_refuses(self):
        # Without a session id or cwd the absence cannot be PROVEN, so the old refusal
        # stands — this is what keeps `task_gate(None, today)` honest.
        token, _ = close_idle_tab.task_gate(None, self.today)
        self.assertEqual(token, "task-unresolved")

    def test_failed_lookup_refuses_instead_of_closing(self):
        # ⚠️ `find_task` returns None on a grep TIMEOUT as well as on a genuine no-match.
        # Reading that as an absent anchor would close a live, anchored tab on a transient
        # read failure — the unreadable-vs-absent collapse the repo forbids everywhere else.
        token, detail = close_idle_tab.task_gate(
            None, self.today, SID, "/tmp/worktree", "task lookup failed (timed out)"
        )
        self.assertEqual(token, "task-unresolved")
        self.assertIn("unproven", detail)


if __name__ == "__main__":
    unittest.main()
