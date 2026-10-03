"""The waiting-approval producer refuses what the drive leg actually got wrong.

Every test here stands on a measured failure from 2026-10-03, not on a hypothetical.

The leg's `Card (1)` block handed its caller the template's own example as the value to
use — `post-batch --dedup-key "<the round's waiting-approval row set>"` — across two of
three consecutive ticks, and two rounds held the card. One tick later it handed over a
*different* wrong value: `under-target:manager-layer:5850b257b5f5ecd2`, a key belonging to
another card's branch that **round-trips cleanly**, so a recompute check passes it.

So the assertions are shaped as refusals and as separations: a key that moves when the
order moves, a key over an empty set, a key that carries another branch's namespace, a
write that half-lands, and a placeholder a caller must be told to derive past. Each is a
thing a hand-rolled implementation could do and this one must not.
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.dirname(HERE)
SCRIPT = os.path.join(SCRIPTS, "waiting-approval-state.py")
UNDER_TARGET = os.path.join(SCRIPTS, "under-target-state.py")

ROWS = ["Alpha Row", "Beta Row", "Gamma Row"]
PLACEHOLDER = "<the round's waiting-approval row set>"


def run(*args, state_dir):
    return subprocess.run(
        [sys.executable, SCRIPT, *args, "--state-dir", state_dir],
        capture_output=True, text=True,
    )


def rows_args(rows):
    out = []
    for r in rows:
        out += ["--row", r]
    return out


def read_state(state_dir, topic="manager-layer"):
    with open(os.path.join(state_dir, "%s.json" % topic), encoding="utf-8") as fh:
        return json.load(fh)


class KeyIsOverMembership(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def test_order_does_not_move_the_key(self):
        a = run("key", *rows_args(ROWS), state_dir=self.dir)
        b = run("key", *rows_args(list(reversed(ROWS))), state_dir=self.dir)
        self.assertEqual(0, a.returncode)
        self.assertEqual(a.stdout.strip(), b.stdout.strip())

    def test_a_changed_row_changes_the_key(self):
        a = run("key", *rows_args(ROWS), state_dir=self.dir)
        b = run("key", *rows_args(ROWS + ["Delta Row"]), state_dir=self.dir)
        self.assertNotEqual(a.stdout.strip(), b.stdout.strip())

    def test_the_key_is_bare_hex_with_no_branch_prefix(self):
        """The tick-92 discriminator: a borrowed key carries `under-target:`."""
        out = run("key", *rows_args(ROWS), state_dir=self.dir).stdout.strip()
        self.assertNotIn(":", out)
        self.assertNotIn("under-target", out)
        self.assertRegex(out, r"^[0-9a-f]{16}$")

    def test_the_key_differs_from_the_under_target_branch_for_the_same_rows(self):
        mine = run("key", *rows_args(ROWS), state_dir=self.dir).stdout.strip()
        theirs = subprocess.run(
            [sys.executable, UNDER_TARGET, "key", "--topic", "manager-layer", *rows_args(ROWS)],
            capture_output=True, text=True,
        ).stdout.strip()
        self.assertNotEqual(mine, theirs)
        # …and the digest half is the same derivation, so the separation is the namespace.
        self.assertTrue(theirs.endswith(mine), theirs)

    def test_an_empty_row_set_is_refused_with_a_reason(self):
        r = run("key", state_dir=self.dir)
        self.assertEqual(2, r.returncode)
        self.assertIn("REFUSED", r.stderr)
        self.assertIn("empty row set", r.stderr)

    def test_a_repeated_row_is_refused(self):
        r = run("key", *rows_args(ROWS + [ROWS[0]]), state_dir=self.dir)
        self.assertEqual(2, r.returncode)
        self.assertIn("repeats a row", r.stderr)

    def test_an_empty_row_is_refused(self):
        r = run("key", "--row", "   ", state_dir=self.dir)
        self.assertEqual(2, r.returncode)
        self.assertIn("is empty", r.stderr)


class VerifyIsTheCallersRefusalPath(unittest.TestCase):
    """The obligation the posting rule states, as something that can actually be run."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.key = run("key", *rows_args(ROWS), state_dir=self.dir).stdout.strip()

    def test_the_derived_key_verifies(self):
        r = run("verify", "--dedup-key", self.key, *rows_args(ROWS), state_dir=self.dir)
        self.assertEqual(0, r.returncode)
        self.assertIn("OK", r.stdout)

    def test_the_template_placeholder_is_refused_and_named(self):
        """SC3's forced input — the exact string the leg handed over at ticks 90 and 91."""
        r = run("verify", "--dedup-key", PLACEHOLDER, *rows_args(ROWS), state_dir=self.dir)
        self.assertEqual(2, r.returncode)
        self.assertIn("REFUSED", r.stderr)
        self.assertIn("placeholder", r.stderr)
        self.assertIn("waiting-approval-state.py key", r.stderr)

    def test_a_borrowed_under_target_key_is_refused(self):
        """Tick 92's shape: concrete, round-tripping, and still not this card's."""
        borrowed = subprocess.run(
            [sys.executable, UNDER_TARGET, "key", "--topic", "manager-layer", *rows_args(ROWS)],
            capture_output=True, text=True,
        ).stdout.strip()
        r = run("verify", "--dedup-key", borrowed, *rows_args(ROWS), state_dir=self.dir)
        self.assertEqual(2, r.returncode)
        self.assertIn("REFUSED", r.stderr)

    def test_a_key_over_a_different_row_set_is_refused(self):
        other = run("key", *rows_args(ROWS + ["Delta Row"]), state_dir=self.dir).stdout.strip()
        r = run("verify", "--dedup-key", other, *rows_args(ROWS), state_dir=self.dir)
        self.assertEqual(2, r.returncode)

    def test_an_empty_key_is_refused(self):
        r = run("verify", "--dedup-key", "", *rows_args(ROWS), state_dir=self.dir)
        self.assertEqual(2, r.returncode)
        self.assertIn("empty", r.stderr)

    def test_a_refusal_names_the_expected_key(self):
        r = run("verify", "--dedup-key", "deadbeefdeadbeef", *rows_args(ROWS), state_dir=self.dir)
        self.assertIn(self.key, r.stderr)


