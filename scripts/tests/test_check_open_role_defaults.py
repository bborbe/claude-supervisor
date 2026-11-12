"""check-open-role-defaults.py: a present-but-blank `role`/`mode` is a default, not a value.

`commands/open.md` § Step 0.6 used to resolve `role:` by key *presence* — branch 1 read
"`role:` present → use it" — so a row carrying `role: ""` was used verbatim and opened with
no role at all. `vault-cli task set "<task>" role ""` writes exactly that, so the population
is reachable. `docs/fleet-surface.md` item 6 had the same shape for `mode:`, passing the
argument "whenever the field is present".

The load-bearing cases are the two `pre_change` tests. They feed the wording that shipped
before 2026-11-12 and assert the guard rejects it. A guard that cannot fail on the tree it
was written against proves nothing.
"""
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest

SCRIPT = pathlib.Path(__file__).resolve().parent.parent / "check-open-role-defaults.py"
ROOT = SCRIPT.parent.parent

#: A tree the guard accepts: the blank-value arm present in both files.
GOOD_OPEN = (
    "## Step 0.6 — Classify and fill the missing `role` and `mode`\n\n"
    "Rules and defaults have one home, [`docs/role-mode-matrix.md`](../docs/role-mode-matrix.md).\n\n"
    "1. **`role:` present and non-blank** → use it.\n"
    "2. **`role:` absent, or present but blank** → classify it now from the task's body.\n\n"
    "### Then decide `mode`\n\n"
    "What this file owns is the storage and the write. "
    "⚠️ **A blank `mode:` is absent, not a value.** Resolve it per Rule 2.\n"
)

GOOD_SURFACE = (
    "## Spawn a worker\n\n"
    "6. **Decide the mode.** The mapping has its single home in "
    "[`docs/role-mode-matrix.md`](role-mode-matrix.md). A spawn site passes the argument "
    "**whenever the field carries a non-blank value** and omits it when absent or blank.\n"
)

GOOD_MATRIX = (
    "# Role × mode decision matrix\n\n"
    "| no signal at all | **`agent` (default)** |\n"
    "| agent, none of them | **`headless` (default)** |\n"
)

#: The wording that shipped before this change — the negative control for `role`.
PRE_CHANGE_OPEN = GOOD_OPEN.replace(
    "1. **`role:` present and non-blank** → use it.\n"
    "2. **`role:` absent, or present but blank** → classify it now from the task's body.\n",
    "1. **`role:` present** → use it. Never re-derive over a value already on disk.\n"
    "2. **`role:` absent** → classify it now from the task's body.\n",
).replace(
    "⚠️ **A blank `mode:` is absent, not a value.** Resolve it per Rule 2.\n",
    "Resolve `mode` per that rule, write the field, and read it back at Step 3.\n",
)

#: The item-6 wording that shipped before this change — the negative control for `mode`.
PRE_CHANGE_SURFACE = GOOD_SURFACE.replace(
    "**whenever the field carries a non-blank value**",
    "**whenever the field is present**",
)


class CheckOpenRoleDefaultsTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        (pathlib.Path(self.dir) / "scripts").mkdir()
        shutil.copy(SCRIPT, pathlib.Path(self.dir) / "scripts" / SCRIPT.name)
        self.write("commands/open.md", GOOD_OPEN)
        self.write("docs/fleet-surface.md", GOOD_SURFACE)
        self.write("docs/role-mode-matrix.md", GOOD_MATRIX)

    def write(self, relpath, text):
        path = pathlib.Path(self.dir) / relpath
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def run_check(self, cwd=None):
        return subprocess.run(
            [sys.executable, "scripts/check-open-role-defaults.py"],
            cwd=cwd or self.dir,
            capture_output=True,
            text=True,
        )

    def test_wired_tree_passes(self):
        result = self.run_check()
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_shipped_tree_passes(self):
        """The real tree this branch ships must satisfy its own guard."""
        result = self.run_check(cwd=ROOT)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_pre_change_role_branch_fails(self):
        """`role:` tested by key presence must be rejected — the negative control.

        Without this case the guard could pass on a tree that still uses the blank value
        verbatim, and every later pass would be indistinguishable from a no-op.
        """
        self.write("commands/open.md", PRE_CHANGE_OPEN)
        result = self.run_check()
        self.assertEqual(result.returncode, 1)
        self.assertIn("by key presence", result.stderr)

    def test_pre_change_surface_wording_fails(self):
        """Item 6 passing `mode:` on key presence must be rejected."""
        self.write("docs/fleet-surface.md", PRE_CHANGE_SURFACE)
        result = self.run_check()
        self.assertEqual(result.returncode, 1)
        self.assertIn("fleet-surface.md", result.stderr)
        self.assertIn("by key presence", result.stderr)

    def test_missing_blank_mode_marker_fails(self):
        """Dropping the blank-`mode` sentence leaves the mode arm unguarded."""
        self.write(
            "commands/open.md",
            GOOD_OPEN.replace("⚠️ **A blank `mode:` is absent, not a value.** ", ""),
        )
        result = self.run_check()
        self.assertEqual(result.returncode, 1)
        self.assertIn("blank `mode:`", result.stderr)

    def test_missing_blank_role_markers_fail(self):
        """Reverting branch 1 to the present-only wording is the exact defect."""
        self.write(
            "commands/open.md",
            GOOD_OPEN.replace(
                "1. **`role:` present and non-blank** → use it.",
                "1. **`role:` present** → use it.",
            ),
        )
        result = self.run_check()
        self.assertEqual(result.returncode, 1)
        self.assertIn("by key presence", result.stderr)

    def test_missing_command_fails(self):
        (pathlib.Path(self.dir) / "commands" / "open.md").unlink()
        result = self.run_check()
        self.assertEqual(result.returncode, 1)
        self.assertIn("does not exist", result.stderr)


if __name__ == "__main__":
    unittest.main()
