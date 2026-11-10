#!/usr/bin/env python3
"""Tests for scripts/declared-wait.py — the shared `⏰ Ends:` reader.

Three consumers take this read (`fleet-board.py`, `manager-predispatch.py`, and the
fleet sweep agent's `parked-on-watcher` class), so the properties worth pinning are
the ones where a plausible implementation looks right and is wrong:

  * **Newest wins.** The log is append-only, so an `⏰ Ends:` from three turns ago is
    still on disk after the wait it declared has ended. An implementation that takes
    the FIRST `Stop` record, or any of them, pins the row forever — the same defect
    `agents/manager-drive.md` records for its own limb-2 read.
  * **The slot must be NON-EMPTY.** `⏰ Ends:` with nothing after it declares no wait,
    and a substring test alone reports one.
  * **`⏰ Next:` is not the marker.** It is the general-purpose forward pointer and
    appears on rows waiting on nothing at all, so keying on it would claim every
    closed session.
  * **A failed read is not a negative.** A missing or torn log leaves the session
    ABSENT from the set — the safe direction, since every consumer uses this to
    *remove* a verdict.

Run: python3 -m unittest discover -s scripts/tests
"""

import importlib.util
import json
import os
import shutil
import tempfile
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_SCRIPT = os.path.join(os.path.dirname(_HERE), "declared-wait.py")

_spec = importlib.util.spec_from_file_location("declared_wait", _SCRIPT)
dw = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(dw)

SID = "aaaa1111-0000-0000-0000-000000000001"
OTHER = "aaaa1111-0000-0000-0000-000000000002"


def stop(detail):
    return {"type": "open", "event": "Stop", "item_id": "i", "detail": detail}


class TestDeclaredWait(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)

    def write(self, sid, *records):
        path = os.path.join(self.dir, f"{sid}.events.jsonl")
        with open(path, "w", encoding="utf-8") as f:
            for r in records:
                f.write(json.dumps(r) + "\n")

    def test_a_declared_wait_is_read(self):
        self.write(SID, stop("🟡 WAITING\n⏰ Ends: PR #12 merged (gh pr view, poll)"))
        self.assertTrue(dw.declared_wait(SID, state_dir=self.dir))

    def test_the_newest_stop_record_wins(self):
        self.write(SID, stop("⏰ Ends: an old wait"), stop("👤 You: nothing"))
        self.assertFalse(dw.declared_wait(SID, state_dir=self.dir))

    def test_the_newest_stop_record_wins_in_the_other_order(self):
        self.write(SID, stop("👤 You: nothing"), stop("⏰ Ends: the current wait"))
        self.assertTrue(dw.declared_wait(SID, state_dir=self.dir))

    def test_a_bare_marker_declares_nothing(self):
        self.write(SID, stop("⏰ Ends:"))
        self.assertFalse(dw.declared_wait(SID, state_dir=self.dir))

    def test_a_blank_slot_declares_nothing(self):
        self.write(SID, stop("⏰ Ends:    "))
        self.assertFalse(dw.declared_wait(SID, state_dir=self.dir))

    def test_next_is_not_the_marker(self):
        self.write(SID, stop("⏰ Next: wait for CI"))
        self.assertFalse(dw.declared_wait(SID, state_dir=self.dir))

    def test_no_stop_record_declares_nothing(self):
        self.write(SID, {"type": "open", "event": "Notification", "item_id": "i",
                         "detail": "⏰ Ends: not a Stop record"})
        self.assertFalse(dw.declared_wait(SID, state_dir=self.dir))

    def test_a_missing_log_is_absent_not_declared(self):
        self.assertFalse(dw.declared_wait(SID, state_dir=self.dir))

    def test_a_torn_final_line_is_tolerated(self):
        path = os.path.join(self.dir, f"{SID}.events.jsonl")
        with open(path, "w", encoding="utf-8") as f:
            f.write(json.dumps(stop("⏰ Ends: the real wait")) + "\n")
            f.write('{"type": "open", "event": "Sto')
        self.assertTrue(dw.declared_wait(SID, state_dir=self.dir))

    def test_an_empty_sid_is_not_declared(self):
        self.assertFalse(dw.declared_wait("", state_dir=self.dir))

    def test_a_non_string_detail_declares_nothing(self):
        """A dict or list `detail` is "no declaration", never an exception.

        ⚠️ Without the type guard this raises out of `declared_wait` and takes
        `fleet-board.py`'s whole render and every `manager-predispatch.py` row with it.
        This module's own failed-read contract says a malformed log leaves the session
        ABSENT from the set — it never says the read may raise.
        """
        self.write(SID, {"type": "open", "event": "Stop", "item_id": "i",
                         "detail": {"nested": "⏰ Ends: inside a dict"}})
        self.assertFalse(dw.declared_wait(SID, state_dir=self.dir))

    def test_a_non_dict_line_keeps_an_earlier_declaration(self):
        """A bare JSON string or list after a real Stop must not reset the session.

        `rec.get` on a non-dict raises, and letting that reach the outer handler discards
        the Stop record already collected — flipping the session back to "not declared",
        which is the false-positive direction this reader exists to remove.
        """
        path = os.path.join(self.dir, f"{SID}.events.jsonl")
        with open(path, "w", encoding="utf-8") as f:
            f.write(json.dumps(stop("⏰ Ends: the real wait")) + "\n")
            f.write(json.dumps("a bare string line") + "\n")
            f.write(json.dumps([1, 2, 3]) + "\n")
        self.assertTrue(dw.declared_wait(SID, state_dir=self.dir))

    def test_a_sid_carrying_a_path_separator_is_refused(self):
        """The sid is not a path, and the guard that says so is pinned here.

        ⚠️ Without a case the guard could be deleted by a future refactor with nothing
        failing — and the contract it protects is this module's own: a malformed input
        leaves the session ABSENT from the set rather than raising or reading elsewhere.
        No production caller passes a separator-bearing sid; this is defence-in-depth.
        """
        for bad in ("../x", "a/b", "a\\b"):
            self.assertFalse(dw.declared_wait(bad, state_dir=self.dir))

    def test_the_set_covers_only_declaring_sessions(self):
        self.write(SID, stop("⏰ Ends: a wait"))
        self.write(OTHER, stop("👤 You: nothing"))
        self.assertEqual(dw.declared_wait_ids(state_dir=self.dir), {SID})


