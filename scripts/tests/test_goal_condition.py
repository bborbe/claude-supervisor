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
import os
import pathlib
import shlex
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
            [sys.executable, str(SCRIPT), "--file", str(self.task(text)),
             "--vault", "test-vault", *args],
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
            [sys.executable, str(SCRIPT), "--file", str(pathlib.Path(self.dir) / "absent.md"),
             "--vault", "test-vault"],
            capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("cannot read", result.stderr)

    def test_no_success_criteria_is_refused_not_emitted(self):
        """A condition with nothing to prove is armed, then satisfied by the disk probe alone."""
        result = self.run_script("# Tasks\n\n- [ ] do it\n")
        self.assertEqual(result.returncode, 3)
        self.assertIn("refusing to arm", result.stderr)
        self.assertEqual(result.stdout, "")

    def test_vault_is_required_so_no_placeholder_can_reach_a_worker(self):
        result = subprocess.run(
            [sys.executable, str(SCRIPT), "--file", str(self.task(SHORT))],
            capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("--vault", result.stderr)

    def test_an_unresolvable_task_exits_two(self):
        result = subprocess.run(
            [sys.executable, str(SCRIPT), "--task", "no such task anywhere",
             "--vault", "test-vault"],
            capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("cannot resolve", result.stderr)

    def test_compact_form_does_not_cut_inside_an_abbreviation(self):
        text = ("# Success Criteria\n\n- [ ] SC1: Use e.g. the CLI to do the first thing. "
                "Then keep going for a long while so that the form has to compact.\n")
        result = self.run_script(text, "--compact-at", "1", "--max-chars", "4000")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("form=compact", result.stderr)
        self.assertIn("SC1 Use e.g. the CLI to do the first thing.", result.stdout)


    def test_output_carries_the_goal_prefix(self):
        """stdout is the spawn prompt verbatim: a caller that had to prepend `/goal ` would eventually not."""
        result = self.run_script(SHORT)
        self.assertTrue(result.stdout.startswith("/goal "), result.stdout[:40])

    def test_task_resolution_through_vault_cli(self):
        """The production `--task`/`--vault` path, with a fake vault-cli standing in for the real one.

        This is the only coverage of `task_file()` and of the `file_path` JSON key it reads —
        a schema drift there fails closed to exit 2, but only a live spawn would have shown it.
        """
        task = self.task(SHORT)
        bindir = pathlib.Path(self.dir) / "bin"
        bindir.mkdir()
        fake = bindir / "vault-cli"
        fake.write_text(f'#!/bin/sh\nprintf \'{{"file_path": "{task}"}}\'\n', encoding="utf-8")
        fake.chmod(0o755)
        env = {**os.environ, "PATH": f"{bindir}:{os.environ.get('PATH', '')}"}
        result = subprocess.run(
            [sys.executable, str(SCRIPT), "--task", "any task", "--vault", "test-vault"],
            capture_output=True, text=True, env=env,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("form=", result.stderr)
        self.assertIn("Status: completed", result.stdout)

    def test_a_missing_file_path_key_fails_closed(self):
        """A vault-cli whose JSON loses `file_path` must not arm anything."""
        bindir = pathlib.Path(self.dir) / "bin"
        bindir.mkdir()
        fake = bindir / "vault-cli"
        fake.write_text("#!/bin/sh\nprintf '{}'\n", encoding="utf-8")
        fake.chmod(0o755)
        env = {**os.environ, "PATH": f"{bindir}:{os.environ.get('PATH', '')}"}
        result = subprocess.run(
            [sys.executable, str(SCRIPT), "--task", "any", "--vault", "v"],
            capture_output=True, text=True, env=env,
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("cannot resolve", result.stderr)

    def test_a_shell_hostile_name_and_vault_survive_quoting(self):
        """The worker is told to run the emitted probe, so both values must round-trip through a shell."""
        path = self.task(SHORT)
        hostile_name = "na me$(touch pwned)"
        hostile_vault = "v'ault"
        result = subprocess.run(
            [sys.executable, str(SCRIPT), "--file", str(path),
             "--task", hostile_name, "--vault", hostile_vault],
            capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        probe_line = [l for l in result.stdout.splitlines() if "vault-cli task show" in l][0]
        tokens = shlex.split(probe_line.split("`")[1])
        self.assertIn(hostile_name, tokens)
        self.assertIn(hostile_vault, tokens)

    def test_headless_row_is_refused(self):
        result = self.run_script("---\nmode: headless\n---\n" + SHORT)
        self.assertEqual(result.returncode, 4)
        self.assertIn("headless", result.stderr)
        self.assertEqual(result.stdout, "")

    def test_cluster_target_is_refused(self):
        for target in ("docker", "k8s", "k8s:nukedev"):
            with self.subTest(target=target):
                result = self.run_script(f"---\ntarget: {target}\n---\n" + SHORT)
                self.assertEqual(result.returncode, 4)
                self.assertIn(target, result.stderr)

    def test_unnumbered_bullet_does_not_collide_with_an_explicit_id(self):
        text = "# Success Criteria\n\n- [ ] SC2: A thing.\n- [ ] B thing.\n"
        result = self.run_script(text)
        self.assertIn("SC2 A thing.", result.stdout)
        self.assertIn("SC3 B thing.", result.stdout)


if __name__ == "__main__":
    unittest.main()
