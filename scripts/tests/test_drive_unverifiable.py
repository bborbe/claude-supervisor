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

    def round(self, tag, entries):
        ledger, reason = drive_unverifiable.read_ledger()
        self.assertIsNone(reason)
        payload = {"round": tag, "entries": entries}
        acted = drive_unverifiable.plan(
            payload["entries"],
            ledger,
            tag,
            self.pane_for,
            self.pane_text,
            lambda pane: "/supervisor:jump %s" % pane,
        )
        self.merge(payload)
        return acted, drive_unverifiable.render(acted)

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
        acted = drive_unverifiable.plan(
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
        acted = drive_unverifiable.plan(
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
        acted = drive_unverifiable.plan(
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


if __name__ == "__main__":
    unittest.main()
