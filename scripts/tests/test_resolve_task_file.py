"""resolve-task-file.py: the sweep's task-file resolution, pinned.

The defect this guards against is measured. A first cut of the cross-vault rule treated
ANY two hits as ambiguous, which regressed `BRO-22032 MDM Merge of Parties based on names`
— a name that is a task in one vault's `25 Tasks` and a goal in the *same* vault's
`24 Goals`. The single-vault rule it replaced resolved that to the task, so the ambiguity
reading silently turned a working resolution into none. Nothing in the repo ran against
the rule, so the cases that caught it lived only in a PR body until this file existed.

The load-bearing cases are `test_task_beats_goal_in_the_same_vault` (that regression) and
`test_ambiguous_tasks_tier_does_not_fall_through_to_goals` (the tier condition is `not
tasks`, not "tasks had no unique hit"). The payload cases cover the second review finding:
the loop over `vault-cli`'s output must sit inside the guard, or a payload that is not a
list of dicts raises AttributeError straight through a pass the reader declares
"not skippable, and not partially skippable".
"""
import json
import os
import pathlib
import stat
import subprocess
import tempfile
import unittest

SCRIPT = pathlib.Path(__file__).resolve().parent.parent / "resolve-task-file.py"

#: A fake `vault-cli` that prints whatever `$VAULT_CLI_FIXTURE` holds, so the fixture
#: reaches the real subprocess call rather than a monkeypatched one. `$VAULT_CLI_EXIT`
#: drives the non-zero-exit degradation path.
FAKE_VAULT_CLI = """#!/bin/sh
if [ -n "$VAULT_CLI_EXIT" ]; then exit "$VAULT_CLI_EXIT"; fi
printf '%s' "$VAULT_CLI_FIXTURE"
"""


class ResolveTaskFileTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        shim = self.bin / "vault-cli"
        shim.write_text(FAKE_VAULT_CLI)
        shim.chmod(shim.stat().st_mode | stat.S_IEXEC)

    def tearDown(self):
        self.tmp.cleanup()

    def vault(self, name, dirs=("25 Tasks", "24 Goals"), files=()):
        """Materialise a vault dir with `files` placed under `dirs[0]`, and return its
        config entry. `dirs` is `(tasks_dir, goals_dir)`; an empty string omits the key."""
        path = self.root / name
        for d in dirs:
            if d:
                (path / d).mkdir(parents=True, exist_ok=True)
        for f in files:
            (path / dirs[0] / f).write_text("")
        entry = {"name": name, "path": str(path)}
        if dirs[0]:
            entry["tasks_dir"] = dirs[0]
        if dirs[1]:
            entry["goals_dir"] = dirs[1]
        return entry

    def run_script(self, name, payload, exit_code=""):
        env = dict(os.environ)
        env["PATH"] = f"{self.bin}:{env['PATH']}"
        env["VAULT_CLI_FIXTURE"] = payload if isinstance(payload, str) else json.dumps(payload)
        env["VAULT_CLI_EXIT"] = exit_code
        return subprocess.run(
            ["python3", str(SCRIPT), name],
            capture_output=True, text=True, env=env, timeout=30,
        )

    # --- the resolution rule -------------------------------------------------

    def test_task_beats_goal_in_the_same_vault(self):
        """The regression: one name, a task and a goal in one vault, resolves to the TASK."""
        v = self.vault("seibert-brogrammers",
                       files=("BRO-22032 MDM Merge of Parties based on names.md",))
        (self.root / "seibert-brogrammers" / "24 Goals" / "BRO-22032 MDM Merge of Parties Based on Names.md").write_text("")
        r = self.run_script("BRO-22032 MDM Merge of Parties based on names", [v])
        self.assertEqual(0, r.returncode)
        self.assertEqual(str(self.root / "seibert-brogrammers" / "25 Tasks" / "BRO-22032 MDM Merge of Parties based on names.md"), r.stdout.strip())
        self.assertEqual("", r.stderr)

    def test_sibling_vault_task_resolves(self):
        """The defect the whole change exists for: a task in another vault."""
        personal = self.vault("private-personal", files=("Something Else.md",))
        bro = self.vault("seibert-brogrammers", files=("Vuln Fix Agent Bumps @latest.md",))
        r = self.run_script("Vuln Fix Agent Bumps @latest", [personal, bro])
        self.assertEqual(str(self.root / "seibert-brogrammers" / "25 Tasks" / "Vuln Fix Agent Bumps @latest.md"), r.stdout.strip())

    def test_goal_only_resolves(self):
        v = self.vault("private-personal")
        (self.root / "private-personal" / "24 Goals" / "Fleet Communication.md").write_text("")
        r = self.run_script("Fleet Communication", [v])
        self.assertEqual(str(self.root / "private-personal" / "24 Goals" / "Fleet Communication.md"), r.stdout.strip())

    def test_no_match_resolves_nothing(self):
        v = self.vault("private-personal", files=("Something Else.md",))
        r = self.run_script("zzz-no-such-task", [v])
        self.assertEqual("", r.stdout.strip())
        self.assertEqual("", r.stderr)

    def test_two_vault_task_collision_resolves_nothing_and_names_both(self):
        """Ambiguity inside the tier reached is no resolution — and every candidate is
        printed, which is what docs/subject-resolution.md § The page test requires."""
        a = self.vault("vault-a", files=("Collide.md",))
        b = self.vault("vault-b", files=("Collide.md",))
        r = self.run_script("Collide", [a, b])
        self.assertEqual("", r.stdout.strip())
        self.assertEqual(2, r.stderr.count("AMBIGUOUS"))
        self.assertIn("vault-a", r.stderr)
        self.assertIn("vault-b", r.stderr)

    def test_ambiguous_tasks_tier_does_not_fall_through_to_goals(self):
        """`not tasks` is the condition, not "tasks had no unique hit" — a unique goal must
        NOT rescue an ambiguous task tier."""
        a = self.vault("vault-a", files=("Collide.md",))
        b = self.vault("vault-b", files=("Collide.md",))
        (self.root / "vault-a" / "24 Goals" / "Collide.md").write_text("")
        r = self.run_script("Collide", [a, b])
        self.assertEqual("", r.stdout.strip(), "an ambiguous tasks tier must not drop to the goal tier")

    def test_vault_with_no_dirs_is_skipped(self):
        """A config entry carrying a path but no tasks_dir/goals_dir (the assistant-*
        vaults) must be skipped, never probed at the vault root."""
        bare = {"name": "assistant-personal", "path": str(self.root)}
        v = self.vault("private-personal", files=("Real Task.md",))
        r = self.run_script("Real Task", [bare, v])
        self.assertEqual(str(self.root / "private-personal" / "25 Tasks" / "Real Task.md"), r.stdout.strip())

    # --- degradation: this pass must never raise -----------------------------

    def test_non_list_payload_degrades_instead_of_raising(self):
        """The second review finding: a payload that is not a list of dicts must reach the
        no-resolution path, not raise AttributeError through an unskippable pass."""
        r = self.run_script("Any Task", {"private-personal": {"path": "/tmp"}})
        self.assertEqual(0, r.returncode, f"exited {r.returncode}; stderr={r.stderr}")
        self.assertNotIn("Traceback", r.stderr)
        self.assertEqual("", r.stdout.strip())

    def test_non_zero_exit_degrades(self):
        r = self.run_script("Any Task", [], exit_code="1")
        self.assertEqual(0, r.returncode)
        self.assertNotIn("Traceback", r.stderr)
        self.assertEqual("", r.stdout.strip())

    def test_malformed_json_degrades(self):
        r = self.run_script("Any Task", "not json at all")
        self.assertEqual(0, r.returncode)
        self.assertNotIn("Traceback", r.stderr)
        self.assertEqual("", r.stdout.strip())

    def test_list_containing_non_dicts_degrades(self):
        r = self.run_script("Any Task", ["private-personal", 42, None])
        self.assertEqual(0, r.returncode)
        self.assertNotIn("Traceback", r.stderr)
        self.assertEqual("", r.stdout.strip())


if __name__ == "__main__":
    unittest.main()
