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
                    "--startable", "Alpha Row", state_dir=self.dir)
        self.assertEqual(0, first.returncode, first.stderr)
        path = os.path.join(self.dir, "t.json")
        with open(path, encoding="utf-8") as fh:
            before = fh.read()

        run("write", "--topic", "t", "--item-id", "", "--row", "Alpha Row",
            "--startable", "Alpha Row", state_dir=self.dir)
        with open(path, encoding="utf-8") as fh:
            after = fh.read()
        self.assertEqual(before, after, "a refused write changed the recorded state")

    def test_round_trip_and_read_exit_code(self):
        missing = run("read", "--topic", "absent", state_dir=self.dir)
        self.assertEqual(3, missing.returncode, "absent state must exit 3, not 0")

        run("write", "--topic", "t", "--item-id", "abc123", "--row", "Alpha Row",
            "--row", "Beta Row", "--startable", "Alpha Row", "--startable", "Beta Row",
            state_dir=self.dir)
        out = run("read", "--topic", "t", "--json", state_dir=self.dir)
        data = json.loads(out.stdout)
        self.assertEqual("abc123", data["card_item_id"])
        self.assertEqual(["Alpha Row", "Beta Row"], data["row_set"])
        self.assertTrue(data["posted_at"])

    def test_clear_removes_it(self):
        run("write", "--topic", "t", "--item-id", "abc123", "--row", "Alpha Row",
            "--startable", "Alpha Row", state_dir=self.dir)
        run("clear", "--topic", "t", state_dir=self.dir)
        self.assertFalse(os.path.exists(os.path.join(self.dir, "t.json")))

    def test_read_and_clear_refuse_a_path_shaped_topic(self):
        """Both build a path from --topic, so both must refuse a topic that is a path."""
        for cmd in ("read", "clear"):
            r = run(cmd, "--topic", "../../etc/passwd", state_dir=self.dir)
            self.assertEqual(2, r.returncode, f"{cmd} accepted a path-shaped topic")
            self.assertIn("REFUSED", r.stderr)


