"""check-open-manager-resolution.py: a subject's manager is read from its page unconditionally.

`commands/open.md` resolves a goal's or topic's manager from the session registry, whose empty
match is ambiguous — it cannot tell "no manager ever existed" from "the manager exited". The
page's `claude_session_id` is what distinguishes them, so the read must run *before* the
registry result rather than only when it matches nothing.

The load-bearing case is `test_pre_change_text_fails`. It feeds the wording that shipped before
2026-11-02 — the conditional page read and the bare `None` spawn arm — and asserts the check
rejects it. A check that cannot fail on the tree it was written against proves nothing.
"""
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest

SCRIPT = pathlib.Path(__file__).resolve().parent.parent / "check-open-manager-resolution.py"
ROOT = SCRIPT.parent.parent

#: A tree the check accepts: both branches unconditional, both stating the ambiguity, both
#: keeping the no-page-id spawn arm.
GOOD = (
    "## Step 2B — Goal branch\n\n"
    "1. Resolve from the registry.\n"
    "   ⚠️ **Read the goal page's `claude_session_id` UNCONDITIONALLY, before the registry "
    "result.**\n"
    "   ⚠️ **An empty match never *proves* \"no manager ever existed\".**\n"
    "4. **`UNKNOWN` or `AMBIGUOUS`, or `None` with no page id → spawn.** ⚠️ **`None` here "
    "means registry empty AND the page carried no session id.** Spawn from "
    '`manager-launcher.py" "<manager_launcher>" "<goal page>" --default-source manager-default`.\n\n'
    "## Step 2C — Topic branch\n\n"
    "1. Resolve from the registry; an empty match is **ambiguous**.\n"
    "   ⚠️ **Read the topic page's `claude_session_id` UNCONDITIONALLY, before the registry "
    "result.**\n"
    "4. **`UNKNOWN` or `AMBIGUOUS`, or `None` with no page id → spawn.** ⚠️ **`None` here "
    "means registry empty AND the page carried no session id.** Spawn from "
    '`manager-launcher.py" "<manager_launcher>" "<topic page>" --default-source manager-default`.\n\n'
    "## Step 3 — Spawn the tab (resume/create branches)\n"
)

#: The launcher negative control — `GOOD` with the flag dropped from Step 2C's invocation, while
#: the flag still appears in that branch's prose. A bare-substring marker is satisfied by the
#: sentence and the command goes unguarded, which is why the marker is pinned as the whole
#: invocation. Measured 2026-11-10: the first version of this guard passed this exact mutation.
PRE_LAUNCHER_FLAG_2B = GOOD.replace(
    '"<goal page>" --default-source manager-default`',
    '"<goal page>"`; pass `--default-source manager-default` as config provenance.',
)

PRE_LAUNCHER_FLAG_2C = GOOD.replace(
    '"<topic page>" --default-source manager-default`',
    '"<topic page>"`; pass `--default-source manager-default` as config provenance.',
)

#: The wording at `origin/master` before this change — the negative control.
PRE_CHANGE = (
    "## Step 2B — Goal branch\n\n"
    "1. Resolve from the registry.\n"
    "   ⚠️ **The goal page's `claude_session_id` is a lead, never the answer.** When the "
    "registry matches nothing, read it and run `session-liveness.py --check <id>` on it.\n"
    "4. **None, `UNKNOWN` or `AMBIGUOUS` → spawn a goal manager.**\n\n"
    "## Step 2C — Topic branch\n\n"
    "1. Resolve from the registry.\n"
    "   ⚠️ **The topic page's `claude_session_id` is a lead, never the answer.** When it "
    "matches nothing, read the topic page's `claude_session_id`.\n"
    "4. **None, `UNKNOWN` or `AMBIGUOUS` → the flag gate, then spawn a manager.**\n\n"
    "## Step 3 — Spawn the tab (resume/create branches)\n"
)


class CheckOpenManagerResolutionTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        (pathlib.Path(self.dir) / "scripts").mkdir()
        shutil.copy(SCRIPT, pathlib.Path(self.dir) / "scripts" / SCRIPT.name)
        self.write("commands/open.md", GOOD)

    def write(self, relpath, text):
        path = pathlib.Path(self.dir) / relpath
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)

    def run_check(self, cwd=None):
        return subprocess.run(
            [sys.executable, "scripts/check-open-manager-resolution.py"],
            cwd=cwd or self.dir,
            capture_output=True,
            text=True,
        )

    def test_wired_tree_passes(self):
        result = self.run_check()
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_shipped_tree_passes(self):
        """The real `commands/open.md` this branch ships must satisfy its own check."""
        result = self.run_check(cwd=ROOT)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_pre_change_text_fails(self):
        """The 2026-10-27 wording must be rejected — this is the negative control.

        Without it the check could pass on a tree that still carries the conditional read,
        and every later pass would be indistinguishable from a no-op.
        """
        self.write("commands/open.md", PRE_CHANGE)
        result = self.run_check()
        self.assertEqual(result.returncode, 1)
        self.assertIn("None, `UNKNOWN` or `AMBIGUOUS`", result.stderr)
        self.assertIn("When the registry matches nothing, read it", result.stderr)
        self.assertIn("When it matches nothing, read the topic page", result.stderr)
        self.assertIn("UNCONDITIONALLY", result.stderr)

    def test_missing_section_fails(self):
        """A renamed heading must fail loudly rather than scan an empty string."""
        self.write("commands/open.md", GOOD.replace("## Step 2C — Topic branch", "## Step 2D"))
        result = self.run_check()
        self.assertEqual(result.returncode, 1)
        self.assertIn("no Step 2C section", result.stderr)

    def test_one_branch_without_the_unconditional_read_fails(self):
        """'Updated one of two' is the defect this check exists to catch."""
        text = GOOD.replace(
            "   ⚠️ **Read the topic page's `claude_session_id` UNCONDITIONALLY, before the "
            "registry result.**\n",
            "",
        )
        self.write("commands/open.md", text)
        result = self.run_check()
        self.assertEqual(result.returncode, 1)
        self.assertIn("Step 2C", result.stderr)
        self.assertNotIn("Step 2B: the page read", result.stderr)

    def test_spawn_arm_without_the_no_page_id_scope_fails(self):
        """Dropping the replacement branch leaves every new goal with no spawn route."""
        text = GOOD.replace(
            "⚠️ **`None` here means registry empty AND the page carried no session id.**",
            "",
        )
        self.write("commands/open.md", text)
        result = self.run_check()
        self.assertEqual(result.returncode, 1)
        self.assertIn("registry empty AND the page carried no", result.stderr)

    def test_missing_launcher_flag_on_the_topic_arm_fails(self):
        """Dropping the flag from Step 2C's invocation sends the manager to the sibling rung.

        The flag survives in this fixture's *prose*, so the test also pins that the marker is
        the whole invocation: a bare-substring marker passes this text and would report the
        branch clean with its command unguarded.
        """
        self.write("commands/open.md", PRE_LAUNCHER_FLAG_2C)
        result = self.run_check()
        self.assertEqual(result.returncode, 1)
        self.assertIn("Step 2C: the manager spawn", result.stderr)
        self.assertNotIn("Step 2B: the manager spawn", result.stderr)

    def test_missing_launcher_flag_on_the_goal_arm_fails(self):
        """Step 2B needs its own control — it is the branch the 2026-10-27 incident came from."""
        self.write("commands/open.md", PRE_LAUNCHER_FLAG_2B)
        result = self.run_check()
        self.assertEqual(result.returncode, 1)
        self.assertIn("Step 2B: the manager spawn", result.stderr)
        self.assertNotIn("Step 2C: the manager spawn", result.stderr)

    def test_missing_command_fails(self):
        (pathlib.Path(self.dir) / "commands" / "open.md").unlink()
        result = self.run_check()
        self.assertEqual(result.returncode, 1)
        self.assertIn("missing", result.stderr)


if __name__ == "__main__":
    unittest.main()
