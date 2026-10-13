"""check-drive-ledger-writes.py: a gate that only ever exits 0 grades itself.

The check asserts three things about the prose in `agents/` and `commands/`, and every
one of them is an ABSENCE except the first. A negative assertion that is never shown
firing is indistinguishable from a pattern that matches nothing -- the exact trap the
`\\b`-in-`git grep -E` note records, where a broken pattern reads as a clean sweep. So
each clause here is exercised against a tree crafted to violate it, and the assertion
is that the check exits NON-zero and names the right clause.

The staged tree is a real directory layout, not a monkeypatched REPO: the check derives
its root from its own `__file__`, so copying it into `<tmp>/scripts/` is what makes the
resolution path under test the real one.
"""
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest

SCRIPT = pathlib.Path(__file__).resolve().parent.parent / "check-drive-ledger-writes.py"

#: A conforming drive leg: names the script twice, the path once, no inline interpreter.
GOOD = """\
`P=${CLAUDE_PLUGIN_ROOT:-$HOME/.claude/plugins/marketplaces/claude-supervisor}/scripts`

3. **Load the ledger** -- `python3 $P/drive-ledger.py read`.

7. **Persist the ledger** -- `python3 $P/drive-ledger.py merge`, this round's entries on stdin.

LEDGER  ~/.claude/state/fleet-drive/ledger.json · <n> entries · written <ts>
"""

#: Clause (c)'s violation: a ledger line reaching for an inline interpreter.
INLINE_WRITE = "\n  jq '.entries' ~/.claude/state/fleet-drive/ledger.json > /tmp/out\n"


