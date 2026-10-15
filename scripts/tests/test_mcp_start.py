#!/usr/bin/env python3
"""Tests for scripts/mcp-start.py.

The launcher exists because the server refuses to boot when the `CLAUDE_CONFIG_DIR` it
inherits disagrees with its resolved Claude home, and the shipped `.mcp.json` never set the
opt-in that settles it — so every profile other than `~/.claude` was refused by
construction. Each case guards one way the launcher could restore the binding while
reintroducing the defect the refusal protects against:

  * the ambient config dir IS declared when nothing else is — the whole point, and the one
    case whose absence makes the plugin bind under `~/.claude` only.
  * an UNSET ambient value declares nothing. This is the case a naive
    `"env": {"SUPERVISOR_CLAUDE_CONFIG_DIR": "${CLAUDE_CONFIG_DIR}"}` gets wrong, and it
    gets it wrong silently: the client leaves the reference literal, so the server adopts
    the string `${CLAUDE_CONFIG_DIR}` as its home — the silent-wrong-home defect itself.
  * an explicit `SUPERVISOR_CLAUDE_CONFIG_DIR` is never overwritten. A launcher that pins
    it must keep winning, or this script becomes a second way to trip the guard.
  * "declared" is REPORTED, not inferred from the value — a caller that compared values
    could not tell "nothing to declare" from "declared the value already present".
  * bun is resolved through `$BUN_INSTALL`, then `~/.bun/bin`, then `PATH`, because
    `~/.bun/bin` reaches `PATH` only from `~/.zshrc` and an MCP server is spawned without a
    login shell.
  * a bun that cannot be found is reported on stderr with a non-zero exit, rather than
    leaving the client to report an unexplained closed connection.
  * each token resolves from ITS OWN TeamVault key. One key resolved into two variables is
    the failure this guards: it reads as configured and fails on one leg only.
  * an UNRESOLVED token degrades — the server still starts. Exiting here would cost the
    session every supervisor tool, not only the two spawn targets that consume a token, so a
    vault outage would take the supervisor away from every session at once.
  * a value never reaches stderr or stdout, on the success path or the failure one. Only the
    lookup KEY NAME is logged, and a lookup key is a short identifier rather than a secret.

Run: python3 -m unittest discover -s scripts/tests -v
"""

import contextlib
import importlib.util
import io
import os
import subprocess
import tempfile
import unittest
from unittest import mock

_HERE = os.path.dirname(os.path.abspath(__file__))
_SCRIPT = os.path.join(os.path.dirname(_HERE), "mcp-start.py")

_spec = importlib.util.spec_from_file_location("mcp_start", _SCRIPT)
ms = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ms)

# A config dir that is not this server's default home -- the clean-profile shape.
CLEAN = "/tmp/cc-clean-profile"


