"""The detected→verified share: the numerator must EXCLUDE an action that moved nothing.

The load-bearing test is `test_action_without_movement_counts_as_unverified`. A share line
whose shape is all that is checked can be a hardcoded ratio; the only thing that
distinguishes a computed one is a case where the numerator and the denominator disagree
for a stated reason, and a case where an action was taken and still does not verify.
"""
import importlib.util
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

    def test_both_round_spellings_parse(self):
        self.assertEqual(
            drive_share.round_epoch("2026-10-22T10-15-00Z"),
            drive_share.round_epoch("2026-10-22T10:15:00Z"),
        )


if __name__ == "__main__":
    unittest.main()
