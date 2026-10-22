"""goal-condition.py: the three forms and the ladder that picks between them.

The `/goal` evaluator is a tool-less model reading the transcript, so a condition that
only names "the Success Criteria" leaves it guessing what "met" means — the script
inlines them instead. The risk that inlining introduces is the mirror image: an inlined
criterion reads like a fact, so a worker can assert it is satisfied while the transcript
shows nothing. Every form therefore has to carry the "an inlined criterion is not proof"
sentence and the disk probe, and the probe is what actually decides "done".

The ladder is not decoration either: the command refuses a condition over 4,000 chars, so
a task with many or long criteria has to fall back rather than be rejected at spawn time.
Each form is asserted below, along with the two properties that must survive every one.
"""
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest

SCRIPT = pathlib.Path(__file__).resolve().parent.parent / "goal-condition.py"

SHORT = """---
name: short
---

# Success Criteria

- [ ] SC1: The first thing works.
- [ ] SC2: The second thing works.

# Tasks

- [ ] do it
"""

LONG = """# Success Criteria

- [ ] SC1: """ + ("The first criterion states a long condition that runs on and on. " * 6) + """
- [ ] SC2: """ + ("The second criterion also states a long condition that runs on. " * 6) + """
"""

UNNUMBERED = """# Success Criteria

- [ ] The spawn path sets the goal.
- [ ] The worker finishes on a disk fact.
"""


class GoalConditionTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)

    def task(self, text):
        path = pathlib.Path(self.dir) / "task.md"
        path.write_text(text, encoding="utf-8")
        return path

    def run_script(self, text, *args):
        return subprocess.run(
            [sys.executable, str(SCRIPT), "--file", str(self.task(text)), *args],
            capture_output=True, text=True,
        )

    def test_full_form_inlines_every_criterion(self):
        result = self.run_script(SHORT)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("form=full", result.stderr)
        self.assertIn("SC1 The first thing works.", result.stdout)
        self.assertIn("SC2 The second thing works.", result.stdout)

    def test_compact_form_drops_the_tail_but_keeps_the_head(self):
        result = self.run_script(LONG, "--compact-at", "400", "--max-chars", "4000")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("form=compact", result.stderr)
        self.assertIn("SC1 The first criterion states a long condition", result.stdout)
        self.assertNotIn("runs on and on. The first criterion", result.stdout)

    def test_probe_form_drops_the_text_and_keeps_the_count(self):
        result = self.run_script(LONG, "--compact-at", "1", "--max-chars", "700")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("form=probe", result.stderr)
        self.assertNotIn("The first criterion states", result.stdout)
        self.assertIn("All 2 Success Criteria are met", result.stdout)

    def test_unnumbered_bullets_get_sequential_ids(self):
        result = self.run_script(UNNUMBERED)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("SC1 The spawn path sets the goal.", result.stdout)
        self.assertIn("SC2 The worker finishes on a disk fact.", result.stdout)

    def test_every_form_keeps_the_disk_probe_and_the_turn_cap(self):
        cases = (
            (SHORT, (), "full"),
            (LONG, ("--compact-at", "400", "--max-chars", "4000"), "compact"),
            (LONG, ("--compact-at", "1", "--max-chars", "700"), "probe"),
        )
        for text, args, form in cases:
            with self.subTest(form=form):
                result = self.run_script(text, *args)
                self.assertIn(f"form={form}", result.stderr)
                self.assertIn("grep -cE", result.stdout)
                self.assertIn("Status: completed", result.stdout)
                self.assertIn("40 turns have passed", result.stdout)

    def test_the_inlined_forms_say_an_inlined_criterion_is_not_proof(self):
        """The probe form carries no inlined criterion, so the sentence has no referent there."""
        for text, args in ((SHORT, ()), (LONG, ("--compact-at", "400", "--max-chars", "4000"))):
            with self.subTest(args=args):
                self.assertIn("an inlined criterion is not proof", self.run_script(text, *args).stdout)

    def test_condition_over_the_cap_in_every_form_fails_loudly(self):
        result = self.run_script(LONG, "--compact-at", "1", "--max-chars", "10")
        self.assertEqual(result.returncode, 1)
        self.assertIn("exceeds the cap", result.stderr)

    def test_missing_file_exits_two(self):
        result = subprocess.run(
            [sys.executable, str(SCRIPT), "--file", str(pathlib.Path(self.dir) / "absent.md")],
            capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("cannot read", result.stderr)


if __name__ == "__main__":
    unittest.main()
