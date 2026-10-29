"""The gate on the drive round's `UNVERIFIABLE` block: green on the tree, red on a break.

The load-bearing tests are the broken-tree ones. A check that only ever runs on a healthy
tree is indistinguishable from one that can never fail -- so each static assertion is
tripped by a copy with exactly that clause removed, and the artifact half is tripped by a
deliberately-broken round whose `.drive` lost the paste.

The artifact half is what the round itself runs. Its negative control is the point of the
whole task: a round whose block never arrived must fail loudly rather than read as "nothing
was unverifiable".
"""
import importlib.util
import io
import pathlib
import shutil
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout

SCRIPTS = pathlib.Path(__file__).resolve().parent.parent
CHECK = SCRIPTS / "check-drive-unverifiable-paste.py"

_spec = importlib.util.spec_from_file_location("check_drive_unverifiable_paste", CHECK)
check = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(check)

#: A payload that carries the block, in the shape `agents/fleet-drive.md`'s template names.
DRIVE_WITH_BLOCK = """\
fleet-drive — 1 candidates (0 retained from the ledger) · 0 finished (reaped) · 0 revive (0 routine continue) · 0 blocked · 1 unverifiable
ESCALATION — grouped by cause
  unverifiable for 2 or more consecutive rounds
    Task aaaaaaaa [aaaaaaaa] · pane 700 · /supervisor:jump 700 — read, no closer line → the pane renders no closer line
UNVERIFIABLE — a session unverifiable 2+ consecutive rounds, what its pane read returned, and the outcome (observation = no pane resolved, not actioned)
  Task aaaaaaaa [aaaaaaaa] · streak 2 · pane 700 · escalation
      pane: wezterm cli get-text --pane-id 700 → read, no closer line
      why: the pane renders no closer line
UNVERIFIABLE NUDGES — paste each line into NUDGES
  (none)
UNVERIFIABLE ESCALATIONS — paste into ESCALATION and post one attention card each
  unverifiable for 2 or more consecutive rounds
    Task aaaaaaaa [aaaaaaaa] · pane 700 · /supervisor:jump 700 — wezterm cli get-text --pane-id 700 → read, no closer line → the pane renders no closer line
      card: Unverifiable for 2 consecutive rounds: Task aaaaaaaa [aaaaaaaa] — …
REAPS
  (none)
NUDGES
  (none)
"""

#: A payload that lost the paste: the exact shape of round `T1358`.
DRIVE_WITHOUT_BLOCK = """\
fleet-drive — 1 candidates (0 retained from the ledger) · 0 finished (reaped) · 0 revive (0 routine continue) · 0 blocked · 1 unverifiable
ESCALATION — grouped by cause
  unverifiable for 2 or more consecutive rounds
    Task aaaaaaaa [aaaaaaaa] · pane 700 · /supervisor:jump 700 — read, no closer line
REAPS
  (none)
NUDGES
  (none)
"""

DIGEST = """\
DIGEST 20261029T135800Z · 1 sessions · snapshot written: 2026-10-29T13:58:00Z
CLASSIFICATION  1 classified · progressing=0 · stalled=0 · parked=0 · finished=0
  Task aaaaaaaa [aaaaaaaa] · idle · 12m · parked · Task aaaaaaaa.md · 2
BLOCKED (feed, raised — not verified open)
  (none)
"""


def run_main(argv):
    """`check.main` with its output captured -- the tests assert the verdict, not the log."""
    with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
        return check.main(argv)


class StaticOnTheTree(unittest.TestCase):
    def test_passes_on_the_real_tree(self):
        self.assertEqual(check.static_failures(), [])

    def test_main_is_green(self):
        self.assertEqual(run_main([]), 0)


