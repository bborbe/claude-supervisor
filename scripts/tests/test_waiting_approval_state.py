"""The waiting-approval producer refuses what the drive leg actually got wrong.

Every test here stands on a measured failure from 2026-10-03, not on a hypothetical.

The leg's `Card (1)` block handed its caller the template's own example as the value to
use — `post-batch --dedup-key "<the round's waiting-approval row set>"` — across two of
three consecutive ticks, and two rounds held the card. One tick later it handed over a
*different* wrong value: `under-target:manager-layer:5850b257b5f5ecd2`, a key belonging to
another card's branch that **round-trips cleanly**, so a recompute check passes it.

So the assertions are shaped as refusals and as separations: a key that moves when the
order moves, a key over an empty set, a key that collides across two topics, a record that
is unusable or unparseable, and a placeholder a caller must be told to derive past. Each is
a thing a hand-rolled implementation could do and this one must not.

⚠️ **Not covered: the atomicity of `write_state`.** `test_a_malformed_write_leaves_the_
previous_state_intact` passes a repeated row, which `validate_rows` rejects *before*
`write_state` is entered — so no temp file is ever created and the test proves the
validation, not the atomicity. The `os.replace` claim is inherited verbatim from
`under-target-state.py` and is untested here.
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

TOPIC = "manager-layer"
OTHER_TOPIC = "managers-spawn"
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


def key_for(rows, state_dir, topic=TOPIC):
    return run("key", "--topic", topic, *rows_args(rows), state_dir=state_dir).stdout.strip()


def read_state(state_dir, topic=TOPIC):
    with open(os.path.join(state_dir, "%s.json" % topic), encoding="utf-8") as fh:
        return json.load(fh)


class TempDirCase(unittest.TestCase):
    """The repo's cleaning idiom — `test_worker_target.py`, `test_inbox.py`."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = self.tmp.name


class KeyIsOverMembershipAndTopic(TempDirCase):
    def test_order_does_not_move_the_key(self):
        a = key_for(ROWS, self.dir)
        b = key_for(list(reversed(ROWS)), self.dir)
        self.assertTrue(a)
        self.assertEqual(a, b)

    def test_a_changed_row_changes_the_key(self):
        a, b = key_for(ROWS, self.dir), key_for(ROWS + ["Delta Row"], self.dir)
        self.assertTrue(a and b)  # guard: "" != "" would pass vacuously
        self.assertNotEqual(a, b)

    def test_a_changed_topic_changes_the_key(self):
        """The store scopes suppression on `producer_id`, which is a *session*, not a
        topic (`attention-ask.py:110,:319`) — so the topic has to be in the key or two
        topics with the same rows suppress against each other."""
        a, b = key_for(ROWS, self.dir, TOPIC), key_for(ROWS, self.dir, OTHER_TOPIC)
        self.assertTrue(a and b)  # guard: "" != "" would pass vacuously
        self.assertNotEqual(a, b)

    def test_the_key_is_bare_hex_with_no_branch_prefix(self):
        """The tick-92 discriminator: a borrowed key carries `under-target:`."""
        out = key_for(ROWS, self.dir)
        self.assertNotIn(":", out)
        self.assertNotIn("under-target", out)
        self.assertRegex(out, r"^[0-9a-f]{16}$")

    def test_the_key_differs_from_the_under_target_branch_for_the_same_rows(self):
        """The separation is the namespace, not the digest: both fold the topic in."""
        mine = key_for(ROWS, self.dir)
        theirs = subprocess.run(
            [sys.executable, UNDER_TARGET, "key", "--topic", TOPIC, *rows_args(ROWS)],
            capture_output=True, text=True,
        ).stdout.strip()
        self.assertNotEqual(mine, theirs)
        self.assertTrue(theirs.startswith("under-target:%s:" % TOPIC), theirs)
        self.assertFalse(mine.startswith("under-target"), mine)

    def test_an_empty_row_set_is_refused_with_a_reason(self):
        r = run("key", "--topic", TOPIC, state_dir=self.dir)
        self.assertEqual(2, r.returncode)
        self.assertIn("REFUSED", r.stderr)
        self.assertIn("empty row set", r.stderr)

    def test_a_repeated_row_is_refused(self):
        r = run("key", "--topic", TOPIC, *rows_args(ROWS + [ROWS[0]]), state_dir=self.dir)
        self.assertEqual(2, r.returncode)
        self.assertIn("repeats a row", r.stderr)

    def test_an_empty_row_is_refused(self):
        r = run("key", "--topic", TOPIC, "--row", "   ", state_dir=self.dir)
        self.assertEqual(2, r.returncode)
        self.assertIn("is empty", r.stderr)

    def test_a_topic_that_is_not_a_slug_is_refused(self):
        r = run("key", "--topic", "../escape", *rows_args(ROWS), state_dir=self.dir)
        self.assertEqual(2, r.returncode)
        self.assertIn("not a slug", r.stderr)


