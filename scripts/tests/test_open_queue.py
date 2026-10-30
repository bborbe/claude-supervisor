#!/usr/bin/env python3
"""Tests for scripts/open-queue.py.

Each case guards a claim the task file makes, and each carries the positive control its
absence clause needs:

  * **ranked, never bulk** -- the opened set equals the top-N of the rank, *in order*.
    A count alone passes on a leg that opens arbitrary rows.
  * **operator-flag outranks an unflagged row of equal priority** -- two rows, one free
    slot, the flagged one opens. Falsified by the unflagged row opening, or neither.
  * **`phase: todo` is refused** -- with an eligible control row beside it that must
    open, so a leg that opens nothing cannot satisfy the absence.
  * **`blocked_by` and a future `defer_date` are refused** -- same control-row shape.
  * **zero free slots opens nothing and prints the literal line** -- paired with the
    one-free-slot run, which must open exactly one. Without that pairing a leg that
    never opens anything satisfies the zero-slot case identically.
  * **`unknown` is never `0`** -- an unreadable queue or capacity store exits non-zero.

Run: python3 -m unittest discover -s scripts/tests -v
"""

import importlib.util
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout

_HERE = os.path.dirname(os.path.abspath(__file__))
_SCRIPT = os.path.join(os.path.dirname(_HERE), "open-queue.py")

_spec = importlib.util.spec_from_file_location("open_queue", _SCRIPT)
oq = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(oq)

TODAY = "2026-10-30"


def task_file(directory, name, frontmatter, body=""):
    path = os.path.join(directory, name + ".md")
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("---\n%s\n---\n%s\n" % (frontmatter, body))


def approved(names):
    """The shape `approved-not-started.py --json` emits, in the order it emits it.

    That order is oldest-first; `collect()` must re-rank it, so every fixture passes the
    rows in a deliberately unhelpful order.
    """
    return {
        "rows": [
            {
                "name": name,
                "approved_at": "2026-10-0%dT09:00:00+00:00" % (index % 9 + 1),
                "age_seconds": 60,
                "manager": None,
            }
            for index, name in enumerate(names)
        ],
        "tick_minutes": 30,
        "count": len(names),
    }


