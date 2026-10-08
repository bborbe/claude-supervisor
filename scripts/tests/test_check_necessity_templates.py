"""check-necessity-templates.py: the necessity row shapes must not drift or be dropped.

The widening of 2026-10-07 introduced three row shapes and preserved two strings
byte-identically, and the guard's whole value is that it fails when one of them goes missing.

The counting cases are why this file exists rather than being skipped as boilerplate, and
there are four of them because three successive guards were each too weak:

- A **substring** count passes with a real template deleted, because the same string also
  appears inside a prose sentence — `test_prose_mention_does_not_count_as_a_site`.
- A **floor** passes when a site is duplicated to mask a deletion — `test_duplicated_site_fails`.
- **Prefix** matching passes when a row drifts past the pinned shape —
  `test_divergent_row_shape_fails`.
- An **asymmetric** guard that line-anchors its rows but substring-matches its pinned strings
  is blind to a single deleted summary line — `test_deleting_one_summary_site_fails`.
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

SERVES = 'needed: <task> — serves <SC<n>|DoD<n>>: "<served line>" ← "<task line>"'
FOUNDATION = 'needed: <task> — foundation for <SC<n>|DoD<n>>: "<the foundation line>" ← "<task line>"'
NOT_NEEDED = (
    "not needed: <task> — serves none of {Success Criteria, Definition of "
    "Done}, checked against those two"
)
UNPROVEN = (
    "not needed: <task> — unproven: <which of the two lines you could not produce>, checked "
    "against {Success Criteria, Definition of Done}"
)
PRODUCT = "product: <task> — output of <goal> SC<n>"
SUMMARY = (
    "Necessity: <M> needed · <K> not needed · <P> product — inverted set <M+K+P> of "
    "<N> tracked — over <topic page> (<member goals>)"
)
#: The same shape as SERVES, but inside a sentence rather than beginning a line.
PROSE_MENTION = f"Emit the row in the template's shape: `{SERVES}`."


def reader_body(serves: int = 3, summaries: int = 3, prose: bool = True) -> str:
    """A minimal stand-in for step 8: the block, then the two frames' copies."""
    lines = ["## step 8", "", "```", FOUNDATION, NOT_NEEDED, UNPROVEN, PRODUCT]
    lines += [SUMMARY] * summaries
    lines += [SERVES] * serves
    lines += ["```"]
    if prose:
        lines.append(PROSE_MENTION)
    return "\n".join(lines) + "\n"


VERIFY_BODY = (
    "Echo every failing row, plus at least one serving row when any task serves.\n"
    "  1 Necessity ..... PASS | FAIL | UNKNOWN | SKIPPED — <at least one serving row when "
    "any task serves>\n"
)


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
        self.assertIn("pinned at their exact sites", result.stdout)

    def test_deleting_a_template_site_fails(self):
        """The reason the guard counts lines, not substrings: the prose mention would keep a
        substring count above its threshold with a real template deleted."""
        self.write(READER, reader_body(serves=2))
        result = self.run_check()
        self.assertEqual(result.returncode, 1)
        self.assertIn("the two-source `needed:` row", result.stderr)

    def test_prose_mention_does_not_count_as_a_site(self):
        """The converse: the sentence carrying the shape must not satisfy the count alone."""
        self.write(READER, reader_body(serves=0))
        result = self.run_check()
        self.assertEqual(result.returncode, 1)
        self.assertIn("want exactly 3", result.stderr)

    def test_duplicated_site_fails(self):
        """Why the comparison is equality and not a floor: with `found < want` a duplicated
        site keeps the count at its threshold while a real one is deleted."""
        self.write(READER, reader_body(serves=4))
        result = self.run_check()
        self.assertEqual(result.returncode, 1)
        self.assertIn("want exactly 3", result.stderr)

    def test_retired_source_reappearing_fails(self):
        """The RETIRED assertion is the only check here that reads *wording* rather than
        shape, and the reason it exists: every other assertion matches a row's **prefix**,
        so a source deleted from **inside** a row — or restored uniformly at all three
        sites — satisfies every count and the one-shape assertion alike. That makes it the
        one check no other test can reach, in either direction, so it carries its own."""
        self.write(
            READER,
            reader_body().replace("<SC<n>|DoD<n>>", "<goal sentence|SC<n>|DoD<n>>"),
        )
        result = self.run_check()
        self.assertEqual(result.returncode, 1)
        self.assertIn("still carries the retired goal-sentence source", result.stderr)

    def test_divergent_row_shape_fails(self):
        """Prefix matching alone would pass this: the drifted row still begins with the
        pinned shape, so only the all-sites-identical check catches it."""
        drifted = SERVES.replace("<task line>", "<the task line>")
        self.write(READER, reader_body().replace(SERVES + "\n", drifted + "\n", 1))
        result = self.run_check()
        self.assertEqual(result.returncode, 1)
        self.assertIn("more than one shape", result.stderr)

    def test_deleting_one_summary_site_fails(self):
        """The asymmetric-guard case: a bare substring check is blind to one deleted copy."""
        self.write(READER, reader_body(summaries=2))
        result = self.run_check()
        self.assertEqual(result.returncode, 1)
        self.assertIn("summary line", result.stderr)

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

    def test_removing_one_verify_rule_fails(self):
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
