"""The detected→verified share: the numerator must EXCLUDE an action that moved nothing.

The load-bearing test is `test_action_without_movement_counts_as_unverified`. A share line
whose shape is all that is checked can be a hardcoded ratio; the only thing that
distinguishes a computed one is a case where the numerator and the denominator disagree
for a stated reason, and a case where an action was taken and still does not verify.
"""
import importlib.util
import contextlib
import io
import os
import pathlib
import shutil
import tempfile
import unittest

SCRIPT = pathlib.Path(__file__).resolve().parent.parent / "drive-share.py"

_spec = importlib.util.spec_from_file_location("drive_share", SCRIPT)
drive_share = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(drive_share)

DIGEST = """\
FLEET SWEEP — 4 sessions
CLASSIFICATION  4 classified · progressing=1 · parked=2 · stalled=1
  Task A [aaaaaaaa] · idle · 12m · parked · Task A.md · 3 · /supervisor:jump 700
  Task B [bbbbbbbb] · busy · 40m · stalled · Task B.md · 2 · /supervisor:jump 701
  Task C [cccccccc] · idle · 2m · parked · Task C.md · 1 · /supervisor:jump 702
  Task D [dddddddd] · busy · 1m · progressing · Task D.md · 4 · /supervisor:jump 703
BLOCKED (feed, raised — not verified open)
  (none)
"""

ROUND = "2026-10-22T10-00-00Z"
START = drive_share.round_epoch(ROUND)

#: A digest captured from live round `2026-10-22T18:35:05Z`, trimmed to the rows that
#: mattered: `name · state · CLASS · task · boxes · link`, with no age column. The fleet
#: alternates between this shape and the age-column one above -- both were observed on the
#: same afternoon -- which is why the parser must locate the class rather than index it.
#: ⚠️ `agents/fleet-sweep-reader.md:142` still specifies `<status> · <age> · <class>`; the
#: render does not always emit the age, so the spec and the render disagree. This fixture
#: records what was observed, not what the spec says.
REAL_DIGEST = """\
DIGEST 2026-10-22T18:35:05Z · 3 sessions
CLASSIFICATION  3 classified · progressing=1 · parked=2 · finished=0 · unclassified=0
  Parked One [11111111] · idle · parked · Task One.md · 3 · http://127.0.0.1:1337/jump?pane=1
  Parked Two [22222222] · shell · parked · — · — · http://127.0.0.1:1337/jump?pane=2
  Moving Three [33333333] · busy · progressing · Task Three.md · 1 · http://127.0.0.1:1337/jump?pane=3
BLOCKED (feed, raised — not verified open)
  (none)
"""


def entry(sid, **over):
    row = {
        "session_id": sid,
        "name": "Task %s" % sid[:4],
        "verdict": "revive",
        "open_boxes": 2,
        "probe": "",
        "nudge": "none",
        "suppression": "none",
        "task_file": "",
        "reprobe_verdict": "",
        "reprobe_probe": "",
        "unverifiable_streak": 0,
        "unverifiable_round": "",
        "unverifiable_action": "",
    }
    row.update(over)
    return row


class Fixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)
        self.digest = os.path.join(self.tmp, "round.digest")
        with open(self.digest, "w", encoding="utf-8") as handle:
            handle.write(DIGEST)

    def measure(self, entries, moved=(), task_changed=(), sent=None):
        def transcript_ts(sid):
            return START + 60 if sid in moved else START - 60

        def task_mtime(path):
            if not path:
                return None
            return START + 60 if path in task_changed else START - 60

        return drive_share.measure(
            drive_share.digest_classes(self.digest),
            entries,
            START,
            transcript_ts,
            task_mtime,
            sent=sent,
        )