class WriteAndRead(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def test_round_trip(self):
        w = run("write", "--topic", "manager-layer", "--item-id", "abc123",
                *rows_args(ROWS), state_dir=self.dir)
        self.assertEqual(0, w.returncode)
        data = read_state(self.dir)
        self.assertEqual("abc123", data["card_item_id"])
        self.assertEqual(ROWS, data["row_set"])
        self.assertEqual(run("key", *rows_args(ROWS), state_dir=self.dir).stdout.strip(),
                         data["key"])

    def test_read_prints_the_row_set_by_default(self):
        """The sibling script withholds it behind `--json`; a summary read incomplete is
        the defect that taught this one to print it."""
        run("write", "--topic", "manager-layer", "--item-id", "abc123",
            *rows_args(ROWS), state_dir=self.dir)
        r = run("read", "--topic", "manager-layer", state_dir=self.dir)
        self.assertEqual(0, r.returncode)
        for row in ROWS:
            self.assertIn(row, r.stdout)
        self.assertIn("key (derived):", r.stdout)

    def test_read_of_an_unwritten_topic_exits_three(self):
        r = run("read", "--topic", "never-written", state_dir=self.dir)
        self.assertEqual(3, r.returncode)

    def test_a_malformed_write_leaves_the_previous_state_intact(self):
        run("write", "--topic", "manager-layer", "--item-id", "first",
            *rows_args(ROWS), state_dir=self.dir)
        bad = run("write", "--topic", "manager-layer", "--item-id", "second",
                  *rows_args(ROWS + [ROWS[0]]), state_dir=self.dir)
        self.assertEqual(2, bad.returncode)
        self.assertEqual("first", read_state(self.dir)["card_item_id"])

    def test_an_empty_item_id_is_refused(self):
        r = run("write", "--topic", "manager-layer", "--item-id", " ",
                *rows_args(ROWS), state_dir=self.dir)
        self.assertEqual(2, r.returncode)

    def test_a_topic_that_is_not_a_slug_is_refused(self):
        r = run("write", "--topic", "../escape", "--item-id", "x",
                *rows_args(ROWS), state_dir=self.dir)
        self.assertEqual(2, r.returncode)
        self.assertIn("not a slug", r.stderr)

    def test_clear_removes_the_state(self):
        run("write", "--topic", "manager-layer", "--item-id", "abc123",
            *rows_args(ROWS), state_dir=self.dir)
        self.assertEqual(0, run("clear", "--topic", "manager-layer",
                                state_dir=self.dir).returncode)
        self.assertEqual(3, run("read", "--topic", "manager-layer",
                                state_dir=self.dir).returncode)


if __name__ == "__main__":
    unittest.main()