class Base(unittest.TestCase):
    def setUp(self):
        self.root = pathlib.Path(tempfile.mkdtemp(prefix="check-dlw-"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        (self.root / "scripts").mkdir()
        shutil.copyfile(SCRIPT, self.root / "scripts" / SCRIPT.name)

    def stage(self, drive=GOOD, extra=None, script=True):
        """A repo layout with `agents/fleet-drive.md` and any sibling files.

        `script=True` writes `scripts/drive-ledger.py`, because clause (d) asserts the
        callee exists -- a tree that names the script without shipping it is exactly the
        state the PR review caught this gate passing on.
        """
        agents = self.root / "agents"
        agents.mkdir(exist_ok=True)
        (agents / "fleet-drive.md").write_text(drive, encoding="utf-8")
        if script:
            path = self.root / "scripts" / "drive-ledger.py"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("# stub for the gate's existence check\n", encoding="utf-8")
        for rel, body in (extra or {}).items():
            path = self.root / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(body, encoding="utf-8")

    def run_check(self, staged=True):
        """Run the STAGED copy, so REPO resolves to the tmp tree."""
        script = self.root / "scripts" / SCRIPT.name if staged else SCRIPT
        proc = subprocess.run(
            [sys.executable, str(script)], capture_output=True, text=True
        )
        return proc


class TestRealTree(Base):
    def test_the_repository_passes(self):
        """The shipped tree must satisfy the check it ships with."""
        proc = self.run_check(staged=False)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("drive-ledger-writes ok", proc.stdout)


class TestClauseATheScriptIsWiredIn(Base):
    def test_a_drive_leg_naming_the_script_once_fails(self):
        """One mention is a read with no write, or the reverse."""
        self.stage(drive=GOOD.replace("`python3 $P/drive-ledger.py merge`", "the merge step"))
        proc = self.run_check()
        self.assertEqual(proc.returncode, 1, proc.stdout)
        self.assertIn("names `drive-ledger.py` 1 time(s)", proc.stderr)

    def test_a_drive_leg_naming_it_twice_passes(self):
        self.stage()
        self.assertEqual(self.run_check().returncode, 0)


class TestClauseBOnePathLiteral(Base):
    def test_a_sibling_command_naming_the_path_fails(self):
        """The script owns the path; prose naming it again is a hand-rolled write."""
        self.stage(
            extra={
                "commands/steps/drive.md": (
                    "cat ~/.claude/state/fleet-drive/ledger.json\n"
                )
            }
        )
        proc = self.run_check()
        self.assertEqual(proc.returncode, 1, proc.stdout)
        self.assertIn("appears 2 time(s)", proc.stderr)
        self.assertIn("commands/steps/drive.md", proc.stderr)

    def test_a_second_mention_in_the_drive_leg_itself_fails(self):
        self.stage(drive=GOOD + "\nSee `~/.claude/state/fleet-drive/ledger.json` for state.\n")
        proc = self.run_check()
        self.assertEqual(proc.returncode, 1, proc.stdout)
        self.assertIn("appears 2 time(s)", proc.stderr)


class TestClauseCNoInlineInterpreter(Base):
    def test_an_inline_jq_write_fails(self):
        """This is the shape of the 2026-10-12 wipe."""
        self.stage(drive=GOOD + INLINE_WRITE)
        proc = self.run_check()
        self.assertEqual(proc.returncode, 1, proc.stdout)
        self.assertIn("inline interpreter", proc.stderr)
        self.assertIn("agents/fleet-drive.md", proc.stderr)

    def test_an_inline_python_write_fails(self):
        self.stage(
            drive=GOOD
            + "\n  python3 -c 'import json; json.dump(d, open(\"ledger.json\", \"w\"))'\n"
        )
        proc = self.run_check()
        self.assertEqual(proc.returncode, 1, proc.stdout)
        self.assertIn("inline interpreter", proc.stderr)

    def test_a_prohibition_naming_the_tools_is_not_an_offence(self):
        """⚠️ The rule must tell a prohibition from an instruction.

        The first draft did not, and flagged this repo's own step 7 the moment it was
        rewritten -- a line whose whole purpose is forbidding the practice. A rule that
        forces you to stop naming the tool you are forbidding is worse than no rule.
        """
        self.stage(
            drive=GOOD
            + "\n⚠️ **Never write the ledger yourself** — no `jq`, no `python3 -c`.\n"
        )
        self.assertEqual(self.run_check().returncode, 0)

    def test_a_placeholder_angle_bracket_is_not_a_redirect(self):
        """`<ts>` is not `> file`; this doc is full of `<n>`/`<SECTION>` placeholders."""
        self.stage(drive=GOOD + "\nThe ledger row is `jq '<ts>'` here.\n")
        self.assertEqual(self.run_check().returncode, 0)

    def test_a_jq_read_is_not_a_write(self):
        """A read cannot lose data; only a write is the threat."""
        self.stage(drive=GOOD + "\nRead the ledger with `jq '.entries'` when curious.\n")
        self.assertEqual(self.run_check().returncode, 0)

    def test_an_inline_interpreter_elsewhere_is_not_our_business(self):
        """⚠️ Clause (c) is scoped to ledger lines, and the scope is the point.

        Unscoped, this pattern has 12 matches in the shipped tree across files this
        task never touches, so an unscoped check could never exit 0.
        """
        self.stage(extra={"commands/open.md": "  jq '.x' some-other-file.json\n"})
        self.assertEqual(self.run_check().returncode, 0)

    def test_the_pattern_actually_fires_before_its_zero_is_trusted(self):
        """A pattern that matches nothing reads exactly like a clean tree."""
        from importlib.util import module_from_spec, spec_from_file_location

        spec = spec_from_file_location("cdlw", SCRIPT)
        module = module_from_spec(spec)
        spec.loader.exec_module(module)

        self.assertTrue(module.INLINE_INTERPRETER.search("  jq '.a' x.json"))
        self.assertTrue(module.INLINE_INTERPRETER.search("python3 -c 'x'"))
        self.assertTrue(module.INLINE_INTERPRETER.search("python3 <<EOF"))
        # And it must NOT fire on a word merely containing the token.
        self.assertIsNone(module.INLINE_INTERPRETER.search("jqx"))
        self.assertIsNone(module.INLINE_INTERPRETER.search("teeth"))
        # ⚠️ `\b` must never appear: git's ERE has no `\b`, and Python's DOES -- so a
        # pattern written for git grep and pasted here would behave differently.
        self.assertNotIn(r"\b", module.INLINE_INTERPRETER.pattern)


def load_module():
    """The check as an importable module, so its patterns can be asserted directly."""
    from importlib.util import module_from_spec, spec_from_file_location

    spec = spec_from_file_location("cdlw", SCRIPT)
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestClauseDTheCalleeExists(Base):
    def test_naming_a_script_that_is_not_there_fails(self):
        """The PR review's finding: clause (a) counts a NAME, which a broken call also has.

        Measured on the branch that raised it — (a) was green while the drive leg would
        have died at runtime with `No such file`, because the gate checked the mention
        and never the file.
        """
        self.stage(script=False)
        proc = self.run_check()
        self.assertEqual(proc.returncode, 1, proc.stdout)
        self.assertIn("does not exist", proc.stderr)
        self.assertIn("scripts/drive-ledger.py", proc.stderr)

    def test_a_present_script_passes(self):
        self.stage()
        self.assertEqual(self.run_check().returncode, 0)


class TestPatternHygiene(Base):
    def test_no_word_boundary_escape_in_any_pattern(self):
        """⚠️ `\\b` works in Python `re` and matches NOTHING in git's ERE.

        A pattern correct here and silently broken one file over is a worse neighbour
        than a longer one that is correct in both — and this file's own docstring warns
        against `\\b`. Raised by the PR review, which found `WRITE_SIGNAL` using it.
        """
        module = load_module()
        for name in ("INLINE_INTERPRETER", "WRITE_SIGNAL"):
            with self.subTest(pattern=name):
                self.assertNotIn(
                    r"\b",
                    getattr(module, name).pattern,
                    "%s must spell its boundaries [^A-Za-z0-9_], not \\b" % name,
                )

    def test_the_write_signal_still_fires_and_still_excludes_word_internals(self):
        """Rewriting the pattern must not narrow it — proven both ways."""
        w = load_module().WRITE_SIGNAL
        for positive in (
            "jq '.a' x.json > out.json",
            "cat x | tee log",
            "sponge",
            "json.dump(d, f)",
            "os.replace(tmp, path)",
            "shutil.copyfile(a, b)",
            "handle.write(x)",
        ):
            with self.subTest(case=positive):
                self.assertIsNotNone(w.search(positive), positive)
        for negative in (
            "teeth",              # `tee` needs a pipe before it
            "sponges",            # a word merely containing `sponge`
            "replaces it",        # `os.replace` is the whole token
            "writing prose",      # `.write(` is the call, not the verb
        ):
            with self.subTest(case=negative):
                self.assertIsNone(w.search(negative), negative)


class TestTheScanCannotSilentlyFindNothing(Base):
    def test_an_empty_tree_fails_rather_than_reporting_clean(self):
        """An empty glob means the root is wrong, not that the tree is clean."""
        proc = self.run_check()
        self.assertEqual(proc.returncode, 1, proc.stdout)
        self.assertIn("the scan root is wrong", proc.stderr)

    def test_a_missing_drive_leg_fails(self):
        (self.root / "commands").mkdir()
        (self.root / "commands" / "other.md").write_text("nothing\n", encoding="utf-8")
        proc = self.run_check()
        self.assertEqual(proc.returncode, 1, proc.stdout)
        self.assertIn("has no body to check", proc.stderr)


if __name__ == "__main__":
    unittest.main()
