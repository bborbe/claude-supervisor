#!/usr/bin/env python3
"""Tests for scripts/restart-worker.py.

The load-bearing property is that every refusal fires *before* anything is signalled:
a busy target, a headless worker, an ambiguous pid, a held session and a load path that
would reload identical code must each stop the script with its own reason token and
exit 1, and no code path may reach `os.kill` on a target it has not cleared. A manager
is deliberately NOT among them — it is restarted like a worker, which the manager case
pins. The
integration cases drive the real CLI against a fixture registry, so they assert the
exit code and the token a caller actually branches on — not an internal helper's
return value.

Run: python3 -m unittest discover -s scripts/tests -v
"""

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_SCRIPTS = os.path.dirname(_HERE)
_spec = importlib.util.spec_from_file_location(
    "restart_worker", os.path.join(_SCRIPTS, "restart-worker.py")
)
rw = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rw)

SCRIPT = os.path.join(_SCRIPTS, "restart-worker.py")


def entry(sid, **over):
    """A registry record with the fields the script reads."""
    rec = {
        "sessionId": sid,
        "pid": 4242,
        "status": "idle",
        "kind": "interactive",
        "name": "Some Worker",
        "startedAt": "2020-01-01T00:00:00Z",
        "cwd": "/tmp",
    }
    rec.update(over)
    return rec


