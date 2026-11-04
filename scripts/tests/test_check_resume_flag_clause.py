"""check-resume-flag-clause.py: the auto-resume gate admits flagged rows; only open reads provenance.

The load-bearing case is `test_token_deleted_but_new_bullet_excludes_fails` — the adversarial
edit the task names: drop the `flag` token from `not parked` and add a fresh bullet that
excludes flagged rows. Any text check on the `not parked` line passes it; this guard fails it
because it evaluates every gate bullet that names a flag field. `test_provenance_check_added_fails`
is the second: the operator ruled (2026-10-30, re-affirmed 2026-11-04) that the resume gate must
not gain a `flag_set_by` check, and the agent-flag fixture is what catches one.
"""
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest

SCRIPT = pathlib.Path(__file__).resolve().parent.parent / "check-resume-flag-clause.py"

FIXED = "- **not parked**: phase not `human_review`, status not `hold` (`docs/fleet-surface.md` § Approval provenance);"
OLD = "- **not parked**: frontmatter `flag: true` absent, and phase not `human_review`, status not `hold`;"


def gate(parked=FIXED, extra=""):
    return (
        "---\nname: goal-drive\n---\n\n"
        "3. **Then run the auto-resume gate on confirmed orphans**\n\n"
        "- task `status: in_progress` AND `phase` is `planning` or `execution`;\n"
        f"{parked}\n"
        "- **transcript stale**: read the mtime, never a bare `stat` flag: fail closed;\n"
        f"{extra}"
        "- **not terminal**: `status` not `completed`/`aborted`;\n\n"
        "**Gate holds → resume.** Resolve the script.\n"
    )


SELECTOR = (
    "## Step 0.5\n\n```bash\n"
    "OPENING_ACTORS=('operator','legacy')\n"
    "cand=[t for t in tasks if t.get('flag') is True and t.get('status')=='in_progress' and due(t)]\n"
    "sel=[t for t in cand if str(t.get('flag_set_by') or '').strip() in OPENING_ACTORS]\n"
    "```\n\nWhich surface reads provenance: `docs/fleet-surface.md` § Approval provenance.\n"
)
RESOLVER = 'OPENING_ACTORS = ("operator", "legacy")\n'
DOC = "# Fleet\n\n## Approval provenance\n\nThe split.\n"
SPAWN = "- **not parked**: `flag: true` is not a parked marker — `docs/fleet-surface.md` § Approval provenance.\n"


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.write("scripts/check-resume-flag-clause.py", SCRIPT.read_text())
        self.write("agents/goal-drive.md", gate())
        self.write("commands/open.md", SELECTOR)
        self.write("scripts/flagged-rows.py", RESOLVER)
        self.write("docs/fleet-surface.md", DOC)
        self.write("commands/steps/goal-spawn.md", SPAWN)

    def write(self, relpath, text):
        path = pathlib.Path(self.dir) / relpath
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)

    def run_check(self):
        return subprocess.run(
            [sys.executable, "scripts/check-resume-flag-clause.py"],
            cwd=self.dir, capture_output=True, text=True,
        )

    def assertFails(self, needle):
        result = self.run_check()
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn(needle, result.stderr)


class TestResumeGate(Base):
    def test_fixed_clause_passes(self):
        result = self.run_check()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("resume admits 5/5 fixtures", result.stdout)
        self.assertIn("excludes 2/2 controls", result.stdout)

    def test_old_clause_fails(self):
        self.write("agents/goal-drive.md", gate(OLD))
        self.assertFails("excludes fixture 'operator-flag'")

    def test_token_deleted_but_new_bullet_excludes_fails(self):
        extra = "- **not operator-flagged**: frontmatter `flag: true` absent;\n"
        self.write("agents/goal-drive.md", gate(extra=extra))
        self.assertFails("`not operator-flagged`")

    def test_provenance_check_added_fails(self):
        extra = "- **not agent-flagged**: `flag_set_by` not `agent`;\n"
        self.write("agents/goal-drive.md", gate(extra=extra))
        self.assertFails("excludes fixture 'agent-flag'")

    def test_unreadable_flag_bullet_fails_closed(self):
        extra = "- **approval honoured**: skip any row whose `flag` someone else set;\n"
        self.write("agents/goal-drive.md", gate(extra=extra))
        self.assertFails("is not a readable predicate")

    def test_emptied_parked_class_fails_on_controls(self):
        self.write("agents/goal-drive.md", gate("- **not parked**: status not `hold`;"))
        self.assertFails("admits control 'human_review'")

    def test_missing_parked_bullet_fails(self):
        self.write("agents/goal-drive.md", gate("- **not stalled**: phase not `human_review`;"))
        self.assertFails("0 `not parked` bullet(s)")

    def test_duplicated_parked_bullet_fails(self):
        self.write("agents/goal-drive.md", gate(FIXED + "\n" + FIXED))
        self.assertFails("2 `not parked` bullet(s)")

    def test_prose_word_flag_is_not_a_field(self):
        """The real `transcript stale` bullet says "a bare `stat` flag:" — not a field reference."""
        result = self.run_check()
        self.assertEqual(result.returncode, 0, result.stderr)


class TestOpenPath(Base):
    def test_open_admitting_agent_flag_fails(self):
        self.write("commands/open.md", SELECTOR.replace("('operator','legacy')", "('operator','legacy','agent')"))
        self.write("scripts/flagged-rows.py", 'OPENING_ACTORS = ("operator", "legacy", "agent")\n')
        self.assertFails("open selector admits")

    def test_open_dropping_provenance_fails(self):
        self.write("commands/open.md", SELECTOR.replace(
            "sel=[t for t in cand if str(t.get('flag_set_by') or '').strip() in OPENING_ACTORS]",
            "sel=list(cand)",
        ))
        self.assertFails("open selector admits")

    def test_resolver_disagreeing_fails(self):
        self.write("scripts/flagged-rows.py", 'OPENING_ACTORS = ("operator",)\n')
        self.assertFails("disagrees")


class TestOwnership(Base):
    def test_missing_section_fails(self):
        self.write("docs/fleet-surface.md", "# Fleet\n")
        self.assertFails("exactly once")

    def test_missing_pointer_fails(self):
        self.write("commands/steps/goal-spawn.md", "- **not parked**: phase only.\n")
        self.assertFails("goal-spawn.md does not name")


class TestRealTree(unittest.TestCase):
    def test_repo_passes(self):
        result = subprocess.run([sys.executable, str(SCRIPT)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
