#!/usr/bin/env python3
"""Tests for scripts/restart-worker.py.

The load-bearing property is that every refusal fires *before* anything is signalled:
a manager, a busy target, a headless worker, an ambiguous pid and a load path that
would reload identical code must each stop the script with its own reason token and
exit 1, and no code path may reach `os.kill` on a target it has not cleared. The
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
        self.assertIn('exec "/l/cc-private-claude" --resume sid', inner)

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
    """The resume runs the worker's OWN launcher: task > goal > vault `claude_script`."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = self.tmp.name
        self.vault = os.path.join(d, "vault")
        self.scripts = os.path.join(d, "scripts")
        for sub in ("25 Tasks", "24 Goals"):
            os.makedirs(os.path.join(self.vault, sub))
        os.makedirs(self.scripts)
        for name in ("cc-private", "cc-private-claude", "cc-goal"):
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

    def test_task_launcher_wins(self):
        self.page("25 Tasks", "T", "claude_session_id: s1\nlauncher: cc-private-claude")
        path, source = rw.resolve_launcher("s1", self.vault, self.fb)
        self.assertEqual((path, source), (os.path.join(self.scripts, "cc-private-claude"), "task"))

    def test_goal_launcher_when_task_names_none(self):
        self.page("24 Goals", "G", "launcher: cc-goal")
        self.page("25 Tasks", "T", "claude_session_id: s1\ngoals:\n    - '[[G]]'")
        self.assertEqual(rw.resolve_launcher("s1", self.vault, self.fb)[1], "goal")

    def test_vault_default_for_an_unbound_session(self):
        path, source = rw.resolve_launcher("nobody", os.path.join(self.vault, "25 Tasks"), self.fb)
        self.assertEqual((path, source), (os.path.join(self.scripts, "cc-private"), "vault"))

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

    def test_goals_naming_different_launchers_is_none(self):
        self.page("24 Goals", "G1", "launcher: cc-goal")
        self.page("24 Goals", "G2", "launcher: cc-private-claude")
        self.page("25 Tasks", "T", "claude_session_id: s1\ngoals:\n    - '[[G1]]'\n    - '[[G2]]'")
        self.assertIsNone(rw.resolve_launcher("s1", self.vault, self.fb)[0])

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

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, pid, rec):
        with open(os.path.join(self.reg, f"{pid}.json"), "w") as fh:
            json.dump(rec, fh)

    def run_cli(self, sid, *extra):
        env = dict(os.environ)
        env["SUPERVISOR_LOAD_PATH"] = self.load
        env["SUPERVISOR_LAUNCHER"] = os.path.join(self.dir, "loadpath")
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

    def test_headless_target(self):
        self.write(1, entry("hl", kind="headless"))
        self.assert_refusal("hl", "headless-target")

    def test_manager_target(self):
        self.write(1, entry("mgr", name="Fleet Manager"))
        self.assert_refusal("mgr", "manager-target")

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

    def test_happy_path_dry_run_signals_nothing(self):
        """Cleared target: the dry run reports what it WOULD do and exits 0."""
        self.write(1, entry("ok", name="Some Worker"))
        code, lines = self.run_cli("ok")
        self.assertEqual(code, 0, lines)
        self.assertIn("would kill", "\n".join(lines))

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


if __name__ == "__main__":
    unittest.main()