class RegistryReads(unittest.TestCase):
    def test_missing_dir_is_none_not_empty(self):
        """An unreadable registry must not read as an empty one — `None` != `[]`."""
        self.assertIsNone(rw.registry_entries("/nonexistent/registry/dir"))

    def test_empty_dir_is_empty_list(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(rw.registry_entries(d), [])


class FindTarget(unittest.TestCase):
    def test_absent(self):
        self.assertEqual(rw.find_target([("p", entry("a"))], "b"), (None, None))

    def test_single(self):
        path, rec = rw.find_target([("p", entry("a"))], "a")
        self.assertEqual(path, "p")
        self.assertEqual(rec["sessionId"], "a")

    def test_ambiguous_pid_is_its_own_verdict(self):
        """Two entries claiming one session id must not silently pick one."""
        matches = [("p1", entry("a")), ("p2", entry("a"))]
        verdict, found = rw.find_target(matches, "a")
        self.assertEqual(verdict, "ambiguous")
        self.assertEqual(len(found), 2)


class RoleOf(unittest.TestCase):
    """Rule 1's four signals, and the fail-closed `None`."""

    class FB:
        ROOT_NAME = "Fleet Manager"
        MANAGER_COLOUR = "orange"

        def __init__(self, subject=None):
            self._subject = subject

        def manager_subject(self, name, index, slugs):
            return self._subject

    def test_root_name_is_a_manager(self):
        """There is no `Fleet Manager` page, so only this signal catches the root."""
        fb = self.FB(subject=None)
        self.assertEqual(rw.role_of("s", "Fleet Manager", fb, {}, set(), {}), "manager")

    def test_resolved_page_is_a_manager(self):
        fb = self.FB(subject=("topic", "Manager Layer"))
        self.assertEqual(rw.role_of("s", "Manager Layer", fb, {}, set(), {}), "manager")

    def test_orange_colour_is_a_manager(self):
        fb = self.FB(subject=None)
        self.assertEqual(rw.role_of("s", "Anything", fb, {}, set(), {"s": "Orange"}), "manager")

    def test_ordinary_worker(self):
        fb = self.FB(subject=None)
        self.assertEqual(rw.role_of("s", "Some Worker", fb, {}, set(), {"s": "pink"}), "worker")

    def test_empty_name_is_undetermined_not_worker(self):
        """A nameless entry proves nothing about its role — never assume worker."""
        fb = self.FB(subject=None)
        self.assertIsNone(rw.role_of("s", "", fb, {}, set(), {}))

    def test_unreadable_index_is_undetermined_not_worker(self):
        """An index that could not be read is not an index that says 'no manager'."""
        fb = self.FB(subject=None)
        self.assertIsNone(rw.role_of("s", "Some Worker", fb, None, set(), {}))


class ParseStarted(unittest.TestCase):
    def test_iso(self):
        self.assertAlmostEqual(
            rw.parse_started({"startedAt": "1970-01-01T00:00:10Z"}), 10.0, places=0
        )

    def test_epoch_seconds(self):
        self.assertEqual(rw.parse_started({"startedAt": 12}), 12.0)

    def test_epoch_milliseconds(self):
        """The live registry writes milliseconds. Read as seconds the date lands
        ~57,000 years out, so every mtime comparison fails and precondition 4
        refuses every restart — the check failing in the direction that hides
        real fixes."""
        self.assertEqual(rw.parse_started({"startedAt": 1790269714645}), 1790269714.645)

    def test_millisecond_value_compares_correctly_against_a_load_path_mtime(self):
        """The regression stated as the comparison that actually broke, using the
        values measured on the real cache 2026-09-24."""
        started = rw.parse_started({"startedAt": 1790269714645})
        load_mtime = 1790282384.495  # 0.52.0's directory, installed later
        self.assertGreater(load_mtime, started)

    def test_missing_and_garbage_are_none(self):
        for rec in ({}, {"startedAt": ""}, {"startedAt": "not-a-date"}):
            self.assertIsNone(rw.parse_started(rec), rec)


class ResumeCommand(unittest.TestCase):
    """The resume must carry the session's own cwd.

    Regression, measured 2026-09-24: `wezterm cli spawn` without `--cwd` inherits
    wezterm's own working directory — a home directory, for a home-started server — so
    the resumed session stalled on Claude Code's *"do you trust this folder?"* dialog
    and registered **no pid at all**. A working kill+resume therefore looked like a
    no-op, and the failure was invisible to every test that did not actually restart a
    session.
    """

    def test_cwd_is_passed(self):
        argv = rw.resume_command("sid", "title", "pink", "/some/dir", "/l/cc")
        self.assertIn("--cwd", argv)
        self.assertEqual(argv[argv.index("--cwd") + 1], "/some/dir")

    def test_no_cwd_flag_when_empty(self):
        self.assertNotIn("--cwd", rw.resume_command("sid", "title", "pink", "", "/l/cc"))

    def test_colour_chip_carries_no_literal_quotes(self):
        """The shell quotes in `open.md`'s recipe are syntax, not value.

        Regression, measured 2026-09-25 on the first live `/supervisor:worker-restart` run:
        a byte-for-byte copy of Step 3.1 wrote `"/color 'pink'"` — the `\\'` in the Python
        f-string is a *literal* quote, because there is no outer `bash -lc '…'` to break out
        of here. Claude Code rejected it (`Invalid color "'pink'"`) and every restarted
        worker kept the default colour instead of pink. The colour is the fleet's only role
        cue, so a restarted worker that should be pink is exactly the mis-coloured session
        that nearly got a manager relaunched as a worker on 2026-09-18.
        """
        inner = rw.resume_command("sid", "title", "pink", "/d", "/l/cc")[-1]
        self.assertIn('"/color pink"', inner)
        self.assertNotIn("'/color", inner)
        self.assertNotIn("pink'", inner)

    def test_resumes_through_the_given_launcher_not_bare_claude(self):
        """Regression 2026-10-10: a bare-`claude` resume dropped `--mcp-config` and `a2a`."""
        inner = rw.resume_command("sid", "title", "pink", "/d", "/l/cc-private-claude")[-1]
        self.assertIn('"/l/cc-private-claude" --resume sid', inner)

    def test_fresh_prompt_starts_a_new_session_not_a_resume(self):
        """`--fresh-prompt` (keeper `restart fresh`, 2026-11-10) must never carry `--resume`:
        a resumed session brings its exhausted context back, which is the whole failure."""
        inner = rw.resume_command("sid", "title", "pink", "/d", "/l/cc",
                                  "/supervisor:fleet-loop")[-1]
        self.assertNotIn("--resume", inner)
        self.assertIn('"/l/cc" -n "title" "/supervisor:fleet-loop"', inner)
        self.assertIn("trap _sx EXIT", inner)

    def test_fresh_prompt_is_allowlisted(self):
        self.assertEqual(("/supervisor:fleet-loop",), rw.FRESH_PROMPTS)

    def test_resume_command_uses_the_resolved_wezterm_not_a_bare_name(self):
        """Regression 2026-11-11: a bare `"wezterm"` is not on launchd's PATH, so the keeper's
        `restart fresh` killed the Fleet Manager and could not spawn the replacement."""
        argv = rw.resume_command("sid", "title", "pink", "/d", "/l/cc",
                                 wezterm="/Applications/WezTerm.app/Contents/MacOS/wezterm")
        self.assertEqual("/Applications/WezTerm.app/Contents/MacOS/wezterm", argv[0])

    def test_no_exec_so_the_exit_status_survives(self):
        """`exec` makes a SIGKILLed worker leave no record anywhere — the whole point.

        Measured 2026-10-27: `bash -lc 'exec bash -c "kill -9 $$"'` reports 137 to its own
        caller but leaves nothing to run a trap; the same child run *without* `exec`, behind
        `trap _sx EXIT`, records `rc=137`. A test that only checked the launcher path would
        pass with `exec` still in place and silently re-open the gap.
        """
        argv = rw.resume_command("sid", "title", "pink", "/d", "/l/cc")
        inner = argv[-1]
        self.assertNotIn("exec ", inner)
        self.assertNotIn("exec ", " ".join(argv))
        self.assertIn("trap _sx EXIT", inner)
        self.assertIn("rc=$?", inner)

    def test_the_writer_path_rides_as_an_env_assignment(self):
        """The pane cannot resolve the plugin root for a script this file already locates."""
        argv = rw.resume_command("sid", "title", "pink", "/d", "/l/cc")
        self.assertIn("--", argv)
        assignments = [a for a in argv if a.startswith("SESSION_EXIT_RECORD=")]
        self.assertEqual(len(assignments), 1)
        self.assertTrue(assignments[0].endswith("/scripts/session-exit-record.py"))

    def test_the_spawn_pins_the_entrypoint_to_cli(self):
        """`wezterm cli spawn` forwards the CALLER's environment, so a headless leg leaks.

        Measured 2026-11-12: five live goal managers sat in real WezTerm panes running their
        loops while their registry entrypoint read `sdk-cli`, because the leg that resumed
        them was headless. The census flags a manager whose mode is headless as
        `kind="invalid"` — a manager cannot be headless — so the chart's `invalid / headless`
        series was this leak and nothing else. `closer-gate-lint.py` reads the same variable
        as its paneless test, so those managers' `Shift+Tab` and `/reload-plugins` closers
        were refused as unactionable asks.

        The assignment must be POSITIVE, in the `env` prefix, not an addition to the `unset`
        list: an absent value leaves Claude Code to derive the entrypoint itself, and this
        pane is a tab by construction.
        """
        argv = rw.resume_command("sid", "title", "pink", "/d", "/l/cc")
        self.assertIn("CLAUDE_CODE_ENTRYPOINT=cli", argv)
        # It must ride as an env assignment before `bash`, never inside `bash -lc '…'`,
        # where it would be a shell word rather than an exported variable.
        self.assertLess(argv.index("CLAUDE_CODE_ENTRYPOINT=cli"), argv.index("bash"))
        self.assertNotIn("CLAUDE_CODE_ENTRYPOINT", argv[-1])

    def test_a_spawn_marker_is_written_before_the_launcher_runs(self):
        """Without it an empty record cannot be told from an unwired writer."""
        inner = rw.resume_command("sid", "title", "pink", "/d", "/l/cc")[-1]
        self.assertIn("--kind spawn --session \"sid\"", inner)
        self.assertLess(inner.index("--kind spawn"), inner.index('--resume sid'))

    def test_the_observer_watches_this_shell(self):
        """`$$` outlives the launcher and dies exactly when the group is killed.

        That is the death the trap cannot record, so it is the one the out-of-group observer
        exists for; watching anything else would leave the group kill unattributed.
        """
        inner = rw.resume_command("sid", "title", "pink", "/d", "/l/cc")[-1]
        self.assertIn('observe --pid $$ --session "sid"', inner)

    def test_the_observer_starts_before_the_launcher(self):
        """Started after the launcher would be started after the first kill can land."""
        inner = rw.resume_command("sid", "title", "pink", "/d", "/l/cc")[-1]
        self.assertLess(inner.index(" observe "), inner.index('--resume sid'))

    def test_the_trap_and_the_marker_both_carry_the_pid(self):
        """A record without a pid cannot be joined to the process it describes."""
        inner = rw.resume_command("sid", "title", "pink", "/d", "/l/cc")[-1]
        self.assertEqual(inner.count("--pid $$"), 3)

    def test_resume_target_and_unset_list_intact(self):
        """The copied recipe's own invariants must survive the --cwd addition."""
        inner = rw.resume_command("sid", "title", "pink", "/d", "/l/cc")[-1]
        self.assertIn("--resume sid", inner)
        self.assertIn("-n \"title\"", inner)
        for var in (
            "CLAUDE_CODE_MESSAGING_SOCKET",
            "CLAUDE_CODE_MESSAGING_TOKEN",
            "CLAUDE_CODE_SESSION_ID",
            "CLAUDE_CODE_CHILD_SESSION",
        ):
            self.assertIn(var, inner)


class ResolveLauncher(unittest.TestCase):
    """The resume runs the worker's OWN launcher: task > vault worker default > `claude_script`.

    ⚠️ A goal is NOT in that chain, and the cases named for it pin its absence. A goal's
    `launcher:` is a manager statement; inheriting it resumed every worker under such a goal on
    its manager's Opus launcher (measured 2026-10-22: 12 private-personal goals).

    The fixture config deliberately carries only `claude_script` — the shape an older vault-cli
    emits — so the compatibility rung is exercised here; `configure()` adds the role keys for
    the cases that need them.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = self.tmp.name
        self.vault = os.path.join(d, "vault")
        self.scripts = os.path.join(d, "scripts")
        for sub in ("25 Tasks", "24 Goals"):
            os.makedirs(os.path.join(self.vault, sub))
        os.makedirs(self.scripts)
        for name in ("cc-private", "cc-private-claude", "cc-private-deepseek", "cc-private-glm", "cc-goal"):
            open(os.path.join(self.scripts, name), "w").close()
        cfg = os.path.join(d, "vaults.json")
        with open(cfg, "w") as fh:
            json.dump([{"name": "v", "path": self.vault,
                        "claude_script": os.path.join(self.scripts, "cc-private")}], fh)
        self._env = {k: os.environ.get(k) for k in ("SUPERVISOR_VAULT_CONFIG", "SUPERVISOR_LAUNCHER")}
        os.environ["SUPERVISOR_VAULT_CONFIG"] = cfg
        os.environ.pop("SUPERVISOR_LAUNCHER", None)
        self.fb = rw.load_sibling("fleet-board.py")

    def tearDown(self):
        for k, v in self._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self.tmp.cleanup()

    def page(self, sub, title, fm):
        with open(os.path.join(self.vault, sub, f"{title}.md"), "w") as fh:
            fh.write(f"---\n{fm}\n---\nbody\n")

    def configure(self, **extra):
        """Rewrite the vault config with the role keys a current vault-cli emits."""
        entry = {"name": "v", "path": self.vault,
                 "claude_script": os.path.join(self.scripts, "cc-private")}
        entry.update(extra)
        with open(os.environ["SUPERVISOR_VAULT_CONFIG"], "w") as fh:
            json.dump([entry], fh)

    def test_task_launcher_wins(self):
        self.page("25 Tasks", "T", "claude_session_id: s1\nlauncher: cc-private-claude")
        path, source = rw.resolve_launcher("s1", self.vault, self.fb)
        self.assertEqual((path, source), (os.path.join(self.scripts, "cc-private-claude"), "task"))

    def test_goal_launcher_is_not_inherited(self):
        """The regression lock: a goal's `launcher:` must never reach its workers."""
        self.page("24 Goals", "G", "launcher: cc-goal")
        self.page("25 Tasks", "T", "claude_session_id: s1\ngoals:\n    - '[[G]]'")
        path, source = rw.resolve_launcher("s1", self.vault, self.fb)
        self.assertEqual(source, "vault")
        self.assertEqual(path, os.path.join(self.scripts, "cc-private"))

    def test_worker_default_wins_when_the_task_names_none(self):
        self.configure(worker_launcher=os.path.join(self.scripts, "cc-private-deepseek"))
        self.page("24 Goals", "G", "launcher: cc-goal")
        self.page("25 Tasks", "T", "claude_session_id: s1\ngoals:\n    - '[[G]]'")
        path, source = rw.resolve_launcher("s1", self.vault, self.fb)
        self.assertEqual((path, source), (os.path.join(self.scripts, "cc-private-deepseek"), "worker-default"))

    def test_a_bare_configured_worker_launcher_resolves_beside_the_vault_launcher(self):
        self.configure(worker_launcher="cc-private-deepseek")
        self.page("25 Tasks", "T", "claude_session_id: s1")
        path, source = rw.resolve_launcher("s1", self.vault, self.fb)
        self.assertEqual((path, source), (os.path.join(self.scripts, "cc-private-deepseek"), "worker-default"))

    def test_task_launcher_still_beats_the_worker_default(self):
        self.configure(worker_launcher=os.path.join(self.scripts, "cc-private-deepseek"))
        self.page("25 Tasks", "T", "claude_session_id: s1\nlauncher: cc-private-claude")
        path, source = rw.resolve_launcher("s1", self.vault, self.fb)
        self.assertEqual((path, source), (os.path.join(self.scripts, "cc-private-claude"), "task"))

    def test_a_human_row_takes_the_claude_sibling_over_the_worker_default(self):
        """Operator ruling 2026-11-06 / matrix Rule 4: a human row is not a delegate."""
        self.configure(worker_launcher=os.path.join(self.scripts, "cc-private-deepseek"))
        self.page("25 Tasks", "T", "claude_session_id: s1\nrole: human")
        path, source = rw.resolve_launcher("s1", self.vault, self.fb)
        self.assertEqual((path, source), (os.path.join(self.scripts, "cc-private-claude"), "claude-sibling"))

    def test_the_human_sibling_sits_ahead_of_a_configured_manager_launcher(self):
        """Both private-personal and seibert-brogrammers point `manager_launcher` at `-deepseek`;
        taking it first would leave the human row on the worker model while reporting success."""
        self.configure(worker_launcher=os.path.join(self.scripts, "cc-private-deepseek"),
                       manager_launcher=os.path.join(self.scripts, "cc-private-glm"))
        self.page("25 Tasks", "T", "claude_session_id: s1\nrole: human")
        path, source = rw.resolve_launcher("s1", self.vault, self.fb)
        self.assertEqual((path, source), (os.path.join(self.scripts, "cc-private-claude"), "claude-sibling"))

    def test_the_human_rule_falls_through_to_the_manager_default_without_a_sibling(self):
        os.remove(os.path.join(self.scripts, "cc-private-claude"))
        self.configure(worker_launcher=os.path.join(self.scripts, "cc-private-deepseek"),
                       manager_launcher="cc-private-glm")
        self.page("25 Tasks", "T", "claude_session_id: s1\nrole: human")
        path, source = rw.resolve_launcher("s1", self.vault, self.fb)
        self.assertEqual((path, source), (os.path.join(self.scripts, "cc-private-glm"), "manager-default"))

    def test_the_human_rule_ends_on_the_vault_claude_script_never_the_worker_default(self):
        """Matrix Rule 4's last rung. A vault with a `worker_launcher` but no `-claude`
        sibling and no `manager_launcher` must still open the row on `claude_script` —
        returning the worker default here puts a human row on `-deepseek`."""
        os.remove(os.path.join(self.scripts, "cc-private-claude"))
        self.configure(worker_launcher=os.path.join(self.scripts, "cc-private-deepseek"))
        self.page("25 Tasks", "T", "claude_session_id: s1\nrole: human")
        path, source = rw.resolve_launcher("s1", self.vault, self.fb)
        self.assertEqual((path, source), (os.path.join(self.scripts, "cc-private"), "vault"))

    def test_two_pages_claiming_one_session_are_refused(self):
        """First-match would resume under a subject chosen by page ordering."""
        os.makedirs(os.path.join(self.vault, "23 Topics"))
        self.page("24 Goals", "G", "claude_session_id: m1")
        self.page("23 Topics", "Fleet", "claude_session_id: m1")
        role, subject, error = rw.resume_role("m1", self.vault, self.fb)
        self.assertIsNone(subject)
        self.assertIn("2 pages claim session m1", error)

    def test_a_human_rows_own_launcher_still_wins(self):
        self.page("25 Tasks", "T", "claude_session_id: s1\nrole: human\nlauncher: cc-private-glm")
        path, source = rw.resolve_launcher("s1", self.vault, self.fb)
        self.assertEqual((path, source), (os.path.join(self.scripts, "cc-private-glm"), "task"))

    def test_an_agent_row_is_unaffected_by_the_human_rule(self):
        self.configure(worker_launcher=os.path.join(self.scripts, "cc-private-deepseek"))
        self.page("25 Tasks", "T", "claude_session_id: s1\nrole: agent")
        path, source = rw.resolve_launcher("s1", self.vault, self.fb)
        self.assertEqual((path, source), (os.path.join(self.scripts, "cc-private-deepseek"), "worker-default"))

    def test_claimant_role_reports_the_bound_task_and_nothing_else(self):
        self.page("25 Tasks", "T", "claude_session_id: s1\nrole: human")
        self.assertEqual(rw._role_field(os.path.join(self.vault, "25 Tasks", "T.md")), "human")
        self.assertEqual(rw._role_field(os.path.join(self.vault, "25 Tasks", "absent.md")), "")

    def test_resume_role_reads_a_manager_from_its_goal_page(self):
        """The registry is deleted when a session exits, and a manager only reaches the
        resume path once it has exited — so the page field, not the registry, is the source."""
        self.page("24 Goals", "G", "claude_session_id: m1")
        self.assertEqual(rw.resume_role("m1", self.vault, self.fb), ("manager", ("goal", "G"), None))

    def test_resume_role_reads_a_manager_from_its_topic_page(self):
        os.makedirs(os.path.join(self.vault, "23 Topics"))
        self.page("23 Topics", "Fleet", "claude_session_id: m1")
        self.assertEqual(rw.resume_role("m1", self.vault, self.fb), ("manager", ("topic", "Fleet"), None))

    def test_resume_role_reads_a_human_from_the_claimant_task(self):
        self.page("25 Tasks", "T", "claude_session_id: s1\nrole: human")
        self.assertEqual(rw.resume_role("s1", self.vault, self.fb), ("human", None, None))

    def test_resume_role_defaults_to_agent(self):
        self.page("25 Tasks", "T", "claude_session_id: s1")
        self.assertEqual(rw.resume_role("s1", self.vault, self.fb), ("agent", None, None))
        self.assertEqual(rw.resume_role("nobody", self.vault, self.fb), ("agent", None, None))

    def test_unsafe_configured_worker_launcher_is_none(self):
        self.configure(worker_launcher="cc;rm -rf")
        self.page("25 Tasks", "T", "claude_session_id: s1")
        self.assertIsNone(rw.resolve_launcher("s1", self.vault, self.fb)[0])

    def test_vault_default_for_an_unbound_session_on_an_older_vault_cli(self):
        """No `worker_launcher` in config (old vault-cli): the compatibility rung, visibly."""
        path, source = rw.resolve_launcher("nobody", os.path.join(self.vault, "25 Tasks"), self.fb)
        self.assertEqual((path, source), (os.path.join(self.scripts, "cc-private"), "vault"))

    def test_manager_takes_the_claude_sibling(self):
        path, source = rw.resolve_launcher("m1", self.vault, self.fb, ("topic", "Fleet"))
        self.assertEqual((path, source), (os.path.join(self.scripts, "cc-private-claude"), "claude-sibling"))

    def test_manager_takes_the_configured_manager_default(self):
        self.configure(manager_launcher=os.path.join(self.scripts, "cc-private-claude"))
        path, source = rw.resolve_launcher("m1", self.vault, self.fb, ("topic", "Fleet"))
        self.assertEqual((path, source), (os.path.join(self.scripts, "cc-private-claude"), "manager-default"))

    def test_a_configured_manager_launcher_need_not_end_in_claude(self):
        """Provenance, not the filename, decides the source — a glm manager stays `manager-default`."""
        self.configure(manager_launcher="cc-private-glm")
        path, source = rw.resolve_launcher("m1", self.vault, self.fb, ("topic", "Fleet"))
        self.assertEqual((path, source), (os.path.join(self.scripts, "cc-private-glm"), "manager-default"))

    def test_an_old_vault_cli_with_a_claude_script_is_still_the_sibling_rung(self):
        """No `manager_launcher` in config: the naming convention decided, and it must say so."""
        with open(os.environ["SUPERVISOR_VAULT_CONFIG"], "w") as fh:
            json.dump([{"name": "v", "path": self.vault,
                        "claude_script": os.path.join(self.scripts, "cc-private-claude")}], fh)
        path, source = rw.resolve_launcher("m1", self.vault, self.fb, ("topic", "Fleet"))
        self.assertEqual((path, source), (os.path.join(self.scripts, "cc-private-claude"), "claude-sibling"))

    def test_an_unsafe_configured_manager_launcher_is_refused_before_any_exec(self):
        self.configure(manager_launcher='cc-x"; rm -rf /')
        self.assertIsNone(rw.resolve_launcher("m1", self.vault, self.fb, ("topic", "Fleet"))[0])

    def test_a_bare_configured_manager_launcher_resolves_beside_the_vault_launcher(self):
        """A config value with no slash is a name, not a relative path — join it, never fail."""
        self.configure(manager_launcher="cc-private-claude")
        path, source = rw.resolve_launcher("m1", self.vault, self.fb, ("topic", "Fleet"))
        self.assertEqual((path, source), (os.path.join(self.scripts, "cc-private-claude"), "manager-default"))

    def test_manager_subject_manager_launcher_wins(self):
        os.makedirs(os.path.join(self.vault, "23 Topics"))
        self.page("23 Topics", "Fleet", "manager_launcher: cc-goal")
        path, source = rw.resolve_launcher("m1", self.vault, self.fb, ("topic", "Fleet"))
        self.assertEqual((path, source), (os.path.join(self.scripts, "cc-goal"), "subject"))

    def test_manager_keeps_vault_launcher_without_sibling(self):
        os.remove(os.path.join(self.scripts, "cc-private-claude"))
        path, source = rw.resolve_launcher("m1", self.vault, self.fb, ("goal", "G"))
        self.assertEqual((path, source), (os.path.join(self.scripts, "cc-private"), "vault"))

    def test_manager_sibling_strips_deepseek(self):
        ml = rw.load_sibling("manager-launcher.py")
        self.assertEqual(ml.sibling("/s/cc-seibert-deepseek"), "/s/cc-seibert-claude")
        self.assertEqual(ml.sibling("/s/cc-private-claude"), "/s/cc-private-claude")

    def test_manager_unsafe_manager_launcher_is_none(self):
        os.makedirs(os.path.join(self.vault, "23 Topics"))
        self.page("23 Topics", "Fleet", "manager_launcher: cc;rm -rf")
        self.assertIsNone(rw.resolve_launcher("m1", self.vault, self.fb, ("topic", "Fleet"))[0])

    def test_cwd_outside_every_vault_is_none(self):
        self.assertIsNone(rw.resolve_launcher("s1", "/nowhere", self.fb)[0])

    def test_unsafe_launcher_is_none(self):
        self.page("25 Tasks", "T", "claude_session_id: s1\nlauncher: cc;rm -rf")
        self.assertIsNone(rw.resolve_launcher("s1", self.vault, self.fb)[0])

    def test_metrics_sessions_only_binding_is_matched(self):
        self.page("25 Tasks", "T", "launcher: cc-private-claude\nmetrics_sessions:\n    - session_id: s9\n      started_at: x")
        self.assertEqual(rw.resolve_launcher("s9", self.vault, self.fb)[1], "task")

    def test_two_claimants_is_none(self):
        self.page("25 Tasks", "A", "claude_session_id: s1")
        self.page("25 Tasks", "B", "claude_session_id: s1")
        path, reason = rw.resolve_launcher("s1", self.vault, self.fb)
        self.assertIsNone(path)
        self.assertIn("2 task pages", reason)

    def test_goals_naming_different_launchers_no_longer_refuse(self):
        """Goal values are inert, so a disagreement between them cannot block a resume."""
        self.page("24 Goals", "G1", "launcher: cc-goal")
        self.page("24 Goals", "G2", "launcher: cc-private-claude")
        self.page("25 Tasks", "T", "claude_session_id: s1\ngoals:\n    - '[[G1]]'\n    - '[[G2]]'")
        path, source = rw.resolve_launcher("s1", self.vault, self.fb)
        self.assertEqual(source, "vault")
        self.assertEqual(path, os.path.join(self.scripts, "cc-private"))

    def test_unreadable_vault_config_is_none(self):
        os.environ["SUPERVISOR_VAULT_CONFIG"] = os.path.join(self.tmp.name, "absent.json")
        self.assertIsNone(rw.resolve_launcher("s1", self.vault, self.fb)[0])

    def test_non_dict_config_element_is_skipped_not_raised(self):
        with open(os.environ["SUPERVISOR_VAULT_CONFIG"], "w") as fh:
            json.dump(["junk", {"name": "v", "path": self.vault,
                                "claude_script": os.path.join(self.scripts, "cc-private")}], fh)
        self.assertEqual(rw.resolve_launcher("x", self.vault, self.fb)[1], "vault")

    def test_config_entry_without_path_claims_no_cwd(self):
        with open(os.environ["SUPERVISOR_VAULT_CONFIG"], "w") as fh:
            json.dump([{"name": "empty", "path": "", "claude_script": "/x/cc"}], fh)
        self.assertIsNone(rw.resolve_launcher("s1", os.getcwd(), self.fb)[0])

    def test_launcher_survives_long_frontmatter(self):
        filler = "\n".join(f"    - session_id: f{i}\n      started_at: x" for i in range(200))
        self.page("25 Tasks", "T", f"claude_session_id: s1\nmetrics_sessions:\n{filler}\nlauncher: cc-private-claude")
        self.assertEqual(rw.resolve_launcher("s1", self.vault, self.fb)[1], "task")

    def test_override_bypasses_shape_and_file_checks(self):
        """Deliberate: the override is the drill/test lever, set by the caller itself."""
        os.environ["SUPERVISOR_LAUNCHER"] = "/no/such/launcher"
        self.assertEqual(rw.resolve_launcher("s1", "/nowhere", self.fb), ("/no/such/launcher", "override"))

    def test_missing_launcher_file_is_none(self):
        self.page("25 Tasks", "T", "claude_session_id: s1\nlauncher: cc-absent")
        self.assertIsNone(rw.resolve_launcher("s1", self.vault, self.fb)[0])


class LoadPathSelection(unittest.TestCase):
    """The newest load path is chosen by VERSION, never by directory mtime.

    Regression, measured 2026-09-24 on the real cache: `0.51.2` and `0.52.0` were
    installed in one operation and their directory mtimes differed by 3ms with the
    **older** one later. An mtime-max therefore selected `0.51.2`, and precondition 4
    then compared the session against the wrong copy and refused a restart that
    should have been allowed — the check failing in the direction that hides a real
    fix.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._saved = rw.LOAD_PATH_ROOT
        rw.LOAD_PATH_ROOT = self.tmp.name

    def tearDown(self):
        rw.LOAD_PATH_ROOT = self._saved
        self.tmp.cleanup()

    def make(self, name, mtime):
        path = os.path.join(self.tmp.name, name)
        os.makedirs(path)
        os.utime(path, (mtime, mtime))
        return path

    def test_newer_version_wins_even_when_its_mtime_is_earlier(self):
        self.make("0.51.2", 2000)
        newer = self.make("0.52.0", 1000)
        path, _ = rw.newest_load_path()
        self.assertEqual(path, newer)

    def test_ordered_numerically_not_lexically(self):
        """`0.10.0` is newer than `0.9.0`, though it sorts lower as a string."""
        self.make("0.9.0", 1000)
        ten = self.make("0.10.0", 1000)
        path, _ = rw.newest_load_path()
        self.assertEqual(path, ten)

    def test_stray_directory_cannot_win(self):
        self.make("0.52.0", 1000)
        self.make("not-a-version", 9999)
        path, _ = rw.newest_load_path()
        self.assertEqual(os.path.basename(path), "0.52.0")

    def test_empty_root_is_none(self):
        self.assertEqual(rw.newest_load_path(), (None, None))


class WorkerArgv(unittest.TestCase):
    """The live `ps` read and the input comparison, without the CLI override."""

    def setUp(self):
        self._env = os.environ.pop("SUPERVISOR_WORKER_ARGV", None)
        self._run = rw.subprocess.run

    def tearDown(self):
        rw.subprocess.run = self._run
        if self._env is not None:
            os.environ["SUPERVISOR_WORKER_ARGV"] = self._env

    def stub(self, returncode=0, stdout="", raises=False):
        class Out:
            pass

        def run(*_a, **_k):
            if raises:
                raise OSError("no ps")
            out = Out()
            out.returncode, out.stdout = returncode, stdout
            return out

        rw.subprocess.run = run

    def test_populated_read(self):
        self.stub(stdout="claude --mcp-config=/x.json\n")
        self.assertEqual(rw.worker_argv(1), "claude --mcp-config=/x.json")

    def test_failed_query_is_none(self):
        self.stub(returncode=1)
        self.assertIsNone(rw.worker_argv(1))

    def test_raising_query_is_none(self):
        self.stub(raises=True)
        self.assertIsNone(rw.worker_argv(1))

    def test_flag_followed_by_flag_is_not_a_path(self):
        self.assertEqual(rw.mcp_config_paths("claude --mcp-config --strict-mcp-config"), [])

    def test_no_load_path_accepts_on_newer_mcp_config(self):
        with tempfile.NamedTemporaryFile() as fh:
            changed = rw.changed_inputs(None, None, f"claude --mcp-config={fh.name}", None, 0.0)
        self.assertEqual([label for label, _ in changed], ["MCP config"])

    def test_none_argv_contributes_nothing(self):
        self.assertEqual(rw.changed_inputs(None, None, None, None, 0.0), [])


class DescribeChanged(unittest.TestCase):
    """Regression 2026-10-10: an f-string followed by `"; ".join(...)` concatenated the two
    literals into the separator, so the hand-off line read `plugin version (…)   ↪ tell …: ; MCP config (…)`."""

    def test_joins_each_input_once(self):
        self.assertEqual(
            rw.describe_changed([("plugin version", "0.1.0"), ("MCP config", "/p")]),
            "plugin version (0.1.0); MCP config (/p)",
        )

    def test_handoff_line_is_built_from_it(self):
        with open(SCRIPT, encoding="utf-8") as fh:
            src = fh.read()
        self.assertIn("what changed: {describe_changed(changed)}", src)


class McpSource(unittest.TestCase):
    """The launch-source report (SC3).

    The two versions must be READ LIVE, never hardcoded: the warning exists to catch a
    restart that reloads stale server code and reads as a success, so a printed version
    has to be evidence. The control case — equal versions print no warning — is what
    makes a hardcoded warning unable to pass.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def checkout(self, version):
        root = os.path.join(self.dir, "checkout")
        os.makedirs(os.path.join(root, ".claude-plugin"), exist_ok=True)
        with open(os.path.join(root, ".claude-plugin", "plugin.json"), "w") as fh:
            json.dump({"version": version}, fh)
        os.makedirs(os.path.join(root, "scripts"), exist_ok=True)
        return root

    def config(self, args, name="mcp.json"):
        path = os.path.join(self.dir, name)
        with open(path, "w") as fh:
            json.dump({"mcpServers": {"supervisor": {"command": "python3", "args": args}}}, fh)
        return path

    def test_reads_root_and_version_live(self):
        root = self.checkout("0.1.0")
        cfg = self.config([os.path.join(root, "scripts", "mcp-start.py")])
        got_root, got_version, _ = rw.mcp_source(f"claude --mcp-config={cfg}")
        self.assertEqual(got_root, root)
        self.assertEqual(got_version, "0.1.0")

    def test_plugin_version_missing_file_is_none(self):
        self.assertIsNone(rw.plugin_version(self.dir))

    def test_no_supervisor_entry_is_unknown(self):
        cfg = self.config(["/somewhere/else/other.py"])
        self.assertIsNone(rw.mcp_source(f"claude --mcp-config={cfg}")[0])

    def test_no_argv_and_no_config_are_unknown(self):
        self.assertIsNone(rw.mcp_source(None)[0])
        self.assertIsNone(rw.mcp_source("claude --strict-mcp-config")[0])

    def test_unreadable_config_is_unknown_not_a_crash(self):
        path = os.path.join(self.dir, "broken.json")
        with open(path, "w") as fh:
            fh.write("{not json")
        self.assertIsNone(rw.mcp_source(f"claude --mcp-config={path}")[0])

    def test_no_warning_when_versions_match(self):
        """The control case: a hardcoded warning cannot pass this."""
        lines = rw.describe_source("/checkout", "0.1.0", "0.1.0")
        self.assertEqual(len(lines), 1, lines)
        self.assertFalse(lines[0][1])

    def test_warning_when_source_lags_installed(self):
        lines = rw.describe_source("/checkout", "0.1.0", "0.2.0")
        self.assertEqual(len(lines), 2, lines)
        self.assertTrue(lines[1][1])
        self.assertIn("0.1.0", lines[1][0])
        self.assertIn("0.2.0", lines[1][0])

    def test_unreadable_version_is_reported_and_never_warns(self):
        lines = rw.describe_source("/checkout", None, "0.2.0")
        self.assertEqual(len(lines), 1, lines)
        self.assertIn("unreadable", lines[0][0])

    def test_unknown_source_says_so(self):
        self.assertIn("unknown", rw.describe_source(None, None, "0.2.0")[0][0])


class RefusalCode(unittest.TestCase):
    """Pin the exit code every refusal routes through.

    Both resume-failure paths in `main()` return via `refuse()`, and there is no
    fall-through to the success `return 0` — so this single assertion is what
    guarantees a killed-but-not-resumed session can never read as a successful
    restart. A review pass misread that control flow as falling through to 0; this
    test makes the invariant executable rather than a claim in a comment.
    """

    def test_refuse_returns_nonzero(self):
        self.assertEqual(rw.refuse("some-token", "some detail"), 1)


class Cli(unittest.TestCase):
    """End-to-end against a fixture registry: exit code + reason token."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name
        self.reg = os.path.join(self.dir, "sessions")
        os.makedirs(self.reg)
        self.load = os.path.join(self.dir, "loadpath")
        os.makedirs(os.path.join(self.load, "9.9.9"))
        # No role map: the Hold-window source is off for the store/CLI tests, so they
        # never shell out to a real WezTerm and stay host-independent. The window is
        # exercised on its own in HoldWindow below.
        os.environ["SUPERVISOR_ROLE_MAP"] = os.path.join(self.dir, "no-role-map.json")

    def tearDown(self):
        os.environ.pop("SUPERVISOR_ROLE_MAP", None)
        self.tmp.cleanup()

    def write(self, pid, rec):
        with open(os.path.join(self.reg, f"{pid}.json"), "w") as fh:
            json.dump(rec, fh)

    def run_cli(self, sid, *extra):
        env = dict(os.environ)
        env["SUPERVISOR_LOAD_PATH"] = self.load
        env["SUPERVISOR_LAUNCHER"] = os.path.join(self.dir, "loadpath")
        # `main()` resolves the wezterm binary BEFORE the kill, and refuses with exit 1 when
        # it cannot — deliberately, so a spawn that cannot happen never leaves a killed pid
        # behind it. A CI runner has no wezterm, so without this every success-path test
        # would take that refusal. `setdefault`, never an assignment: the tests that PROVE
        # the refusal (and the ones that point the override at a missing path) set it
        # themselves and must keep what they set.
        env.setdefault("SUPERVISOR_WEZTERM", sys.executable)
        out = subprocess.run(
            [sys.executable, SCRIPT, sid, "--sessions-dir", self.reg, "--dry-run", *extra],
            capture_output=True, text=True, env=env,
        )
        return out.returncode, out.stdout.strip().splitlines()

    def assert_refusal(self, sid, token):
        code, lines = self.run_cli(sid)
        self.assertEqual(code, 1, lines)
        self.assertEqual(lines[0], token, lines)

    def test_unknown_session_id(self):
        self.assert_refusal("nope", "unknown-session-id")

    def test_ambiguous_pid(self):
        self.write(1, entry("dup"))
        self.write(2, entry("dup"))
        self.assert_refusal("dup", "ambiguous-pid")

    def test_non_integer_pid(self):
        self.write(1, entry("nopid", pid="not-an-int"))
        self.assert_refusal("nopid", "ambiguous-pid")

    def test_busy_target(self):
        self.write(1, entry("busy", status="busy"))
        self.assert_refusal("busy", "busy-target")

    def test_allow_busy_lapse_lifts_only_the_busy_refusal(self):
        """Operator ruling 2026-11-12: a lapsed manager is usually still BUSY.

        Measured 2026-11-12: 30 of 30 direct attempts refused `busy-target` because the
        manager runs fleet rounds continuously. The flag must lift that one refusal and
        let the restart reach its normal dry-run report.
        """
        self.write(1, entry("busylapse", name="Fleet Manager", status="busy"))
        code, lines = self.run_cli("busylapse", "--reason", "lapse",
                                   "--fresh-prompt", "/supervisor:fleet-loop",
                                   "--allow-busy-lapse")
        joined = "\n".join(lines)
        self.assertEqual(code, 0, joined)
        # The refusal token is never the FIRST line — the lift note mentions it by name, so a
        # plain `assertNotIn` would fail on the very message that proves the lift happened.
        self.assertNotEqual("busy-target", lines[0], joined)
        self.assertIn("busy-target lifted by --allow-busy-lapse", joined)
        self.assertIn("would kill the pid above and start a fresh session", joined)

    def test_allow_busy_lapse_is_rejected_without_the_lapse_fresh_path(self):
        """The flag's safety is its narrowness: anywhere else it would be a bare mid-turn kill."""
        self.write(1, entry("busyx", name="Fleet Manager", status="busy"))
        for extra in (["--allow-busy-lapse"],
                      ["--reason", "code", "--fresh-prompt", "/supervisor:fleet-loop",
                       "--allow-busy-lapse"],
                      ["--reason", "lapse", "--allow-busy-lapse"]):
            env = dict(os.environ)
            env["SUPERVISOR_LOAD_PATH"] = self.load
            env["SUPERVISOR_LAUNCHER"] = os.path.join(self.dir, "loadpath")
            out = subprocess.run(
                [sys.executable, SCRIPT, "busyx", "--sessions-dir", self.reg, "--dry-run", *extra],
                capture_output=True, text=True, env=env,
            )
            self.assertEqual(out.returncode, 2, f"{extra}: {out.stdout}{out.stderr}")
            self.assertIn("--allow-busy-lapse requires", out.stderr + out.stdout, extra)

    def test_headless_target(self):
        self.write(1, entry("hl", kind="headless"))
        self.assert_refusal("hl", "headless-target")

    def test_manager_is_restarted_not_refused(self):
        """Regression 2026-10-18: the `manager-target` refusal is gone. A manager is
        restarted like a worker — the role selects the colour and the re-arm note, and
        only `role-undetermined` still stops on a role."""
        self.write(1, entry("mgr", name="Fleet Manager"))
        code, lines = self.run_cli("mgr")
        self.assertEqual(code, 0, lines)
        self.assertIn("role: manager", lines[1], lines)

    def test_role_undetermined(self):
        self.write(1, entry("anon", name=""))
        self.assert_refusal("anon", "role-undetermined")

    def test_stale_load_path_when_session_is_newer(self):
        """A session started after the load path would reload identical code."""
        self.write(1, entry("fresh", name="Some Worker", startedAt="2099-01-01T00:00:00Z"))
        self.assert_refusal("fresh", "stale-load-path")

    def run_with(self, sid, argv=None, launcher=None):
        """Drive the CLI with a fixture worker argv and launcher."""
        env = dict(os.environ)
        env["SUPERVISOR_LOAD_PATH"] = self.load
        if argv is not None:
            env["SUPERVISOR_WORKER_ARGV"] = argv
        # A stale launcher by default, so the fixture — never the real vault config or
        # a real launcher's mtime — decides the case.
        env["SUPERVISOR_LAUNCHER"] = launcher or self.stale(self.touch("cc-default"))
        # Same seam as `run_cli`, and for the same reason: `main()` resolves wezterm before
        # the kill and refuses when it cannot, and a CI runner has none. `setdefault` so a
        # test that deliberately points the override at a missing path keeps its value.
        env.setdefault("SUPERVISOR_WEZTERM", sys.executable)
        out = subprocess.run(
            [sys.executable, SCRIPT, sid, "--sessions-dir", self.reg, "--dry-run"],
            capture_output=True, text=True, env=env,
        )
        return out.returncode, out.stdout.strip().splitlines()

    def touch(self, name):
        path = os.path.join(self.dir, name)
        with open(path, "w") as fh:
            fh.write("x")
        return path

    def stale(self, path):
        os.utime(path, (946684800, 946684800))  # 2000-01-01, before every fixture start
        return path

    def fresh_session(self, sid):
        """A session started after the load path but before files touched now."""
        os.utime(os.path.join(self.load, "9.9.9"), (946684800, 946684800))
        self.write(1, entry(sid, name="Some Worker", startedAt="2020-01-01T00:00:00Z"))

    def test_accepts_when_only_mcp_config_is_newer(self):
        self.fresh_session("mcp")
        cfg = self.touch("mcp.json")
        code, lines = self.run_with("mcp", argv=f"claude --mcp-config={cfg} --strict-mcp-config")
        self.assertEqual(code, 0, lines)
        self.assertIn("MCP config", "\n".join(lines))

    def test_accepts_spaced_mcp_config_form(self):
        self.fresh_session("mcp2")
        cfg = self.touch("mcp.json")
        code, lines = self.run_with("mcp2", argv=f"claude --mcp-config {cfg}")
        self.assertEqual(code, 0, lines)

    def test_accepts_when_only_launcher_is_newer(self):
        self.fresh_session("lau")
        script = self.touch("cc-launcher")
        code, lines = self.run_with("lau", argv="claude", launcher=script)
        self.assertEqual(code, 0, lines)
        self.assertIn("launcher", "\n".join(lines))

    def test_refuses_when_all_three_are_older(self):
        self.fresh_session("old")
        cfg = self.stale(self.touch("mcp.json"))
        script = self.stale(self.touch("cc-launcher"))
        code, lines = self.run_with("old", argv=f"claude --mcp-config={cfg}", launcher=script)
        self.assertEqual(code, 1, lines)
        self.assertEqual(lines[0], "stale-load-path", lines)
        self.assertIn(cfg, "\n".join(lines))

    def test_no_mcp_config_falls_back_to_load_path_check(self):
        """No --mcp-config and a stale launcher: only the (stale) load path decides."""
        self.fresh_session("noargv")
        code, lines = self.run_with("noargv", argv="")
        self.assertEqual(code, 1, lines)
        self.assertEqual(lines[0], "stale-load-path", lines)

    def test_unresolved_launcher_refuses_before_the_kill(self):
        """No override, cwd under no vault: exit 1 with an error line, nothing signalled."""
        self.write(1, entry("nolauncher", name="Some Worker"))
        cfg = os.path.join(self.dir, "vaults.json")
        with open(cfg, "w") as fh:
            json.dump([{"name": "v", "path": os.path.join(self.dir, "elsewhere"),
                        "claude_script": "/x/cc"}], fh)
        env = dict(os.environ)
        env.pop("SUPERVISOR_LAUNCHER", None)
        env["SUPERVISOR_LOAD_PATH"] = self.load
        env["SUPERVISOR_VAULT_CONFIG"] = cfg
        out = subprocess.run(
            [sys.executable, SCRIPT, "nolauncher", "--sessions-dir", self.reg],
            capture_output=True, text=True, env=env,
        )
        self.assertEqual(out.returncode, 1, out.stdout)
        self.assertIn("no launcher could be resolved", out.stdout)
        self.assertNotIn("killed pid", out.stdout)

    def test_missing_wezterm_refuses_before_the_kill(self):
        """Regression 2026-11-11: the fresh restart killed the manager, then could not spawn.

        `find_wezterm()` honours SUPERVISOR_WEZTERM, so pointing it at a missing path proves
        the refusal fires on the same code path the keeper takes — and that it fires BEFORE
        anything is signalled, which is what makes the failure non-destructive.
        """
        self.write(1, entry("nowez", name="Fleet Manager"))
        env = dict(os.environ)
        env["SUPERVISOR_LOAD_PATH"] = self.load
        env["SUPERVISOR_LAUNCHER"] = os.path.join(self.dir, "loadpath")
        env["SUPERVISOR_WEZTERM"] = os.path.join(self.dir, "no-such-wezterm")
        out = subprocess.run(
            [sys.executable, SCRIPT, "nowez", "--sessions-dir", self.reg,
             "--reason", "lapse", "--fresh-prompt", "/supervisor:fleet-loop"],
            capture_output=True, text=True, env=env,
        )
        self.assertEqual(out.returncode, 1, out.stdout)
        self.assertIn("no wezterm found", out.stdout)
        self.assertNotIn("killing-pid", out.stdout)

    def _run_with_vault_cli(self, sid, vault_cli):
        """Run the CLI with NO config override, so `vault-cli` itself must be resolved."""
        env = dict(os.environ)
        for key in ("SUPERVISOR_LAUNCHER", "SUPERVISOR_VAULT_CONFIG"):
            env.pop(key, None)
        env["SUPERVISOR_LOAD_PATH"] = self.load
        env["SUPERVISOR_VAULT_CLI"] = vault_cli
        env["PATH"] = "/usr/bin:/bin:/usr/sbin:/sbin"
        return subprocess.run(
            [sys.executable, SCRIPT, sid, "--sessions-dir", self.reg],
            capture_output=True, text=True, env=env,
        )

    def test_missing_vault_cli_is_an_environment_fault_not_a_refusal(self):
        """Launcher absent: exit 3 under its own token — never the refusal line."""
        self.write(1, entry("envfault", name="Some Worker"))
        out = self._run_with_vault_cli("envfault", os.path.join(self.dir, "no-vault-cli"))
        lines = out.stdout.splitlines()
        self.assertEqual(out.returncode, 3, out.stdout)
        self.assertEqual(lines[0], "environment-fault", out.stdout)
        self.assertIn("vault-cli not found", out.stdout)
        self.assertNotIn("no launcher could be resolved", out.stdout)
        self.assertNotIn("killed pid", out.stdout)

    def test_present_vault_cli_with_ineligible_target_is_a_refusal(self):
        """Launcher present, cwd under no vault: the genuine refusal, exit 1."""
        self.write(1, entry("ineligible", name="Some Worker"))
        fake = os.path.join(self.dir, "vault-cli")
        payload = json.dumps([{"name": "v", "path": os.path.join(self.dir, "elsewhere"),
                               "claude_script": "/x/cc"}])
        with open(fake, "w") as fh:
            fh.write(f"#!/bin/sh\ncat <<'EOF'\n{payload}\nEOF\n")
        os.chmod(fake, 0o755)
        out = self._run_with_vault_cli("ineligible", fake)
        self.assertEqual(out.returncode, 1, out.stdout)
        self.assertIn("no launcher could be resolved", out.stdout)
        self.assertNotIn("environment-fault", out.stdout)

    def test_vault_cli_off_path_is_found_via_gopath(self):
        """No override, `vault-cli` absent from PATH, present in $GOPATH/bin: resolved, so
        the outcome is the ordinary refusal (exit 1), never the environment fault."""
        self.write(1, entry("gopath", name="Some Worker"))
        gobin = os.path.join(self.dir, "gopath", "bin")
        os.makedirs(gobin)
        fake = os.path.join(gobin, "vault-cli")
        payload = json.dumps([{"name": "v", "path": os.path.join(self.dir, "elsewhere"),
                               "claude_script": "/x/cc"}])
        with open(fake, "w") as fh:
            # The marker proves THIS binary ran — a real vault-cli at a fixed location would
            # produce the same refusal and hide a broken $GOPATH branch.
            fh.write(f"#!/bin/sh\ntouch {self.dir}/gopath-ran\ncat <<'EOF'\n{payload}\nEOF\n")
        os.chmod(fake, 0o755)
        env = dict(os.environ)
        for key in ("SUPERVISOR_LAUNCHER", "SUPERVISOR_VAULT_CONFIG", "SUPERVISOR_VAULT_CLI"):
            env.pop(key, None)
        env["SUPERVISOR_LOAD_PATH"] = self.load
        env["PATH"] = "/usr/bin:/bin:/usr/sbin:/sbin"
        env["GOPATH"] = os.path.join(self.dir, "gopath")
        out = subprocess.run(
            [sys.executable, SCRIPT, "gopath", "--sessions-dir", self.reg],
            capture_output=True, text=True, env=env,
        )
        self.assertEqual(out.returncode, 1, out.stdout)
        self.assertNotIn("environment-fault", out.stdout)
        self.assertTrue(os.path.exists(os.path.join(self.dir, "gopath-ran")), out.stdout)

    def test_happy_path_dry_run_signals_nothing(self):
        """Cleared target: the dry run reports what it WOULD do and exits 0."""
        self.write(1, entry("ok", name="Some Worker"))
        code, lines = self.run_cli("ok")
        self.assertEqual(code, 0, lines)
        self.assertIn("would kill", "\n".join(lines))

    def test_the_restart_header_names_the_full_session_id(self):
        """The round joins a close line and a refusal line on the full id, so the resume
        header must carry it too — a truncated one leaves both unjoinable."""
        sid = "aaaa1111-2222-4333-8444-555566667777"
        self.write(1, entry(sid, name="Some Worker"))
        code, lines = self.run_cli(sid)
        self.assertEqual(code, 0, lines)
        self.assertIn(f"session {sid}", "\n".join(lines))

    def test_a_terminal_task_is_refused_on_the_normal_path(self):
        """Entry point A: the normal restart refuses before the kill when the anchored task
        is finished. The probe is the CLI, so it asserts the token a caller branches on."""
        sid = "bbbb2222-3333-4444-8555-666677778888"
        self.write(1, entry(sid, name="Some Worker", cwd=self.dir))
        tasks = os.path.join(self.dir, "25 Tasks")
        os.makedirs(tasks, exist_ok=True)
        with open(os.path.join(tasks, "A Task.md"), "w") as fh:
            fh.write("---\nstatus: completed\nclaude_session_id: " + sid + "\n---\n\n# Summary\n")
        cfg = os.path.join(self.dir, "vaults.json")
        with open(cfg, "w") as fh:
            json.dump([{"name": "v", "path": self.dir, "tasks_dir": "25 Tasks"}], fh)
        prev = os.environ.get("SUPERVISOR_VAULT_CONFIG")
        os.environ["SUPERVISOR_VAULT_CONFIG"] = cfg

        def restore():
            if prev is None:
                os.environ.pop("SUPERVISOR_VAULT_CONFIG", None)
            else:
                os.environ["SUPERVISOR_VAULT_CONFIG"] = prev

        self.addCleanup(restore)
        code, lines = self.run_cli(sid)
        self.assertEqual(code, 1, lines)
        self.assertEqual(lines[0], "task-terminal", lines)

    def test_help_exits_zero(self):
        out = subprocess.run(
            [sys.executable, SCRIPT, "--help"], capture_output=True, text=True
        )
        self.assertEqual(out.returncode, 0)


SID_HELD = "aaaaaaaa-1111-2222-3333-444444444444"
# Differs from SID_HELD in the EIGHTH character only -- the same near-miss the store's
# own test uses (`test_session_holds.py` NEAR_MISS). A prefix match, a substring match
# or a flag-everything build all trip on it; nothing weaker does.
SID_NEAR_MISS = "aaaaaaab-1111-2222-3333-444444444444"


class AbsentMode(unittest.TestCase):
    """`--absent`: resume a session whose process is GONE — no registry entry, nothing to kill.

    The normal path refuses `unknown-session-id` when no registry entry carries the id, and a
    dead session's `~/.claude/sessions/<pid>.json` is deleted on death, so it could never
    bring back a Fleet Manager that died (measured 2026-11-06: three deaths in one day). The
    absent mode inverts the precondition — the id must be ABSENT — sources `cwd` and launcher
    from the manager registration that outlives death, and never signals anything.

    Both directions are asserted: a too-loose build (resumes a live session) and a too-tight
    one (refuses a genuinely dead one) fail in opposite directions.
    """

    SID = "eeee0000-0000-0000-0000-000000000005"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name
        self.reg = os.path.join(self.dir, "sessions")
        self.mgr = os.path.join(self.dir, "worker-manager")
        self.vault = os.path.join(self.dir, "vault")
        self.bin = os.path.join(self.dir, "bin")
        for d in (self.reg, self.mgr, self.vault, self.bin):
            os.makedirs(d)
        # Three DISTINCT launchers, so the test can tell the manager-default rule from the
        # `claude_script` fallback as well as from the worker launcher.
        self.launcher = os.path.join(self.dir, "cc-manager")
        self.vault_script = os.path.join(self.dir, "cc-vault")
        for path in (self.launcher, self.vault_script):
            with open(path, "w") as fh:
                fh.write("#!/bin/sh\n")
            os.chmod(path, 0o755)
        self.cfg = os.path.join(self.dir, "vaults.json")
        with open(self.cfg, "w") as fh:
            json.dump([{"name": "v1", "path": self.vault, "claude_script": self.vault_script,
                        "manager_launcher": self.launcher, "worker_launcher": "/no/worker",
                        "tasks_dir": "25 Tasks"}], fh)
        self.exits = os.path.join(self.dir, "session-exit")
        os.makedirs(self.exits)
        self.holds = os.path.join(self.dir, "holds.json")
        with open(self.holds, "w") as fh:
            fh.write('{"version": 1, "holds": {}}')
        # A fake `wezterm` that records its argv and prints a pane id, so a non-dry run is
        # observable without opening a tab.
        self.spawned = os.path.join(self.dir, "spawned.txt")
        fake = os.path.join(self.bin, "wezterm")
        with open(fake, "w") as fh:
            fh.write(f'#!/bin/sh\nprintf "%s\\n" "$@" > "{self.spawned}"\necho 999\n')
        os.chmod(fake, 0o755)

    def tearDown(self):
        self.tmp.cleanup()

    def register(self, **over):
        rec = {"subject": "Fleet Manager", "branch": "fleet", "vault": "v1",
               "resolved_at": "2026-11-06T09:00:00+00:00"}
        rec.update(over)
        with open(os.path.join(self.mgr, f"{self.SID}.json"), "w") as fh:
            json.dump(rec, fh)

    def run_absent(self, *extra, dry=True, path=None, wezterm=None):
        env = dict(os.environ)
        env.pop("SUPERVISOR_LAUNCHER", None)
        env.pop("SUPERVISOR_WEZTERM", None)
        env["SUPERVISOR_VAULT_CONFIG"] = self.cfg
        env["SUPERVISOR_WORKER_MANAGER_DIR"] = self.mgr
        env["SUPERVISOR_SESSION_HOLDS"] = self.holds
        # Never the real store: the in-flight scan reads it and the resume lock writes to it.
        env["SUPERVISOR_SESSION_EXIT_DIR"] = self.exits
        env["PATH"] = path if path is not None else self.bin + os.pathsep + env.get("PATH", "")
        if wezterm is not None:
            env["SUPERVISOR_WEZTERM"] = wezterm
        argv = [sys.executable, SCRIPT, self.SID, "--absent", "--sessions-dir", self.reg]
        if dry:
            argv.append("--dry-run")
        out = subprocess.run(argv + list(extra), capture_output=True, text=True, env=env)
        return out.returncode, out.stdout.strip().splitlines()

    def write_task(self, status, defer=None, sid=None):
        tasks = os.path.join(self.vault, "25 Tasks")
        os.makedirs(tasks, exist_ok=True)
        lines = [f"status: {status}", f"claude_session_id: {sid or self.SID}"]
        if defer:
            lines.append(f'defer_date: "{defer}"')
        with open(os.path.join(tasks, "A Task.md"), "w") as fh:
            fh.write("---\n" + "\n".join(lines) + "\n---\n\n# Summary\n")

    def test_a_finished_task_is_refused_before_the_spawn(self):
        """The `--absent` resume is the only resume this script performs for a headless
        worker, and the incident that motivated the guard was exactly this shape: a reaped
        worker whose task read `completed` came back as a live process. Nothing may be
        spawned, and the token must be the first line a caller reads."""
        self.register()
        self.write_task("completed")
        code, lines = self.run_absent(dry=False)
        self.assertEqual(code, 1, lines)
        self.assertEqual(lines[0], "task-terminal", lines)
        self.assertFalse(os.path.exists(self.spawned), "a refused resume must not spawn")

    def test_an_in_progress_task_still_resumes(self):
        """The negative control: a live task must not be caught by the guard."""
        self.register()
        self.write_task("in_progress")
        code, lines = self.run_absent(dry=False)
        self.assertEqual(code, 0, lines)
        self.assertTrue(os.path.exists(self.spawned), "a permitted resume must spawn")

    def test_the_absent_header_names_the_full_session_id(self):
        self.register()
        self.write_task("in_progress")
        code, lines = self.run_absent(dry=True)
        self.assertEqual(code, 0, lines)
        self.assertIn(f"session {self.SID}", "\n".join(lines))

    def test_a_dead_registered_manager_resumes_into_its_vault_on_its_manager_launcher(self):
        self.register()
        code, lines = self.run_absent()
        self.assertEqual(code, 0, lines)
        text = "\n".join(lines)
        self.assertIn(f"launcher: {self.launcher}", text)
        self.assertIn(f"would resume into {self.vault}", text)
        self.assertNotIn("/no/worker", text, "the fleet manager took the worker launcher")

    def test_a_live_session_is_refused_not_absent(self):
        """The too-loose direction: a registry entry means the process may be alive."""
        self.register()
        with open(os.path.join(self.reg, "4242.json"), "w") as fh:
            json.dump(entry(self.SID), fh)
        code, lines = self.run_absent()
        self.assertEqual(code, 1, lines)
        self.assertEqual(lines[0], "not-absent", lines)

    def test_an_unreadable_registry_cannot_prove_absence(self):
        self.register()
        os.rmdir(self.reg)
        code, lines = self.run_absent()
        self.assertEqual(code, 1, lines)
        self.assertEqual(lines[0], "not-absent", lines)

    def test_no_registration_means_no_resume(self):
        """Without the registration there is no vault, so no cwd and no launcher."""
        code, lines = self.run_absent()
        self.assertEqual(code, 1, lines)
        self.assertEqual(lines[0], "unknown-session-id", lines)

    def test_a_held_session_is_never_resumed(self):
        self.register()
        with open(self.holds, "w") as fh:
            fh.write('{"version": 1, "holds": {"%s": {"reason": "operator"}}}' % self.SID)
        code, lines = self.run_absent()
        self.assertEqual(code, 1, lines)
        self.assertEqual(lines[0], "held-session", lines)

    def test_an_unknown_vault_is_an_error_before_any_spawn(self):
        self.register(vault="nope")
        code, lines = self.run_absent(dry=False)
        self.assertEqual(code, 1, lines)
        self.assertFalse(os.path.exists(self.spawned), "spawned with no resolvable cwd")

    def test_a_real_resume_spawns_once_and_signals_nothing(self):
        self.register()
        code, lines = self.run_absent(dry=False)
        self.assertEqual(code, 0, lines)
        with open(self.spawned) as fh:
            argv = fh.read()
        self.assertIn("spawn", argv)
        self.assertIn(self.vault, argv)
        self.assertIn(f"--resume {self.SID}", argv)
        self.assertNotIn("killing-pid", "\n".join(lines))
        self.assertIn("re-arm", "\n".join(lines).lower())

    def test_it_resumes_under_the_launchd_path_where_wezterm_is_not_on_path(self):
        """M1, the bug the suite missed: the keeper's plist PATH is system dirs only.

        A bare `wezterm` is FileNotFoundError there, so every answered resume would have
        failed. `wezterm` must be resolved by absolute path, not looked up on PATH.
        """
        self.register()
        code, lines = self.run_absent(dry=False, path="/usr/bin:/bin:/usr/sbin:/sbin",
                                      wezterm=os.path.join(self.bin, "wezterm"))
        self.assertEqual(code, 0, lines)
        self.assertTrue(os.path.exists(self.spawned), "nothing was spawned under the launchd PATH")

    def test_no_wezterm_anywhere_is_an_error_before_any_spawn(self):
        self.register()
        code, lines = self.run_absent(dry=False, wezterm="/no/such/wezterm")
        self.assertEqual(code, 1, lines)
        self.assertIn("no wezterm", "\n".join(lines))
        self.assertFalse(os.path.exists(self.spawned))

    def test_a_half_written_registry_entry_is_never_proof_of_absence(self):
        self.register()
        with open(os.path.join(self.reg, "777.json"), "w") as fh:
            fh.write('{"sessionId": "' + self.SID[:10])  # truncated mid-write
        code, lines = self.run_absent()
        self.assertEqual(code, 1, lines)
        self.assertEqual(lines[0], "not-absent", lines)

    def test_a_registry_file_that_is_valid_json_but_not_an_object_refuses_not_crashes(self):
        self.register()
        with open(os.path.join(self.reg, "778.json"), "w") as fh:
            fh.write('["not", "an", "object"]')
        code, lines = self.run_absent()
        self.assertEqual(code, 1, lines)
        self.assertEqual(lines[0], "not-absent", lines)

    def _pane_line(self, kind, age):
        stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - age))
        with open(os.path.join(self.exits, "pane-51.log"), "a") as fh:
            fh.write(f"{stamp}\tkind={kind}\tpane=51\tsession={self.SID}\tpid=9\trc=-\t"
                     f"signal=-\tsender=-\twriter=observer\n")

    def test_a_pane_already_running_the_session_is_refused(self):
        """A resume through another path heartbeats before it registers — two copies otherwise."""
        self.register()
        self._pane_line("heartbeat", age=20)
        code, lines = self.run_absent()
        self.assertEqual(code, 1, lines)
        self.assertEqual(lines[0], "not-absent", lines)

    def test_an_old_pane_record_does_not_block_a_resume(self):
        self.register()
        self._pane_line("heartbeat", age=3600)
        code, lines = self.run_absent()
        self.assertEqual(code, 0, lines)

    def test_a_pane_that_ended_does_not_block_a_resume(self):
        self.register()
        self._pane_line("heartbeat", age=40)
        self._pane_line("signalled kill", age=30)
        code, lines = self.run_absent()
        self.assertEqual(code, 0, lines)

    def test_a_second_resume_inside_the_window_is_refused(self):
        self.register()
        first, lines = self.run_absent(dry=False)
        self.assertEqual(first, 0, lines)
        os.remove(self.spawned)
        second, lines = self.run_absent(dry=False)
        self.assertEqual(second, 1, lines)
        self.assertEqual(lines[0], "not-absent", lines)
        self.assertFalse(os.path.exists(self.spawned), "a second copy was spawned")