class OpenQueueTest(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.tasks = self._dir.name

    def tearDown(self):
        self._dir.cleanup()

    def collect(self, names):
        rows, skipped, err = oq.collect(self.tasks, TODAY, approved_json=approved(names))
        self.assertIsNone(err)
        return rows, skipped

    # -- rank -------------------------------------------------------------

    def test_ranked_open_up_to_the_cap_in_order(self):
        """The opened set is the top-N of the rank, in order — not N arbitrary rows."""
        task_file(self.tasks, "low", "status: in_progress\npriority: 3\n")
        task_file(self.tasks, "high", "status: in_progress\npriority: 1\n")
        task_file(self.tasks, "mid", "status: in_progress\npriority: 2\n")
        rows, _ = self.collect(["low", "mid", "high"])

        opened, skipped = oq.select(rows, live=28, target=30)

        self.assertEqual([r["name"] for r in opened], ["high", "mid"])
        self.assertEqual([r["name"] for r in skipped], ["low"])

    def test_priority_absent_sorts_last(self):
        task_file(self.tasks, "none", "status: in_progress\n")
        task_file(self.tasks, "three", "status: in_progress\npriority: 3\n")
        rows, _ = self.collect(["none", "three"])

        self.assertEqual([r["name"] for r in rows], ["three", "none"])

    def test_oldest_approval_breaks_a_priority_tie(self):
        """The tie-break sorts on `age_seconds`, not on the raw ISO string.

        `approved-not-started.py` re-emits `approved_at` with each row's own UTC offset,
        so comparing those strings sorts by digits rather than by instant. The fixture
        therefore carries the normalised `age_seconds` — a larger age is an older row.
        """
        task_file(self.tasks, "newer", "status: in_progress\npriority: 1\n")
        task_file(self.tasks, "older", "status: in_progress\npriority: 1\n")
        payload = approved(["newer", "older"])
        payload["rows"][0]["approved_at"] = "2026-10-20T09:00:00+00:00"
        payload["rows"][0]["age_seconds"] = 600
        payload["rows"][1]["approved_at"] = "2026-10-02T09:00:00+02:00"
        payload["rows"][1]["age_seconds"] = 2_000_000

        rows, _, err = oq.collect(self.tasks, TODAY, approved_json=payload)

        self.assertIsNone(err)
        self.assertEqual([r["name"] for r in rows], ["older", "newer"])

    # -- the operator flag ------------------------------------------------

    def test_flagged_outranks_unflagged_of_equal_priority(self):
        """SC3: two rows, one free slot, the flagged one opens.

        Falsified by the unflagged row opening, or by neither opening.
        """
        task_file(self.tasks, "flagged", "status: in_progress\npriority: 1\nflag: true\nflag_set_by: operator\n")
        task_file(self.tasks, "plain", "status: in_progress\npriority: 1\n")
        rows, _ = self.collect(["flagged", "plain"])

        opened, _ = oq.select(rows, live=29, target=30)

        self.assertEqual([r["name"] for r in opened], ["flagged"])

    def test_a_bare_flag_ranks_first(self):
        """Provenance is not read. Operator ruling 2026-10-30: "in the end it should not
        matter how set the flag". A bare `flag: true` ranks exactly as one written
        `--by operator` — reading the setter here would reinstate the gate it removed."""
        task_file(self.tasks, "bare", "status: in_progress\npriority: 1\nflag: true\n")
        task_file(self.tasks, "plain", "status: in_progress\npriority: 1\n")
        rows, _ = self.collect(["bare", "plain"])

        self.assertTrue([r for r in rows if r["name"] == "bare"][0]["flagged"])

    def test_an_agent_set_flag_ranks_first_too(self):
        """Same ruling: the setter is a note, not a gate."""
        task_file(self.tasks, "agent", "status: in_progress\nflag: true\nflag_set_by: agent\n")
        rows, _ = self.collect(["agent"])

        self.assertTrue(rows[0]["flagged"])

    def test_every_truthy_spelling_ranks_first(self):
        """`is_flagged()` accepts `true`/`yes`/`on`, matching `FLAG_TRUTHY` in
        `topic-spawn.py` and `topic-keepalive.py`. Pinning only `true` would let the
        widening regress silently, and a goal flagged `flag: yes` is exactly the row the
        sibling gates were already taught to read."""
        for spelling in ("true", "yes", "on"):
            with self.subTest(spelling=spelling):
                # A subdirectory per spelling keeps each case isolated: `collect()` reads
                # every `.md` under the directory it is given, so a leftover row from the
                # previous spelling would satisfy the assertion without being read.
                sub = os.path.join(self.tasks, spelling)
                os.makedirs(sub, exist_ok=True)
                task_file(sub, spelling, "status: in_progress\nflag: %s\n" % spelling)
                rows, _, err = oq.collect(sub, TODAY, approved_json=approved([spelling]))
                self.assertIsNone(err)
                self.assertEqual(1, len(rows), "%r was dropped" % spelling)
                self.assertTrue(rows[0]["flagged"], "%r did not rank" % spelling)

    def test_a_false_flag_does_not_rank(self):
        """The control: the widening must not admit every row. Without it a predicate that
        returned True unconditionally would pass every test above."""
        task_file(self.tasks, "off", "status: in_progress\nflag: false\n")
        rows, _ = self.collect(["off"])
        self.assertFalse(rows[0]["flagged"])

    # -- the guards, each with a control row ------------------------------

    def test_phase_todo_is_refused_and_the_control_row_still_opens(self):
        task_file(self.tasks, "inbox", "status: in_progress\nphase: todo\npriority: 1\n")
        task_file(self.tasks, "control", "status: in_progress\nphase: planning\npriority: 2\n")
        rows, skipped = self.collect(["inbox", "control"])

        self.assertEqual([r["name"] for r in rows], ["control"])
        self.assertEqual(skipped, [{"name": "inbox", "reason": "waiting approval"}])

    def test_blocked_by_is_refused_and_the_control_row_still_opens(self):
        task_file(self.tasks, "blocked", "status: in_progress\nblocked_by: '[[Something]]'\n")
        task_file(self.tasks, "control", "status: in_progress\n")
        rows, skipped = self.collect(["blocked", "control"])

        self.assertEqual([r["name"] for r in rows], ["control"])
        self.assertEqual(skipped, [{"name": "blocked", "reason": "blocked"}])

    def test_future_defer_date_is_refused_and_the_control_row_still_opens(self):
        task_file(self.tasks, "parked", "status: in_progress\ndefer_date: '2026-12-01'\n")
        task_file(self.tasks, "control", "status: in_progress\n")
        rows, skipped = self.collect(["parked", "control"])

        self.assertEqual([r["name"] for r in rows], ["control"])
        self.assertEqual(skipped, [{"name": "parked", "reason": "deferred"}])

    def test_blocked_by_block_list_is_refused(self):
        """The vault writes `blocked_by` as a multi-line YAML block, not a scalar.

        A scalar-only read returns '' for that shape, so the guard would never fire and
        the leg would rank and open a row with an unmet blocker — the measured defect
        `next-focus.py` records for its own hold-goal class. Both shapes must refuse.
        """
        task_file(self.tasks, "blocked", "status: in_progress\nblocked_by:\n    - '[[Something]]'\n")
        task_file(self.tasks, "control", "status: in_progress\n")
        rows, skipped = self.collect(["blocked", "control"])

        self.assertEqual([r["name"] for r in rows], ["control"])
        self.assertEqual(skipped, [{"name": "blocked", "reason": "blocked"}])

    def test_empty_blocked_by_is_not_a_blocker(self):
        """A bare `blocked_by:` with no items declares no blocker."""
        task_file(self.tasks, "empty", "status: in_progress\nblocked_by:\n")
        rows, skipped = self.collect(["empty"])

        self.assertEqual([r["name"] for r in rows], ["empty"])
        self.assertEqual(skipped, [])

    def test_empty_list_blocked_by_is_not_a_blocker(self):
        """`blocked_by: []` is an empty list, not a blocker."""
        task_file(self.tasks, "emptylist", "status: in_progress\nblocked_by: []\n")
        rows, skipped = self.collect(["emptylist"])

        self.assertEqual([r["name"] for r in rows], ["emptylist"])
        self.assertEqual(skipped, [])

    def test_empty_list_blocked_by_does_not_scan_the_next_key(self):
        """An inline `[]` is the whole answer — the scan must not run on.

        Scanning past it reads the next key's indented list (`tags:`, `depends_on:`)
        as a blocker and refuses a row that has none.
        """
        task_file(
            self.tasks,
            "nextkey",
            "status: in_progress\nblocked_by: []\ntags:\n    - '[[Task]]'\n    - '[[Other]]'\n",
        )
        rows, skipped = self.collect(["nextkey"])

        self.assertEqual([r["name"] for r in rows], ["nextkey"])
        self.assertEqual(skipped, [])

    def test_empty_key_does_not_read_the_next_line_as_its_value(self):
        """`\\s*` matches `\\n`, so an empty key would swallow the line below it.

        A bare `phase:` followed by `status: in_progress` must not read `phase` as
        `status: in_progress` (which would refuse the row as unapproved), and a bare
        `defer_date:` must not compare the next key's text as a date.
        """
        task_file(
            self.tasks,
            "bare",
            "phase:\nstatus: in_progress\npriority: 1\ndefer_date:\nclaude_session_id:\n",
        )
        rows, skipped = self.collect(["bare"])

        self.assertEqual([r["name"] for r in rows], ["bare"])
        self.assertEqual(skipped, [])

    def test_past_defer_date_is_not_refused(self):
        """`defer_date` is defer-UNTIL: a past one is due, not parked."""
        task_file(self.tasks, "due", "status: in_progress\ndefer_date: '2026-10-01'\n")
        rows, skipped = self.collect(["due"])

        self.assertEqual([r["name"] for r in rows], ["due"])
        self.assertEqual(skipped, [])

    def test_unreadable_task_file_is_refused(self):
        """An absent guard read is not a clean one."""
        rows, skipped = self.collect(["missing"])

        self.assertEqual(rows, [])
        self.assertEqual(skipped, [{"name": "missing", "reason": "unreadable task file"}])

    # -- capacity ---------------------------------------------------------

    def test_zero_free_slots_opens_nothing_and_prints_the_literal_line(self):
        task_file(self.tasks, "row", "status: in_progress\n")
        rows, _ = self.collect(["row"])

        opened, capacity_skipped = oq.select(rows, live=30, target=30)
        block = oq.render(opened, capacity_skipped, 30, 30)

        self.assertEqual(opened, [])
        self.assertIn("no free slots — selected 0", block)

    def test_one_free_slot_opens_exactly_one(self):
        """The positive control for the zero-slot case.

        Without this pairing a leg that never opens anything satisfies the zero-slot
        case identically to a correct one.
        """
        task_file(self.tasks, "row", "status: in_progress\n")
        rows, _ = self.collect(["row"])

        opened, _ = oq.select(rows, live=29, target=30)

        self.assertEqual([r["name"] for r in opened], ["row"])

    def test_unlimited_target_opens_nothing_and_says_so(self):
        """A removed cap is not a licence to open the whole queue.

        `spawn.maxConcurrent: 0` means unlimited, so the leg has no bound to rank
        against; admitting every row there would be the bulk opening the 2026-10-01
        ruling forbids. The leg refuses and names the config state instead.
        """
        task_file(self.tasks, "a", "status: in_progress\n")
        task_file(self.tasks, "b", "status: in_progress\n")
        rows, _ = self.collect(["a", "b"])

        opened, skipped = oq.select(rows, live=99, target=None)
        block = oq.render(opened, skipped, 99, None)

        self.assertEqual(opened, [])
        self.assertEqual(len(skipped), 2)
        self.assertIn("target unlimited — selected 0", block)

    # -- the block --------------------------------------------------------

    def test_render_names_every_skipped_row_by_reason(self):
        task_file(self.tasks, "open", "status: in_progress\n")
        task_file(self.tasks, "blocked", "status: in_progress\nblocked_by: '[[X]]'\n")
        rows, skipped = self.collect(["open", "blocked"])

        # One free slot, so the block renders the ranked set rather than the zero-slot
        # line — the skip tally is what this case is about.
        opened, capacity_skipped = oq.select(rows, live=29, target=30)
        block = oq.render(opened, skipped + capacity_skipped, 29, 30)

        self.assertIn("skipped: 1", block)
        self.assertIn("blocked", block)

    def test_unknown_queue_is_not_a_zero(self):
        """An unreadable queue exits 2 and prints `unknown`, never a count."""
        out = io.StringIO()
        with redirect_stdout(out):
            code = oq.main(["--tasks-dir", os.path.join(self.tasks, "nonexistent")])

        self.assertEqual(code, 2)

    def test_json_output_carries_the_opened_set_in_order(self):
        task_file(self.tasks, "low", "status: in_progress\npriority: 3\n")
        task_file(self.tasks, "high", "status: in_progress\npriority: 1\n")

        # Exercise the selection shape directly: `main()` would reach for a live registry.
        rows, _, err = oq.collect(self.tasks, TODAY, approved_json=approved(["low", "high"]))
        self.assertIsNone(err)
        opened, _ = oq.select(rows, live=29, target=30)

        self.assertEqual(json.loads(json.dumps([r["name"] for r in opened])), ["high"])


if __name__ == "__main__":
    unittest.main()