def _executable(path):
    """Create an empty executable file at `path`, parents included."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as handle:
        handle.write("#!/bin/sh\n")
    os.chmod(path, 0o755)
    return path


class DeclareClaudeHomeTest(unittest.TestCase):
    def test_ambient_config_dir_is_declared(self):
        env = {"CLAUDE_CONFIG_DIR": CLEAN}
        self.assertTrue(ms.declare_claude_home(env))
        self.assertEqual(env["SUPERVISOR_CLAUDE_CONFIG_DIR"], CLEAN)

    def test_unset_ambient_declares_nothing(self):
        env = {}
        self.assertFalse(ms.declare_claude_home(env))
        self.assertNotIn("SUPERVISOR_CLAUDE_CONFIG_DIR", env)

    def test_explicit_declaration_is_never_overwritten(self):
        env = {"CLAUDE_CONFIG_DIR": CLEAN, "SUPERVISOR_CLAUDE_CONFIG_DIR": "/elsewhere"}
        self.assertFalse(ms.declare_claude_home(env))
        self.assertEqual(env["SUPERVISOR_CLAUDE_CONFIG_DIR"], "/elsewhere")

    def test_declared_reports_a_decision_not_a_value(self):
        """Declaring the value that was already there is still a decision -- and the
        caller must be able to tell it from having had nothing to declare."""
        env = {"CLAUDE_CONFIG_DIR": CLEAN}
        self.assertTrue(ms.declare_claude_home(env))
        self.assertFalse(ms.declare_claude_home(env))


class FindBunTest(unittest.TestCase):
    def test_bun_install_wins(self):
        with tempfile.TemporaryDirectory() as tmp:
            _executable(os.path.join(tmp, "install", "bin", "bun"))
            _executable(os.path.join(tmp, "home", ".bun", "bin", "bun"))
            env = {"BUN_INSTALL": os.path.join(tmp, "install"), "HOME": os.path.join(tmp, "home")}
            self.assertEqual(ms.find_bun(env, which=lambda _: None),
                             os.path.join(tmp, "install", "bin", "bun"))

    def test_home_bun_bin_is_the_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            _executable(os.path.join(tmp, "home", ".bun", "bin", "bun"))
            env = {"HOME": os.path.join(tmp, "home")}
            self.assertEqual(ms.find_bun(env, which=lambda _: None),
                             os.path.join(tmp, "home", ".bun", "bin", "bun"))

    def test_path_is_the_last_resort(self):
        """The case the bare `bun` command never covered: no BUN_INSTALL, no ~/.bun, and
        the binary reachable only through PATH."""
        with tempfile.TemporaryDirectory() as tmp:
            env = {"HOME": os.path.join(tmp, "home")}
            self.assertEqual(ms.find_bun(env, which=lambda _: "/from/path/bun"), "/from/path/bun")

    def test_missing_bun_is_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = {"HOME": os.path.join(tmp, "home")}
            self.assertIsNone(ms.find_bun(env, which=lambda _: None))


class TeamvaultPasswordTest(unittest.TestCase):
    """One key resolved to one value, with nothing secret on any stream."""

    def _runner(self, *, stdout="", stderr="", code=0, oserror=False):
        calls = []

        def run(argv, capture_output=True, text=True):
            calls.append(argv)
            if oserror:
                raise FileNotFoundError(2, "No such file or directory", argv[0])
            return subprocess.CompletedProcess(argv, code, stdout, stderr)

        run.calls = calls
        return run

    def test_the_lookup_asks_for_the_configured_key_and_config(self):
        runner = self._runner(stdout="placeholder-x")
        with contextlib.redirect_stderr(io.StringIO()):
            value = ms.teamvault_password("VO0Gmw", {"TEAMVAULT_CONFIG": "/etc/tv.json"}, run=runner)
        self.assertEqual(value, "placeholder-x")
        self.assertIn("--teamvault-key=VO0Gmw", runner.calls[0])
        self.assertIn("--teamvault-config=/etc/tv.json", runner.calls[0])

    def test_the_default_config_is_teamvault_clis_own_location(self):
        runner = self._runner(stdout="placeholder-x")
        with contextlib.redirect_stderr(io.StringIO()):
            ms.teamvault_password("VO0Gmw", {"HOME": "/home/example"}, run=runner)
        self.assertIn(
            "--teamvault-config=/home/example/.config/teamvault-cli/config.json",
            runner.calls[0],
        )

    def test_a_non_zero_exit_degrades_and_names_the_key(self):
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            value = ms.teamvault_password("lO4j5q", {}, run=self._runner(code=1, stderr="no secret"))
        self.assertIsNone(value)
        self.assertIn("lO4j5q", stderr.getvalue())

    def test_an_empty_value_degrades(self):
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            value = ms.teamvault_password("VO0Gmw", {}, run=self._runner(stdout="  \n"))
        self.assertIsNone(value)
        self.assertIn("VO0Gmw", stderr.getvalue())

    def test_a_missing_binary_degrades_rather_than_raising(self):
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            value = ms.teamvault_password("VO0Gmw", {}, run=self._runner(oserror=True))
        self.assertIsNone(value)
        self.assertIn("VO0Gmw", stderr.getvalue())

    def test_the_value_is_returned_but_never_printed(self):
        stderr, stdout = io.StringIO(), io.StringIO()
        with contextlib.redirect_stderr(stderr), contextlib.redirect_stdout(stdout):
            value = ms.teamvault_password("VO0Gmw", {}, run=self._runner(stdout="placeholder-secret-1a2b"))
        self.assertEqual(value, "placeholder-secret-1a2b")
        self.assertNotIn("placeholder-secret-1a2b", stderr.getvalue())
        self.assertNotIn("placeholder-secret-1a2b", stdout.getvalue())


class DeclareTokensTest(unittest.TestCase):
    """Both tokens, and the case that makes the launcher survive a vault outage.

    The load-bearing case is the DEGRADING one: an unresolved token must leave the server
    startable, because exiting here costs the session every supervisor tool — not only the two
    spawn targets that consume a token. A test that only covered the happy path would pass on
    a launcher that takes the whole fleet down whenever TeamVault blinks.
    """

    VALUES = {
        "VO0Gmw": "placeholder-cluster-2f9c41",
        "lO4j5q": "placeholder-a2a-k8s-7b3e08",
        "pLv8Mw": "placeholder-a2a-docker-4d1a92",
    }

    def _lookup(self, failing=(), seen=None):
        def lookup(key, env):
            if seen is not None:
                seen.append(key)
            return None if key in failing else self.VALUES[key]

        return lookup

    def test_every_token_resolves_from_its_own_key(self):
        env, seen = {}, []
        with contextlib.redirect_stderr(io.StringIO()):
            declared = ms.declare_tokens(env, lookup=self._lookup(seen=seen))
        self.assertEqual(sorted(seen), ["VO0Gmw", "lO4j5q", "pLv8Mw"])
        self.assertEqual(env["INTERACTIVE_AUTH_TOKEN"], self.VALUES["VO0Gmw"])
        # ⚠️ The A2A token is PER TARGET — the docker controller runs on this Mac and the k8s
        # controller in nuke dev, so one shared secret would force both deployments to carry the
        # same value. Each target carries the entry its own controller was started with, and a
        # target with no entry refuses by name.
        self.assertEqual(env["SUPERVISOR_A2A_TOKEN_K8S"], self.VALUES["lO4j5q"])
        self.assertEqual(env["SUPERVISOR_A2A_TOKEN_DOCKER"], self.VALUES["pLv8Mw"])
        # Distinctness: one key resolved into several variables is the failure this guards, and
        # it is invisible unless the values are compared.
        self.assertNotEqual(env["INTERACTIVE_AUTH_TOKEN"], env["SUPERVISOR_A2A_TOKEN_K8S"])
        self.assertNotEqual(env["SUPERVISOR_A2A_TOKEN_K8S"], env["SUPERVISOR_A2A_TOKEN_DOCKER"])
        self.assertEqual(
            sorted(declared),
            [
                "INTERACTIVE_AUTH_TOKEN",
                "SUPERVISOR_A2A_TOKEN_DOCKER",
                "SUPERVISOR_A2A_TOKEN_K8S",
            ],
        )

    def test_an_explicit_value_wins_and_its_key_is_not_consulted(self):
        """config.mjs reads env-first, so a lookup here would spend a vault call to overwrite
        a deliberate override."""
        env, seen = {"INTERACTIVE_AUTH_TOKEN": "override-9a1c"}, []
        with contextlib.redirect_stderr(io.StringIO()):
            ms.declare_tokens(env, lookup=self._lookup(seen=seen))
        self.assertEqual(env["INTERACTIVE_AUTH_TOKEN"], "override-9a1c")
        self.assertEqual(seen, ["lO4j5q", "pLv8Mw"])

    def test_an_unresolved_token_degrades_rather_than_raising(self):
        env, stderr = {}, io.StringIO()
        with contextlib.redirect_stderr(stderr):
            declared = ms.declare_tokens(env, lookup=self._lookup(failing=["lO4j5q"]))
        # The FAILING target is absent and every other target still resolves — that split is
        # the degradation the module docstring promises, not a whole-launcher failure.
        self.assertEqual(declared, ["INTERACTIVE_AUTH_TOKEN", "SUPERVISOR_A2A_TOKEN_DOCKER"])
        self.assertEqual(env["INTERACTIVE_AUTH_TOKEN"], self.VALUES["VO0Gmw"])
        self.assertNotIn("SUPERVISOR_A2A_TOKEN_K8S", env)
        self.assertEqual(env["SUPERVISOR_A2A_TOKEN_DOCKER"], self.VALUES["pLv8Mw"])
        self.assertIn("lO4j5q", stderr.getvalue())

    def test_a_degraded_token_never_reaches_stderr(self):
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            ms.declare_tokens({}, lookup=self._lookup(failing=["VO0Gmw"]))
        self.assertNotIn(self.VALUES["VO0Gmw"], stderr.getvalue())

    def test_all_unresolved_still_returns_and_declares_nothing(self):
        env = {}
        with contextlib.redirect_stderr(io.StringIO()):
            declared = ms.declare_tokens(
                env, lookup=self._lookup(failing=["VO0Gmw", "lO4j5q", "pLv8Mw"])
            )
        self.assertEqual(declared, [])
        self.assertEqual(env, {})


class MainTest(unittest.TestCase):
    def test_missing_bun_is_reported_not_exec_d(self):
        """No bun must be a readable error and a non-zero exit -- an exec-less return is
        the only outcome that keeps this test from replacing the test process."""
        original = ms.find_bun
        ms.find_bun = lambda env, which=None: None
        try:
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                code = ms.main(env={})
        finally:
            ms.find_bun = original
        self.assertEqual(code, 1)
        self.assertIn("bun not found", stderr.getvalue())

    def test_an_unresolved_token_does_not_stop_the_server(self):
        """The degradation at the integration level, and why it is a rule rather than a
        nicety: this process gates every supervisor tool, so a TeamVault outage that exited
        here would take the supervisor away from the session entirely — not just the two
        spawn targets that consume a token."""
        original = ms.find_bun
        ms.find_bun = lambda env, which=None: "/stub/bun"
        captured = {}
        try:
            with mock.patch.object(ms.os, "execve", lambda path, args, env: captured.update(env=env)):
                with contextlib.redirect_stderr(io.StringIO()):
                    code = ms.main(env={"HOME": "/tmp", "SUPERVISOR_TEAMVAULT_CLI": "/nonexistent/cli"})
        finally:
            ms.find_bun = original
        self.assertEqual(code, 0, "the server must still start")
        self.assertNotIn("INTERACTIVE_AUTH_TOKEN", captured["env"])
        self.assertNotIn("SUPERVISOR_A2A_TOKEN", captured["env"])


if __name__ == "__main__":
    unittest.main()