class HeldSession(Cli):
    """A held session is refused with `held-session` -- an EIGHTH refusal.

    Both directions are asserted by name. The too-tight case (a held session is refused)
    and the too-loose case (the SAME session, unheld, is not refused for being held) fail
    in opposite directions, and only one of them looks like a bug: a build that refuses
    everything satisfies the first clause alone.

    The refusal outranks the status and kind gates -- `busy` says the moment is wrong, a
    hold says the session is not yours to touch at all.
    """

    def setUp(self):
        super().setUp()
        self.holds = os.path.join(self.dir, "holds.json")
        os.environ["SUPERVISOR_SESSION_HOLDS"] = self.holds
        self.write_holds()

    def tearDown(self):
        os.environ.pop("SUPERVISOR_SESSION_HOLDS", None)
        super().tearDown()

    def write_holds(self, *session_ids):
        entries = ", ".join(
            '"%s": {"reason": "operator: leave it"}' % sid for sid in session_ids
        )
        with open(self.holds, "w", encoding="utf-8") as handle:
            handle.write('{"version": 1, "holds": {%s}}' % entries)

    def seed(self):
        self.write(
            4242,
            {"sessionId": SID_HELD, "pid": 4242, "status": "idle", "kind": "interactive"},
        )

    def test_fires_on_a_held_session(self):
        """Too-tight: a held session is refused, with the stable token."""
        self.seed()
        self.write_holds(SID_HELD)
        self.assert_refusal(SID_HELD, "held-session")

    def test_does_not_fire_on_a_clean_session(self):
        """Too-loose: an UNHELD session is NOT refused for being held.

        Written against a POPULATED store carrying a near-miss id -- `aaaaaaab-`
        beside the held `aaaaaaaa-` -- so a prefix match, a substring match or a
        flag-everything build all fail here.

        ⚠️ An EMPTY store would let all three pass, which is why `write_holds()`
        with no arguments is not this case. `[[A Suppression Guard Needs
        Both-Direction Tests]]` names empty input explicitly: *"a guard tested with
        an empty stream and a guard tested with a realistic stream containing
        near-misses are different tests; only the second one exercises the
        boundary."* This test seeded an empty store until 2026-09-30, so the
        inlined reader it guards -- a copy that does NOT inherit the store's own
        near-miss coverage -- was asserted only against the weakest input.
        """
        self.seed()
        self.write_holds(SID_NEAR_MISS)
        _code, lines = self.run_cli(SID_HELD)
        self.assertNotEqual(lines[0] if lines else "", "held-session", lines)

    def test_a_corrupt_store_reads_as_nothing_held(self):
        """Fail-open: an unreadable store must not refuse a restart."""
        self.seed()
        with open(self.holds, "w", encoding="utf-8") as handle:
            handle.write("{not json")
        _code, lines = self.run_cli(SID_HELD)
        self.assertNotEqual(lines[0] if lines else "", "held-session", lines)


