"""check-spawn-target.py: a target-reading call site must reach the eligibility rule.

The rule lives in `docs/fleet-surface.md` § Spawn a worker item 7 and it is **advisory** —
a site that reads a disqualified `target:` prints a warning naming the disqualifier and
leaves the decision with the caller; it never refuses the spawn and never rewrites the row.
This check asserts each site points at the rule and carries both halves of that binding.

The load-bearing case is `test_unregistered_citing_site_fails`. The registered set is three,
and the check *discovers* citing sites rather than trusting its own list — so a fourth site
that cites item 7 fails until it is registered. That is the whole point: `commands/open-a2a.md`
joined on 2026-10-17 and no gate covered it, which is the failure this check exists to catch.
"""
import importlib.util
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest

SCRIPT = pathlib.Path(__file__).resolve().parent.parent / "check-spawn-target.py"


def citing_sites():
    """The checker's own `CITING_SITES`, loaded from it rather than copied.

    ⚠️ **Derived, never hand-listed.** A second copy of the site list is exactly the drift the
    checker exists to catch — the sibling fixture held one until 2026-10-17, and the failure
    surfaced as three tests asserting the CHECKER was wrong. This fixture's job is to build a
    tree the checker accepts, so it must agree with the checker by construction.
    """
    spec = importlib.util.spec_from_file_location("check_spawn_target", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.CITING_SITES


#: A wired site: cites the home AND carries both halves of the binding.
SITE = (
    "See `docs/fleet-surface.md` § Spawn a worker item 7 for the rule.\n"
    'Print `⚠️ target "<t>" is not eligible for "<task>": needs <disqualifier>`.\n'
    "⚠️ **Advisory, never a refusal** — print it and leave the decision here.\n"
)
#: Names the home but not the disqualifier — a site that cannot say what disqualifies a row.
SITE_NO_DISQUALIFIER = (
    "See `docs/fleet-surface.md` § Spawn a worker item 7 for the rule.\n"
    "⚠️ **Advisory, never a refusal** — print it and leave the decision here.\n"
)
#: Warns but omits the advisory clause, so the rule reads as a refusal.
SITE_NO_ADVISORY = (
    "See `docs/fleet-surface.md` § Spawn a worker item 7 for the rule.\n"
    'Print `⚠️ target "<t>" is not eligible for "<task>": needs <disqualifier>`.\n'
)
#: The binding with no pointer: a second copy of the rule, which the single-home rule forbids.
SITE_NO_ANCHOR = (
    "The rule is described elsewhere in this file.\n"
    'Print `⚠️ target "<t>" is not eligible for "<task>": needs <disqualifier>`.\n'
    "⚠️ **Advisory, never a refusal** — print it and leave the decision here.\n"
)
#: A site that cites the rule but is not registered — the SC3 falsification.
SITE_UNREGISTERED = SITE
HOME = (
    "## Spawn a worker\n\n"
    "7. **Decide the TARGET before you spawn — per-call, never from a config file.**\n"
)
#: The heading is present but item 7 is gone — renumbered or retitled away.
HOME_WITHOUT_ITEM_7 = "## Spawn a worker\n\n6. **Decide the mode before you spawn.**\n"


class CheckSpawnTargetTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        (pathlib.Path(self.dir) / "scripts").mkdir()
        shutil.copy(SCRIPT, pathlib.Path(self.dir) / "scripts" / "check-spawn-target.py")
        self.write("docs/fleet-surface.md", HOME)
        for site in citing_sites():
            self.write(site, SITE)

    def write(self, relpath, text):
        path = pathlib.Path(self.dir) / relpath
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)

    def run_check(self):
        return subprocess.run([sys.executable, "scripts/check-spawn-target.py"], cwd=self.dir,
                              capture_output=True, text=True)

    def test_wired_tree_passes(self):
        result = self.run_check()
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_site_without_the_reference_fails(self):
        """SC1 — the pointer is missing, so a reader following the site has no route to the
        rule at all, and the warning it does carry is a second copy of the rule."""
        self.write("commands/open.md", SITE_NO_ANCHOR)
        result = self.run_check()
        self.assertEqual(result.returncode, 1)
        self.assertIn("commands/open.md", result.stderr)
        self.assertIn("no reference to the rule's home", result.stderr)

    def test_registered_site_that_stops_citing_fails(self):
        """SC1, from the discovery side: the file still exists but no longer cites item 7."""
        self.write("commands/steps/manager-spawn.md", SITE_NO_ANCHOR)
        result = self.run_check()
        self.assertEqual(result.returncode, 1)
        self.assertIn("commands/steps/manager-spawn.md", result.stderr)

    def test_site_without_the_disqualifier_warning_fails(self):
        """SC2, first branch: a site that never names the disqualifier cannot warn about one."""
        self.write("commands/open-a2a.md", SITE_NO_DISQUALIFIER)
        result = self.run_check()
        self.assertEqual(result.returncode, 1)
        self.assertIn("commands/open-a2a.md", result.stderr)
        self.assertIn("disqualifier", result.stderr)

    def test_site_without_the_advisory_clause_fails(self):
        """SC2, second branch: without the advisory clause the rule reads as a refusal, and a
        misclassified row loses its route to a local spawn."""
        self.write("commands/steps/manager-spawn.md", SITE_NO_ADVISORY)
        result = self.run_check()
        self.assertEqual(result.returncode, 1)
        self.assertIn("commands/steps/manager-spawn.md", result.stderr)
        self.assertIn("advisory", result.stderr)

    def test_unregistered_citing_site_fails(self):
        """SC3 — the set is discovered, not trusted. This is the `commands/open-a2a.md` case:
        a new site that cites item 7 while no gate knows about it."""
        self.write("commands/brand-new.md", SITE_UNREGISTERED)
        result = self.run_check()
        self.assertEqual(result.returncode, 1)
        self.assertIn("commands/brand-new.md", result.stderr)
        self.assertIn("not a registered", result.stderr)

    def test_registered_site_that_does_not_exist_fails(self):
        (pathlib.Path(self.dir) / "commands" / "open-a2a.md").unlink()
        result = self.run_check()
        self.assertEqual(result.returncode, 1)
        self.assertIn("does not exist", result.stderr)

    def test_home_must_carry_item_7(self):
        """A home reduced to its heading passes a naive grep while defining nothing."""
        self.write("docs/fleet-surface.md", HOME_WITHOUT_ITEM_7)
        result = self.run_check()
        self.assertEqual(result.returncode, 1)
        self.assertIn("no item 7", result.stderr)

    def test_missing_home_fails_cleanly(self):
        (pathlib.Path(self.dir) / "docs" / "fleet-surface.md").unlink()
        result = self.run_check()
        self.assertEqual(result.returncode, 1)
        self.assertIn("home is missing", result.stderr)
        self.assertNotIn("Traceback", result.stderr)


if __name__ == "__main__":
    unittest.main()
