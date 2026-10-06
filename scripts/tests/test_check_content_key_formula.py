"""check-content-key-formula.py: the content_key derivation must stay stated, and its guard
must actually fail when the clause loses a part of it.

The guard's own value is entirely in whether it can fail, so the load-bearing cases here are
the two false-greens the maintainer bot found on PR #172 — both of which the first version of
the guard had, and neither of which the real clause would have revealed:

  * `test_mode_removed_from_the_exclusion_sentence_fails` — the clause *mentions* `mode:`
    elsewhere (its measured note quotes `` `mode: interactive` ``), so a whole-clause
    substring test stays green after `mode:` is deleted from the list it belongs in. That is
    the one field the entire fix depends on, since clause (6) writes it and that write is what
    orphaned the key.
  * `test_algorithm_tokens_in_separate_sentences_fail` — the original assertion was
    `re.compile(r"sha256.*?16 hex", re.S)`, an unbounded lazy span under DOTALL, satisfied by
    `sha256` anywhere in the clause and `16 hex` anywhere later. The replacement requires both
    tokens in one sentence, and this case is what pins that.

`test_shipped_clause_passes` is the third: it runs the guard against this repository's real
`agents/manager-drive.md` rather than a fixture, so a future edit that satisfies the fixture
while breaking the shipped clause is caught.
"""
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest

SCRIPT = pathlib.Path(__file__).resolve().parent.parent / "check-content-key-formula.py"
REPO = SCRIPT.parent.parent
HOME = "agents/manager-drive.md"

#: A minimal clause (1) carrying every token the guard asserts, in the sentence structure the
#: guard requires: the formula and its truncation in ONE sentence, the excluded fields in a
#: sentence opening with the anchor, and the cut stated as a rule.
CLAUSE = (
    "**The cache — re-audit only what changed.** Persist each row's verdict and score **keyed "
    "to the row file's content**, and re-audit only when that key changes. ⚠️ **The key is "
    "derived by the formula stated here, never chosen per tick** — `sha256` over the row file's "
    "bytes with the four tick-written fields the audit does not judge removed, truncated to the "
    "first 16 hex. The excluded fields are `mode:`, `last_auto_resume`, `claude_session_id` and "
    "`metrics_sessions`. ⚠️ **`status:` and `phase:` are NOT excluded, and the cut is *the "
    "fields whose value the audit does not read*, never *everything a tick writes*.** Drop each "
    "key line together with that key's continuation block, and leave every other byte as it is."
)

#: Clause (6)'s measured note, which also mentions `mode:` — the reason the exclusion assertion
#: must be scoped to the exclusion sentence rather than the whole clause.
MEASURED_NOTE = (
    " ⚠️ Measured 2026-10-06: the key is `f66c7a1136d52022` with its `mode: interactive` line "
    "present and `2763ce6e2710efe6` with it removed."
)


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        (pathlib.Path(self.dir) / "scripts").mkdir()
        shutil.copy(SCRIPT, pathlib.Path(self.dir) / "scripts" / "check-content-key-formula.py")
        self.write_clause(CLAUSE + MEASURED_NOTE)

    def write_clause(self, clause):
        path = pathlib.Path(self.dir) / HOME
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(clause + "\n\n**(2) The next clause.**\n")

    def run_check(self, cwd=None):
        return subprocess.run(
            [sys.executable, "scripts/check-content-key-formula.py"],
            cwd=cwd or self.dir,
            capture_output=True,
            text=True,
        )