SID_WINDOW = "cccccccc-1111-2222-3333-444444444444"


class HoldWindow(Cli):
    """The Hold WINDOW -- a pane parked in the role map's Hold window is refused.

    The second source of heldness, and the one where a failed read must REFUSE: a
    restart KILLS a process, so a broken transport read as "not held" is the
    direction that restarts a session the operator parked. `docs/pane-reads.md`
    prescribes *refuse non-zero, naming the transport* for a caller that already
    exits non-zero -- which this one does on every refusal.

    Both directions are asserted by name. The whole path is real -- a fixture role
    map, a fixture registry and a fake `wezterm` on PATH -- so the test never
    depends on the host having WezTerm.
    """

    NAME = "⚙ held worker"
    PANE = 874
    HOLD_WINDOW = 11
    AGENT_WINDOW = 9

    def setUp(self):
        super().setUp()
        self.role_map = os.path.join(self.dir, "role-map.json")
        with open(self.role_map, "w", encoding="utf-8") as handle:
            json.dump({"Hold": {"window_id": self.HOLD_WINDOW}}, handle)
        os.environ["SUPERVISOR_ROLE_MAP"] = self.role_map
        # registry_names() reads SESSIONS_DIR, so point it at the fixture registry and
        # seed the session's name -- the join the pane title carries.
        os.environ["SUPERVISOR_SESSIONS_DIR"] = self.reg
        self.bin = os.path.join(self.dir, "bin")
        os.makedirs(self.bin)
        self.write_wezterm(self.HOLD_WINDOW)
        self.write(4242, entry(SID_WINDOW, name=self.NAME))

    def tearDown(self):
        os.environ.pop("SUPERVISOR_SESSIONS_DIR", None)
        super().tearDown()

    def write_wezterm(self, window, fail=False):
        path = os.path.join(self.bin, "wezterm")
        with open(path, "w", encoding="utf-8") as handle:
            if fail:
                handle.write("#!/bin/sh\nexit 1\n")
            else:
                listing = json.dumps(
                    [{"pane_id": self.PANE, "window_id": window,
                      "title": "✳ " + self.NAME}]
                )
                handle.write("#!/bin/sh\ncat <<'JSON'\n" + listing + "\nJSON\n")
        os.chmod(path, 0o755)

    def run_hold(self):
        env = dict(os.environ)
        env["PATH"] = self.bin + os.pathsep + env.get("PATH", "")
        env["SUPERVISOR_LOAD_PATH"] = self.load
        env["SUPERVISOR_LAUNCHER"] = os.path.join(self.dir, "loadpath")
        out = subprocess.run(
            [sys.executable, SCRIPT, SID_WINDOW, "--sessions-dir", self.reg, "--dry-run"],
            capture_output=True, text=True, env=env,
        )
        return out.returncode, out.stdout.strip().splitlines()

    def test_a_session_in_the_hold_window_is_refused(self):
        """Too-tight: a Hold-window pane refuses the restart with no store entry."""
        code, lines = self.run_hold()
        self.assertEqual(code, 1, lines)
        self.assertEqual(lines[0], "held-session", lines)

    def test_a_session_outside_the_hold_window_is_not_refused_for_a_hold(self):
        """Too-loose: the SAME session, pane in another window, is not held."""
        self.write_wezterm(self.AGENT_WINDOW)
        code, lines = self.run_hold()
        self.assertNotEqual(lines[0] if lines else "", "held-session", lines)

    def test_an_unreadable_pane_list_refuses_naming_the_transport(self):
        """The chosen response for THIS caller: refuse, never read as not held."""
        self.write_wezterm(self.HOLD_WINDOW, fail=True)
        code, lines = self.run_hold()
        self.assertEqual(code, 1, lines)
        self.assertEqual(lines[0], "hold-unreadable", lines)


