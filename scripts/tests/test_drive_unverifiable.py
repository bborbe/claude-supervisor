"""The 2-round rule, driven as a replay: two rounds, one ledger, a fixture pane.

The load-bearing test is `test_two_round_session_gets_one_pane_read_and_one_nudge`. The
defect the change exists to remove is a drive leg that marks a row `unverifiable` and does
nothing for as long as it stays that way -- a build that never reads a pane passes every
content assertion, so the test counts pane reads and asserts WHICH round they happened in.

The two negative controls run in the same replay, because both fail in a different way:
a 1-round session that gets acted on means the counter is not consecutive, and a session
that resets after being verified means a stale count survives a recovery.
"""
import importlib.util
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
import io

SCRIPTS = pathlib.Path(__file__).resolve().parent.parent
SCRIPT = SCRIPTS / "drive-unverifiable.py"
LEDGER = SCRIPTS / "drive-ledger.py"

_spec = importlib.util.spec_from_file_location("drive_unverifiable", SCRIPT)
drive_unverifiable = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(drive_unverifiable)

#: The fixture pane for the 2-round session. Deliberately holds TWO closer lines, so
#: "last" is a real choice rather than the only match, and a status bar below them, so a
#: positional "last non-empty line" implementation is caught.
PANE = """\
  Running the precommit target.
  ✔ precommit green

  ────────────────────────────────────────
  👤 You:    approve: start subtask 3
  ⏵⏵ auto mode on
"""


def entry(sid, verdict, **over):
    row = {
        "session_id": sid,
        "name": "Task %s" % sid,
        "verdict": verdict,
        "open_boxes": 2,
        "probe": "no task file recorded",
        "nudge": "none",
        "suppression": "none",
        "task_file": "",
        "reprobe_verdict": "",
        "reprobe_probe": "",
    }
    row.update(over)
    return row


class Replay(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)
        self.ledger_path = os.path.join(self.tmp, "ledger.json")
        self._prior = os.environ.get("SUPERVISOR_DRIVE_LEDGER")
        os.environ["SUPERVISOR_DRIVE_LEDGER"] = self.ledger_path

        def restore():
            if self._prior is None:
                os.environ.pop("SUPERVISOR_DRIVE_LEDGER", None)
            else:
                os.environ["SUPERVISOR_DRIVE_LEDGER"] = self._prior

        self.addCleanup(restore)
        self.pane_reads = []

        def pane_for(sid):
            return {"aaaaaaaa": "700", "bbbbbbbb": "701", "cccccccc": "702"}.get(sid, "")

        def pane_text(pane):
            self.pane_reads.append(pane)
            return PANE

        self.pane_for = pane_for
        self.pane_text = pane_text

    def merge(self, payload):
        out = subprocess.run(
            [sys.executable, str(LEDGER), "merge"],
            input=json.dumps(payload),
            capture_output=True,
            text=True,
        )
        self.assertEqual(out.returncode, 0, out.stderr)

    def round(self, tag, entries, max_actions=drive_unverifiable.MAX_ACTIONS):
        ledger, reason = drive_unverifiable.read_ledger()
        self.assertIsNone(reason)
        payload = {"round": tag, "entries": entries}
        acted, held = drive_unverifiable.plan(
            payload["entries"],
            ledger,
            tag,
            self.pane_for,
            self.pane_text,
            lambda pane: "/supervisor:jump %s" % pane,
            max_actions=max_actions,
        )
        self.merge(payload)
        return acted, drive_unverifiable.render(acted, held, max_actions)

    def ledger_entries(self):
        ledger, reason = drive_unverifiable.read_ledger()
        self.assertIsNone(reason)
        return {row["session_id"]: row for row in ledger["entries"]}


