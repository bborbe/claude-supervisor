#!/usr/bin/env python3
"""Tests for scripts/answer-path-check.py.

The load-bearing property is that EVERY branch refuses: the script is the observable a
headless spawn's refusal is built on, so a branch that failed open would open exactly the
worker the rule exists to prevent (2026-09-20: two workers stranded, four gates expired in
~90 minutes).

What is pinned here is the real code path — the registration read, the `--state` subprocess
call, and the CLI envelope — rather than an injected fake. `server/answer-path.test.mjs`
covers the Node wrapper against a fake `exec`; this covers the script that fake stands in for.

Run: python3 -m unittest discover -s scripts/tests -v
"""

import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout

_HERE = os.path.dirname(os.path.abspath(__file__))
_SCRIPT = os.path.join(os.path.dirname(_HERE), "answer-path-check.py")


def load(manager_dir):
    """Import the hyphenated module with its registration dir pointed at `manager_dir`."""
    os.environ["WORKER_MANAGER_STATE_DIR"] = manager_dir
    spec = importlib.util.spec_from_file_location("answer_path_check", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def fake_liveness(tmp, body, exit_code=0):
    """A stand-in for manager-liveness.py that prints `body` and exits `exit_code`."""
    path = os.path.join(tmp, "fake-liveness.py")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("import sys\n")
        fh.write(f"sys.stdout.write({body!r})\n")
        fh.write(f"sys.exit({exit_code})\n")
    return path


class AnswerPathCheckTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        # The established override wins over the fixture knob, so it must be absent here or
        # every test would read the operator's real store instead of its tempdir.
        os.environ.pop("SUPERVISOR_WORKER_MANAGER_DIR", None)
        self.mod = load(self.tmp)

    def write_registration(self, session_id, payload):
        with open(os.path.join(self.tmp, f"{session_id}.json"), "w", encoding="utf-8") as fh:
            fh.write(payload if isinstance(payload, str) else json.dumps(payload))

    # --- the registration half -------------------------------------------------

    def test_no_registration_refuses_and_names_the_path(self):
        ok, subject, branch, detail = self.mod.check("missing-session")
        self.assertFalse(ok)
        self.assertIsNone(subject)
        self.assertIn("no manager registration", detail)
        self.assertIn("missing-session.json", detail)

    def test_unreadable_registration_refuses(self):
        self.write_registration("bad-json", "{not json")
        ok, _, _, detail = self.mod.check("bad-json")
        self.assertFalse(ok)
        self.assertIn("unreadable", detail)

    def test_non_object_registration_refuses_without_raising(self):
        # Valid JSON that is not an object parses fine and then blows up on `.get` — a
        # traceback where the caller is promised a quotable refusal.
        for payload in ("null", "[]", '"a string"', "42", "true"):
            with self.subTest(payload=payload):
                self.write_registration("odd", payload)
                ok, _, _, detail = self.mod.check("odd")
                self.assertFalse(ok)
                self.assertIn("not a JSON object", detail)

    def test_non_string_subject_or_branch_refuses_without_raising(self):
        # Truthy-but-not-a-string passes a bare `if not subject` and then raises TypeError
        # inside subprocess.run — outside the classes liveness_state catches.
        # The type names are asserted, not just the refusal: a misspelled key in the fixture
        # would also refuse, so "no subject/branch" alone cannot prove the guard read the value.
        for payload, types in (
            ({"subject": 123, "branch": "goal"}, "int/str"),
            ({"subject": "A Goal", "branch": ["goal"]}, "str/list"),
            ({"subject": {"a": 1}, "branch": "goal"}, "dict/str"),
        ):
            with self.subTest(payload=payload):
                self.write_registration("typed", json.dumps(payload))
                ok, _, _, detail = self.mod.check("typed")
                self.assertFalse(ok)
                self.assertIn("no subject/branch", detail)
                self.assertIn(types, detail)

    def test_a_session_id_that_is_not_a_bare_filename_refuses(self):
        # The id is joined into a path; `../` or an absolute value would read an arbitrary
        # *.json outside the registration dir.
        # `"a\x00b"` is in the list because the guard has a null-byte clause of its own; a
        # tuple of only slashed ids leaves that clause unexecuted.
        for bad in ("../escape", "a/b", "..", ".", "/etc/passwd", "a\\b", "a\x00b"):
            with self.subTest(session_id=bad):
                ok, subject, branch, detail = self.mod.check(bad)
                self.assertFalse(ok)
                self.assertIsNone(subject)
                self.assertIsNone(branch)
                self.assertIn("not a bare filename", detail)

    def test_an_absent_session_id_says_so_and_names_no_escape(self):
        # An absent id has no path escape at stake, so a refusal that names one claims more
        # than it carries. `None`/`123` also exercise the non-string clause, which is
        # reachable only through a programmatic `check()` call — exactly what this does.
        for bad in ("", None, 123):
            with self.subTest(session_id=bad):
                ok, _, _, detail = self.mod.check(bad)
                self.assertFalse(ok)
                self.assertIn("no session id", detail)
                self.assertNotIn("bare filename", detail)

    def test_the_established_env_override_wins_over_the_fixture_knob(self):
        # `SUPERVISOR_WORKER_MANAGER_DIR` is what the rest of the repo sets (adopt-orphans,
        # inbox); a probe honouring a different name reads a directory nobody writes.
        other = tempfile.mkdtemp()
        os.environ["SUPERVISOR_WORKER_MANAGER_DIR"] = other
        try:
            # The fixture knob points at `self.tmp`; the established name must still win.
            reloaded = load(self.tmp)
            self.assertEqual(reloaded.MANAGER_DIR, os.path.expanduser(other))
        finally:
            os.environ.pop("SUPERVISOR_WORKER_MANAGER_DIR", None)

    def test_registration_without_subject_or_branch_refuses(self):
        self.write_registration("half", {"vault": "private-personal"})
        ok, _, _, detail = self.mod.check("half")
        self.assertFalse(ok)
        self.assertIn("no subject/branch", detail)

    def test_a_fleet_registration_with_an_armed_loop_passes(self):
        # ⚠️ THE END-TO-END PATH THE SPAWN GUARD GATES ON, and the one a cadence-only fix
        # silently failed: a registration alone reads UNARMED, an arm alone reads no
        # registration. Both, and the probe passes — the real manager-liveness.py, not a fake.
        state = tempfile.mkdtemp()
        os.environ["MANAGER_LIVENESS_STATE_DIR"] = state
        try:
            self.write_registration("fleet-session", {"subject": "Fleet Manager", "branch": "fleet"})
            subprocess.run(
                [sys.executable, self.mod.LIVENESS, "--arm", "--subject", "Fleet Manager",
                 "--branch", "fleet", "--interval", "900"],
                check=True, capture_output=True,
            )
            ok, subject, branch, detail = self.mod.check("fleet-session")
            self.assertTrue(ok, detail)
            self.assertEqual((subject, branch), ("Fleet Manager", "fleet"))
        finally:
            os.environ.pop("MANAGER_LIVENESS_STATE_DIR", None)

    def test_a_fleet_registration_alone_still_refuses(self):
        # The half the reviewer caught: the cadence is not the registration, and an
        # unarmed subject refuses exactly like an unregistered one.
        state = tempfile.mkdtemp()
        os.environ["MANAGER_LIVENESS_STATE_DIR"] = state
        try:
            self.write_registration("fleet-session", {"subject": "Fleet Manager", "branch": "fleet"})
            ok, _, _, detail = self.mod.check("fleet-session")
            self.assertFalse(ok)
            self.assertIn("UNARMED", detail)
        finally:
            os.environ.pop("MANAGER_LIVENESS_STATE_DIR", None)

    # --- the liveness half -----------------------------------------------------

    def test_ok_state_is_the_only_pass(self):
        self.write_registration("good", {"subject": "A Goal", "branch": "goal"})
        self.mod.LIVENESS = fake_liveness(self.tmp, "OK\tgoal--a-goal\tfresh\n")
        ok, subject, branch, detail = self.mod.check("good")
        self.assertTrue(ok)
        self.assertEqual((subject, branch), ("A Goal", "goal"))
        self.assertIn("OK", detail)

    def test_any_state_but_ok_refuses(self):
        self.write_registration("stale", {"subject": "A Goal", "branch": "goal"})
        self.mod.LIVENESS = fake_liveness(self.tmp, "STALE\tgoal--a-goal\tage=4328s\n")
        ok, _, _, detail = self.mod.check("stale")
        self.assertFalse(ok)
        self.assertIn("STALE", detail)
        self.assertIn("not OK", detail)

    def test_a_non_zero_state_exit_refuses(self):
        self.write_registration("broken", {"subject": "A Goal", "branch": "goal"})
        self.mod.LIVENESS = fake_liveness(self.tmp, "", exit_code=3)
        ok, _, _, detail = self.mod.check("broken")
        self.assertFalse(ok)
        self.assertIn("exited 3", detail)

    def test_a_missing_liveness_script_refuses(self):
        self.write_registration("gone", {"subject": "A Goal", "branch": "goal"})
        self.mod.LIVENESS = os.path.join(self.tmp, "does-not-exist.py")
        ok, _, _, detail = self.mod.check("gone")
        self.assertFalse(ok)
        # A missing script does not raise: python itself exits 2 with the path on stderr, so
        # the refusal arrives through the non-zero-exit branch rather than the OSError one.
        # Either way the reason names the failed call, which is what the caller quotes.
        self.assertIn("manager-liveness.py --state", detail)

    def test_empty_state_output_refuses(self):
        self.write_registration("quiet", {"subject": "A Goal", "branch": "goal"})
        self.mod.LIVENESS = fake_liveness(self.tmp, "\n")
        ok, _, _, detail = self.mod.check("quiet")
        self.assertFalse(ok)
        self.assertIn("printed nothing", detail)

    # --- the CLI envelope ------------------------------------------------------

    def run_main(self, argv):
        old = sys.argv
        # ⚠️ The suite itself runs inside a Claude session, so the real session id is in the
        # environment and would satisfy `--session-id`'s default — the empty-id branch would
        # then never be reached and the test would pass on the wrong path.
        old_env = os.environ.pop("CLAUDE_CODE_SESSION_ID", None)
        sys.argv = ["answer-path-check.py"] + argv
        buf = io.StringIO()
        try:
            with redirect_stdout(buf):
                code = self.mod.main()
        finally:
            sys.argv = old
            if old_env is not None:
                os.environ["CLAUDE_CODE_SESSION_ID"] = old_env
        return code, buf.getvalue()

    def test_missing_session_id_emits_the_json_envelope(self):
        code, out = self.run_main(["--json"])
        self.assertEqual(code, 1)
        payload = json.loads(out)
        self.assertFalse(payload["ok"])
        self.assertIn("no session id", payload["detail"])

    def test_missing_session_id_without_json_is_plain_text(self):
        code, out = self.run_main([])
        self.assertEqual(code, 1)
        self.assertTrue(out.startswith("REFUSED\t"))

    def test_ok_branch_exits_zero_with_json(self):
        self.write_registration("good", {"subject": "A Goal", "branch": "goal"})
        self.mod.LIVENESS = fake_liveness(self.tmp, "OK\tgoal--a-goal\tfresh\n")
        code, out = self.run_main(["--session-id", "good", "--json"])
        self.assertEqual(code, 0)
        payload = json.loads(out)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["subject"], "A Goal")


if __name__ == "__main__":
    unittest.main()
