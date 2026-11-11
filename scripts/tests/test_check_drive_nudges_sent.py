"""The gate on a drive round's drafted-vs-sent nudges: green on the tree, red on a miss.

The load-bearing tests are the deficit ones. A check that only ever runs on a healthy round
is indistinguishable from one that can never fail, so the payload fixture is the MEASURED
round -- `2026-11-11T08-51-29Z`, 6 `TO:` lines over 5 recipients, no send -- and the clean
reading is the same payload with a complete sent record. That pairing is what proves the
gate discriminates: one file, two verdicts, differing only in whether the send record
covers the drafted set.

The static half is tripped by a copy of the tree with exactly one clause removed each time,
because every one of those clauses is a sentence in a markdown file that a later edit can
delete without anything else noticing.
"""
import importlib.util
import io
import pathlib
import shutil
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout

SCRIPTS = pathlib.Path(__file__).resolve().parent.parent
CHECK = SCRIPTS / "check-drive-nudges-sent.py"

_spec = importlib.util.spec_from_file_location("check_drive_nudges_sent", CHECK)
check = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(check)

#: The measured round's shape: 5 `TO:` lines under `NUDGES`, plus one under
#: `UNVERIFIABLE NUDGES` for `private-personal-94`, which `NUDGES` already names. Six lines,
#: five recipients -- the dedupe rule SC2 pins.
RECIPIENTS = (
    "⚙ Make daily-maintenance Create Jira Tickets from Vault Proposals",
    "⚙ Serve the git-rest Protocols vault-api Clients Still Need",
    "private-personal-78",
    "private-personal-7c",
    "private-personal-94",
)

PAYLOAD = """\
fleet-drive — 5 candidates (0 retained from the ledger) · 0 finished (reaped) · 0 revive (0 routine continue) · 0 blocked · 1 unverifiable
ESCALATION — grouped by cause
  (none)
UNVERIFIABLE — a session unverifiable 2+ consecutive rounds, what its pane read returned, and the outcome
  private-personal-94 [873771e6] · streak 2 · pane 309 · nudge
      pane: wezterm cli get-text --pane-id 309 → read
      closer: pick — 1. complete the task (recommended) · 2. leave it
UNVERIFIABLE NUDGES — paste each line into NUDGES
  TO: private-personal-94 | Read-only context from the fleet drive leg — its pane was read this round.
UNVERIFIABLE ESCALATIONS — paste into ESCALATION and post one attention card each
  (none)
REAPS
  (none)
CLOSED — one line per pane closed by 5b
  (none)
NUDGES
  TO: ⚙ Make daily-maintenance Create Jira Tickets from Vault Proposals | Read-only context — continue under your own execution rules.
  TO: ⚙ Serve the git-rest Protocols vault-api Clients Still Need | Read-only context — continue under your own execution rules.
  TO: private-personal-78 | Read-only context — continue under your own execution rules.
  TO: private-personal-7c | Read-only context — continue under your own execution rules.
  TO: private-personal-94 | Read-only context — continue under your own execution rules.
FREEABLE TABS (a slot the operator can free without a diagnosis)
  (none)
Waiting on your keystroke — re-read 2026-11-11T08:51:29Z
  (nothing waiting on you)
LEDGER  ~/.claude/state/fleet-drive/ledger.json · 7 entries · written 2026-11-11T08:51:29Z
"""


def run_main(argv):
    """`check.main` with its output captured -- the tests assert the verdict, not the log."""
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        rc = check.main(argv)
    return rc, out.getvalue(), err.getvalue()


