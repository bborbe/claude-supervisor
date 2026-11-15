"""check-open-resume-mode.py: a resume branch selects its path, it never assumes one.

Two call sites carry the same rule. `commands/open.md` chose the wezterm path for every resumed
row until 2026-11-06 (26 headless rows opened as tabs in one `--flagged` batch).
`agents/fleet-keepalive-actor.md` chose by `mode:` alone until 2026-11-15, so a topic page with
no `mode:` took the headless resume and the manager came back invisible — no tab, no pane, no
jump link (measured on session `6f14c290`, 2026-11-14).

The load-bearing cases are the `pre_change` tests. They feed the wording that shipped before each
fix and assert the guard rejects it. A guard that cannot fail on the tree it was written against
proves nothing — and the leg case is the one that matters here: the shipped leg read
`Which path a row takes is its mode:'s decision` and nothing else, which is exactly the shape a
later editor would restore while believing the branch already consulted `role:`.
"""
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest

SCRIPT = pathlib.Path(__file__).resolve().parent.parent / "check-open-resume-mode.py"
ROOT = SCRIPT.parent.parent

#: A tree the guard accepts, for the open half — the shipped selector paragraph.
GOOD_OPEN = (
    "**The resume branch selects its path; not always this one.** `mode:` first, then liveness —\n"
    "single home: [`docs/fleet-surface.md` § Spawn a worker](../docs/fleet-surface.md#spawn-a-worker)'s\n"
    "resume table. `interactive` → this path; headless/absent + **ABSENT** → § 3 headless resume\n"
    "(`cwd`, paced); **LIVE** → neither; **UNKNOWN**/**AMBIGUOUS** → this path.\n"
)

#: A tree the guard accepts, for the docs half — the manager-resume block.
GOOD_SURFACE = (
    "## Spawn a worker\n"
    "\n"
    "6. **Decide the mode.** The mapping has its single home in `docs/role-mode-matrix.md`.\n"
    "\n"
    "⚠️ **A manager row never resumes headless, and `role:` is read before `mode:`.** A resume\n"
    "site reads only `mode:`, so a topic page carrying none takes the headless resume.\n"
    "\n"
    "- **`role: manager`** — path B only. A manager restart never takes path A.\n"
    "- **`role: manager` carrying an explicit `mode: headless`** — **refuse**: print\n"
    "  `role=manager, mode=headless — refused`.\n"
    "- **`role: agent`** — path A.\n"
    "\n"
    "7. **Decide the TARGET before you spawn.**\n"
)

#: A tree the guard accepts, for the agent half — the pointer, not a restatement.
GOOD_LEG = (
    "- **`mcp__supervisor__spawn_agent`** — **path A**, the headless resume. Every resume goes\n"
    "  through the path its `role` selects — a `manager` row never takes path A — then its\n"
    "  `mode:`; anything that would open a new worker answers to\n"
    "  `docs/fleet-surface.md` § Spawn a worker item 6.\n"
)

#: The surface wording that shipped before the fix — no manager-resume block at all.
PRE_CHANGE_SURFACE = (
    "## Spawn a worker\n"
    "\n"
    "6. **Decide the mode.** The mapping has its single home in `docs/role-mode-matrix.md`.\n"
    "\n"
    "7. **Decide the TARGET before you spawn.**\n"
)

#: The agent wording that shipped before the fix — the mode-only path choice.
PRE_CHANGE_LEG = (
    "- **`mcp__supervisor__spawn_agent`** — **path A**, the headless resume. Which path a row\n"
    "  takes is its `mode:`'s decision, single home `docs/fleet-surface.md` § Spawn a worker\n"
    "  item 6 — read it there, never restate it.\n"
)


class CheckOpenResumeModeTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        (pathlib.Path(self.dir) / "scripts").mkdir()
        shutil.copy(SCRIPT, pathlib.Path(self.dir) / "scripts" / SCRIPT.name)
        self.write("commands/open.md", GOOD_OPEN)
        self.write("docs/fleet-surface.md", GOOD_SURFACE)
        self.write("agents/fleet-keepalive-actor.md", GOOD_LEG)

    def write(self, relpath, text):
        path = pathlib.Path(self.dir) / relpath
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def run_check(self, cwd=None):
        return subprocess.run(
            [sys.executable, "scripts/check-open-resume-mode.py"],
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

    def test_pre_change_surface_fails(self):
        """A docs tree with no manager-resume block must be rejected — the negative control."""
        self.write("docs/fleet-surface.md", PRE_CHANGE_SURFACE)
        result = self.run_check()
        self.assertEqual(result.returncode, 1)
        self.assertIn("carries no", result.stderr)

    def test_pre_change_leg_fails(self):
        """A mode-only path choice in the agent must be rejected — the second control."""
        self.write("agents/fleet-keepalive-actor.md", PRE_CHANGE_LEG)
        result = self.run_check()
        self.assertEqual(result.returncode, 1)
        self.assertIn("never takes path A", result.stderr)

    def test_surface_without_refusal_fails(self):
        """Dropping the refusal literal must be rejected — silence is the silent override."""
        self.write(
            "docs/fleet-surface.md",
            GOOD_SURFACE.replace("`role=manager, mode=headless — refused`", "it refuses"),
        )
        result = self.run_check()
        self.assertEqual(result.returncode, 1)
        self.assertIn("must refuse, not route", result.stderr)

    def test_leg_without_pointer_fails(self):
        """Dropping the rule's home from the agent must be rejected — a rule with no route."""
        self.write(
            "agents/fleet-keepalive-actor.md",
            GOOD_LEG.replace("§ Spawn a worker item 6", "elsewhere"),
        )
        result = self.run_check()
        self.assertEqual(result.returncode, 1)
        self.assertIn("no route", result.stderr)


if __name__ == "__main__":
    unittest.main()