class StaticOnABrokenCopy(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        for name in ("agents", "commands/steps", "scripts"):
            (self.tmp / name).mkdir(parents=True, exist_ok=True)
        shutil.copy(check.AGENT, self.tmp / "agents" / "fleet-drive.md")
        shutil.copy(check.ROUND, self.tmp / "commands" / "steps" / "drive.md")
        shutil.copy(check.SCRIPT, self.tmp / "scripts" / "drive-unverifiable.py")
        self._saved = (check.AGENT, check.ROUND, check.SCRIPT)
        check.AGENT = self.tmp / "agents" / "fleet-drive.md"
        check.ROUND = self.tmp / "commands" / "steps" / "drive.md"
        check.SCRIPT = self.tmp / "scripts" / "drive-unverifiable.py"

        def restore():
            check.AGENT, check.ROUND, check.SCRIPT = self._saved

        self.addCleanup(restore)

    def edit(self, path, old, new):
        body = path.read_text(encoding="utf-8")
        self.assertIn(old, body, "fixture drifted: %r not in %s" % (old, path.name))
        path.write_text(body.replace(old, new), encoding="utf-8")

    def test_a_template_without_the_section_fails(self):
        self.edit(check.AGENT, check.TEMPLATE_ANCHOR, "DRIVE BLOCK")
        failures = check.static_failures()
        self.assertTrue(any("no `UNVERIFIABLE —` section" in f for f in failures), failures)

    def test_step_6b_without_the_digest_flag_fails(self):
        body = check.AGENT.read_text(encoding="utf-8")
        self.assertIn("--digest", body)
        # Remove the flag from the invoking line only, keeping the write-surface mention
        # intact -- which is the case the anchored read exists for.
        line = [l for l in body.splitlines() if "handle --entries /tmp/drive-round.json" in l][0]
        self.assertIn("--digest", line)
        self.edit(check.AGENT, line, line.replace(" --digest <payload= path>", ""))
        failures = check.static_failures()
        self.assertTrue(
            any("no longer passes" in f and "--digest" in f for f in failures), failures
        )

    def test_a_round_that_stops_printing_the_block_fails(self):
        self.edit(check.ROUND, "`UNVERIFIABLE` block", "escalation groups")
        failures = check.static_failures()
        self.assertTrue(any("no longer names the" in f for f in failures), failures)

    def test_a_round_that_stops_running_the_check_fails(self):
        self.edit(check.ROUND, "check-drive-unverifiable-paste.py", "some-other-check.py")
        failures = check.static_failures()
        self.assertTrue(any("no longer runs this check" in f for f in failures), failures)

    def test_a_script_without_the_producer_line_fails(self):
        self.edit(check.SCRIPT, "UNVERIFIABLE ESCALATIONS —", "ESCALATION BLOCK")
        failures = check.static_failures()
        self.assertTrue(any("no longer emits" in f for f in failures), failures)

    def test_a_script_without_the_digest_flag_fails(self):
        self.edit(check.SCRIPT, '"--digest",', '"--somewhere-else",')
        failures = check.static_failures()
        self.assertTrue(any("no longer declares `--digest`" in f for f in failures), failures)


class Artifacts(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)

    def write(self, name, text):
        path = self.tmp / name
        path.write_text(text, encoding="utf-8")
        return path

    def digest_with(self, block):
        return self.write("round.digest", DIGEST + "\n" + check.DIGEST_BEGIN + "\n" + block + "\n" + check.DIGEST_END + "\n")

    def test_a_matching_pair_passes(self):
        block = check.drive_block(DRIVE_WITH_BLOCK)
        digest = self.digest_with(block)
        drive = self.write("round.drive", DRIVE_WITH_BLOCK)
        self.assertEqual(check.artifact_failures(digest, drive), [])
        self.assertEqual(run_main(["--digest", str(digest), "--drive", str(drive)]), 0)

    def test_a_drive_that_lost_the_paste_fails_loudly(self):
        """The deliberately-broken run: the exact round-`T1358` shape."""
        block = check.drive_block(DRIVE_WITH_BLOCK)
        digest = self.digest_with(block)
        drive = self.write("round.drive", DRIVE_WITHOUT_BLOCK)
        failures = check.artifact_failures(digest, drive)
        self.assertTrue(any("drive artifact carries no UNVERIFIABLE block" in f for f in failures), failures)
        self.assertEqual(run_main(["--digest", str(digest), "--drive", str(drive)]), 1)

    def test_a_digest_that_lost_the_mirror_fails_loudly(self):
        digest = self.write("round.digest", DIGEST)
        drive = self.write("round.drive", DRIVE_WITH_BLOCK)
        failures = check.artifact_failures(digest, drive)
        self.assertTrue(any("digest artifact carries no UNVERIFIABLE block" in f for f in failures), failures)

    def test_a_disagreeing_pair_fails(self):
        digest = self.digest_with("UNVERIFIABLE — something else entirely\n  (none)")
        drive = self.write("round.drive", DRIVE_WITH_BLOCK)
        failures = check.artifact_failures(digest, drive)
        self.assertTrue(any("blocks disagree" in f for f in failures), failures)

    def test_both_paths_are_required(self):
        drive = self.write("round.drive", DRIVE_WITH_BLOCK)
        self.assertEqual(run_main(["--drive", str(drive)]), 1)

    def test_a_valueless_flag_is_an_error_not_a_static_only_pass(self):
        """A trailing flag with no value must fail, never downgrade to the static run.

        `main(['--digest'])` used to yield digest_path=None, which is indistinguishable from
        "no artifacts requested" -- so a round that forgot `--drive` got a green gate over an
        unchecked pair. Found in review on PR #544.
        """
        self.assertEqual(run_main(["--digest"]), 1)
        self.assertEqual(run_main(["--drive"]), 1)
        self.assertEqual(run_main(["--digest", "--drive"]), 1)

    def test_the_equals_form_is_accepted(self):
        block = check.drive_block(DRIVE_WITH_BLOCK)
        digest = self.digest_with(block)
        drive = self.write("round.drive", DRIVE_WITH_BLOCK)
        self.assertEqual(
            run_main(["--digest=%s" % digest, "--drive=%s" % drive]), 0
        )

    def test_a_payload_keeping_an_old_placeholder_heading_still_passes(self):
        """An agent that kept the old template line above the pasted block must still pass.

        The old placeholder started with `UNVERIFIABLE —`, the block's own prefix, so
        `drive_block()` began there and the exact comparison false-failed a correct round.
        The shipped template no longer collides; this pins the guard that also rescues a
        payload rendered from the old one.
        """
        block = check.drive_block(DRIVE_WITH_BLOCK)
        digest = self.digest_with(block)
        placeholder = (
            "UNVERIFIABLE — `drive-unverifiable.py handle`'s block, pasted verbatim (step 6b)\n"
        )
        body = DRIVE_WITH_BLOCK.replace("UNVERIFIABLE — a session", placeholder + "UNVERIFIABLE — a session", 1)
        drive = self.write("round.drive", body)
        self.assertEqual(check.artifact_failures(digest, drive), [])


if __name__ == "__main__":
    unittest.main()