class TheSnapshotCarriesARowBitAndCompareOwnsTheVerdict(unittest.TestCase):
    """The re-post bound is a CHANGE, and only a recorded bit can show one.

    Measured 2026-10-08: the branch fired and withheld its card on five unstartable rows
    that were a subset of a declined ask. A row set alone cannot distinguish "still the
    same ask" from "the work became startable since", so the snapshot carries a bit per
    row and `compare` — never each manager's own set-difference over JSON — answers the
    branch. The arms asserted here are the ones that must not drift: an unknown verdict
    re-posts rather than suppresses, a row that became startable re-posts even though
    every row can start now, and a set that did not change does not re-post at all.
    """

    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def write(self, classification, item_id="9d179506fc62495c284d299eda235469"):
        return run("write", "--topic", "t", "--item-id", item_id,
                   *sum([["--row", r] for r in classification], []),
                   *sum([["--" + tok, r] for r, tok in classification.items()], []),
                   state_dir=self.dir)

    def compare(self, classification):
        return run("compare", "--topic", "t",
                   *sum([["--row", r] for r in classification], []),
                   *sum([["--" + tok, r] for r, tok in classification.items()], []),
                   state_dir=self.dir)

    def test_write_refuses_without_a_bit_for_every_row(self):
        """A snapshot missing its bits is the fail-closed state — not a decision."""
        r = run("write", "--topic", "t", "--item-id", "abc", "--row", "Alpha Row",
                state_dir=self.dir)
        self.assertEqual(2, r.returncode)
        self.assertIn("no startability", r.stderr)
        self.assertEqual([], os.listdir(self.dir), "a refused write left a file behind")

    def test_write_refuses_a_bit_for_a_row_outside_the_set(self):
        r = run("write", "--topic", "t", "--item-id", "abc", "--row", "Alpha Row",
                "--startable", "Alpha Row", "--startable", "Delta Row", state_dir=self.dir)
        self.assertEqual(2, r.returncode)
        self.assertIn("not in the row set", r.stderr)

    def test_write_refuses_a_row_classified_twice(self):
        r = run("write", "--topic", "t", "--item-id", "abc", "--row", "Alpha Row",
                "--startable", "Alpha Row", "--unstartable", "Alpha Row", state_dir=self.dir)
        self.assertEqual(2, r.returncode)
        self.assertIn("classified twice", r.stderr)

    def test_the_snapshot_is_per_row_and_read_prints_it(self):
        self.write({"Alpha Row": "startable", "Beta Row": "unstartable"})
        data = json.loads(run("read", "--topic", "t", "--json", state_dir=self.dir).stdout)
        self.assertEqual({"Alpha Row": "startable", "Beta Row": "unstartable"},
                         data["startability"])
        plain = run("read", "--topic", "t", state_dir=self.dir).stdout
        self.assertIn("Alpha Row=startable", plain)
        self.assertIn("Beta Row=unstartable", plain)

    def test_compare_with_no_snapshot_exits_3(self):
        """Nothing declined yet — the caller posts, and exit 3 is how it knows."""
        r = self.compare({"Alpha Row": "startable"})
        self.assertEqual(3, r.returncode, r.stderr)

    def test_an_unchanged_declined_set_suppresses(self):
        """The no-widening control: same rows, same startability, no re-post."""
        self.write({"Alpha Row": "startable", "Beta Row": "startable"})
        r = self.compare({"Alpha Row": "startable", "Beta Row": "startable"})
        self.assertEqual(0, r.returncode, r.stderr)
        self.assertIn("SUPPRESS", r.stdout)

    def test_a_row_that_became_startable_reposts(self):
        """SC1's arm — the one the row set alone could never answer."""
        self.write({"Alpha Row": "unstartable"})
        r = self.compare({"Alpha Row": "startable"})
        self.assertEqual(10, r.returncode, r.stdout + r.stderr)
        self.assertIn("was unstartable in the declined snapshot", r.stdout)

    def test_a_row_that_cannot_start_reposts(self):
        """Measured 2026-10-08: five unstartable rows withheld a card for hours."""
        self.write({"Alpha Row": "startable"})
        r = self.compare({"Alpha Row": "unstartable"})
        self.assertEqual(10, r.returncode, r.stdout + r.stderr)
        self.assertIn("cannot start this tick", r.stdout)

    def test_an_unknown_verdict_reposts_rather_than_suppressing(self):
        """A degraded audit read is asked about, never read as unstartable."""
        self.write({"Alpha Row": "startable"})
        r = self.compare({"Alpha Row": "unknown"})
        self.assertEqual(10, r.returncode, r.stdout + r.stderr)
        self.assertIn("no readable startability", r.stdout)

    def test_a_row_outside_the_declined_batch_reposts(self):
        self.write({"Alpha Row": "startable"})
        r = self.compare({"Alpha Row": "startable", "Beta Row": "startable"})
        self.assertEqual(10, r.returncode, r.stdout + r.stderr)
        self.assertIn("not a subset", r.stdout)

    def test_a_snapshot_without_bits_fails_closed(self):
        """A state written before the field existed cannot show a change — so it suppresses."""
        with open(os.path.join(self.dir, "t.json"), "w", encoding="utf-8") as fh:
            json.dump({"card_item_id": "legacy", "row_set": ["Alpha Row"],
                       "posted_at": "2026-10-08T00:00:00+02:00"}, fh)
        r = self.compare({"Alpha Row": "startable"})
        self.assertEqual(0, r.returncode, r.stdout + r.stderr)
        self.assertIn("SUPPRESS", r.stdout)

    def test_compare_refuses_a_row_it_cannot_classify(self):
        """Silence here would mark every row unstartable and re-post every tick."""
        self.write({"Alpha Row": "startable"})
        r = run("compare", "--topic", "t", "--row", "Alpha Row", state_dir=self.dir)
        self.assertEqual(2, r.returncode)
        self.assertIn("no startability", r.stderr)

    def test_a_mixed_tick_names_every_reason(self):
        """The caller puts this line in the card's `--context`, so a second reason must survive.

        A row unstartable now and a row newly startable are different facts, and returning on
        the first match would drop the second from the only place it is ever rendered.
        """
        self.write({"Alpha Row": "startable", "Beta Row": "unstartable"})
        r = self.compare({"Alpha Row": "unstartable", "Beta Row": "startable"})
        self.assertEqual(10, r.returncode, r.stdout + r.stderr)
        self.assertIn("'Alpha Row' cannot start this tick", r.stdout)
        self.assertIn("'Beta Row' was unstartable in the declined snapshot", r.stdout)

    def test_a_corrupt_state_file_is_refused_not_a_traceback(self):
        """Exit 1 is not in the contract, and a caller reads "not suppressed" as "post"."""
        for cmd, extra in (("read", []), ("compare", ["--startable", "Alpha Row"])):
            with open(os.path.join(self.dir, "t.json"), "w", encoding="utf-8") as fh:
                fh.write('{"card_item_id": "abc", "row_set": ["Alpha Row"')
            r = run(cmd, "--topic", "t", "--row", "Alpha Row", *extra, state_dir=self.dir)
            self.assertEqual(2, r.returncode, f"{cmd} did not refuse a corrupt state")
            self.assertIn("REFUSED", r.stderr)
            self.assertIn("unreadable", r.stderr)

    def test_a_state_that_is_not_an_object_is_refused(self):
        with open(os.path.join(self.dir, "t.json"), "w", encoding="utf-8") as fh:
            fh.write('["not", "an", "object"]')
        r = self.compare({"Alpha Row": "startable"})
        self.assertEqual(2, r.returncode)
        self.assertIn("not an object", r.stderr)


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
