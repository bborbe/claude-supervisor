#!/usr/bin/env python3
"""Tests for scripts/fleet-board.py.

Covers the two things the board must not get wrong, both of which fail silently
in production:

  * **Every bucket consults a signal the registry status cannot supply.** A stub
    that maps `busy`->running, `idle`->idle, else->problem passes a naive reading
    of the design and is wrong: it has no `needs-input` output at all, and it
    reports a stuck `busy` session as `running`. The fixtures below make both
    failures observable — `GATED` is `idle` and must still reach `needs-input`,
    and `STUCK` is `busy` and must reach `problem` rather than `running`.

  * **No row is silently dropped.** The board's whole reason to exist is that the
    previous join was done in prose and could miss a session without saying so.
    `coverage_errors()` is the positive control, and
    `test_coverage_control_fails_on_a_dropped_row` proves it can fail — a control
    that has never failed is indistinguishable from one that cannot.

The fixtures are `constructed`: hand-written to exercise the branches, carrying no
claim about the live fleet. The live claims belong to `# Results` on the task page,
which records a real run against the real registry.

Run: python3 -m unittest discover -s scripts/tests -v
"""

import importlib.util
import os
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_SCRIPT = os.path.join(os.path.dirname(_HERE), "fleet-board.py")

_spec = importlib.util.spec_from_file_location("fleet_board", _SCRIPT)
fb = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fb)

CWD = "/Users/x/Documents/Obsidian/Personal"


def _sid(n):
    return f"aaaabbbb-0000-0000-0000-00000000000{n}"


# `constructed` — one session of each kind, plus the two collision cases.
REGISTRY = {
    _sid(1): {"status": "busy", "name": "Busy One", "cwd": CWD},
    _sid(2): {"status": "idle", "name": "Idle One", "cwd": CWD},
    _sid(3): {"status": "idle", "name": "Gated One", "cwd": CWD},
    _sid(4): {"status": "busy", "name": "Stuck One", "cwd": CWD},
    _sid(5): {"status": "waiting", "name": "Waiting One", "cwd": CWD},
    _sid(6): {"status": "busy", "name": "Busy And Gated", "cwd": CWD},
}
GATES = {_sid(3), _sid(6)}
STUCK = {_sid(4)}
AGES = {sid: 5.0 for sid in REGISTRY}
TITLES = {_sid(1): ["Some Task"]}


def buckets(registry=None, gates=None, stuck=None):
    rows, _ = fb.build_rows(
        registry if registry is not None else REGISTRY,
        GATES if gates is None else gates,
        STUCK if stuck is None else stuck,
        TITLES,
        AGES,
    )
    return {r["session_id"]: r["bucket"] for r in rows}


