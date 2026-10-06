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
