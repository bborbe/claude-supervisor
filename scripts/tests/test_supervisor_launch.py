#!/usr/bin/env python3
"""Tests for scripts/supervisor-launch.sh.

The wrapper is the only thing that puts the two supervisor tokens into the environment, and
it runs at the top of every session's MCP handshake. The cases below are the ones whose
failure is either silent or catastrophic:

  * both tokens resolve from THEIR OWN key. One key resolved into both variables is the
    failure this wrapper exists to prevent: it reads as configured and fails on one leg only.
  * a failing lookup REFUSES, naming the key. A wrapper that started anyway would hand the
    server no token, and the service answers a header-less request with 401 *before* its
    route handler runs -- so an unconfigured supervisor and a wrong token would be one
    indistinguishable observable.
  * the VALUE never reaches stdout. stdout carries MCP frames, so a stray `printf` is not a
    logging defect, it is protocol corruption.
  * a value already in the environment wins. server/config.mjs reads env-first, so a wrapper
    that overwrote an explicit override would silently undo a caller's choice.

Both `teamvault-cli` and `bun` are stubbed, so nothing here touches the real vault or starts
a server. Every placeholder value is obviously fake.

Run: python3 -m unittest discover -s scripts/tests -v
"""

import os
import shutil
import stat
import subprocess
import tempfile
import unittest

WRAPPER = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "supervisor-launch.sh"
)

CLUSTER_KEY = "VO0Gmw"
A2A_KEY = "lO4j5q"
CLUSTER_VALUE = "placeholder-cluster-2f9c41"
A2A_VALUE = "placeholder-a2a-7b3e08"

# `teamvault-cli password --teamvault-config=<path> --teamvault-key=<key>`. Records every key
# it was asked for, so a test can assert which lookups happened rather than only their
# outcome; prints a per-key placeholder, and fails on demand for one named key.
TEAMVAULT_STUB = '''#!/usr/bin/env python3
import os, sys

key = next((a.split("=", 1)[1] for a in sys.argv[1:] if a.startswith("--teamvault-key=")), None)

record = os.environ.get("STUB_KEY_LOG")
if record:
    with open(record, "a") as fh:
        fh.write(str(key) + "\\n")

if key == os.environ.get("STUB_FAIL_KEY"):
    print("teamvault-cli: no secret for key " + str(key), file=sys.stderr)
    sys.exit(1)

values = {"__CLUSTER_KEY__": "__CLUSTER_VALUE__", "__A2A_KEY__": "__A2A_VALUE__"}
print(values.get(key, "placeholder-unknown"))
'''
# Token replacement rather than %-formatting: the stub's own body contains a literal `%`-free
# string but `%` in general, and %-formatting a template that holds Python source is a
# recurring way to corrupt it.
TEAMVAULT_STUB = (
    TEAMVAULT_STUB.replace("__CLUSTER_KEY__", CLUSTER_KEY)
    .replace("__CLUSTER_VALUE__", CLUSTER_VALUE)
    .replace("__A2A_KEY__", A2A_KEY)
    .replace("__A2A_VALUE__", A2A_VALUE)
)

# Stands in for `bun run ... start`, so the test observes the environment the server would
# actually inherit. Its stdout is the wrapper's stdout, which is what makes the
# stdout-must-stay-clean assertion below meaningful.
BUN_STUB = '''#!/usr/bin/env python3
import os, sys

print("INTERACTIVE_AUTH_TOKEN=" + os.environ.get("INTERACTIVE_AUTH_TOKEN", "<unset>"))
print("SUPERVISOR_A2A_TOKEN=" + os.environ.get("SUPERVISOR_A2A_TOKEN", "<unset>"))
print("argv=" + " ".join(sys.argv[1:]))
'''


class WrapperCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="supervisor-launch-test-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.bin = os.path.join(self.tmp, "bin")
        os.makedirs(self.bin)
        self.empty_bin = os.path.join(self.tmp, "empty-bin")
        os.makedirs(self.empty_bin)
        self.bun = self._stub("bun", BUN_STUB)
        self.teamvault = self._stub("teamvault-cli", TEAMVAULT_STUB)
        self.key_log = os.path.join(self.tmp, "keys.txt")

    def _stub(self, name, body):
        path = os.path.join(self.bin, name)
        with open(path, "w") as fh:
            fh.write(body)
        os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR)
        return path

    def run_wrapper(self, *, path=None, env=None, with_teamvault=True):
        """Run the wrapper with stubs on PATH. `path` replaces PATH entirely."""
        # The stubs carry `#!/usr/bin/env python3`, so the real PATH must stay on the end of
        # whatever we prepend -- a stub-only PATH makes `env` itself unresolvable, and every
        # case then fails for a reason unrelated to what it tests.
        environ = {
            "PATH": path if path is not None else self.bin + ":" + os.environ["PATH"],
            "HOME": self.tmp,
            "SUPERVISOR_BUN": self.bun,
            "STUB_KEY_LOG": self.key_log,
        }
        environ.update(env or {})
        if not with_teamvault:
            environ["PATH"] = self.empty_bin
        return subprocess.run(
            [WRAPPER], env=environ, capture_output=True, text=True, timeout=30
        )

    def keys_asked(self):
        if not os.path.exists(self.key_log):
            return []
        with open(self.key_log) as fh:
            return [line.strip() for line in fh if line.strip()]