class TestTwoRoundRule(Replay):
    def test_two_round_session_gets_one_pane_read_and_one_nudge(self):
        # Round 1: aaaa and bbbb unverifiable, cccc verified.
        _, out1 = self.round(
            "2026-10-22T10-00-00Z",
            [
                entry("aaaaaaaa", "unverifiable"),
                entry("bbbbbbbb", "unverifiable"),
                entry("cccccccc", "revive", nudge="drafted"),
            ],
        )
        self.assertEqual(self.pane_reads, [], "round 1 must read no pane")
        self.assertNotIn("TO: Task aaaaaaaa", out1)

        # Round 2: aaaa still unverifiable (streak 2), bbbb verified (reset), cccc stays verified.
        acted, out2 = self.round(
            "2026-10-22T10-15-00Z",
            [
                entry("aaaaaaaa", "unverifiable"),
                entry("bbbbbbbb", "revive", nudge="drafted"),
                entry("cccccccc", "revive", nudge="drafted"),
            ],
        )

        self.assertEqual(self.pane_reads, ["700"], "exactly one pane read, for aaaa only")
        self.assertEqual(len(acted), 1)
        self.assertEqual(acted[0][0]["session_id"], "aaaaaaaa")
        self.assertEqual(acted[0][1]["action"], "nudge")

        # The quoted line is the fixture pane's last closer, verbatim.
        self.assertIn("👤 You:    approve: start subtask 3", out2)
        self.assertNotIn("TO: Task bbbbbb", out2)
        self.assertNotIn("TO: Task cccccc", out2)

        # Negative control: the reset really happened on disk, not just in this round's dict.
        rows = self.ledger_entries()
        self.assertEqual(rows["bbbbbbbb"]["unverifiable_streak"], 0)
        self.assertEqual(rows["bbbbbbbb"]["unverifiable_round"], "")
        self.assertEqual(rows["cccccccc"]["unverifiable_streak"], 0)
        self.assertEqual(rows["aaaaaaaa"]["unverifiable_streak"], 2)

    def test_a_gap_resets_the_count_to_one(self):
        """Round 1 unverifiable, round 2 verified, round 3 unverifiable again -> streak 1.

        This is the control the counter's consecutiveness rests on: without the reset the
        count would read 2 at round 3 and act on a session that had recovered in between.
        """
        self.round("2026-10-22T10-00-00Z", [entry("aaaaaaaa", "unverifiable")])
        self.round("2026-10-22T10-15-00Z", [entry("aaaaaaaa", "revive")])
        acted, _ = self.round("2026-10-22T10-30-00Z", [entry("aaaaaaaa", "unverifiable")])
        self.assertEqual(acted, [])
        self.assertEqual(self.pane_reads, [])
        self.assertEqual(self.ledger_entries()["aaaaaaaa"]["unverifiable_streak"], 1)

    def test_acts_once_at_two_and_never_again(self):
        """A session silent for four rounds gets one action, not three.

        The ledger's whole suppression philosophy: a re-nudge every round is the nag it
        exists to prevent.
        """
        for tag in ("2026-10-22T10-00-00Z", "2026-10-22T10-15-00Z"):
            self.round(tag, [entry("aaaaaaaa", "unverifiable")])
        acted, _ = self.round("2026-10-22T10-30-00Z", [entry("aaaaaaaa", "unverifiable")])
        self.assertEqual(acted, [])
        self.assertEqual(self.ledger_entries()["aaaaaaaa"]["unverifiable_streak"], 3)