class VerifyIsTheCallersRefusalPath(TempDirCase):
    """The obligation the posting rule states, as something that can actually be run."""

    def setUp(self):
        super().setUp()
        self.key = key_for(ROWS, self.dir)

    def test_the_derived_key_verifies(self):
        r = run("verify", "--dedup-key", self.key, "--topic", TOPIC,
                *rows_args(ROWS), state_dir=self.dir)
        self.assertEqual(0, r.returncode)
        self.assertIn("OK", r.stdout)

    def test_the_template_placeholder_is_refused_and_named(self):
        """SC3's forced input — the exact string the leg handed over at ticks 90 and 91."""
        r = run("verify", "--dedup-key", PLACEHOLDER, "--topic", TOPIC,
                *rows_args(ROWS), state_dir=self.dir)
        self.assertEqual(2, r.returncode)
        self.assertIn("REFUSED", r.stderr)
        self.assertIn("placeholder", r.stderr)
        self.assertIn("waiting-approval-state.py key", r.stderr)

    def test_a_borrowed_under_target_key_is_refused(self):
        """Tick 92's shape: concrete, round-tripping, and still not this card's."""
        borrowed = subprocess.run(
            [sys.executable, UNDER_TARGET, "key", "--topic", TOPIC, *rows_args(ROWS)],
            capture_output=True, text=True,
        ).stdout.strip()
        r = run("verify", "--dedup-key", borrowed, "--topic", TOPIC,
                *rows_args(ROWS), state_dir=self.dir)
        self.assertEqual(2, r.returncode)
        self.assertIn("REFUSED", r.stderr)

    def test_a_key_over_a_different_row_set_is_refused(self):
        other = key_for(ROWS + ["Delta Row"], self.dir)
        r = run("verify", "--dedup-key", other, "--topic", TOPIC,
                *rows_args(ROWS), state_dir=self.dir)
        self.assertEqual(2, r.returncode)

    def test_a_key_over_a_different_topic_is_refused(self):
        other = key_for(ROWS, self.dir, OTHER_TOPIC)
        r = run("verify", "--dedup-key", other, "--topic", TOPIC,
                *rows_args(ROWS), state_dir=self.dir)
        self.assertEqual(2, r.returncode)

    def test_an_empty_key_is_refused(self):
        r = run("verify", "--dedup-key", "", "--topic", TOPIC,
                *rows_args(ROWS), state_dir=self.dir)
        self.assertEqual(2, r.returncode)
        self.assertIn("empty", r.stderr)

    def test_a_refusal_names_the_expected_key(self):
        r = run("verify", "--dedup-key", "deadbeefdeadbeef", "--topic", TOPIC,
                *rows_args(ROWS), state_dir=self.dir)
        self.assertIn(self.key, r.stderr)


