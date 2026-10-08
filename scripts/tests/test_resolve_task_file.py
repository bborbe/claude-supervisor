"""resolve-task-file.py: the sweep's name-based task-file resolution, pinned.

The defect this guards against is measured. A first cut of the cross-vault rule treated
ANY two hits as ambiguous, which regressed `BRO-22032 MDM Merge of Parties based on names`
— a name that is a task in one vault's `25 Tasks` and a goal in the *same* vault's
`24 Goals`. The single-vault rule it replaced resolved that to the task, so the ambiguity
reading silently turned a working resolution into none. Nothing in the repo ran against the
rule, so the cases that caught it lived only in a PR body until this file existed.

The load-bearing cases are `test_task_beats_goal_in_the_same_vault` (that regression),
`test_ambiguous_tasks_tier_does_not_fall_through_to_goals` (the tier condition is `not
tasks`, not "tasks had no unique hit"), and `test_degraded_read_is_announced` — a silent
`[]` on the failure path renders every session unowned, which is the same false-*unowned*
defect this whole change exists to remove, reached from the other side.
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

    def vault(self, name, dirs=("25 Tasks", "24 Goals"), files=(), goals=()):
        """Materialise a vault dir with `files` under `dirs[0]` and `goals` under `dirs[1]`,
        and return its config entry. An empty string omits the key."""
        path = self.root / name
        for d in dirs:
            if d:
                (path / d).mkdir(parents=True, exist_ok=True)
        for f in files:
            (path / dirs[0] / f).write_text("")
        for g in goals:
            (path / dirs[1] / g).write_text("")
        entry = {"name": name, "path": str(path)}
        if dirs[0]:
            entry["tasks_dir"] = dirs[0]
        if dirs[1]:
            entry["goals_dir"] = dirs[1]
        return entry

    def run_script(self, names, payload, exit_code="", stdin=False):
        env = dict(os.environ)
        env["PATH"] = f"{self.bin}:{env['PATH']}"
        env["VAULT_CLI_FIXTURE"] = payload if isinstance(payload, str) else json.dumps(payload)
        env["VAULT_CLI_EXIT"] = exit_code
        args = ["--stdin"] if stdin else list(names)
        return subprocess.run(
            ["python3", str(SCRIPT)] + args,
            input="\n".join(names) if stdin else None,
            capture_output=True, text=True, env=env, timeout=30,
        )

    def path_for(self, r, name):
        """The resolved path for `name` out of the `<name>\\t<path>` output."""
        for line in r.stdout.splitlines():
            n, _, p = line.partition("\t")
            if n == name:
                return p
        self.fail(f"no output line for {name!r}; stdout={r.stdout!r} stderr={r.stderr!r}")

    # --- the truncation tier -------------------------------------------------

    def test_a_truncated_registry_name_resolves_to_its_full_title(self):
        """⚠️ The measured case. A session registry name is a *truncated* task title —
        46 characters held against 108, measured 2026-10-07 — so an exact-only reader
        resolves nothing for it and the session reads unowned."""
        full = ("A Renamed Task's Session Becomes Unaddressable, Because the Registry "
                "Name Is Write-Once and the Title Is Not")
        v = self.vault("private-personal", files=(f"{full}.md",))
        name = "⚙ A Renamed Task's Session Becomes Unaddressable"
        r = self.run_script([name], [v])
        self.assertEqual(0, r.returncode)
        self.assertEqual(str(self.root / "private-personal" / "25 Tasks" / f"{full}.md"),
                         self.path_for(r, name))

    def test_a_short_name_that_opens_a_title_is_not_read_as_a_truncation(self):
        """The length floor — a role name is not a truncation, and resolving it would
        hand the caller a task the session never owned."""
        v = self.vault("private-personal", files=("boss of nothing in particular.md",))
        r = self.run_script(["boss"], [v])
        self.assertEqual("", self.path_for(r, "boss"))

    def test_a_decorated_filename_is_not_folded_into_its_plain_twin(self):
        """⚠️ **The exact tier stays exact.** Running the *session-label* normalizer over
        an on-disk basename folded `- Notes.md` and `Notes.md` into one key, so a
        previously unique resolution became a false `AMBIGUOUS` — the round-3 review's
        MAJOR. Whitespace still collapses on both sides (a filename rule too), but the
        leading-decoration strip is a session-name rule and is not applied here."""
        v = self.vault("private-personal", files=("Notes.md", "- Notes.md"))
        r = self.run_script(["Notes"], [v])
        self.assertEqual(str(self.root / "private-personal" / "25 Tasks" / "Notes.md"),
                         self.path_for(r, "Notes"))

    def test_a_non_md_attachment_is_not_read_as_a_truncation(self):
        """⚠️ **The prefix tier still requires a `.md` entry.** Without that it matches
        anything merely *starting* with the stem — an Obsidian attachment (`….png`) or a
        sibling (`….md.gpg`) in the tasks dir — and the caller then runs `grep -cE` and
        `date -r` against a binary."""
        full = ("A Renamed Task's Session Becomes Unaddressable, Because the Registry "
                "Name Is Write-Once and the Title Is Not")
        name = "⚙ A Renamed Task's Session Becomes Unaddressable"
        v = self.vault("private-personal", files=(f"{full}.png",))
        r = self.run_script([name], [v])
        self.assertEqual("", self.path_for(r, name))

    def test_a_sibling_suffix_after_md_is_not_read_as_a_truncation(self):
        """The other shape the `.md` requirement excludes, and it fails for a
        different-looking reason than the `.png` case: `….md.gpg` DOES start with the stem
        and does contain `.md`, but does not END in it."""
        full = ("A Renamed Task's Session Becomes Unaddressable, Because the Registry "
                "Name Is Write-Once and the Title Is Not")
        name = "⚙ A Renamed Task's Session Becomes Unaddressable"
        v = self.vault("private-personal", files=(f"{full}.md.gpg",))
        r = self.run_script([name], [v])
        self.assertEqual("", self.path_for(r, name))

    def test_an_ambiguous_truncation_resolves_nothing_and_names_both(self):
        """⚠️ **"Never guesses" survives the new tier.** Two titles under one prefix is a
        guess with no single answer, so the name resolves to nothing — the same
        discipline the exact tier already applies to a two-vault collision."""
        v = self.vault("private-personal",
                       files=("Shared Prefix That Is Long Enough One.md",
                              "Shared Prefix That Is Long Enough Two.md"))
        name = "Shared Prefix That Is Long Enough"
        r = self.run_script([name], [v])
        self.assertEqual("", self.path_for(r, name))
        self.assertIn("AMBIGUOUS", r.stderr)

    def test_an_exact_goal_beats_a_prefix_task(self):
        """⚠️ **Certainty before guess, across folders.** The truncation tiers must run
        only after BOTH exact tiers are exhausted. A tasks-first prefix tier resolves the
        guess and never consults the goal that matches exactly — the one ordering that
        makes the new tier change behaviour where an exact match exists."""
        v = self.vault("private-personal",
                       files=("Some Long Goal Name Here And Then Some More.md",),
                       goals=("Some Long Goal Name Here.md",))
        name = "Some Long Goal Name Here"
        r = self.run_script([name], [v])
        self.assertEqual(str(self.root / "private-personal" / "24 Goals" / f"{name}.md"),
                         self.path_for(r, name))

    def test_the_exact_tier_still_wins_over_a_truncation(self):
        """An exact hit is never displaced by the prefix tier."""
        v = self.vault("private-personal",
                       files=("Some Task.md", "Some Task And A Much Longer Suffix.md"))
        r = self.run_script(["Some Task"], [v])
        self.assertEqual(str(self.root / "private-personal" / "25 Tasks" / "Some Task.md"),
                         self.path_for(r, "Some Task"))

    # --- the resolution rule -------------------------------------------------

    def test_task_beats_goal_in_the_same_vault(self):
        """The regression: one name, a task and a goal in one vault, resolves to the TASK."""
        v = self.vault("seibert-brogrammers",
                       files=("BRO-22032 MDM Merge of Parties based on names.md",),
                       goals=("BRO-22032 MDM Merge of Parties Based on Names.md",))
        name = "BRO-22032 MDM Merge of Parties based on names"
        r = self.run_script([name], [v])
        self.assertEqual(0, r.returncode)
        self.assertEqual(str(self.root / "seibert-brogrammers" / "25 Tasks" / f"{name}.md"),
                         self.path_for(r, name))
        self.assertEqual("", r.stderr)

    def test_sibling_vault_task_resolves(self):
        """The defect the whole change exists for: a task in another vault."""
        personal = self.vault("private-personal", files=("Something Else.md",))
        bro = self.vault("seibert-brogrammers", files=("Vuln Fix Agent Bumps @latest.md",))
        r = self.run_script(["Vuln Fix Agent Bumps @latest"], [personal, bro])
        self.assertEqual(str(self.root / "seibert-brogrammers" / "25 Tasks" / "Vuln Fix Agent Bumps @latest.md"),
                         self.path_for(r, "Vuln Fix Agent Bumps @latest"))

    def test_goal_only_resolves(self):
        v = self.vault("private-personal", goals=("Fleet Communication.md",))
        r = self.run_script(["Fleet Communication"], [v])
        self.assertEqual(str(self.root / "private-personal" / "24 Goals" / "Fleet Communication.md"),
                         self.path_for(r, "Fleet Communication"))

    def test_no_match_resolves_nothing(self):
        v = self.vault("private-personal", files=("Something Else.md",))
        r = self.run_script(["zzz-no-such-task"], [v])
        self.assertEqual("", self.path_for(r, "zzz-no-such-task"))
        self.assertEqual("", r.stderr)

    def test_two_vault_task_collision_resolves_nothing_and_names_both(self):
        """Ambiguity inside the tier reached is no resolution — and every candidate is
        printed, which is what docs/subject-resolution.md § The page test requires."""
        a = self.vault("vault-a", files=("Collide.md",))
        b = self.vault("vault-b", files=("Collide.md",))
        r = self.run_script(["Collide"], [a, b])
        self.assertEqual("", self.path_for(r, "Collide"))
        self.assertEqual(2, r.stderr.count("AMBIGUOUS"))
        self.assertIn("vault-a", r.stderr)
        self.assertIn("vault-b", r.stderr)

    def test_ambiguous_tasks_tier_does_not_fall_through_to_goals(self):
        """`not tasks` is the condition, not "tasks had no unique hit" — a unique goal must
        NOT rescue an ambiguous task tier."""
        a = self.vault("vault-a", files=("Collide.md",))
        b = self.vault("vault-b", files=("Collide.md",))
        (self.root / "vault-a" / "24 Goals" / "Collide.md").write_text("")
        r = self.run_script(["Collide"], [a, b])
        self.assertEqual("", self.path_for(r, "Collide"),
                         "an ambiguous tasks tier must not drop to the goal tier")

    def test_vault_with_no_dirs_is_skipped(self):
        """A config entry carrying a path but no tasks_dir/goals_dir (the assistant-*
        vaults) must be skipped, never probed at the vault root."""
        bare = {"name": "assistant-personal", "path": str(self.root)}
        v = self.vault("private-personal", files=("Real Task.md",))
        r = self.run_script(["Real Task"], [bare, v])
        self.assertEqual(str(self.root / "private-personal" / "25 Tasks" / "Real Task.md"),
                         self.path_for(r, "Real Task"))

    # --- inputs the caller can plausibly send --------------------------------

    def test_already_suffixed_name_resolves(self):
        """`<name>.md` is what a caller reading a filename will pass. Without normalisation
        it searches for `<name>.md.md` and silently renders the session unowned."""
        v = self.vault("private-personal", files=("Real Task.md",))
        r = self.run_script(["Real Task.md"], [v])
        self.assertEqual(str(self.root / "private-personal" / "25 Tasks" / "Real Task.md"),
                         self.path_for(r, "Real Task.md"))

    def test_directory_named_like_a_task_does_not_resolve(self):
        """A directory whose basename ends in `.md` is not a task file; downstream it feeds
        `grep -cE` (errors on a directory) and `date -r` (reports the directory's mtime)."""
        v = self.vault("private-personal")
        (self.root / "private-personal" / "25 Tasks" / "Trap.md").mkdir()
        r = self.run_script(["Trap"], [v])
        self.assertEqual("", self.path_for(r, "Trap"))

    def test_absolute_tasks_dir_is_refused(self):
        """An absolute config-supplied `tasks_dir` would make os.path.join discard the vault
        path and probe an arbitrary directory, so it is skipped rather than honoured."""
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "Sneaky.md").write_text("")
        v = {"name": "weird", "path": str(self.root / "weird"), "tasks_dir": str(outside)}
        r = self.run_script(["Sneaky"], [v])
        self.assertEqual("", self.path_for(r, "Sneaky"))

    def test_dotdot_tasks_dir_is_refused(self):
        """A relative `..` component walks out of the vault just as an absolute path does,
        so the guard must refuse both — otherwise its own comment overclaims."""
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "Sneaky.md").write_text("")
        vault = self.root / "weird"
        vault.mkdir()
        v = {"name": "weird", "path": str(vault), "tasks_dir": "../outside"}
        r = self.run_script(["Sneaky"], [v])
        self.assertEqual("", self.path_for(r, "Sneaky"))

    # --- batch form ----------------------------------------------------------

    def test_stdin_batch_resolves_every_name(self):
        """One process, one vault enumeration, many names — the form step 4 uses, since a
        per-session call would pay ~46 vault-cli invocations per sweep."""
        a = self.vault("private-personal", files=("One.md",))
        b = self.vault("seibert-brogrammers", files=("Two.md",))
        r = self.run_script(["One", "Two", "Three"], [a, b], stdin=True)
        self.assertEqual(0, r.returncode)
        self.assertEqual(str(self.root / "private-personal" / "25 Tasks" / "One.md"), self.path_for(r, "One"))
        self.assertEqual(str(self.root / "seibert-brogrammers" / "25 Tasks" / "Two.md"), self.path_for(r, "Two"))
        self.assertEqual("", self.path_for(r, "Three"))

    # --- degradation: announced, never silent --------------------------------

    def test_degraded_read_is_announced_on_stdout_not_only_stderr(self):
        """A silent `[]` on the failure path renders every session unowned — the same
        false-*unowned* defect this change removes, reached from the other side. And the
        marker must reach **stdout**, because a caller that pipes stdout alone (`| cut -f2`,
        which is how `commands/fleet-status.md` consumes this) cannot see stderr and would
        read the empty value as "no resolution"."""
        r = self.run_script(["Any Task"], [], exit_code="1")
        self.assertEqual(0, r.returncode)
        self.assertIn("DEGRADED", r.stderr)
        self.assertEqual("UNKNOWN", self.path_for(r, "Any Task"))

    def test_non_list_payload_degrades_and_is_announced(self):
        """The second review finding: a payload that is not a list of dicts must reach the
        no-resolution path, not raise AttributeError through an unskippable pass."""
        r = self.run_script(["Any Task"], {"private-personal": {"path": "/tmp"}})
        self.assertEqual(0, r.returncode, f"exited {r.returncode}; stderr={r.stderr}")
        self.assertNotIn("Traceback", r.stderr)
        self.assertIn("DEGRADED", r.stderr)
        self.assertEqual("UNKNOWN", self.path_for(r, "Any Task"))

    def test_malformed_json_degrades_and_is_announced(self):
        r = self.run_script(["Any Task"], "not json at all")
        self.assertEqual(0, r.returncode)
        self.assertNotIn("Traceback", r.stderr)
        self.assertIn("DEGRADED", r.stderr)
        self.assertEqual("UNKNOWN", self.path_for(r, "Any Task"))

    def test_list_containing_non_dicts_degrades_and_is_announced(self):
        r = self.run_script(["Any Task"], ["private-personal", 42, None])
        self.assertEqual(0, r.returncode)
        self.assertNotIn("Traceback", r.stderr)
        self.assertIn("DEGRADED", r.stderr)

    def test_successful_read_emits_no_degraded_line(self):
        """The marker must mean something — a healthy read never carries it."""
        v = self.vault("private-personal", files=("Real Task.md",))
        r = self.run_script(["Real Task"], [v])
        self.assertNotIn("DEGRADED", r.stderr)
        self.assertEqual(str(self.root / "private-personal" / "25 Tasks" / "Real Task.md"),
                         self.path_for(r, "Real Task"))

    # --- the tier that was actually reached ----------------------------------

    def test_duplicate_vault_entry_is_not_a_collision(self):
        """A config listing one vault twice — or twice through a symlink — must not make the
        same file count as two hits. `fleet-sessions.py` dedupes on `os.path.realpath`."""
        v = self.vault("private-personal", files=("Real Task.md",))
        r = self.run_script(["Real Task"], [v, dict(v)])
        self.assertEqual(str(self.root / "private-personal" / "25 Tasks" / "Real Task.md"),
                         self.path_for(r, "Real Task"),
                         "a duplicated config entry must not read as an ambiguity")
        self.assertEqual("", r.stderr)

    def test_ambiguous_tasks_tier_announces_only_task_candidates(self):
        """`goals_dir` was never consulted, so its paths must not be labelled AMBIGUOUS."""
        a = self.vault("vault-a", files=("Collide.md",))
        b = self.vault("vault-b", files=("Collide.md",))
        (self.root / "vault-a" / "24 Goals" / "Collide.md").write_text("")
        r = self.run_script(["Collide"], [a, b])
        self.assertEqual(2, r.stderr.count("AMBIGUOUS"))
        self.assertNotIn("24 Goals", r.stderr,
                         "a goal-tier path must not be announced for a tier never reached")


if __name__ == "__main__":
    unittest.main()