class Parser(unittest.TestCase):
    def test_counts_distinct_recipients_not_lines(self):
        """Six `TO:` lines, five recipients -- first-seen order, deduplicated."""
        drafted = check.drafted_recipients(PAYLOAD)
        self.assertEqual(len(drafted), 5, drafted)
        self.assertEqual(sorted(drafted), sorted(RECIPIENTS))

    def test_a_payload_with_no_to_lines_drafts_nothing(self):
        self.assertEqual(check.drafted_recipients("fleet-drive — 0 candidates\nNUDGES\n"), [])

    def test_a_to_line_outside_a_counted_block_is_ignored(self):
        payload = "fleet-drive — 1 candidates\nWaiting on your keystroke\n  TO: nobody | x\nNUDGES\n  TO: somebody | y\n"
        self.assertEqual(check.drafted_recipients(payload), ["somebody"])

    def test_the_sent_path_is_derived_from_the_payload(self):
        self.assertEqual(
            check.sent_path_for("/tmp/2026-11-11T08-51-29Z.drive"),
            pathlib.Path("/tmp/2026-11-11T08-51-29Z.sent"),
        )


class Artifacts(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        self.drive = self.tmp / "2026-11-11T08-51-29Z.drive"
        self.drive.write_text(PAYLOAD, encoding="utf-8")
        self.sent = self.tmp / "2026-11-11T08-51-29Z.sent"

    def write_sent(self, names):
        self.sent.write_text("\n".join(names) + ("\n" if names else ""), encoding="utf-8")

    def test_the_measured_round_reports_five_missing(self):
        rc, out, err = run_main(["--drive", str(self.drive)])
        self.assertEqual(rc, 1, err)
        self.assertIn("nudges: drafted 5 · sent 0 · missing 5", out)

    def test_an_empty_sent_record_is_the_same_as_no_file(self):
        self.write_sent([])
        rc, out, _ = run_main(["--drive", str(self.drive)])
        self.assertEqual(rc, 1)
        self.assertIn("nudges: drafted 5 · sent 0 · missing 5", out)

    def test_a_complete_sent_record_reports_zero_missing(self):
        self.write_sent(RECIPIENTS)
        rc, out, err = run_main(["--drive", str(self.drive)])
        self.assertEqual(rc, 0, err)
        self.assertIn("nudges: drafted 5 · sent 5 · missing 0", out)

    def test_a_partial_sent_record_reports_the_deficit(self):
        self.write_sent(RECIPIENTS[:3])
        rc, out, _ = run_main(["--drive", str(self.drive)])
        self.assertEqual(rc, 1)
        self.assertIn("nudges: drafted 5 · sent 3 · missing 2", out)

    def test_a_duplicate_in_the_sent_record_does_not_inflate_the_count(self):
        self.write_sent(list(RECIPIENTS) + ["private-personal-94"])
        rc, out, _ = run_main(["--drive", str(self.drive)])
        self.assertEqual(rc, 0)
        self.assertIn("nudges: drafted 5 · sent 5 · missing 0", out)

    def test_an_undeclared_recipient_is_reported_and_cannot_mask_a_deficit(self):
        """A send to a name the payload never drafted is an over-send, not a delivery."""
        self.write_sent([RECIPIENTS[0], "somebody-else"])
        rc, out, _ = run_main(["--drive", str(self.drive)])
        self.assertEqual(rc, 1)
        self.assertIn("nudges: drafted 5 · sent 1 · missing 4 · unexpected sent 1", out)

    def test_a_closed_reaps_recipient_is_not_missing(self):
        """A finished headless row is closed, never messaged -- that is not a miss."""
        self.write_sent(["closed\t" + RECIPIENTS[0]])
        rc, out, _ = run_main(["--drive", str(self.drive)])
        self.assertEqual(rc, 1)
        self.assertIn("nudges: drafted 5 · sent 0 · missing 4", out)

    def test_a_skipped_recipient_is_not_missing(self):
        """A peer that moved to busy is skipped and reported -- also not a miss."""
        self.write_sent(["skipped\t" + RECIPIENTS[0]])
        rc, out, _ = run_main(["--drive", str(self.drive)])
        self.assertIn("nudges: drafted 5 · sent 0 · missing 4", out)

    def test_a_fully_actioned_round_has_no_deficit_and_counts_only_real_sends(self):
        self.write_sent(
            ["sent\t" + name for name in RECIPIENTS[:4]] + ["closed\t" + RECIPIENTS[4]]
        )
        rc, out, err = run_main(["--drive", str(self.drive)])
        self.assertEqual(rc, 0, err)
        self.assertIn("nudges: drafted 5 · sent 4 · missing 0", out)

    def test_the_sent_flag_overrides_the_derived_path(self):
        other = self.tmp / "elsewhere.txt"
        other.write_text("\n".join(RECIPIENTS) + "\n", encoding="utf-8")
        rc, out, _ = run_main(["--drive", str(self.drive), "--sent", str(other)])
        self.assertEqual(rc, 0)
        self.assertIn("missing 0", out)

    def test_an_unreadable_payload_is_refused_not_measured(self):
        rc, out, err = run_main(["--drive", str(self.tmp / "absent.drive")])
        self.assertEqual(rc, 1)
        self.assertIn("payload unreadable", err)

    def test_a_malformed_invocation_is_refused(self):
        rc, _, err = run_main(["--digest", "x"])
        self.assertEqual(rc, check.REFUSED)
        self.assertIn("unknown flag --digest", err)


class StaticOnTheTree(unittest.TestCase):
    def test_passes_on_the_real_tree(self):
        self.assertEqual(check.static_failures(), [])

    def test_main_is_green(self):
        self.assertEqual(run_main([])[0], 0)


class StaticOnABrokenCopy(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        for name in ("commands/steps",):
            (self.tmp / name).mkdir(parents=True, exist_ok=True)
        shutil.copy(check.ROUND, self.tmp / "commands" / "steps" / "drive.md")
        shutil.copy(check.OUTPUT_SHAPE, self.tmp / "commands" / "steps" / "output-shape.md")
        self._saved = (check.ROUND, check.OUTPUT_SHAPE)
        check.ROUND = self.tmp / "commands" / "steps" / "drive.md"
        check.OUTPUT_SHAPE = self.tmp / "commands" / "steps" / "output-shape.md"

        def restore():
            check.ROUND, check.OUTPUT_SHAPE = self._saved

        self.addCleanup(restore)

    def edit(self, path, old, new):
        body = path.read_text(encoding="utf-8")
        self.assertIn(old, body, "fixture drifted: %r not in %s" % (old, path.name))
        path.write_text(body.replace(old, new), encoding="utf-8")

    def test_a_send_order_without_the_unverifiable_block_fails(self):
        body = check.ROUND.read_text(encoding="utf-8")
        line = [l for l in body.splitlines() if check.SEND_ORDER_ANCHOR in l][0]
        self.assertIn(check.UNVERIFIABLE_BLOCK, line)
        self.edit(check.ROUND, line, line.replace(check.UNVERIFIABLE_BLOCK, "the nudge block"))
        failures = check.static_failures()
        self.assertTrue(
            any("send order no longer names" in f for f in failures), failures
        )

    def test_a_round_without_a_sent_record_fails(self):
        self.edit(check.ROUND, check.SENT_RECORD_MARKER, "some scratch file")
        failures = check.static_failures()
        self.assertTrue(any("no longer writes" in f for f in failures), failures)

    def test_a_round_that_stops_running_the_check_fails(self):
        self.edit(check.ROUND, check.CHECK_NAME, "some-other-check.py")
        failures = check.static_failures()
        self.assertTrue(any("no longer runs" in f for f in failures), failures)

    def test_an_output_shape_without_the_line_fails(self):
        self.edit(check.OUTPUT_SHAPE, check.OUTPUT_SHAPE_MARKER, "a nudges line")
        failures = check.static_failures()
        self.assertTrue(any("no longer mandates" in f for f in failures), failures)


if __name__ == "__main__":
    unittest.main()
