#!/usr/bin/env python3
"""Tests for scripts/parked-lever.py.

The fixture exists because the row's whole value is that it CANNOT be filled in
with a vague answer. Three properties, each of which a prose rule would pass
while the round still printed an unactionable row:

1. **"the fleet" and "CI" are refused as levers.** They are the two strings a
   round reaches for when it does not know, and they read exactly like an
   answer — so the operator still opens the pane. Refusal is the point.

2. **The owner taxonomy is closed.** `manager` / `operator` / `machine: <watcher>`
   and nothing else. An owner outside it ("the fleet", "nobody") cannot be
   acted on by anyone.

3. **Every row carries all four fields.** A row missing its wait, its lever or
   its owner is not a scan row; it is a sentence that looks like one.

Run: python3 -m unittest discover -s scripts/tests -v
"""

import importlib.util
import os
import unittest
from unittest import mock

_HERE = os.path.dirname(os.path.abspath(__file__))
_SCRIPTS = os.path.dirname(_HERE)


def _load(name, filename):
    spec = importlib.util.spec_from_file_location(name, os.path.join(_SCRIPTS, filename))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


pl = _load("parked_lever", "parked-lever.py")


class RejectedLevers(unittest.TestCase):
    def test_the_fleet_is_not_a_lever(self):
        with self.assertRaises(pl.LeverError):
            pl.validate_lever("the fleet")

    def test_ci_is_not_a_lever(self):
        for raw in ("CI", "ci", "the CI"):
            with self.subTest(raw=raw), self.assertRaises(pl.LeverError):
                pl.validate_lever(raw)

    def test_restating_the_wait_is_not_a_lever(self):
        for raw in ("wait", "time", "eventually", "someone"):
            with self.subTest(raw=raw), self.assertRaises(pl.LeverError):
                pl.validate_lever(raw)

    def test_a_concrete_act_is_a_lever(self):
        for raw in (
            "nudge the worker with the disk evidence",
            "re-arm the attention watcher",
            "reap: send the close evidence",
            "approve the todo row",
        ):
            with self.subTest(raw=raw):
                self.assertEqual(pl.validate_lever(raw), raw)

    def test_empty_lever_refused(self):
        with self.assertRaises(pl.LeverError):
            pl.validate_lever("   ")


class OwnerTaxonomy(unittest.TestCase):
    def test_manager_and_operator(self):
        self.assertEqual(pl.validate_owner("manager"), "manager")
        self.assertEqual(pl.validate_owner("Operator"), "operator")

    def test_machine_names_its_watcher(self):
        self.assertEqual(
            pl.validate_owner("machine: attention-watcher"),
            "machine: attention-watcher",
        )

    def test_machine_without_a_watcher_refused(self):
        with self.assertRaises(pl.LeverError):
            pl.validate_owner("machine:")
        with self.assertRaises(pl.LeverError):
            pl.validate_owner("machine:   ")

    def test_machine_naming_a_non_watcher_refused(self):
        with self.assertRaises(pl.LeverError):
            pl.validate_owner("machine: the fleet")

    def test_unknown_owner_refused(self):
        for raw in ("the fleet", "nobody", "everyone", "ops"):
            with self.subTest(raw=raw), self.assertRaises(pl.LeverError):
                pl.validate_owner(raw)

    def test_empty_owner_refused(self):
        with self.assertRaises(pl.LeverError):
            pl.validate_owner("")


class RowShape(unittest.TestCase):
    def test_all_four_fields_present(self):
        with mock.patch.object(pl, "jump_link", return_value="jump:123"):
            row = pl.render("123", "an approve: card", "nudge it", "manager")
        self.assertEqual(
            row,
            "jump:123 · waits: an approve: card · lever: nudge it · owner: manager",
        )

    def test_fields_are_separated_consistently(self):
        with mock.patch.object(pl, "jump_link", return_value="jump:1"):
            row = pl.render("1", "a gate", "approve it", "operator")
        self.assertEqual(row.count(pl.FIELD_SEP), 3)

    def test_empty_waits_refused(self):
        with self.assertRaises(pl.LeverError):
            pl.render("-", "  ", "nudge it", "manager")

    def test_headless_has_no_dead_link(self):
        row = pl.render("-", "a gate", "nudge it", "manager")
        self.assertTrue(row.startswith("no pane (headless)"))

    def test_unresolvable_jump_falls_back_to_the_command(self):
        with mock.patch.object(pl.subprocess, "run", side_effect=OSError("no wezterm")):
            self.assertEqual(pl.jump_link("77"), "/supervisor:jump 77")


class CommandLine(unittest.TestCase):
    def test_render_exits_zero(self):
        with mock.patch.object(pl, "jump_link", return_value="jump:1"):
            self.assertEqual(
                pl.main(["--pane", "1", "--waits", "a gate",
                         "--lever", "nudge it", "--owner", "manager"]),
                0,
            )

    def test_refusal_exits_two(self):
        self.assertEqual(
            pl.main(["--pane", "1", "--waits", "a gate",
                     "--lever", "the fleet", "--owner", "manager"]),
            2,
        )


if __name__ == "__main__":
    unittest.main()