class TestShare(Fixture):
    def test_empty_digest_prints_the_n_a_form(self):
        empty = os.path.join(self.tmp, "empty.digest")
        with open(empty, "w", encoding="utf-8") as handle:
            handle.write("FLEET SWEEP — 0 sessions\nCLASSIFICATION  UNKNOWN (pass not run)\n")
        rows = drive_share.measure(
            drive_share.digest_classes(empty), [], START, lambda s: None, lambda p: None
        )
        self.assertEqual(drive_share.render(rows).splitlines()[-1], "share: 0/0 (n/a)")

    def test_detected_counts_only_problem_classes(self):
        rows = self.measure([])
        # aaaa parked, bbbb stalled, cccc parked -- dddd progressing is NOT detected.
        self.assertEqual(len(rows), 3)
        self.assertEqual({row["sid"] for row in rows}, {"aaaaaaaa", "bbbbbbbb", "cccccccc"})

    def test_action_without_movement_counts_as_unverified(self):
        """The discriminating case: an action was taken, the session did not move.

        A build that counts every action as verified reports 3/3 here; the honest count is
        1/3, because only bbbb's session moved after the round start.
        """
        rows = self.measure(
            [
                entry("aaaaaaaa", nudge="drafted"),
                entry("bbbbbbbb", nudge="drafted"),
                entry("cccccccc", nudge="drafted"),
            ],
            moved=("bbbbbbbb",),
        )
        self.assertEqual([row["verified"] for row in rows], [False, True, False])
        self.assertEqual(drive_share.render(rows).splitlines()[-1], "share: 1/3 (33%)")

    def test_no_action_never_verifies(self):
        """Movement alone is not verification -- a row nobody acted on stays out."""
        rows = self.measure([entry("aaaaaaaa", nudge="none")], moved=("aaaaaaaa",))
        aaaa = [row for row in rows if row["sid"] == "aaaaaaaa"][0]
        self.assertFalse(aaaa["verified"])
        self.assertEqual(aaaa["evidence"], "no action taken")

    def test_task_file_change_verifies_too(self):
        rows = self.measure(
            [entry("aaaaaaaa", nudge="drafted", task_file="/vault/25 Tasks/Task A.md")],
            task_changed=("/vault/25 Tasks/Task A.md",),
        )
        self.assertTrue(rows[0]["verified"])
        self.assertEqual(rows[0]["evidence"], "task file changed")

    def test_unverifiable_entry_joins_the_denominator(self):
        rows = self.measure(
            [
                entry(
                    "eeeeeeee",
                    verdict="unverifiable",
                    unverifiable_streak=2,
                    unverifiable_action="escalation",
                ),
            ],
            moved=("eeeeeeee",),
        )
        self.assertEqual(len(rows), 4, "three digest rows plus the unverifiable entry")
        joined = [row for row in rows if row["sid"] == "eeeeeeee"]
        self.assertEqual(len(joined), 1)
        self.assertEqual(joined[0]["class"], "unverifiable")
        self.assertTrue(joined[0]["verified"], "the 2-round pane read counts as an action")


