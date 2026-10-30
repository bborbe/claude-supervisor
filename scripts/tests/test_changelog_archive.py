"""changelog-archive.py: the rolling archive keeps CHANGELOG.md under the watcher's cap.

The defect it exists for: `CHANGELOG.md` gains a section per release and is never
pruned, so it grows without bound until it crosses the release watcher's 1 MiB read
limit — and the watcher then drops the whole repo from every poll cycle, so no release
can be cut at all. Measured 2026-10-13 on `bborbe/claude-supervisor`: 1 056 367 bytes
and `repo dropped from cycle … too large` on every cycle.

Two properties are load-bearing and are the reason this file is longer than the script:

* **The retained and archived text is sliced, never re-serialised.** A released
  section's text is frozen history, and the changelog fold guard byte-compares each
  released section against its own tag. A re-joining implementation silently rewrote
  the blank lines around 70 released headings while every count still matched, so the
  verbatim cases below are asserted byte-for-byte rather than by size.
* **Nothing is dropped.** The counts on both sides must add up to the input's, which is
  what makes "history is not lost" checkable instead of merely asserted.
"""
import importlib.util
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

SCRIPT = pathlib.Path(__file__).resolve().parent.parent / "changelog-archive.py"
spec = importlib.util.spec_from_file_location("changelog_archive", SCRIPT)
archive = importlib.util.module_from_spec(spec)
spec.loader.exec_module(archive)

PREAMBLE = "# Changelog\n\nPlease choose versions by Semantic Versioning.\n\n"


def changelog(releases, bullet_bytes=200, unreleased=True):
    """A CHANGELOG with `releases` released sections, newest first."""
    out = [PREAMBLE.rstrip("\n")]
    if unreleased:
        out += ["", "## Unreleased", "", "- fix: something in flight"]
    for i in range(releases, 0, -1):
        out += ["", f"## v0.{i}.0", "", "- feat: " + "x" * bullet_bytes]
    return "\n".join(out) + "\n"


# Big enough that the 384 KiB target bites: 400 sections of ~2 KB is ~800 KiB.
BIG = changelog(400, bullet_bytes=2000)


class ChangelogArchiveTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        (pathlib.Path(self.dir) / "scripts").mkdir()
        shutil.copy(SCRIPT, pathlib.Path(self.dir) / "scripts" / "changelog-archive.py")

    def write(self, text):
        (pathlib.Path(self.dir) / "CHANGELOG.md").write_text(text, encoding="utf-8")

    def read(self, name="CHANGELOG.md"):
        return (pathlib.Path(self.dir) / name).read_text(encoding="utf-8")

    def run_script(self, *args):
        return subprocess.run(
            [sys.executable, "scripts/changelog-archive.py", *args],
            cwd=self.dir, capture_output=True, text=True,
        )

    def section_count(self, text):
        return len(re.findall(r"^## v\d+\.\d+\.\d+$", text, re.M))

    def test_archives_down_to_the_target_and_keeps_unreleased_first(self):
        source = changelog(400)
        self.write(source)
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stderr)

        kept = self.read()
        self.assertLessEqual(len(kept.encode()), archive.ARCHIVE_TARGET_BYTES)
        self.assertEqual(re.findall(r"^## .*$", kept, re.M)[0], "## Unreleased")

    def test_nothing_is_dropped_and_the_text_is_sliced_verbatim(self):
        """The counts add up, and both halves are the input's own bytes."""
        source = BIG
        self.write(source)
        self.run_script()

        kept, archived = self.read(), self.read("CHANGELOG-archive.md")
        self.assertEqual(
            self.section_count(kept) + self.section_count(archived),
            self.section_count(source),
        )

        # The pointer names the newest archived release, which is where the input was cut.
        pointer = re.search(r"^_Releases older than (\S+) ", kept, re.M)
        self.assertIsNotNone(pointer, kept[-400:])
        cut = source.index(f"## {pointer.group(1)}")

        # Retained text is the input's own prefix; the archive is its own suffix. A
        # re-serialising implementation passes the count above and fails here.
        self.assertTrue(
            kept.startswith(source[:cut].rstrip("\n") + "\n\n_Releases older than "),
            "the retained text is not the input's own bytes",
        )
        self.assertTrue(kept.rstrip("\n").endswith("(CHANGELOG-archive.md)._"))
        self.assertEqual(archived, archive.ARCHIVE_HEADER + source[cut:])

    def test_second_run_is_a_no_op(self):
        self.write(BIG)
        self.run_script()
        before = (self.read(), self.read("CHANGELOG-archive.md"))

        result = self.run_script()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("nothing to archive", result.stdout)
        self.assertEqual((self.read(), self.read("CHANGELOG-archive.md")), before)

    def test_a_second_rotation_extends_the_archive_instead_of_replacing_it(self):
        """A rotation that actually cuts again must keep every section archived by the first.

        ⚠️ `test_second_run_is_a_no_op` above never reaches this branch: the second run finds
        nothing to cut, so it returns before writing the archive. That is why a rotation that
        *overwrote* `CHANGELOG-archive.md` passed the suite — observed 2026-10-29, when a real
        second rotation dropped 316 released sections (v0.102.3 … v0.1.0) out of the tree.
        """
        self.write(BIG)
        self.run_script()
        first_archive = self.read("CHANGELOG-archive.md")
        first_sections = self.section_count(first_archive)
        oldest = re.findall(r"^## v\d+\.\d+\.\d+$", first_archive, re.M)[-1]

        # Grow the changelog past the target again, with strictly NEWER versions, so the next
        # run really rotates and its sections belong ahead of the ones already archived.
        newer = [PREAMBLE.rstrip("\n"), "", "## Unreleased", "", "- fix: second batch"]
        for i in range(800, 400, -1):
            newer += ["", f"## v0.{i}.0", "", "- feat: " + "x" * 2000]
        self.write("\n".join(newer) + "\n")
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stderr)

        archive_now = self.read("CHANGELOG-archive.md")
        self.assertGreater(
            self.section_count(archive_now), first_sections,
            "the second rotation did not add to the archive",
        )
        self.assertIn(oldest, archive_now, "a previously archived release was dropped")

        heads = re.findall(r"^## v(\d+)\.(\d+)\.(\d+)$", archive_now, re.M)
        self.assertEqual(len(heads), len(set(heads)), "a release was duplicated in the archive")

        # ⚠️ Count-growth and "the oldest survived" are both satisfiable by an archive written
        # OLDEST-FIRST, or by one that kept only the oldest prior section. The header says
        # "newest first, verbatim", so assert the property itself.
        for head in re.findall(r"^## v\d+\.\d+\.\d+$", first_archive, re.M):
            self.assertIn(head, archive_now, f"a previously archived section was dropped: {head}")
        # ⚠️ A heading surviving is not the section surviving: the file's own contract is
        # byte-for-byte slicing, so a merge that re-serialised the bodies would pass the check
        # above. Assert the OLDEST prior section's full text, verbatim.
        oldest_block = first_archive[first_archive.index(oldest):].rstrip("\n")
        self.assertIn(oldest_block, archive_now,
                      "a previously archived section's body was rewritten, not carried verbatim")
        numeric = [tuple(int(x) for x in h) for h in heads]
        self.assertEqual(
            numeric, sorted(numeric, reverse=True),
            "the archive is not in strictly descending version order",
        )

    def test_the_merge_junction_is_separated_by_a_blank_line(self):
        """Every section in the archive is preceded by exactly one blank line, junction included.

        ⚠️ A bare `archived + kept` concatenation still renders — ATX headings interrupt
        paragraphs — so nothing else in this suite catches it. Observed 2026-10-29 by review:
        the last newly-archived bullet ran straight into the pre-existing heading.
        """
        self.write(BIG)
        self.run_script()
        first = self.read("CHANGELOG-archive.md")

        newer = [PREAMBLE.rstrip("\n"), "", "## Unreleased", "", "- fix: second batch"]
        for i in range(800, 400, -1):
            newer += ["", f"## v0.{i}.0", "", "- feat: " + "x" * 2000]
        self.write("\n".join(newer) + "\n")
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stderr)

        archive_now = self.read("CHANGELOG-archive.md")
        orphans = [
            m.group(0)
            for m in re.finditer(r"^## v\d+\.\d+\.\d+$", archive_now, re.M)
            if not archive_now[: m.start()].endswith("\n\n")
        ]
        self.assertEqual(orphans, [], f"heading(s) with no blank line before them: {orphans}")
        self.assertNotEqual(archive_now, first)

    def test_a_small_changelog_is_left_alone(self):
        source = changelog(3)
        self.write(source)
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("nothing to archive", result.stdout)
        self.assertEqual(self.read(), source)
        self.assertFalse((pathlib.Path(self.dir) / "CHANGELOG-archive.md").exists())

    def test_check_fails_above_the_guard_and_names_the_remedy(self):
        self.write(PREAMBLE + "## Unreleased\n\n- fix: x\n\n" + "filler\n" * (archive.MAX_BYTES // 6))
        result = self.run_script("--check")
        self.assertEqual(result.returncode, 1)
        self.assertIn("make changelog-archive", result.stderr)
        self.assertIn(str(archive.MAX_BYTES), result.stderr)

    def test_check_passes_below_the_guard(self):
        self.write(BIG)
        self.run_script()
        result = self.run_script("--check")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("changelog size ok", result.stdout)

    def test_an_unrecognised_section_heading_is_refused(self):
        """A non-`## vX.Y.Z` section is never silently archived or left behind —
        a Keep-a-Changelog-style heading stops the run instead."""
        self.write(BIG.replace("## v0.400.0", "## Notes", 1))
        result = self.run_script()
        self.assertEqual(result.returncode, 1)
        self.assertIn("unrecognised section heading", result.stderr)
        self.assertFalse((pathlib.Path(self.dir) / "CHANGELOG-archive.md").exists())


if __name__ == "__main__":
    unittest.main()
