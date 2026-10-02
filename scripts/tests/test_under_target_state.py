"""The under-target state writer refuses what the fleet actually got wrong.

Every test here stands on a measured failure from 2026-10-02, not on a hypothetical.
Four managers wrote four different `--dedup-key` derivations — two of them carrying a
timestamp, so a re-post of an unchanged set produced a *different* key, the store's
open-scoped suppression never matched, and the operator was asked the same question
twice. Separately, one suppressed tick printed a line that dropped the withheld row
names, and another dropped the capacity line and the ranked rows along with the ask —
the one thing the rule says suppression must never take.

So the assertions are shaped as refusals: a key that moves when the order moves, a key
over an empty set, a write that half-lands, a suppression line missing its information.
Each is a thing a hand-rolled implementation could do and this one must not.
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.dirname(HERE)
SCRIPT = os.path.join(SCRIPTS, "under-target-state.py")

ROWS = ["Alpha Row", "Beta Row", "Gamma Row"]


def run(*args, state_dir):
    return subprocess.run(
        [sys.executable, SCRIPT, *args, "--state-dir", state_dir],
        capture_output=True, text=True,
    )


class KeyIsOverMembership(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def test_order_does_not_move_the_key(self):
        """A re-ranking naming the same rows is not a changed ask."""
        a = run("key", "--topic", "t", *sum([["--row", r] for r in ROWS], []), state_dir=self.dir)
        b = run("key", "--topic", "t",
                *sum([["--row", r] for r in reversed(ROWS)], []), state_dir=self.dir)
        self.assertEqual(0, a.returncode, a.stderr)
        self.assertEqual(a.stdout, b.stdout, "reordering the same set changed the key")

    def test_a_different_set_is_a_different_key(self):
        a = run("key", "--topic", "t", "--row", "Alpha Row", "--row", "Beta Row",
                state_dir=self.dir)
        b = run("key", "--topic", "t", "--row", "Alpha Row", "--row", "Delta Row",
                state_dir=self.dir)
        self.assertNotEqual(a.stdout, b.stdout)

    def test_key_carries_no_timestamp(self):
        """The live defect: a key containing a date changes every minute."""
        out = run("key", "--topic", "t", *sum([["--row", r] for r in ROWS], []),
                  state_dir=self.dir).stdout
        for marker in ("2026", "T17:", ":"):
            if marker == ":":
                continue
            self.assertNotIn(marker, out, f"key carries {marker!r} — not set-pure")

    def test_empty_row_set_is_refused(self):
        r = run("key", "--topic", "t", state_dir=self.dir)
        self.assertEqual(2, r.returncode)
        self.assertIn("REFUSED", r.stderr)

    def test_repeated_row_is_refused(self):
        r = run("key", "--topic", "t", "--row", "Alpha Row", "--row", "Alpha Row",
                state_dir=self.dir)
        self.assertEqual(2, r.returncode)
        self.assertIn("repeats", r.stderr)

    def test_non_slug_topic_is_refused(self):
        r = run("key", "--topic", "Not A Slug", "--row", "Alpha Row", state_dir=self.dir)
        self.assertEqual(2, r.returncode)


class WriteRefusesRatherThanHalfLanding(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def test_empty_item_id_writes_nothing(self):
        r = run("write", "--topic", "t", "--item-id", " ", "--row", "Alpha Row",
                state_dir=self.dir)
        self.assertEqual(2, r.returncode)
        self.assertEqual([], os.listdir(self.dir), "a refused write left a file behind")

    def test_refused_write_leaves_previous_state_intact(self):
        """The sibling discipline: a bad write must not truncate a good state."""
        first = run("write", "--topic", "t", "--item-id", "good-id", "--row", "Alpha Row",
                    state_dir=self.dir)
        self.assertEqual(0, first.returncode, first.stderr)
        path = os.path.join(self.dir, "t.json")
        with open(path, encoding="utf-8") as fh:
            before = fh.read()

        run("write", "--topic", "t", "--item-id", "", "--row", "Alpha Row", state_dir=self.dir)
        with open(path, encoding="utf-8") as fh:
            after = fh.read()
        self.assertEqual(before, after, "a refused write changed the recorded state")

    def test_round_trip_and_read_exit_code(self):
        missing = run("read", "--topic", "absent", state_dir=self.dir)
        self.assertEqual(3, missing.returncode, "absent state must exit 3, not 0")

        run("write", "--topic", "t", "--item-id", "abc123", "--row", "Alpha Row",
            "--row", "Beta Row", state_dir=self.dir)
        out = run("read", "--topic", "t", "--json", state_dir=self.dir)
        data = json.loads(out.stdout)
        self.assertEqual("abc123", data["card_item_id"])
        self.assertEqual(["Alpha Row", "Beta Row"], data["row_set"])
        self.assertTrue(data["posted_at"])

    def test_clear_removes_it(self):
        run("write", "--topic", "t", "--item-id", "abc123", "--row", "Alpha Row",
            state_dir=self.dir)
        run("clear", "--topic", "t", state_dir=self.dir)
        self.assertFalse(os.path.exists(os.path.join(self.dir, "t.json")))


class SuppressionLineKeepsTheInformation(unittest.TestCase):
    """SC3 and SC5 in one place: the line names the ask, and never withholds the rest."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def test_line_carries_id_names_capacity_and_rows(self):
        r = run("suppress", "--topic", "t", "--item-id", "9d179506fc62495c284d299eda235469",
                "--live", "15", "--target", "20",
                *sum([["--row", x] for x in ROWS], []), state_dir=self.dir)
        self.assertEqual(0, r.returncode, r.stderr)
        out = r.stdout
        self.assertIn("under-target: suppressed", out)
        self.assertIn("9d179506fc62495c284d299eda235469", out, "the item id is abbreviated")
        for row in ROWS:
            self.assertIn(row, out, f"row {row!r} is not named — SC3's failure")
        self.assertIn("workers: 15/20", out, "the capacity line is missing — SC5's failure")
        self.assertIn("ranked:", out, "the ranked rows are missing — SC5's failure")

    def test_suppress_without_capacity_is_refused(self):
        """A suppressed tick with no capacity line cannot be told from an empty one."""
        r = run("suppress", "--topic", "t", "--item-id", "abc", "--row", "Alpha Row",
                state_dir=self.dir)
        self.assertNotEqual(0, r.returncode)


if __name__ == "__main__":
    unittest.main()
