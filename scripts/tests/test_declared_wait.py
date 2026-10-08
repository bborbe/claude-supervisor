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
        self.write(SID, stop("🟡 WAITING\n⏰ Ends: watch.sh (background Bash, x)"))
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


if __name__ == "__main__":
    unittest.main()