class TaskTerminalRefusal(unittest.TestCase):
    """A resume must not start a process for a task that is already finished.

    Measured 2026-11-11: session 2e540d65 was reaped at 07:18:34Z while its task read
    `completed`, and the same id was live again at 07:33:55Z — a finished worker was handed
    a spawn slot and the close was undone. Every case below is a fixture task in one of the
    states the guard must tell apart, and the negative control (`in_progress`) is what keeps
    the guard from being a blanket refusal that would stop every resume.
    """

    SID = "2e540d65-7916-4609-98aa-3fd8f1923211"

    def _vault(self, frontmatter_lines):
        """A one-task vault plus the config override that points the lookup at it."""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        tasks = os.path.join(tmp.name, "25 Tasks")
        os.makedirs(tasks)
        task_path = os.path.join(tasks, "A Task.md")
        self._write_task(task_path, frontmatter_lines)
        config = os.path.join(tmp.name, "vaults.json")
        with open(config, "w", encoding="utf-8") as fh:
            json.dump([{"name": "v", "path": tmp.name, "tasks_dir": "25 Tasks"}], fh)
        previous = os.environ.get("SUPERVISOR_VAULT_CONFIG")
        os.environ["SUPERVISOR_VAULT_CONFIG"] = config

        def restore():
            if previous is None:
                os.environ.pop("SUPERVISOR_VAULT_CONFIG", None)
            else:
                os.environ["SUPERVISOR_VAULT_CONFIG"] = previous

        self.addCleanup(restore)
        return tmp.name, task_path

    @staticmethod
    def _write_task(path, frontmatter_lines):
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("---\n" + "\n".join(frontmatter_lines) + "\n---\n\n# Summary\n")

    def _refusal(self, status, defer=None, sid=None):
        lines = [f"status: {status}", f"claude_session_id: {sid or self.SID}"]
        if defer:
            lines.append(f'defer_date: "{defer}"')
        cwd, _ = self._vault(lines)
        return rw.task_terminal_refusal(self.SID, cwd)

    def test_completed_is_refused(self):
        token, detail = self._refusal("completed")
        self.assertEqual(token, "task-terminal")
        self.assertIn("completed", detail)
        # The close record names the full id, so the refusal must too — the round joins the
        # two lines on it. A refusal naming only the task basename cannot be joined.
        self.assertIn(self.SID, detail)

    def test_aborted_is_refused(self):
        self.assertEqual(self._refusal("aborted")[0], "task-terminal")

    def test_hold_is_refused(self):
        self.assertEqual(self._refusal("hold")[0], "task-terminal")

    def test_a_future_defer_is_refused(self):
        token, detail = self._refusal("in_progress", defer="2099-01-01")
        self.assertEqual(token, "task-terminal")
        self.assertIn("2099-01-01", detail)

    def test_in_progress_still_resumes(self):
        """The negative control: the guard must not refuse every resume."""
        self.assertIsNone(self._refusal("in_progress")[0])

    def test_a_past_defer_still_resumes(self):
        self.assertIsNone(self._refusal("in_progress", defer="2020-01-01")[0])

    def test_no_anchor_is_not_a_refusal(self):
        """A session whose task belongs to someone else is not this guard's business."""
        cwd, _ = self._vault(["status: completed", "claude_session_id: some-other-session"])
        self.assertIsNone(rw.task_terminal_refusal(self.SID, cwd)[0])

    def test_the_status_is_read_at_call_time_not_cached(self):
        """The flip between enumeration and spawn must be seen.

        A census or digest read taken earlier in the round would return `None` here and
        start the process — which is the defect the guard exists to close.
        """
        cwd, task_path = self._vault(
            ["status: in_progress", f"claude_session_id: {self.SID}"]
        )
        self.assertIsNone(rw.task_terminal_refusal(self.SID, cwd)[0])
        self._write_task(task_path, ["status: completed", f"claude_session_id: {self.SID}"])
        self.assertEqual(rw.task_terminal_refusal(self.SID, cwd)[0], "task-terminal")


    def test_the_cli_task_guard_exits_one_on_a_finished_task(self):
        """`--task-guard` is the route a leg that spawns on its own uses, so the CLI's own
        exit code and first line are the contract — not the helper's return value."""
        cwd, _ = self._vault(["status: completed", f"claude_session_id: {self.SID}"])
        out = subprocess.run(
            [sys.executable, SCRIPT, self.SID, "--task-guard", "--cwd", cwd],
            capture_output=True, text=True,
        )
        self.assertEqual(out.returncode, 1, out.stdout + out.stderr)
        self.assertEqual(out.stdout.strip().splitlines()[0], "task-terminal")

    def test_the_cli_task_guard_exits_zero_on_a_live_task(self):
        cwd, _ = self._vault(["status: in_progress", f"claude_session_id: {self.SID}"])
        out = subprocess.run(
            [sys.executable, SCRIPT, self.SID, "--task-guard", "--cwd", cwd],
            capture_output=True, text=True,
        )
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)


if __name__ == "__main__":
    unittest.main()
