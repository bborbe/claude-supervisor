"""check-spawn-mode.py: a new-worker spawn site must reach the mode decision.

The defect this guards against, measured 2026-09-23: the mode rule lived only in
`commands/open.md` § Step 0.6, every other spawn site never read it, and a day produced
**63 new-worker spawns and 0 of them headless** — 57 sourced from the fleet config rather
than from any decision. The rule now has one home (`docs/fleet-surface.md` § Spawn a
worker item 6); this check asserts each site *points at* it and carries the binding.

The load-bearing case is `test_resume_call_is_not_a_new_worker_site`. A resume passes a
prompt too — `spawn_agent(prompt="<the answer>", resume="<session-id>", interactive=false)`
in `commands/fleet-loop.md` — so a check keyed on `prompt=` alone would demand the mode
decision of a resume. That is the one way this check could be plausibly wrong.
"""
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest

SCRIPT = pathlib.Path(__file__).resolve().parent.parent / "check-spawn-mode.py"

#: A wired site: references the home AND carries the binding in both directions.
SITE = (
    "See `docs/fleet-surface.md` § Spawn a worker item 6 for the rule.\n"
    "Pass `interactive=false` for `mode: headless`, `interactive=true` for `mode: interactive`.\n"
)
#: The one-directional shape this check exists to reject: `mode: interactive` goes unhonoured,
#: so it flips with the fleet default and reports `mode_source=config`.
SITE_ONE_DIRECTION = (
    "See `docs/fleet-surface.md` § Spawn a worker item 6 for the rule.\n"
    "Pass `interactive=false` when the task reads `mode: headless`.\n"
)
SITE_NO_ANCHOR = (
    "The rule is described elsewhere in this file.\n"
    "Pass `interactive=false` when the task reads `mode: headless`.\n"
)
SITE_NO_BINDING = (
    "See `docs/fleet-surface.md` § Spawn a worker item 6 for the rule.\n"
    "Never pass `interactive` here.\n"
)
HOME = (
    "6. Decide the mode before you spawn.\n\n"
    "Ask: does finishing this task raise questions mid-flight that only a human can settle?\n\n"
    "`interactive` is a floor, not a tie-break.\n"
)
#: A resume: both keys present, so NOT a new-worker site.
RESUME_CALL = 'mcp__supervisor__spawn_agent(prompt="<the answer>", resume="<sid>", interactive=false)\n'
#: A new-worker call, multi-line, to exercise the paren-balanced span parser.
NEW_WORKER_CALL = 'mcp__supervisor__spawn_agent(\n  prompt="/vault-cli:work-on-task \\"<task>\\"",\n  cwd="<dir>",\n)\n'


#: A resume-only file that GRANTS the spawn tool. Its prose shows no spawn call at all, which
#: is exactly how `agents/manager-drive.md` opened two workers directly on 2026-09-24 while
#: its own text said to route through `/supervisor:open` — a site defined by a grant, not text.
GRANT_NO_ANCHOR = (
    "---\nallowed-tools:\n  - mcp__supervisor__spawn_agent\n---\n"
    "Resume an exited worker with `spawn_agent(resume=<id>)`.\n"
)
GRANT_WITH_ANCHOR = (
    "---\nallowed-tools:\n  - mcp__supervisor__spawn_agent\n---\n"
    "Never open with it; see `docs/fleet-surface.md` § Spawn a worker item 6 for the rule.\n"
)


class CheckSpawnModeTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        (pathlib.Path(self.dir) / "scripts").mkdir()
        shutil.copy(SCRIPT, pathlib.Path(self.dir) / "scripts" / "check-spawn-mode.py")
        self.write("docs/fleet-surface.md", HOME)
        for site in ("commands/open.md", "commands/manager-loop.md", "commands/manager-spawn.md",
                     "commands/manager-drive.md", "agents/manager-drive.md"):
            self.write(site, SITE)

    def write(self, relpath, text):
        path = pathlib.Path(self.dir) / relpath
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)

    def run_check(self):
        return subprocess.run([sys.executable, "scripts/check-spawn-mode.py"], cwd=self.dir,
                              capture_output=True, text=True)

    def test_wired_tree_passes(self):
        result = self.run_check()
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_site_without_the_reference_fails(self):
        self.write("commands/manager-loop.md", SITE_NO_ANCHOR)
        result = self.run_check()
        self.assertEqual(result.returncode, 1)
        self.assertIn("no reference to the rule's home", result.stderr)

    def test_site_without_the_binding_fails(self):
        """A pointer with no binding is the shape this check exists to reject: the site
        names the home but never passes the argument, so the rule stays unreachable."""
        self.write("commands/manager-spawn.md", SITE_NO_BINDING)
        result = self.run_check()
        self.assertEqual(result.returncode, 1)
        self.assertIn("binding", result.stderr)

    def test_one_directional_site_fails(self):
        """Passing only `interactive=false` leaves `mode: interactive` inert — the defect
        this check gained a second assertion for on 2026-09-24."""
        self.write("commands/manager-loop.md", SITE_ONE_DIRECTION)
        result = self.run_check()
        self.assertEqual(result.returncode, 1)
        self.assertIn("interactive=true", result.stderr)
        self.assertIn("unhonoured", result.stderr)

    def test_grant_without_the_anchor_fails(self):
        """The dimension a text scan cannot reach. A file whose frontmatter grants the spawn
        tool can OPEN a worker even when its prose only ever shows `resume=` calls, so it must
        point at the rule — the gap that let `manager-drive` bypass the wiring."""
        self.write("agents/new-agent.md", GRANT_NO_ANCHOR)
        result = self.run_check()
        self.assertEqual(result.returncode, 1)
        self.assertIn("grants", result.stderr)
        self.assertIn("new-agent.md", result.stderr)

    def test_grant_with_the_anchor_passes(self):
        self.write("agents/new-agent.md", GRANT_WITH_ANCHOR)
        result = self.run_check()
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_raw_wezterm_spawn_grant_is_also_a_spawn_route(self):
        """The second surface of the same class, found 2026-09-25. `agents/manager-drive.md`
        had its supervisor grant removed and still granted `Bash(wezterm cli spawn:*)` — a raw
        terminal spawn the tool-only pattern could not see, and the exact bypass the change
        existed to close. The grant dimension enumerates the routes, so a file granting either
        one must point at the rule."""
        self.write("agents/new-agent.md",
                   "---\nallowed-tools:\n  - Bash(wezterm cli spawn:*)\n---\n"
                   "Open a tab with `wezterm cli spawn`.\n")
        result = self.run_check()
        self.assertEqual(result.returncode, 1)
        self.assertIn("wezterm cli spawn", result.stderr)
        self.assertIn("new-agent.md", result.stderr)

    def test_unknown_new_worker_site_fails(self):
        self.write("commands/brand-new.md", SITE + NEW_WORKER_CALL)
        result = self.run_check()
        self.assertEqual(result.returncode, 1)
        self.assertIn("not a known", result.stderr)

    def test_resume_call_is_not_a_new_worker_site(self):
        """The case that would make this check wrong if keyed on `prompt=` alone."""
        self.write("commands/brand-new.md", RESUME_CALL)
        result = self.run_check()
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_multiline_new_worker_call_is_detected(self):
        self.write("commands/brand-new.md", SITE + NEW_WORKER_CALL)
        result = self.run_check()
        self.assertIn("brand-new.md", result.stderr)

    def test_home_must_define_the_rule(self):
        """A home reduced to a heading passes a naive grep while defining nothing."""
        self.write("docs/fleet-surface.md", "6. Decide the mode before you spawn.\n")
        result = self.run_check()
        self.assertEqual(result.returncode, 1)
        self.assertIn("no longer defines it", result.stderr)

    def test_missing_home_fails_cleanly(self):
        (pathlib.Path(self.dir) / "docs" / "fleet-surface.md").unlink()
        result = self.run_check()
        self.assertEqual(result.returncode, 1)
        self.assertIn("home is missing", result.stderr)
        self.assertNotIn("Traceback", result.stderr)


if __name__ == "__main__":
    unittest.main()