class TestBuckets(unittest.TestCase):
    def test_each_bucket_is_reachable(self):
        """One session of each kind lands in its own bucket."""
        b = buckets()
        self.assertEqual(b[_sid(1)], "running")
        self.assertEqual(b[_sid(2)], "idle")
        self.assertEqual(b[_sid(3)], "needs-input")
        self.assertEqual(b[_sid(4)], "problem")
        self.assertEqual(sorted(set(b.values())), ["idle", "needs-input", "problem", "running"])

    def test_problem_outranks_running(self):
        """A `busy` session past --stuck-min is `problem`, never `running`.

        The collision case. Without it the precedence rule is prose, and a stub
        that reads only `status` reports a wedged session as working.
        """
        self.assertEqual(buckets()[_sid(4)], "problem")

    def test_needs_input_outranks_running(self):
        """A `busy` session holding an open gate is `needs-input` — a gate blocks
        whatever the status field says."""
        self.assertEqual(buckets()[_sid(6)], "needs-input")

    def test_a_gate_on_an_idle_session_is_needs_input(self):
        """The second signal is independent of status: `idle` alone would say
        nothing is wanted, and the gate says otherwise."""
        b = buckets(gates=set())
        self.assertEqual(b[_sid(3)], "idle", "without a gate the same session is idle")
        self.assertEqual(buckets()[_sid(3)], "needs-input")

    def test_waiting_falls_through_to_idle(self):
        """`waiting` is transient and explicitly NOT blocked-on-a-human, so it
        must not invent a fifth bucket — the classification has to be total."""
        self.assertEqual(buckets()[_sid(5)], "idle")

    def test_a_status_only_stub_cannot_pass(self):
        """The lazy implementation, run against the same fixtures, gets two rows
        wrong — so the suite fails it rather than blessing it."""

        def stub(status):
            return "running" if status in ("busy", "shell") else "idle"

        real = buckets()
        lazy = {sid: stub(rec["status"]) for sid, rec in REGISTRY.items()}
        self.assertNotEqual(real, lazy)
        self.assertEqual(lazy[_sid(4)], "running", "the stub calls the stuck session running")
        self.assertEqual(lazy[_sid(3)], "idle", "the stub misses the gate entirely")

    def test_every_registry_entry_gets_exactly_one_row(self):
        rows, _ = fb.build_rows(REGISTRY, GATES, STUCK, TITLES, AGES)
        self.assertEqual(len(rows), len(REGISTRY))
        self.assertEqual(len({r["session_id"] for r in rows}), len(REGISTRY))

    def test_an_absent_transcript_renders_as_absent(self):
        """`session_transcript_age()` answers `inf` for no transcript, which is not
        a duration — it must not reach `human_age()` and must not print `inf`."""
        rows, _ = fb.build_rows(REGISTRY, GATES, STUCK, TITLES, {_sid(1): float("inf")})
        cell = next(r for r in rows if r["session_id"] == _sid(1))["cells"][-1]
        self.assertEqual(cell, "—")

    def test_rows_render_through_box_table(self):
        """The document is valid box-table input, and no cell overruns its width."""
        rows, _ = fb.build_rows(REGISTRY, GATES, STUCK, TITLES, AGES)
        for r in rows:
            self.assertEqual(len(r["cells"]), len(fb.HEADER))
            for cell, w in zip(r["cells"], fb.WIDTHS):
                self.assertLessEqual(_dwidth(cell), w, f"cell {cell!r} overruns its column")


def _dwidth(s):
    """Display width, mirroring box-table.py — emoji occupy two cells."""
    import unicodedata

    total = 0
    for i, ch in enumerate(s):
        w = 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
        if w == 1 and i + 1 < len(s) and s[i + 1] == "️":
            w = 2
        total += w
    return total


class TestCoverageControl(unittest.TestCase):
    """SC3 — the assertion that makes a silently-dropped row impossible."""

    def setUp(self):
        self.registry_ids = set(REGISTRY)
        self.row_ids = list(self.registry_ids)
        self.fresh = {_sid(1), _sid(2)}

    def test_a_complete_board_passes(self):
        self.assertEqual(fb.coverage_errors(self.row_ids, self.registry_ids, self.fresh), [])

    def test_a_dropped_row_fails(self):
        """The positive control. If this cannot fail, the assertion proves nothing."""
        errors = fb.coverage_errors(self.row_ids[:-1], self.registry_ids, self.fresh)
        self.assertTrue(errors, "a dropped registry entry must be an error")

    def test_a_dropped_transcript_fresh_row_fails(self):
        """(b): the independently-derived half. A fresh session the registry
        carries but the rows omit is exactly the silent-drop failure."""
        errors = fb.coverage_errors(
            [i for i in self.row_ids if i != _sid(1)], self.registry_ids, self.fresh
        )
        self.assertTrue(any("transcript-fresh" in e for e in errors))

    def test_a_duplicated_row_fails(self):
        errors = fb.coverage_errors(self.row_ids + [_sid(1)], self.registry_ids, self.fresh)
        self.assertTrue(any("duplicate" in e for e in errors))

    def test_a_residual_is_not_an_error(self):
        """(c): a transcript-fresh session holding no registry entry is a real
        population — a headless worker holds no entry at all — so it is reported,
        never treated as a coverage failure and never asserted equal."""
        fresh = self.fresh | {"ccccdddd-0000-0000-0000-000000000099"}
        self.assertEqual(fb.coverage_errors(self.row_ids, self.registry_ids, fresh), [])


if __name__ == "__main__":
    unittest.main()