class TestRealDigestFormat(unittest.TestCase):
    """The class column must be LOCATED, not indexed.

    Measured 2026-10-22 on live rounds: the parser read `parts[3]`, which is the task file
    in the real render, and returned classes `{idle, shell}` -- zero parked rows counted
    while the same digest's header said `parked=5`. The share's denominator then omitted
    every stalled and parked row, and the printed ratio was wrong while looking plausible.

    The fixture in this file could not catch it: it carries an age column, so it mirrors
    the parser's wrong assumption instead of the render's real shape.
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)
        self.path = os.path.join(self.tmp, "real.digest")
        with open(self.path, "w", encoding="utf-8") as handle:
            handle.write(REAL_DIGEST)

    def test_class_is_located_not_indexed(self):
        rows = drive_share.digest_classes(self.path)
        self.assertEqual(
            [(row[1], row[2]) for row in rows],
            [("11111111", "parked"), ("22222222", "parked"), ("33333333", "progressing")],
        )

    def test_parked_rows_reach_the_denominator(self):
        """The consequence the defect had: two parked rows must be two detected rows."""
        rows = drive_share.measure(
            drive_share.digest_classes(self.path),
            [],
            START,
            lambda sid: None,
            lambda path: None,
        )
        self.assertEqual(len(rows), 2)
        self.assertEqual({row["sid"] for row in rows}, {"11111111", "22222222"})

    def test_qualified_class_still_counts(self):
        """`parked (closer pick)` is a parked row; an exact match drops it.

        Captured live on round `2026-10-22T19-23-20Z`, where an exact-match vocabulary
        counted 1 parked row and the qualified render carried 2.
        """
        path = os.path.join(self.tmp, "qualified.digest")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(
                "DIGEST x · 2 sessions\n"
                "CLASSIFICATION  2 classified · parked=2\n"
                "  Plain One [11111111] · shell · 11m · parked · — · — · url\n"
                "  Closer Two [22222222] · shell · 11m · parked (closer pick) · — · — · url\n"
                "BLOCKED (feed, raised — not verified open)\n"
            )
        rows = drive_share.measure(
            drive_share.digest_classes(path),
            [],
            START,
            lambda sid: None,
            lambda path: None,
        )
        self.assertEqual(len(rows), 2)
        self.assertEqual({row["sid"] for row in rows}, {"11111111", "22222222"})

    def test_age_shape_with_unknown_class_is_counted_unparsed(self):
        """The index-2 fallback must still be loud, not silently misread.

        An age-shape row whose class token is unknown falls back to index 2 and reads the
        age cell. The row must land in `unparsed` — the loud path — rather than being
        dropped or classified as something else.
        """
        path = os.path.join(self.tmp, "age-unknown.digest")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(
                "DIGEST x · 1 sessions\n"
                "CLASSIFICATION  1 classified\n"
                "  Odd [88888888] · shell · 0.3h · brandnewclass · Task.md · 1 · url\n"
                "BLOCKED (feed, raised — not verified open)\n"
            )
        rows, unparsed = drive_share.digest_rows(path)
        self.assertEqual(rows, [])
        self.assertEqual(unparsed, 1)

    def test_unknown_class_token_is_counted_not_swallowed(self):
        """The fail-quiet path: an unrecognised token must be REPORTED, not skipped.

        The defect this change fixes was a parser that dropped rows without a trace. A
        silent `continue` on an unknown token would reintroduce exactly that shape for the
        next class the render adds, so the count is returned and printed.
        """
        path = os.path.join(self.tmp, "odd.digest")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(
                "DIGEST x · 2 sessions\n"
                "CLASSIFICATION  2 classified · parked=1\n"
                "  Parked One [11111111] · idle · parked · Task.md · 1 · url\n"
                "  Odd One [22222222] · idle · brandnewclass · Task.md · 1 · url\n"
                "BLOCKED (feed, raised — not verified open)\n"
            )
        rows, unparsed = drive_share.digest_rows(path)
        self.assertEqual([row[1] for row in rows], ["11111111"])
        self.assertEqual(unparsed, 1)
        self.assertIn(
            "1 CLASSIFICATION line(s) could not be read as a row",
            drive_share.render([], unparsed=unparsed),
        )

    def test_task_basename_beginning_with_a_class_word_is_not_the_class(self):
        """The scan must not walk into the task column.

        `brandnewclass · Parked Ideas.md` was read as a **parked** row (denominator
        inflated, `unparsed` 0), and `brandnewclass · Finished Migration.md` was dropped
        silently — both the fail-quiet shape this change exists to remove.
        """
        path = os.path.join(self.tmp, "collision.digest")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(
                "DIGEST x · 2 sessions\n"
                "CLASSIFICATION  2 classified\n"
                "  Odd One [22222222] · idle · brandnewclass · Parked Ideas.md · 1 · url\n"
                "  Odd Two [33333333] · idle · brandnewclass · Finished Migration.md · 1 · url\n"
                "BLOCKED (feed, raised — not verified open)\n"
            )
        rows, unparsed = drive_share.digest_rows(path)
        self.assertEqual(rows, [])
        self.assertEqual(unparsed, 2)

    def test_semicolon_in_a_cell_does_not_hide_the_row(self):
        """A `;` inside the task name must not make a real row disappear."""
        path = os.path.join(self.tmp, "semicolon.digest")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(
                "DIGEST x · 1 sessions\n"
                "CLASSIFICATION  1 classified\n"
                "  Real One [11111111] · idle · parked · Task; part two.md · 1 · url\n"
                "BLOCKED (feed, raised — not verified open)\n"
            )
        rows, unparsed = drive_share.digest_rows(path)
        self.assertEqual(rows, [("Real One", "11111111", "parked")])
        self.assertEqual(unparsed, 0)

    def test_age_column_shape_still_reads_the_class_after_the_age(self):
        """Every age form the render emits, not just `11m`.

        `18:04Z`, `stall 2` and `0.3h` are age cells too. A `\\d+[smhdw]`-only test dropped
        two real **stalled** rows on round `19-39-16Z`; the decimal form alone dropped
        **13** stalled/parked rows on 2026-10-28. The position is therefore decided by the
        structural rule in `class_index` (a class cell is never a task basename), not by
        matching the age.
        """
        path = os.path.join(self.tmp, "aged.digest")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(
                "DIGEST x · 4 sessions\n"
                "CLASSIFICATION  4 classified\n"
                "  Aged One [44444444] · shell · 11m · parked · Task.md · 1 · url\n"
                "  Aged Two [55555555] · idle · 18:04Z · finished · Task.md · 1 · url\n"
                "  Aged Three [66666666] · shell · stall 2 · stalled · Task.md · 1 · url\n"
                "  Aged Four [77777777] · shell · 0.3h · stalled (stall=2) · Task.md · 1 · url\n"
                "BLOCKED (feed, raised — not verified open)\n"
            )
        rows, unparsed = drive_share.digest_rows(path)
        self.assertEqual(
            rows,
            [
                ("Aged One", "44444444", "parked"),
                ("Aged Two", "55555555", "finished"),
                ("Aged Three", "66666666", "stalled"),
                ("Aged Four", "77777777", "stalled"),
            ],
        )
        self.assertEqual(unparsed, 0)

    def test_absent_classification_section_is_empty_not_an_error(self):
        path = os.path.join(self.tmp, "nosection.digest")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("DIGEST x · 0 sessions\nNOTES\n  nothing here\n")
        self.assertEqual(drive_share.digest_rows(path), ([], 0))

    def test_capitalised_token_still_counts_as_detected(self):
        """A case-insensitive scan feeding a case-sensitive consumer must not disagree."""
        path = os.path.join(self.tmp, "caps.digest")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(
                "DIGEST x · 1 sessions\n"
                "CLASSIFICATION  1 classified · parked=1\n"
                "  Parked One [11111111] · idle · Parked · Task.md · 1 · url\n"
                "BLOCKED (feed, raised — not verified open)\n"
            )
        rows = drive_share.measure(
            drive_share.digest_classes(path),
            [],
            START,
            lambda sid: None,
            lambda path: None,
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["class"], "parked")


class TestSentSet(Fixture):
    """The drive leg DRAFTS; the round SENDS. Only a send may reach the numerator.

    Measured 2026-10-22T13:15Z: a round with every send deliberately held still printed
    `share: 2/112 (2%)`, because two drafted rows happened to move for unrelated reasons.
    """

    def test_drafted_but_unsent_is_not_an_action(self):
        rows = self.measure(
            [entry("aaaaaaaa", nudge="drafted")], moved=("aaaaaaaa",), sent=set()
        )
        aaaa = [row for row in rows if row["sid"] == "aaaaaaaa"][0]
        self.assertFalse(aaaa["verified"], "movement alone must not credit an unsent draft")
        self.assertEqual(aaaa["evidence"], "drafted, not sent")

    def test_a_real_send_is_credited(self):
        rows = self.measure(
            [entry("aaaaaaaa", nudge="drafted")],
            moved=("aaaaaaaa",),
            sent={"aaaaaaaa"},
        )
        aaaa = [row for row in rows if row["sid"] == "aaaaaaaa"][0]
        self.assertTrue(aaaa["verified"])

    def test_a_send_with_no_movement_is_not_verified(self):
        rows = self.measure(
            [entry("aaaaaaaa", nudge="drafted")], moved=(), sent={"aaaaaaaa"}
        )
        aaaa = [row for row in rows if row["sid"] == "aaaaaaaa"][0]
        self.assertFalse(aaaa["verified"])
        self.assertEqual(aaaa["evidence"], "no movement after the round start")

    def test_unknown_send_state_is_declared_in_the_header(self):
        rows = self.measure([entry("aaaaaaaa", nudge="drafted")])
        self.assertIn("send state unknown", drive_share.render(rows, sent_known=False))
        self.assertNotIn("send state unknown", drive_share.render(rows, sent_known=True))

    def test_the_sent_none_fallback_reads_the_action_marker_not_the_streak(self):
        """A row held back and acted at streak 3 must still count as actioned.

        The fallback used to test `unverifiable_streak == 2`; under the `>= 2` trigger a
        row acted at 3 fell through it and rendered "no action taken" although a nudge or
        escalation was taken.
        """
        rows = self.measure(
            [
                entry(
                    "aaaaaaaa",
                    nudge="none",
                    unverifiable_streak=3,
                    unverifiable_action="nudge",
                )
            ],
            moved=("aaaaaaaa",),
        )
        aaaa = [row for row in rows if row["sid"] == "aaaaaaaa"][0]
        self.assertTrue(aaaa["verified"])
        self.assertEqual(aaaa["action"], "action")


class TestRefusals(Fixture):
    def test_unreadable_digest_refuses_rather_than_reporting_zero(self):
        rc = drive_share.main(
            ["share", "--round", ROUND, "--digest", os.path.join(self.tmp, "absent")]
        )
        self.assertEqual(rc, drive_share.REFUSED)

    def test_bad_round_tag_refuses(self):
        rc = drive_share.main(["share", "--round", "round-14", "--digest", self.digest])
        self.assertEqual(rc, drive_share.REFUSED)

    def test_missing_classification_section_refuses(self):
        """A renamed or truncated section is unreadable, not a fleet with no problems."""
        path = os.path.join(self.tmp, "nosection.digest")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("DIGEST x · 0 sessions\nNOTES\n  nothing here\n")
        rc = drive_share.main(["share", "--round", ROUND, "--digest", path])
        self.assertEqual(rc, drive_share.REFUSED)

    def test_cmd_share_prints_the_unparsed_warning_end_to_end(self):
        """The warning must reach stdout beside a real denominator, not just from render()."""
        path = os.path.join(self.tmp, "odd.digest")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(
                "DIGEST x · 2 sessions\n"
                "CLASSIFICATION  2 classified\n"
                "  Parked One [11111111] · idle · parked · Task.md · 1 · url\n"
                "  Odd One [22222222] · idle · brandnewclass · Task.md · 1 · url\n"
                "BLOCKED (feed, raised — not verified open)\n"
            )
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = drive_share.main(["share", "--round", ROUND, "--digest", path])
        self.assertEqual(rc, 0)
        printed = out.getvalue()
        self.assertIn("1 CLASSIFICATION line(s) could not be read as a row", printed)
        self.assertIn("share: 0/1 (0%)", printed)

    def test_both_round_spellings_parse(self):
        self.assertEqual(
            drive_share.round_epoch("2026-10-22T10-15-00Z"),
            drive_share.round_epoch("2026-10-22T10:15:00Z"),
        )


if __name__ == "__main__":
    unittest.main()
