"""check-subject-write-scope.py: the reconcile's write-scope table must stay true of the
four commands it names.

The guard's whole value is whether it can fail, so the load-bearing cases here are the
mutations that would otherwise ship green — each one is a defect a real review round on
PR #173 actually found:

  * a writer losing its invocation — the table would claim a write that never fires;
  * a non-writer carrying the imperative — the file would instruct a vault write it is not
    permitted to make, which is the rule collision the third round found;
  * the two writers' paragraphs diverging outside the step slot;
  * a non-writer regaining `Bash(vault-cli:*)` while the table still says it holds none.

`test_shipped_tree_passes` runs against this repository's real files rather than a fixture,
so an edit that satisfies the fixture while breaking the shipped tree is still caught.
"""
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest

SCRIPT = pathlib.Path(__file__).resolve().parent.parent / "check-subject-write-scope.py"
REPO = SCRIPT.parent.parent
DOC = "docs/subject-resolution.md"
COMMANDS = [
    "commands/manager-loop.md",
    "commands/manager-drive.md",
    "commands/manager-status.md",
    "commands/manager-verify.md",
]


class CheckSubjectWriteScopeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = pathlib.Path(self.tmp.name)
        (self.root / "docs").mkdir()
        (self.root / "commands").mkdir()
        shutil.copy(REPO / DOC, self.root / DOC)
        for rel in COMMANDS:
            shutil.copy(REPO / rel, self.root / rel)

    def run_guard(self):
        # The script resolves REPO from its own location, so it runs from a copy placed one
        # level inside the fixture tree — the same layout it expects in the real repo.
        scripts = self.root / "scripts"
        scripts.mkdir(exist_ok=True)
        shutil.copy(SCRIPT, scripts / SCRIPT.name)
        return subprocess.run(
            [sys.executable, str(scripts / SCRIPT.name)], capture_output=True, text=True
        )

    def mutate(self, rel, old, new):
        path = self.root / rel
        text = path.read_text(encoding="utf-8")
        self.assertIn(old, text, f"{rel}: fixture anchor missing")
        path.write_text(text.replace(old, new, 1), encoding="utf-8")

    def test_shipped_tree_passes(self):
        result = self.run_guard()
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_writer_losing_its_invocation_fails(self):
        self.mutate(
            "commands/manager-drive.md",
            "§ *Reconcile the subject's status* **now**",
            "§ *Reconcile the subject's status*",
        )
        result = self.run_guard()
        self.assertEqual(result.returncode, 1)
        self.assertIn("manager-drive.md", result.stderr)

    def test_non_writer_carrying_the_imperative_fails(self):
        self.mutate(
            "commands/manager-status.md",
            "**⚠️ The subject's status is left exactly as found",
            "**⚠️ Reconcile the subject's status — a step to run, not a reference to follow.** "
            "Run § *Reconcile the subject's status* **now**, before the report.\n\n"
            "**⚠️ The subject's status is left exactly as found",
        )
        result = self.run_guard()
        self.assertEqual(result.returncode, 1)
        self.assertIn("manager-status.md", result.stderr)

    def test_writers_diverging_outside_the_step_slot_fails(self):
        self.mutate("commands/manager-drive.md", "never again mid-run", "never again this pass")
        result = self.run_guard()
        self.assertEqual(result.returncode, 1)
        self.assertIn("manager-drive.md", result.stderr)

    def test_non_writer_that_never_names_the_section_fails(self):
        # Assertion (b): a non-writer that says nothing at all reads as a missing step rather
        # than a stated carve-out.
        # Both of this file's mentions must go — the divergence list's entry and the carve-out
        # sentence that names the section. Removing only one leaves the assertion satisfied.
        self.mutate(
            "commands/manager-verify.md",
            "the recording block, and § *Reconcile the subject's status*.",
            "the recording block.",
        )
        self.mutate(
            "commands/manager-verify.md",
            "§ *Reconcile the subject's status*, which names this command and `/manager-status`",
            "the write-scope carve-out, which names this command and `/manager-status`",
        )
        result = self.run_guard()
        self.assertEqual(result.returncode, 1)
        self.assertIn("manager-verify.md", result.stderr)

    def test_table_row_removed_fails(self):
        self.mutate(
            DOC,
            "| `/manager-verify` | yes | **no** — holds no `Bash(vault-cli:*)` |\n",
            "",
        )
        result = self.run_guard()
        self.assertEqual(result.returncode, 1)
        self.assertIn("3 row(s)", result.stderr)

    def test_table_row_duplicated_fails(self):
        self.mutate(
            DOC,
            "| `/manager-drive` | yes | **yes** |",
            "| `/manager-loop` | yes | **yes** |",
        )
        result = self.run_guard()
        self.assertEqual(result.returncode, 1)
        self.assertIn("distinct", result.stderr)

    def test_missing_command_file_fails(self):
        (self.root / "commands/manager-drive.md").unlink()
        result = self.run_guard()
        self.assertEqual(result.returncode, 1)
        self.assertIn("named in the table but missing", result.stderr)

    def test_unreadable_frontmatter_fails_closed(self):
        self.mutate("commands/manager-verify.md", "---\n", "--- \n")
        result = self.run_guard()
        self.assertEqual(result.returncode, 1)
        self.assertIn("could not be read", result.stderr)

    def test_writer_losing_the_paragraph_marker_fails(self):
        # The one branch with no mutation pinning it: the imperative can survive while the
        # shared marker does not, and the paragraph comparison then has nothing to compare.
        self.mutate(
            "commands/manager-drive.md",
            "**⚠️ Reconcile the subject's status — a step to run, not a reference to follow.**",
            "**⚠️ Reconcile the subject's status.**",
        )
        result = self.run_guard()
        self.assertEqual(result.returncode, 1)
        self.assertIn("shared paragraph marker", result.stderr)

    def test_reworded_non_writer_reason_fails(self):
        # The grant check is keyed on the row's stated reason, so a reworded cell must fail
        # rather than skip the assertion silently.
        self.mutate(
            DOC,
            "**no** — holds no `Bash(vault-cli:*)` |",
            "**no** — lacks the grant |",
        )
        result = self.run_guard()
        self.assertEqual(result.returncode, 1)
        self.assertIn("neither recognised reason", result.stderr)

    def test_writer_missing_a_required_grant_fails(self):
        # The guarantee that shipped false: manager-drive carried the invocation without the
        # awk grant the rule needs, and nothing asserted the writers' grants.
        self.mutate("commands/manager-drive.md", "  - Bash(awk:*)\n", "")
        result = self.run_guard()
        self.assertEqual(result.returncode, 1)
        self.assertIn("Bash(awk:*)", result.stderr)

    def test_non_writer_regaining_the_grant_fails(self):
        self.mutate(
            "commands/manager-verify.md",
            "  - Bash(find:*)",
            "  - Bash(vault-cli:*)\n  - Bash(find:*)",
        )
        result = self.run_guard()
        self.assertEqual(result.returncode, 1)
        self.assertIn("manager-verify.md", result.stderr)


if __name__ == "__main__":
    unittest.main()