NOW = 1_800_000_000.0


def stop_at(detail, ts):
    r = stop(detail)
    r["ts"] = ts
    return r


class TestDeclarationVerdict(unittest.TestCase):
    """A declaration that can no longer fire must stop suppressing the nudge.

    The `ts` on the `Stop` record is when the wait was declared, so a declaration
    CAN be expired — the "cannot expire it" gap in the module docstring was a reading
    of the slot text alone. Measured 2026-11-10: 16 of 27 idle 🟡 sessions would never
    wake — 7 named a session-bound mechanism, the rest had outlived their event.
    """

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)

    def write(self, sid, *records):
        path = os.path.join(self.dir, f"{sid}.events.jsonl")
        with open(path, "w", encoding="utf-8") as f:
            for r in records:
                f.write(json.dumps(r) + "\n")

    def verdict(self, detail, age_s):
        self.write(SID, stop_at(detail, NOW - age_s))
        return dw.declaration(SID, state_dir=self.dir, now=NOW)["verdict"]

    def test_a_fresh_external_wait_is_declared(self):
        self.assertEqual(self.verdict("⏰ Ends: PR #753 approved (gh pr view)", 600), "declared")

    def test_each_session_bound_mechanism_is_flagged(self):
        for ends in (
            "⏰ Ends: CI green (Monitor, bx3)",
            "⏰ Ends: build done (ScheduleWakeup, 1200s)",
            "⏰ Ends: retry (CronCreate, c1)",
            "⏰ Ends: watch.sh exits (run_in_background shell, b2)",
            "⏰ Ends: watch.sh exits (background Bash, x)",
            "⏰ Ends: audit returns (background sub-agent, a9)",
            "⏰ Ends: hand-back lands (background subagent)",
        ):
            with self.subTest(ends=ends):
                self.write(SID, stop_at(ends, NOW - 60))
                d = dw.declaration(SID, state_dir=self.dir, now=NOW)
                self.assertEqual(d["mechanism"], "session-bound")
                self.assertEqual(d["verdict"], "declared")

    def test_an_external_observable_is_not_flagged(self):
        self.write(SID, stop_at("⏰ Ends: PR #753 approved (gh pr view)", NOW - 60))
        self.assertEqual(dw.declaration(SID, state_dir=self.dir, now=NOW)["mechanism"],
                         "external")

    def test_an_old_session_bound_wake_expires(self):
        """The audit's WAKE-DEAD shape: the watcher died, nothing ever wakes it."""
        self.assertEqual(self.verdict("⏰ Ends: CI (Monitor, m1)", dw.MAX_AGE_S + 1),
                         "expired")

    def test_a_future_clock_holds_an_old_declaration(self):
        future = "2030-01-02T09:00+01:00"
        self.assertEqual(self.verdict(f"⏰ Ends: re-check at {future} (defer_date)",
                                      dw.MAX_AGE_S * 10), "declared")

    def test_a_future_bare_date_holds_through_that_day(self):
        self.assertEqual(self.verdict("⏰ Ends: defer_date 2030-01-02 resurfaces it",
                                      dw.MAX_AGE_S * 10), "declared")

    def test_a_past_clock_does_not_hold(self):
        """The audit's EVENT-ALREADY-HAPPENED shape: an 18:00 re-check long passed."""
        self.assertEqual(self.verdict("⏰ Ends: re-check 2020-01-01T18:00Z",
                                      dw.MAX_AGE_S + 1), "expired")

    def test_an_old_declaration_is_expired(self):
        self.assertEqual(self.verdict("⏰ Ends: PR #753 approved (gh pr view)",
                                      dw.MAX_AGE_S + 1), "expired")

    def test_a_record_without_ts_keeps_the_old_behaviour(self):
        self.write(SID, stop("⏰ Ends: PR #753 approved"))
        self.assertEqual(dw.declaration(SID, state_dir=self.dir, now=NOW)["verdict"],
                         "declared")

    def test_only_a_live_declaration_suppresses(self):
        self.write(SID, stop_at("⏰ Ends: PR merged (gh)", NOW - 60))
        self.write(OTHER, stop_at("⏰ Ends: CI (Monitor, m1)", NOW - 60))
        third = "aaaa1111-0000-0000-0000-000000000003"
        self.write(third, stop_at("⏰ Ends: PR merged (gh)", NOW - dw.MAX_AGE_S - 5))
        self.assertEqual(dw.declared_wait_ids(state_dir=self.dir, now=NOW), {SID, OTHER})

    def test_max_age_is_overridable(self):
        os.environ["SUPERVISOR_WAIT_MAX_AGE_S"] = "120"
        self.addCleanup(os.environ.pop, "SUPERVISOR_WAIT_MAX_AGE_S", None)
        self.write(SID, stop_at("⏰ Ends: PR merged (gh)", NOW - 300))
        self.assertEqual(dw.declaration(SID, state_dir=self.dir, now=NOW)["verdict"],
                         "expired")


if __name__ == "__main__":
    unittest.main()
