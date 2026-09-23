"""check-changelog.py: the released-tree branch must fail when unreleased code is behind it.

The fold this guards against: a merge landing *after* the release cut puts its entry
under the released heading and leaves no `## Unreleased` section at all, so the release
watcher has nothing to cut and the change never ships. Measured 2026-09-23 — #138's
entry folded under `## v0.39.1` and master carried unreleased code with no Unreleased
section to cut it from.

Counting commits since the tag is too coarse, so the check asks instead whether any file
other than CHANGELOG.md differs from the newest tag. Both of the cases that made the
coarse version wrong are covered below.
"""
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest

SCRIPT = pathlib.Path(__file__).resolve().parent.parent / "check-changelog.py"
PREAMBLE = "# Changelog\n\nPlease choose versions by Semantic Versioning.\n\n"
UNRELEASED = PREAMBLE + "## Unreleased\n\n- fix: something in flight\n\n## v1.0.0\n\n- feat: shipped\n"
RELEASED = PREAMBLE + "## v1.0.0\n\n- feat: shipped\n"


class CheckChangelogTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        (pathlib.Path(self.dir) / "scripts").mkdir()
        shutil.copy(SCRIPT, pathlib.Path(self.dir) / "scripts" / "check-changelog.py")
        self.git("init", "-q")
        self.git("config", "user.email", "test@example.com")
        self.git("config", "user.name", "test")

    def git(self, *args):
        return subprocess.run(["git", *args], cwd=self.dir, capture_output=True, text=True, check=True)

    def write(self, relpath, text):
        path = pathlib.Path(self.dir) / relpath
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)

    def commit(self, message):
        self.git("add", "-A")
        self.git("commit", "-q", "-m", message)

    def run_check(self):
        return subprocess.run([sys.executable, "scripts/check-changelog.py"], cwd=self.dir,
                              capture_output=True, text=True)

    def test_unreleased_section_passes(self):
        self.write("CHANGELOG.md", UNRELEASED)
        self.commit("init")
        result = self.run_check()
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_released_tree_with_nothing_unreleased_passes(self):
        self.write("CHANGELOG.md", RELEASED)
        self.commit("init")
        self.git("tag", "v1.0.0")
        result = self.run_check()
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_released_tree_with_unreleased_code_fails(self):
        self.write("CHANGELOG.md", RELEASED)
        self.commit("init")
        self.git("tag", "v1.0.0")
        self.write("server/thing.mjs", "export const x = 1;\n")
        self.commit("code landed with no Unreleased section")
        result = self.run_check()
        self.assertEqual(result.returncode, 1)
        self.assertIn("differ from the newest tag", result.stderr)
        self.assertIn("server/thing.mjs", result.stderr)

    def test_released_tree_with_only_changelog_repaired_passes(self):
        """Repairing a released section's text after the cut leaves CHANGELOG.md
        differing from the tag while nothing is unreleased. Not a fold."""
        self.write("CHANGELOG.md", RELEASED)
        self.commit("init")
        self.git("tag", "v1.0.0")
        self.write("CHANGELOG.md", RELEASED + "\n- fix: reworded inside the released section\n")
        self.commit("repair released text")
        result = self.run_check()
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
