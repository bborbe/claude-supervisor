"""check-worker-target.py: the fleet-wide worker target has one home, and the home
agrees with the code.

The defect this guards against is the repo's own measured history: the spawn cap was
restated in `commands/manager-loop.md`, `commands/manager-verify.md` and the manager
runbook's Guardrail 2 until 2026-09-24 — four homes for one number, which is how a
single cap becomes four counters the day one home is edited and the others are not,
with no error and no diff to catch it.

The load-bearing cases are the two drift tests. `test_code_moves_without_the_doc` and
`test_doc_moves_without_the_code` are the halves a prose-only check cannot reach: both
surfaces stay internally consistent while they disagree, so nothing a reader sees is
wrong. A check that only asserted "the home exists" would pass both.
"""
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest

SCRIPT = pathlib.Path(__file__).resolve().parent.parent / "check-worker-target.py"

#: The home: the section heading, the item the constant lives at, and the stated default.
#: ⚠️ The heading and item are what identify the home — NOT the `Spawn a worker item 5`
#: pointer, which the real file also carries at two unrelated places that cite the rule.
HOME = (
    "## Spawn a worker\n\n"
    "5. **Respect the fleet-wide worker target.** The default is 20. The hard default is 50.\n"
    "An absent key resolves to `DEFAULT_MAX_CONCURRENT`; `0` means unlimited.\n"
)
#: The code side. The regex reads the exported constant, so the shape matters. Both
#: thresholds are here because (c) is asserted per constant: `DEFAULT_MAX_CONCURRENT` must
#: not read the `_HARD` line, and the `\s*=` anchor is what keeps them apart.
CODE = (
    "export const MAX_CONCURRENT_ENV = 'SUPERVISOR_MAX_CONCURRENT'\n"
    "export const DEFAULT_MAX_CONCURRENT = 20\n"
    "export const MAX_CONCURRENT_HARD_ENV = 'SUPERVISOR_MAX_CONCURRENT_HARD'\n"
    "export const DEFAULT_MAX_CONCURRENT_HARD = 50\n"
)
#: A referencing site: names the constant and points at the home rather than restating it.
SITE = (
    "Respect `spawn.maxConcurrent`; see `docs/fleet-surface.md` § Spawn a worker item 5.\n"
)
#: The shape the check exists to reject: the number is copied into a command file, so the
#: home and this copy can drift with nothing to catch it.
SITE_RESTATES = (
    "Respect `spawn.maxConcurrent`, which defaults to 20.\n"
)
#: A site that reads ONE side of the comparison and remembers the other. It names the
#: constant and points at its home, so (b) is satisfied and the file looks complete — but
#: only the count is an instrument, which is the shape measured on 2026-10-02.
SITE_READS_COUNT_ONLY = (
    "Respect `spawn.maxConcurrent`; see `docs/fleet-surface.md` § Spawn a worker item 5.\n"
    "Count with `python3 scripts/worker-sessions.py --count`.\n"
)
#: The pair. Reading both sides is what makes a tick a comparison rather than a memory.
SITE_READS_BOTH = (
    "Respect `spawn.maxConcurrent`; see `docs/fleet-surface.md` § Spawn a worker item 5.\n"
    "Count with `python3 scripts/worker-sessions.py --count` and read the target with\n"
    "`python3 scripts/worker-target.py`.\n"
)


class CheckWorkerTargetTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        (pathlib.Path(self.dir) / "scripts").mkdir()
        shutil.copy(SCRIPT, pathlib.Path(self.dir) / "scripts" / "check-worker-target.py")
        self.write("docs/fleet-surface.md", HOME)
        self.write("server/spawn-mode.mjs", CODE)
        self.write("commands/fleet-loop.md", SITE)
        self.write("agents/manager-drive.md", "No mention of the limit here.\n")

    def write(self, relpath, text):
        path = pathlib.Path(self.dir) / relpath
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)

    def run_check(self):
        return subprocess.run([sys.executable, "scripts/check-worker-target.py"], cwd=self.dir,
                              capture_output=True, text=True)

    def test_wired_tree_passes(self):
        result = self.run_check()
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_referencing_site_without_the_pointer_fails(self):
        """A site that names the constant but cannot reach its home — the file a reader
        following it has no route out of."""
        self.write("commands/fleet-loop.md", SITE_RESTATES)
        result = self.run_check()
        self.assertEqual(result.returncode, 1)
        self.assertIn("no pointer to the constant's home", result.stderr)
        self.assertIn("fleet-loop.md", result.stderr)

    def test_count_read_without_the_target_fails(self):
        """THE LOAD-BEARING CASE FOR (d). The file names the constant, points at its home,
        and reads the live count — so every earlier assertion passes and it reads as
        complete. But only one side of the comparison is an instrument, so the tick measures
        the count and remembers the target. Measured 2026-10-02: a manager carried a
        hand-read value for two hours and acted on 18 against a configured 12."""
        self.write("commands/fleet-loop.md", SITE_READS_COUNT_ONLY)
        result = self.run_check()
        self.assertEqual(result.returncode, 1)
        self.assertIn("never the target", result.stderr)
        self.assertIn("fleet-loop.md", result.stderr)

    def test_count_read_with_the_target_passes(self):
        """The pair satisfies (b) and (d) together: the pointer is present and both sides of
        the comparison are read rather than remembered."""
        self.write("commands/fleet-loop.md", SITE_READS_BOTH)
        result = self.run_check()
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_code_moves_without_the_doc(self):
        """THE LOAD-BEARING CASE. The code default moves and the home is not updated.
        Both files still read as self-consistent, so nothing a reader sees is wrong —
        which is exactly why a grep for the number cannot catch it."""
        self.write("server/spawn-mode.mjs", CODE.replace("= 20", "= 30"))
        result = self.run_check()
        self.assertEqual(result.returncode, 1)
        self.assertIn("have drifted apart", result.stderr)
        self.assertIn("The default is 30", result.stderr)

    def test_doc_moves_without_the_code(self):
        """The mirror: the prose is edited and the constant is not. Same invisibility."""
        self.write("docs/fleet-surface.md", HOME.replace("The default is 20", "The default is 5"))
        result = self.run_check()
        self.assertEqual(result.returncode, 1)
        self.assertIn("have drifted apart", result.stderr)

    def test_hard_default_moves_without_the_doc(self):
        """The hard half of (c), asserted against its OWN constant. A check reading one
        marker for both numbers would let the soft default satisfy the hard one, so the band
        could be widened or closed in code while the home still described the old pair."""
        self.write("server/spawn-mode.mjs", CODE.replace("= 50", "= 70"))
        result = self.run_check()
        self.assertEqual(result.returncode, 1)
        self.assertIn("have drifted apart", result.stderr)
        self.assertIn("The hard default is 70", result.stderr)

    def test_a_home_stating_only_the_soft_default_fails(self):
        """One marker cannot satisfy both halves. A home naming only the soft number
        describes half the enforcement — the operator-named band would be undocumented — so
        it fails even though every other assertion about the home still passes."""
        self.write("docs/fleet-surface.md", HOME.replace(" The hard default is 50.", ""))
        result = self.run_check()
        self.assertEqual(result.returncode, 1)
        self.assertIn("The hard default is 50", result.stderr)

    def test_unreadable_constant_fails_rather_than_passing(self):
        """'Could not check' must never read as 'they agree'. A missing or renamed
        constant is reported, not skipped."""
        self.write("server/spawn-mode.mjs", "// the constant moved or was renamed\n")
        result = self.run_check()
        self.assertEqual(result.returncode, 1)
        self.assertIn("could not read", result.stderr)
        self.assertIn("not the same as passing", result.stderr)

    def test_missing_home_fails_cleanly(self):
        (pathlib.Path(self.dir) / "docs" / "fleet-surface.md").unlink()
        result = self.run_check()
        self.assertEqual(result.returncode, 1)
        self.assertIn("home is missing", result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    def test_home_section_renamed_fails(self):
        """The section was renamed, so every `Spawn a worker item 5` pointer into it now
        lands somewhere else while still looking like a pointer."""
        self.write("docs/fleet-surface.md", HOME.replace("## Spawn a worker", "## Open a worker"))
        result = self.run_check()
        self.assertEqual(result.returncode, 1)
        self.assertIn("the constant's home is gone", result.stderr)

    def test_home_item_renumbered_fails(self):
        """The heading survives but item 5 is gone, so the pointer resolves to a
        neighbouring rule. This is the case the pointer string alone cannot catch — the
        real `docs/fleet-surface.md` contains it twice outside the home."""
        self.write("docs/fleet-surface.md", HOME.replace("5. **Respect", "6. **Respect"))
        result = self.run_check()
        self.assertEqual(result.returncode, 1)
        self.assertIn("renumbered or retitled", result.stderr)

    def test_a_file_not_naming_the_constant_is_not_required_to_point_at_it(self):
        """The check must not demand a pointer from every command file — only from the
        ones that name the constant. A false positive here would make the check noise."""
        self.write("commands/unrelated.md", "This file is about the sweep table.\n")
        result = self.run_check()
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
