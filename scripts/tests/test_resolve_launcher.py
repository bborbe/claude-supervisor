#!/usr/bin/env python3
"""Tests for scripts/resolve-launcher.py.

The load-bearing property: the launcher a RESUME runs is chosen by CODE, and the choice
is visible in one line — launcher, provenance, role, chip. Before this script the value
came from prose in `commands/open.md` § 3.1, so whichever session ran the resume picked
it and nothing checked the result; measured 2026-11-07, two `role: agent` rows resumed as
`cc-private-claude --resume … /color cyan` because their resumer ran on Opus.

The rule itself is `restart-worker.py`'s and is pinned there; these cases pin the CLI's
contract — the three launcher precedences, the chip that follows the ROLE rather than the
caller, and a refusal that is a refusal (exit 1, reason on stderr) rather than a guess.

Run: python3 -m unittest discover -s scripts/tests -v
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_SCRIPTS = os.path.dirname(_HERE)
SCRIPT = os.path.join(_SCRIPTS, "resolve-launcher.py")


class ResolveLauncherCli(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = self.tmp.name
        self.vault = os.path.join(d, "vault")
        self.scripts = os.path.join(d, "scripts")
        for sub in ("25 Tasks", "24 Goals"):
            os.makedirs(os.path.join(self.vault, sub))
        os.makedirs(self.scripts)
        for name in ("cc-private", "cc-private-claude", "cc-private-deepseek", "cc-private-glm"):
            open(os.path.join(self.scripts, name), "w").close()
        self.config = os.path.join(d, "vaults.json")
        self.role_map = os.path.join(d, "role-map.json")
        with open(self.role_map, "w") as fh:
            # Deliberately NOT the role defaults (`pink`/`orange`/`cyan`): a map carrying
            # the defaults cannot distinguish "the map was read" from "the map was missing
            # and the role default was used" — the two paths this file must keep apart.
            json.dump({
                "Agents": {"chip": "purple", "window_id": 1},
                "Managers": {"chip": "yellow", "window_id": 2},
                "Direct": {"chip": "green", "window_id": 3},
            }, fh)
        self.configure()

    def tearDown(self):
        self.tmp.cleanup()

    def configure(self, **extra):
        entry = {"name": "v", "path": self.vault,
                 "claude_script": os.path.join(self.scripts, "cc-private")}
        entry.update(extra)
        with open(self.config, "w") as fh:
            json.dump([entry], fh)

    def page(self, title, fm, sub="25 Tasks"):
        with open(os.path.join(self.vault, sub, f"{title}.md"), "w") as fh:
            fh.write(f"---\n{fm}\n---\nbody\n")

    def run_cli(self, sid, cwd=None, role_map=None):
        env = dict(os.environ)
        env["SUPERVISOR_VAULT_CONFIG"] = self.config
        env["SUPERVISOR_ROLE_MAP"] = role_map or self.role_map
        env.pop("SUPERVISOR_LAUNCHER", None)
        return subprocess.run(
            [sys.executable, SCRIPT, sid, "--cwd", cwd or self.vault],
            capture_output=True, text=True, env=env, timeout=30,
        )

    def fields(self, result):
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.strip().split("\t")

    def test_an_agent_row_resolves_to_the_worker_default_with_the_map_chip(self):
        """The regression: a `role: agent` row with no launcher must NOT come back on Opus."""
        self.configure(worker_launcher=os.path.join(self.scripts, "cc-private-deepseek"))
        self.page("T", "claude_session_id: s1\nrole: agent")
        self.assertEqual(
            self.fields(self.run_cli("s1")),
            [os.path.join(self.scripts, "cc-private-deepseek"), "worker-default", "agent", "purple"],
        )

    def test_a_human_row_resolves_to_claude_with_the_map_chip(self):
        self.configure(worker_launcher=os.path.join(self.scripts, "cc-private-deepseek"))
        self.page("T", "claude_session_id: s1\nrole: human")
        self.assertEqual(
            self.fields(self.run_cli("s1")),
            [os.path.join(self.scripts, "cc-private-claude"), "claude-sibling", "human", "green"],
        )

    def test_a_tasks_own_launcher_wins_over_both(self):
        self.configure(worker_launcher=os.path.join(self.scripts, "cc-private-deepseek"))
        self.page("T", "claude_session_id: s1\nrole: human\nlauncher: cc-private-glm")
        self.assertEqual(
            self.fields(self.run_cli("s1")),
            [os.path.join(self.scripts, "cc-private-glm"), "task", "human", "green"],
        )

    def test_an_absent_role_reads_as_agent(self):
        """Matrix Rule 1: no `role:` key is agent, and the chip follows the resolved role."""
        self.page("T", "claude_session_id: s1")
        self.assertEqual(self.fields(self.run_cli("s1"))[2:], ["agent", "purple"])

    def test_a_manager_session_is_coloured_as_a_manager(self):
        """A manager carries no task page, so the claimant lookup alone would read it as a
        worker — the same defect the prose rule had, one layer down. Read from the goal
        page, never the registry: the registry is gone by the time a resume happens."""
        self.page("G", "claude_session_id: m1", sub="24 Goals")
        self.assertEqual(
            self.fields(self.run_cli("m1")),
            [os.path.join(self.scripts, "cc-private-claude"), "claude-sibling", "manager", "yellow"],
        )

    def test_a_chip_outside_the_colour_allow_list_falls_back_to_the_role_default(self):
        """The chip is re-emitted inside a `bash -lc` string; a corrupt map must not reach it."""
        bad = os.path.join(os.path.dirname(self.role_map), "bad-map.json")
        with open(bad, "w") as fh:
            json.dump({"Direct": {"chip": 'cyan"; rm -rf / #'}}, fh)
        self.page("T", "claude_session_id: s1\nrole: human")
        self.assertEqual(self.fields(self.run_cli("s1", role_map=bad))[3], "cyan")

    def test_a_launcher_carrying_shell_text_is_refused(self):
        """`SUPERVISOR_LAUNCHER` skips the resolver's own shape check; the CLI is the boundary."""
        self.page("T", "claude_session_id: s1")
        env = dict(os.environ, SUPERVISOR_VAULT_CONFIG=self.config,
                   SUPERVISOR_ROLE_MAP=self.role_map,
                   SUPERVISOR_LAUNCHER='cc-x"; rm -rf / #')
        result = subprocess.run([sys.executable, SCRIPT, "s1", "--cwd", self.vault],
                                capture_output=True, text=True, env=env, timeout=30)
        self.assertEqual(result.returncode, 1)
        self.assertIn("not a plain script name or path", result.stderr)
        self.assertEqual(result.stdout, "")

    def test_an_unreadable_role_map_falls_back_to_the_roles_own_chip(self):
        """A missing map must not default every role to cyan — the role is the fact. Both
        cases are checked because `human`'s default IS cyan: on its own it cannot tell a
        correct fallback from "everything is cyan"."""
        self.page("T", "claude_session_id: s1\nrole: human")
        self.assertEqual(self.fields(self.run_cli("s1", role_map="/nonexistent/map.json"))[3], "cyan")
        self.page("U", "claude_session_id: s2")
        self.assertEqual(self.fields(self.run_cli("s2", role_map="/nonexistent/map.json"))[3], "pink")

    def test_a_role_carrying_a_tab_cannot_shift_the_output_fields(self):
        """The output is TAB-DELIMITED and the call site splits it with `cut -f4`. A tab in
        the author-controlled `role:` would hand that text to `$CHIP`, past the allow-list
        and into the inner `bash -lc`."""
        self.page("T", 'claude_session_id: s1\nrole: agent\tcyan"; rm -rf / #')
        fields = self.fields(self.run_cli("s1"))
        self.assertEqual(len(fields), 4)
        self.assertEqual(fields[2:], ["agent", "purple"])

    def test_an_unresolvable_launcher_refuses_rather_than_guessing(self):
        result = self.run_cli("nobody", cwd="/tmp")
        self.assertEqual(result.returncode, 1)
        self.assertIn("under no configured vault", result.stderr)
        self.assertEqual(result.stdout, "")

    def test_the_launcher_override_wins_and_says_so(self):
        """`SUPERVISOR_LAUNCHER` short-circuits the whole rule — the one branch that
        returns a value no other check reads, so it needs its own case."""
        self.page("T", "claude_session_id: s1\nrole: human")
        env = dict(os.environ, SUPERVISOR_VAULT_CONFIG=self.config,
                   SUPERVISOR_ROLE_MAP=self.role_map,
                   SUPERVISOR_LAUNCHER="/override/cc-drill")
        result = subprocess.run([sys.executable, SCRIPT, "s1", "--cwd", self.vault],
                                capture_output=True, text=True, env=env, timeout=30)
        self.assertEqual(self.fields(result), ["/override/cc-drill", "override", "human", "green"])

    def test_missing_arguments_is_a_usage_error(self):
        env = dict(os.environ, SUPERVISOR_VAULT_CONFIG=self.config)
        result = subprocess.run([sys.executable, SCRIPT], capture_output=True, text=True, env=env)
        self.assertEqual(result.returncode, 2)


if __name__ == "__main__":
    unittest.main()