class TestActionChoice(Replay):
    def test_unreadable_pane_escalates(self):
        self.round("2026-10-22T10-00-00Z", [entry("aaaaaaaa", "unverifiable")])
        ledger, _ = drive_unverifiable.read_ledger()
        payload = {"round": "2026-10-22T10-15-00Z", "entries": [entry("aaaaaaaa", "unverifiable")]}
        acted, _ = drive_unverifiable.plan(
            payload["entries"],
            ledger,
            payload["round"],
            self.pane_for,
            lambda pane: None,
            lambda pane: "/supervisor:jump %s" % pane,
        )
        self.assertEqual(acted[0][1]["action"], "escalation")
        self.assertIn("unreadable", acted[0][1]["pane_line"])
        self.assertIn("Unverifiable for 2 consecutive rounds", acted[0][1]["text"])

    def test_pane_without_a_closer_escalates(self):
        self.round("2026-10-22T10-00-00Z", [entry("aaaaaaaa", "unverifiable")])
        ledger, _ = drive_unverifiable.read_ledger()
        payload = {"round": "2026-10-22T10-15-00Z", "entries": [entry("aaaaaaaa", "unverifiable")]}
        acted, _ = drive_unverifiable.plan(
            payload["entries"],
            ledger,
            payload["round"],
            self.pane_for,
            lambda pane: "  ⏵⏵ auto mode on\n",
            lambda pane: "/supervisor:jump %s" % pane,
        )
        self.assertEqual(acted[0][1]["action"], "escalation")
        self.assertIn("no closer line", acted[0][1]["pane_line"])

    def test_no_pane_resolves_escalates(self):
        self.round("2026-10-22T10-00-00Z", [entry("aaaaaaaa", "unverifiable")])
        ledger, _ = drive_unverifiable.read_ledger()
        payload = {"round": "2026-10-22T10-15-00Z", "entries": [entry("aaaaaaaa", "unverifiable")]}
        acted, _ = drive_unverifiable.plan(
            payload["entries"],
            ledger,
            payload["round"],
            lambda sid: "",
            self.pane_text,
            lambda pane: "",
        )
        self.assertEqual(acted[0][1]["action"], "escalation")
        self.assertEqual(self.pane_reads, [])
        self.assertIn("no pane — no jump target rendered", drive_unverifiable.render(acted))


class TestCloserExtraction(unittest.TestCase):
    def test_last_closer_wins_and_a_mid_line_quote_does_not_match(self):
        text = (
            "  pick — 1. X · 2. Y\n"
            "  The task page says `👤 You: nothing` in prose.\n"
            "  approve: start subtask 3\n"
            "  ⏵⏵ auto mode on\n"
        )
        self.assertEqual(drive_unverifiable.last_closer(text), "approve: start subtask 3")

    def test_no_closer(self):
        self.assertEqual(drive_unverifiable.last_closer("nothing here\n"), "")


class TestLedgerFields(unittest.TestCase):
    def test_merge_refuses_a_non_int_streak(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp)
        prior = os.environ.get("SUPERVISOR_DRIVE_LEDGER")
        os.environ["SUPERVISOR_DRIVE_LEDGER"] = os.path.join(tmp, "ledger.json")

        def restore():
            if prior is None:
                os.environ.pop("SUPERVISOR_DRIVE_LEDGER", None)
            else:
                os.environ["SUPERVISOR_DRIVE_LEDGER"] = prior

        self.addCleanup(restore)
        payload = {
            "round": "2026-10-22T10-00-00Z",
            "entries": [dict(entry("aaaaaaaa", "unverifiable"), unverifiable_streak="two")],
        }
        out = subprocess.run(
            [sys.executable, str(LEDGER), "merge"],
            input=json.dumps(payload),
            capture_output=True,
            text=True,
        )
        self.assertEqual(out.returncode, 2, out.stdout)
        self.assertIn("unverifiable_streak", out.stderr)
        self.assertFalse(os.path.exists(os.environ["SUPERVISOR_DRIVE_LEDGER"]))


