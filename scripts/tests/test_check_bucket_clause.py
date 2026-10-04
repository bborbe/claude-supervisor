"""check-bucket-clause.py: the per-bucket shape clause must not drift across its three homes.

The defect this guards against, measured across two days: the clause telling a sweep reader
what `--write-buckets` accepts lives byte-identical in `commands/manager-loop.md`,
`manager-drive.md` and `manager-status.md`, and was amended by hand in all three on
2026-10-03 (the vocabulary half) and again on 2026-10-04 (the shape half) — each time with
the copies compared by eye.

The load-bearing case is `test_indentation_differences_are_allowed`. The three files nest
the paragraph to their own depth, so a guard comparing raw lines would fail on a clean tree
and be switched off within a day. `test_duplicated_clause_fails` is the second: the check is
keyed on `len(hits) != 1`, so a copy that carries the clause twice must fail rather than
have the comparison silently pick one of them.
"""
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest

SCRIPT = pathlib.Path(__file__).resolve().parent.parent / "check-bucket-clause.py"

#: The clause as the three commands carry it, trimmed to the opening words the check anchors on.
CLAUSE = (
    "Both doors into the record share one shape check — `--write-buckets` and `--save`'s "
    "read path alike — so **every declared bucket must appear**, each mapping to a list of names."
)
COMMANDS = (
    "commands/manager-loop.md",
    "commands/manager-drive.md",
    "commands/manager-status.md",
)


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        (pathlib.Path(self.dir) / "scripts").mkdir()
        shutil.copy(SCRIPT, pathlib.Path(self.dir) / "scripts" / "check-bucket-clause.py")
        for i, rel in enumerate(COMMANDS):
            # Each file nests the paragraph to its own depth, exactly as the real three do.
            self.write(rel, "---\ntitle: x\n---\n\n" + " " * (3 + i) + CLAUSE + "\n")

    def write(self, relpath, text):
        path = pathlib.Path(self.dir) / relpath
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)

    def run_check(self):
        return subprocess.run(
            [sys.executable, "scripts/check-bucket-clause.py"],
            cwd=self.dir,
            capture_output=True,
            text=True,
        )


class TestBucketClauseGuard(Base):
    def test_identical_clause_passes(self):
        result = self.run_check()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("byte-identical", result.stdout)

    def test_indentation_differences_are_allowed(self):
        """The reason the comparison is on stripped lines. The three real files indent this
        paragraph to their own nesting depth, so a raw-line compare would fail on a clean
        tree — and a guard that fails on a clean tree is switched off, not fixed."""
        result = self.run_check()
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_perturbed_copy_fails(self):
        self.write(COMMANDS[1], "\n" + CLAUSE.replace("a list of names", "a non-empty list of names") + "\n")
        result = self.run_check()
        self.assertEqual(result.returncode, 1)
        self.assertIn("disagree", result.stderr)
        self.assertIn(COMMANDS[1], result.stderr)

    def test_missing_clause_fails(self):
        self.write(COMMANDS[2], "\n---\ntitle: x\n---\n\nNo clause here.\n")
        result = self.run_check()
        self.assertEqual(result.returncode, 1)
        self.assertIn("carries the clause 0 times", result.stderr)

    def test_duplicated_clause_fails(self):
        """`len(hits) != 1` — a copy carrying the clause twice must fail rather than have the
        comparison silently pick one of the two."""
        self.write(COMMANDS[0], "\n" + CLAUSE + "\n" + CLAUSE + "\n")
        result = self.run_check()
        self.assertEqual(result.returncode, 1)
        self.assertIn("carries the clause 2 times", result.stderr)

    def test_missing_file_fails(self):
        (pathlib.Path(self.dir) / COMMANDS[0]).unlink()
        result = self.run_check()
        self.assertEqual(result.returncode, 1)
        self.assertIn("does not exist", result.stderr)


if __name__ == "__main__":
    unittest.main()