class TestContentKeyFormulaGuard(Base):
    def test_valid_clause_passes(self):
        result = self.run_check()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("content-key-formula ok", result.stdout)

    def test_mode_removed_from_the_exclusion_sentence_fails(self):
        """The false-green the bot found. `mode:` still appears in the clause — the measured
        note quotes `` `mode: interactive` `` — so only a scoped assertion catches this."""
        self.write_clause(CLAUSE.replace("`mode:`, `last_auto_resume`", "`last_auto_resume`") + MEASURED_NOTE)
        result = self.run_check()
        self.assertEqual(result.returncode, 1)
        self.assertIn("omits `mode:`", result.stderr)

    def test_algorithm_tokens_in_separate_sentences_fail(self):
        """The other false-green. `sha256` in one sentence and `16 hex` in a later one must not
        satisfy a guard whose message claims a single stated formula."""
        split = CLAUSE.replace(
            "removed, truncated to the first 16 hex.",
            "removed. A leg wrote 16 hex once, another 12.",
        )
        self.write_clause(split + MEASURED_NOTE)
        result = self.run_check()
        self.assertEqual(result.returncode, 1)
        self.assertIn("no single sentence states both", result.stderr)

    def test_truncation_removed_fails(self):
        self.write_clause(CLAUSE.replace(", truncated to the first 16 hex", "") + MEASURED_NOTE)
        self.assertEqual(self.run_check().returncode, 1)

    def test_cut_rule_removed_fails(self):
        self.write_clause(
            CLAUSE.replace("the cut is *the fields whose value the audit does not read*", "the cut is deliberate")
            + MEASURED_NOTE
        )
        result = self.run_check()
        self.assertEqual(result.returncode, 1)
        self.assertIn("the cut is not stated as", result.stderr)

    def test_phase_carve_out_removed_fails(self):
        """Without it the four names read as a sample and the next reader adds `phase:` —
        which freezes every approval's verdict label."""
        self.write_clause(CLAUSE.replace("`status:` and `phase:` are NOT excluded", "`status:` is excluded") + MEASURED_NOTE)
        result = self.run_check()
        self.assertEqual(result.returncode, 1)
        self.assertIn("`status:` / `phase:` are NOT", result.stderr)

    def test_renamed_clause_reports_itself_stale(self):
        """The staleness escape hatch: a wholesale clause rename must fail loudly rather than
        pass by never finding the clause."""
        self.write_clause(CLAUSE.replace("**The cache — re-audit only what changed.**", "**The cache.**") + MEASURED_NOTE)
        result = self.run_check()
        self.assertEqual(result.returncode, 1)
        self.assertIn("this check is stale", result.stderr)

    def test_unbounded_clause_window_fails_closed(self):
        """Without a following `**(2)` heading the clause window would widen to the rest of the
        file, so every assertion below could be satisfied by a later clause while the check
        reported green against text that is not this clause. It must fail, not widen."""
        (pathlib.Path(self.dir) / HOME).write_text(CLAUSE + MEASURED_NOTE + "\n")
        result = self.run_check()
        self.assertEqual(result.returncode, 1)
        self.assertIn("is stale", result.stderr)

    def test_earlier_sha256_mention_does_not_fail_the_guard(self):
        """`sha256` mentioned above the derivation *without* its truncation must not turn the
        guard red. The assertion is that SOME sentence carries both tokens — asserting it of
        the FIRST sentence carrying `sha256` fails here, and its message would name the
        opposite problem from the one on disk."""
        self.write_clause(
            "**The cache — re-audit only what changed.** A note: `sha256[:16]` of the file is "
            "shown below. " + CLAUSE
        )
        result = self.run_check()
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_block_extent_removed_fails(self):
        """The CRITICAL from the second review round. `metrics_sessions` is a multi-line YAML
        block, so a deletion that drops only its key line leaves the indented `- session_id:`
        entries in the key — and those uuids change on every worker run."""
        self.write_clause(
            CLAUSE.replace("together with that key's continuation block, ", "") + MEASURED_NOTE
        )
        result = self.run_check()
        self.assertEqual(result.returncode, 1)
        self.assertIn("continuation block", result.stderr)

    def test_missing_file_fails_closed(self):
        (pathlib.Path(self.dir) / HOME).unlink()
        result = self.run_check()
        self.assertEqual(result.returncode, 1)
        self.assertIn("missing", result.stderr)

    def test_shipped_clause_passes(self):
        """Against the repository's real clause, not the fixture — a future edit that satisfies
        the fixture while breaking the shipped clause is caught here."""
        result = self.run_check(cwd=REPO)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
