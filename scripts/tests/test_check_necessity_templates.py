"""check-necessity-templates.py: the necessity row shapes must not drift or be dropped.

The widening of 2026-10-07 introduced three row shapes and preserved two strings
byte-identically, and the guard's whole value is that it fails when one of them goes missing.

The load-bearing cases are the two counting tests, and they are why this file exists rather
than being skipped as boilerplate. The `needed: <task> — serves` string occurs in three
templates **and** once inside a sentence at step 8's prose. A guard that counted substring
occurrences therefore passes with a real template deleted — the exact failure it exists to
catch — while one that raised its threshold lets the prose mention satisfy it permanently.
`test_deleting_a_template_site_fails` and `test_prose_mention_does_not_count_as_a_site` pin
both halves of that, and the guard asserts on lines that *begin* with the shape.
"""
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest

SCRIPT = pathlib.Path(__file__).resolve().parent.parent / "check-necessity-templates.py"

READER = "agents/manager-sweep-reader.md"
VERIFY = "agents/manager-verify.md"

SERVES = 'needed: <task> — serves <goal sentence|SC<n>|DoD<n>>: "<served line>" ← "<task line>"'
FOUNDATION = 'needed: <task> — foundation for <goal sentence|SC<n>|DoD<n>>: "<the foundation line>" ← "<task line>"'
NOT_NEEDED = (
    "not needed: <task> — serves none of {goal sentence, Success Criteria, Definition of "
    "Done}, checked against those three"
)
UNPROVEN = (
    "not needed: <task> — unproven: <which of the two lines you could not produce>, checked "
    "against {goal sentence, Success Criteria, Definition of Done}"
)
PRODUCT = "product: <task> — output of <goal> SC<n>"
SUMMARY = (
    "Necessity: <M> needed · <K> not needed · <P> product — inverted set <M+K+P> of "
    "<N> tracked — over <topic page> (<member goals>)"
)
#: The same shape as SERVES, but inside a sentence rather than beginning a line.
PROSE_MENTION = f"Emit the row in the template's shape: `{SERVES}`."


def reader_body(sites: int = 3, prose: bool = True) -> str:
    lines = ["## step 8", "", "```", FOUNDATION, NOT_NEEDED, UNPROVEN, PRODUCT, SUMMARY, "```"]
    lines += [SERVES] * sites
    if prose:
        lines.append(PROSE_MENTION)
    return "\n".join(lines) + "\n"


VERIFY_BODY = "Echo every failing row, plus at least one serving row when any task serves.\n"


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        (pathlib.Path(self.dir) / "scripts").mkdir()
        shutil.copy(SCRIPT, pathlib.Path(self.dir) / "scripts" / "check-necessity-templates.py")
        self.write(READER, reader_body())
        self.write(VERIFY, VERIFY_BODY)

    def write(self, relpath, text):
        path = pathlib.Path(self.dir) / relpath
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)

    def run_check(self):
        return subprocess.run(
            [sys.executable, "scripts/check-necessity-templates.py"],
            cwd=self.dir,
            capture_output=True,
            text=True,
        )


class TestNecessityTemplatesGuard(Base):
    def test_clean_tree_passes(self):
        result = self.run_check()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("row shape(s) pinned per site", result.stdout)

    def test_deleting_a_template_site_fails(self):
        """The reason the guard counts lines, not substrings: the prose mention would keep a
        substring count above its threshold with a real template deleted."""
        self.write(READER, reader_body(sites=2))
        result = self.run_check()
        self.assertEqual(result.returncode, 1)
        self.assertIn("the widened three-source `needed:` row", result.stderr)

    def test_prose_mention_does_not_count_as_a_site(self):
        """The converse: the sentence carrying the shape must not satisfy the count alone."""
        self.write(READER, reader_body(sites=0))
        result = self.run_check()
        self.assertEqual(result.returncode, 1)
        self.assertIn("want >= 3", result.stderr)

    def test_removing_the_product_row_fails(self):
        self.write(READER, reader_body().replace(PRODUCT + "\n", ""))
        result = self.run_check()
        self.assertEqual(result.returncode, 1)
        self.assertIn("`product:` row", result.stderr)

    def test_perturbing_the_summary_line_fails(self):
        self.write(READER, reader_body().replace("inverted set", "inverted_set"))
        result = self.run_check()
        self.assertEqual(result.returncode, 1)
        self.assertIn("summary line", result.stderr)

    def test_removing_the_verify_rule_fails(self):
        self.write(VERIFY, "Echo the rows.\n")
        result = self.run_check()
        self.assertEqual(result.returncode, 1)
        self.assertIn(VERIFY, result.stderr)

    def test_missing_file_fails(self):
        (pathlib.Path(self.dir) / READER).unlink()
        result = self.run_check()
        self.assertEqual(result.returncode, 1)
        self.assertIn("does not exist", result.stderr)


if __name__ == "__main__":
    unittest.main()