class WriteAndRead(TempDirCase):
    def test_round_trip(self):
        w = run("write", "--topic", TOPIC, "--item-id", "abc123",
                *rows_args(ROWS), state_dir=self.dir)
        self.assertEqual(0, w.returncode)
        data = read_state(self.dir)
        self.assertEqual("abc123", data["card_item_id"])
        self.assertEqual(ROWS, data["row_set"])
        self.assertEqual(key_for(ROWS, self.dir), data["key"])

    def test_read_prints_the_row_set_by_default(self):
        """The sibling withholds it behind `--json`; a summary read incomplete is the
        defect that taught this one to print it."""
        run("write", "--topic", TOPIC, "--item-id", "abc123",
            *rows_args(ROWS), state_dir=self.dir)
        r = run("read", "--topic", TOPIC, state_dir=self.dir)
        self.assertEqual(0, r.returncode)
        for row in ROWS:
            self.assertIn(row, r.stdout)
        self.assertIn("key (derived):", r.stdout)

    def test_read_of_an_unwritten_topic_exits_three(self):
        r = run("read", "--topic", "never-written", state_dir=self.dir)
        self.assertEqual(3, r.returncode)

    def test_read_refuses_an_empty_recorded_row_set(self):
        """An empty set hashes to the sha256 of the empty string — which would print as a
        perfectly plausible key. It must refuse instead."""
        with open(os.path.join(self.dir, "%s.json" % TOPIC), "w", encoding="utf-8") as fh:
            json.dump({"card_item_id": "abc123", "key": "e3b0c44298fc1c14",
                       "row_set": [], "posted_at": "2026-10-04T00:00:00+02:00"}, fh)
        r = run("read", "--topic", TOPIC, state_dir=self.dir)
        self.assertEqual(2, r.returncode)
        self.assertIn("REFUSED", r.stderr)
        self.assertNotIn("e3b0c44298fc1c14", r.stdout)

    def test_read_json_refuses_an_empty_recorded_row_set_too(self):
        """The `--json` branch returns before the human render — it must not also return
        before the validation, or the empty-set failure is reachable through a flag."""
        with open(os.path.join(self.dir, "%s.json" % TOPIC), "w", encoding="utf-8") as fh:
            json.dump({"card_item_id": "abc123", "key": "e3b0c44298fc1c14",
                       "row_set": [], "posted_at": "2026-10-04T00:00:00+02:00"}, fh)
        r = run("read", "--topic", TOPIC, "--json", state_dir=self.dir)
        self.assertEqual(2, r.returncode)
        self.assertIn("REFUSED", r.stderr)
        self.assertNotIn("e3b0c44298fc1c14", r.stdout)

    def test_read_json_still_dumps_a_valid_record(self):
        run("write", "--topic", TOPIC, "--item-id", "abc123",
            *rows_args(ROWS), state_dir=self.dir)
        r = run("read", "--topic", TOPIC, "--json", state_dir=self.dir)
        self.assertEqual(0, r.returncode)
        self.assertEqual("abc123", json.loads(r.stdout)["card_item_id"])

    def test_read_refuses_an_unparseable_record(self):
        """A truncated file must refuse, not raise — the reader cannot tell a broken tool
        from a damaged store, and only the second is actionable."""
        with open(os.path.join(self.dir, "%s.json" % TOPIC), "w", encoding="utf-8") as fh:
            fh.write('{"card_item_id": "abc123", "row_set": ["Alpha Row"')
        r = run("read", "--topic", TOPIC, state_dir=self.dir)
        self.assertEqual(2, r.returncode)
        self.assertIn("REFUSED", r.stderr)
        self.assertNotIn("Traceback", r.stderr)

    def test_read_flags_a_recorded_key_that_disagrees_with_the_derivation(self):
        run("write", "--topic", TOPIC, "--item-id", "abc123",
            *rows_args(ROWS), state_dir=self.dir)
        data = read_state(self.dir)
        data["key"] = "0000000000000000"
        with open(os.path.join(self.dir, "%s.json" % TOPIC), "w", encoding="utf-8") as fh:
            json.dump(data, fh)
        r = run("read", "--topic", TOPIC, state_dir=self.dir)
        self.assertEqual(0, r.returncode)
        self.assertIn("disagrees", r.stdout)

    def test_a_malformed_write_leaves_the_previous_state_intact(self):
        run("write", "--topic", TOPIC, "--item-id", "first",
            *rows_args(ROWS), state_dir=self.dir)
        bad = run("write", "--topic", TOPIC, "--item-id", "second",
                  *rows_args(ROWS + [ROWS[0]]), state_dir=self.dir)
        self.assertEqual(2, bad.returncode)
        self.assertEqual("first", read_state(self.dir)["card_item_id"])

    def test_an_empty_item_id_is_refused(self):
        r = run("write", "--topic", TOPIC, "--item-id", " ",
                *rows_args(ROWS), state_dir=self.dir)
        self.assertEqual(2, r.returncode)

    def test_a_topic_that_is_not_a_slug_is_refused(self):
        r = run("write", "--topic", "../escape", "--item-id", "x",
                *rows_args(ROWS), state_dir=self.dir)
        self.assertEqual(2, r.returncode)
        self.assertIn("not a slug", r.stderr)

    def test_clear_removes_the_state(self):
        run("write", "--topic", TOPIC, "--item-id", "abc123",
            *rows_args(ROWS), state_dir=self.dir)
        self.assertEqual(0, run("clear", "--topic", TOPIC, state_dir=self.dir).returncode)
        self.assertEqual(3, run("read", "--topic", TOPIC, state_dir=self.dir).returncode)


if __name__ == "__main__":
    unittest.main()
