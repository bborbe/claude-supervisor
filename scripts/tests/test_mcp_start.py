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

Run: python3 -m unittest discover -s scripts/tests -v
"""

import contextlib
import importlib.util
import io
import os
import tempfile
import unittest

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


if __name__ == "__main__":
    unittest.main()