class TestTokenResolution(WrapperCase):
    def test_both_tokens_resolve_from_their_own_key(self):
        result = self.run_wrapper()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("INTERACTIVE_AUTH_TOKEN=%s" % CLUSTER_VALUE, result.stdout)
        self.assertIn("SUPERVISOR_A2A_TOKEN=%s" % A2A_VALUE, result.stdout)
        # The distinctness the task's SC1 asserts: one key resolved into both variables is
        # the failure mode, and it is invisible unless the two values are compared.
        self.assertNotIn("INTERACTIVE_AUTH_TOKEN=%s" % A2A_VALUE, result.stdout)

    def test_each_key_is_looked_up_exactly_once(self):
        self.run_wrapper()
        self.assertEqual(sorted(self.keys_asked()), sorted([CLUSTER_KEY, A2A_KEY]))

    def test_server_is_exec_d_with_the_start_command(self):
        result = self.run_wrapper()
        self.assertIn("argv=run --cwd", result.stdout)
        self.assertIn("--shell=bun --silent start", result.stdout)

    def test_source_key_names_are_logged(self):
        result = self.run_wrapper()
        self.assertIn(CLUSTER_KEY, result.stderr)
        self.assertIn(A2A_KEY, result.stderr)


class TestRefusals(WrapperCase):
    def test_failing_lookup_refuses_and_names_the_key(self):
        result = self.run_wrapper(env={"STUB_FAIL_KEY": A2A_KEY})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(A2A_KEY, result.stderr)

    def test_a_refusal_never_prints_a_value(self):
        result = self.run_wrapper(env={"STUB_FAIL_KEY": A2A_KEY})
        self.assertNotIn(CLUSTER_VALUE, result.stdout + result.stderr)
        self.assertNotIn(A2A_VALUE, result.stdout + result.stderr)

    def test_missing_teamvault_cli_refuses(self):
        result = self.run_wrapper(with_teamvault=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("teamvault-cli", result.stderr)

    def test_one_key_for_both_legs_refuses_before_looking_anything_up(self):
        result = self.run_wrapper(
            env={"SUPERVISOR_CLUSTER_TOKEN_KEY": CLUSTER_KEY,
                 "SUPERVISOR_A2A_TOKEN_KEY": CLUSTER_KEY}
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.keys_asked(), [])

    def test_no_value_is_ever_written_to_stderr(self):
        result = self.run_wrapper()
        self.assertNotIn(CLUSTER_VALUE, result.stderr)
        self.assertNotIn(A2A_VALUE, result.stderr)


class TestEnvPrecedence(WrapperCase):
    def test_an_override_in_the_environment_wins(self):
        result = self.run_wrapper(env={"INTERACTIVE_AUTH_TOKEN": "override-9a1c"})
        self.assertIn("INTERACTIVE_AUTH_TOKEN=override-9a1c", result.stdout)
        # config.mjs reads env-first, so the key must not even be consulted -- a lookup here
        # would spend a vault call and could overwrite a deliberate override.
        self.assertEqual(self.keys_asked(), [A2A_KEY])

    def test_both_overrides_skip_the_vault_entirely(self):
        result = self.run_wrapper(
            env={"INTERACTIVE_AUTH_TOKEN": "override-9a1c",
                 "SUPERVISOR_A2A_TOKEN": "override-4d7f"}
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.keys_asked(), [])


class TestStreamHygiene(WrapperCase):
    def test_stdout_carries_only_the_servers_own_output(self):
        result = self.run_wrapper()
        # stdout carries MCP frames; a log line here corrupts the protocol rather than
        # merely being untidy. Every wrapper line must therefore be on stderr.
        self.assertNotIn("supervisor-launch:", result.stdout)
        self.assertIn("supervisor-launch:", result.stderr)


if __name__ == "__main__":
    unittest.main()