class TestHandleCommand(unittest.TestCase):
    def test_handle_writes_the_patched_entries_and_renders(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp)
        prior = os.environ.get("SUPERVISOR_DRIVE_LEDGER")
        os.environ["SUPERVISOR_DRIVE_LEDGER"] = os.path.join(tmp, "ledger.json")

        def restore():
            if prior is None:
                os.environ.pop("SUPERVISOR_DRIVE_LEDGER", None)
            else:
                os.environ["SUPERVISOR_DRIVE_LEDGER"] = prior

        self.addCleanup(restore)
        entries_path = os.path.join(tmp, "round.json")
        out_path = os.path.join(tmp, "patched.json")
        with open(entries_path, "w", encoding="utf-8") as handle:
            json.dump(
                {
                    "round": "2026-10-22T10-00-00Z",
                    "entries": [entry("aaaaaaaa", "unverifiable")],
                },
                handle,
            )
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = drive_unverifiable.main(
                [
                    "handle",
                    "--entries",
                    entries_path,
                    "--json-out",
                    out_path,
                ]
            )
        self.assertEqual(rc, 0)
        self.assertIn("UNVERIFIABLE —", buf.getvalue())
        with open(out_path, encoding="utf-8") as handle:
            patched = json.load(handle)
        self.assertEqual(patched["entries"][0]["unverifiable_streak"], 1)
        self.assertEqual(patched["entries"][0]["unverifiable_round"], "2026-10-22T10-00-00Z")


class TestPerRoundCap(Replay):
    """The cap: at most N rows act per round, and the rest are named, not dropped."""

    SIDS = ("aaaaaaaa", "bbbbbbbb", "cccccccc", "dddddddd", "eeeeeeee")

    def seed_streak_one(self):
        self.round(
            "2026-10-22T10-00-00Z",
            [entry(sid, "unverifiable") for sid in self.SIDS],
        )
        self.assertEqual(self.pane_reads, [], "round 1 must act on nothing")

    def test_cap_acts_on_n_and_names_the_rest(self):
        self.seed_streak_one()
        acted, out = self.round(
            "2026-10-22T10-15-00Z",
            [entry(sid, "unverifiable") for sid in self.SIDS],
            max_actions=2,
        )
        self.assertEqual(len(acted), 2, "exactly the cap acts")
        self.assertEqual(len(self.pane_reads), 2, "one pane read per acted row, no more")
        self.assertIn("UNVERIFIABLE HELD BACK", out)
        tail = out.split("UNVERIFIABLE HELD BACK", 1)[1]
        held_lines = [line for line in tail.splitlines() if line.startswith("  ")]
        self.assertEqual(len(held_lines), 3, "every held-back row is named")

    def test_held_back_row_still_acts_on_a_later_round(self):
        """The design point: a held row's streak keeps advancing past 2.

        An `== 2` trigger would never match it again and it would be acted on never. The
        action marker, not the count, is what stops a repeat.
        """
        self.seed_streak_one()
        self.round(
            "2026-10-22T10-15-00Z",
            [entry(sid, "unverifiable") for sid in self.SIDS],
            max_actions=2,
        )
        # Round 3, uncapped: the three held rows are now at streak 3 and act; the two
        # already acted on do not act again.
        acted, _ = self.round(
            "2026-10-22T10-30-00Z",
            [entry(sid, "unverifiable") for sid in self.SIDS],
            max_actions=10,
        )
        self.assertEqual(len(acted), 3, "the held-back three act now, the acted-on two do not")
        acted_sids = {row[0]["session_id"] for row in acted}
        rows = self.ledger_entries()
        already = {sid for sid in self.SIDS if rows[sid]["unverifiable_action"]} - acted_sids
        self.assertEqual(len(already), 2)
        for sid in acted_sids:
            self.assertIn(rows[sid]["unverifiable_action"], ("nudge", "escalation"))
            self.assertEqual(rows[sid]["unverifiable_streak"], 3)

    def test_an_acted_row_never_acts_again_while_it_stays_unverifiable(self):
        self.seed_streak_one()
        self.round("2026-10-22T10-15-00Z", [entry("aaaaaaaa", "unverifiable")])
        self.assertEqual(len(self.pane_reads), 1)
        acted, _ = self.round("2026-10-22T10-30-00Z", [entry("aaaaaaaa", "unverifiable")])
        self.assertEqual(acted, [])
        self.assertEqual(len(self.pane_reads), 1, "no second pane read")

    def test_recovery_clears_the_marker_so_a_later_silence_acts_again(self):
        self.seed_streak_one()
        self.round("2026-10-22T10-15-00Z", [entry("aaaaaaaa", "unverifiable")])
        self.round("2026-10-22T10-30-00Z", [entry("aaaaaaaa", "revive")])
        self.assertEqual(self.ledger_entries()["aaaaaaaa"]["unverifiable_action"], "")
        # Two more silent rounds reach the threshold again and act a second time.
        self.round("2026-10-22T10-45-00Z", [entry("aaaaaaaa", "unverifiable")])
        self.round("2026-10-22T11-00-00Z", [entry("aaaaaaaa", "unverifiable")])
        self.assertEqual(len(self.pane_reads), 2, "acted once before and once after recovery")


    def test_the_block_reports_the_effective_cap_not_the_default(self):
        """The header must name the cap that ran, or it lies about what happened."""
        self.seed_streak_one()
        _, out = self.round(
            "2026-10-22T10-15-00Z",
            [entry(sid, "unverifiable") for sid in self.SIDS],
            max_actions=2,
        )
        self.assertIn("per-round cap of 2", out)
        self.assertNotIn("per-round cap of 3", out)

    def test_an_acted_row_reports_its_own_streak_not_the_threshold(self):
        """A row held back and acted at streak 3 must not be told it waited 2 rounds."""
        self.seed_streak_one()
        self.round(
            "2026-10-22T10-15-00Z",
            [entry(sid, "unverifiable") for sid in self.SIDS],
            max_actions=2,
        )
        acted, out = self.round(
            "2026-10-22T10-30-00Z",
            [entry(sid, "unverifiable") for sid in self.SIDS],
            max_actions=10,
        )
        self.assertEqual(len(acted), 3)
        self.assertIn("streak 3", out)
        self.assertIn("unverifiable for 3 consecutive rounds", out)


class TestDuplicateEntries(Replay):
    """A session can arrive twice: it is in step 0's retained set AND the digest's candidates.

    Two entries for one session cost two pane reads and two identical nudge lines -- and,
    worse, `drive-ledger.py merge` REFUSES a payload carrying a session twice, so the whole
    round's ledger write would be lost. Measured 2026-10-22T13:15Z: two sessions' nudge
    lines appeared twice in one round's stdout block.
    """

    def test_a_session_arriving_twice_acts_once_and_merges(self):
        # The merge inside self.round() asserts exit 0 -- with a duplicate entry and no
        # dedupe it would refuse, so this test fails on the ledger write, not just a line.
        self.round(
            "2026-10-22T10-00-00Z",
            [entry("aaaaaaaa", "unverifiable"), entry("aaaaaaaa", "unverifiable")],
        )
        self.assertEqual(list(self.ledger_entries()), ["aaaaaaaa"])
        acted, out = self.round(
            "2026-10-22T10-15-00Z",
            [entry("aaaaaaaa", "unverifiable"), entry("aaaaaaaa", "unverifiable")],
        )
        self.assertEqual(len(acted), 1, "one action for one session")
        self.assertEqual(self.pane_reads, ["700"], "one pane read, not two")
        self.assertEqual(out.count("TO: Task aaaaaaaa"), 1, "one nudge line, not two")

    def test_the_merged_copy_keeps_step_zeros_reprobe_key(self):
        """The digest copy can precede the retained one; the reprobe_* pair must survive.

        Losing it makes the row re-signal PARKED on the next round, because step 0's dedup
        key is that pair and an entry written without it reads as a first observation.
        """
        digest_copy = entry("aaaaaaaa", "unverifiable")
        retained_copy = entry(
            "aaaaaaaa", "unverifiable", reprobe_verdict="needs-step-4", reprobe_probe="stub"
        )
        self.round("2026-10-22T10-00-00Z", [digest_copy, retained_copy])
        row = self.ledger_entries()["aaaaaaaa"]
        self.assertEqual(row["reprobe_verdict"], "needs-step-4")
        self.assertEqual(row["reprobe_probe"], "stub")


if __name__ == "__main__":
    unittest.main()
