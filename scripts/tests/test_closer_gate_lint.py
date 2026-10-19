#!/usr/bin/env python3
"""Tests for scripts/closer-gate-lint.py — the Stop-time closer gate lint.

Load-bearing properties, from the 2026-10-19 measurement:

  * the two observed closers (pane 809 `/github-pr-watch`, pane 832 prose "local review")
    are BLOCKED — including the prose form, which names no verb;
  * every allow-listed verb blocks, so a matcher cannot ship narrower than the list;
  * `gh pr merge` and a prod `make buca` ALLOW — the merge is deliberately outside the
    list, and a production-touching step stays the operator's;
  * a non-gate closer (`pick —`) ALLOWS;
  * fail-open: `stop_hook_active`, the kill switch, a malformed stdin and a truncated
    transcript all exit 0;
  * a record is written on ALLOW and BLOCK alike.

Run: python3 -m unittest discover -s scripts/tests -v
"""

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_FIXTURES = os.path.join(_HERE, "fixtures", "closer-gate-lint")
_SCRIPT = os.path.join(os.path.dirname(_HERE), "closer-gate-lint.py")


def load():
    spec = importlib.util.spec_from_file_location(
        "closer_gate_lint", _SCRIPT
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ⚠️ Every allow-listed verb has a fixture, so a matcher that omits one fails here.
EXPECTED = {
    "01-github-pr-watch.jsonl": 2,
    "02-local-review-prose.jsonl": 2,
    "03-coding-local-review.jsonl": 2,
    "04-coding-pr-review.jsonl": 2,
    "05-gh-pr-ready.jsonl": 2,
    "06-dark-factory-prompt-approve.jsonl": 2,
    "07-dark-factory-spec-approve.jsonl": 2,
    "08-prod-buca.jsonl": 0,
    "09-gh-pr-merge.jsonl": 0,
    "10-pick.jsonl": 0,
    "11-no-closer.jsonl": 0,
    "12-truncated.jsonl": 0,
}


class CloserGateLintTest(unittest.TestCase):
    def setUp(self):
        self.mod = load()
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)

    def payload(self, name, **over):
        body = {"transcript_path": os.path.join(_FIXTURES, name),
                "stop_hook_active": False, "session_id": "test-session"}
        body.update(over)
        return body

    def test_every_fixture_matches_its_expected_verdict(self):
        for name, expected in EXPECTED.items():
            with self.subTest(fixture=name):
                code, _text, _record = self.mod.evaluate(self.payload(name), environ={})
                self.assertEqual(expected, code)

    def test_observed_pane_809_closer_names_github_pr_watch(self):
        _code, text, record = self.mod.evaluate(
            self.payload("01-github-pr-watch.jsonl"), environ={})
        self.assertEqual("github-pr-watch", record["matched"])
        self.assertIn("github-pr-watch", text)
        self.assertEqual("approve", record["verb"])

    def test_observed_pane_832_closer_matches_the_prose_alias(self):
        # The line says "I run the local review", never the verb — a verb-only matcher
        # would miss the exact line the hook exists for.
        _code, text, record = self.mod.evaluate(
            self.payload("02-local-review-prose.jsonl"), environ={})
        self.assertEqual("coding-local-review", record["matched"])
        self.assertIn("coding-local-review", text)
        self.assertEqual("review", record["verb"])

    def test_gh_pr_merge_is_outside_the_allow_list(self):
        _code, text, record = self.mod.evaluate(
            self.payload("09-gh-pr-merge.jsonl"), environ={})
        self.assertIsNone(record["matched"])
        self.assertEqual("not-allow-listed", record["reason"])
        self.assertEqual("", text)

    def test_prod_deploy_stays_the_operators(self):
        _code, _text, record = self.mod.evaluate(
            self.payload("08-prod-buca.jsonl"), environ={})
        self.assertEqual("not-allow-listed", record["reason"])

    def test_stop_hook_active_allows_even_a_gated_closer(self):
        # The loop guard: without it a continued turn ending on the same closer re-blocks
        # forever.
        code, _text, record = self.mod.evaluate(
            self.payload("01-github-pr-watch.jsonl", stop_hook_active=True), environ={})
        self.assertEqual(0, code)
        self.assertEqual("stop-hook-active", record["reason"])

    def test_kill_switch_allows(self):
        code, _text, record = self.mod.evaluate(
            self.payload("01-github-pr-watch.jsonl"),
            environ={self.mod.KILL_SWITCH_ENV: "off"})
        self.assertEqual(0, code)
        self.assertEqual("kill-switch", record["reason"])

    def test_truncated_transcript_fails_open(self):
        code, _text, record = self.mod.evaluate(
            self.payload("12-truncated.jsonl"), environ={})
        self.assertEqual(0, code)
        self.assertEqual("no-closer", record["reason"])

    def test_missing_transcript_path_fails_open(self):
        code, _text, record = self.mod.evaluate(
            {"stop_hook_active": False, "session_id": "x"}, environ={})
        self.assertEqual(0, code)
        self.assertEqual("no-transcript-path", record["reason"])

    def test_record_written_on_allow_and_block(self):
        _code, _text, block = self.mod.evaluate(
            self.payload("01-github-pr-watch.jsonl"), environ={})
        _code, _text, allow = self.mod.evaluate(
            self.payload("08-prod-buca.jsonl"), environ={})
        self.mod.write_record(block, directory=self.dir)
        self.mod.write_record(allow, directory=self.dir)
        path = os.path.join(self.dir, "test-session.jsonl")
        with open(path, encoding="utf-8") as fh:
            rows = [json.loads(line) for line in fh]
        self.assertEqual(["block", "allow"], [r["decision"] for r in rows])

    def test_cli_exit_codes(self):
        # The real contract is the process exit code, so exercise main() once per arm.
        env = dict(os.environ)
        env[self.mod.STATE_DIR_ENV] = self.dir
        for name, expected in (("01-github-pr-watch.jsonl", 2),
                               ("08-prod-buca.jsonl", 0)):
            with self.subTest(fixture=name):
                proc = subprocess.run(
                    [sys.executable, _SCRIPT],
                    input=json.dumps(self.payload(name)),
                    capture_output=True, text=True, env=env, check=False)
                self.assertEqual(expected, proc.returncode)
                if expected:
                    self.assertIn("closer-gate-lint", proc.stderr)
                else:
                    self.assertEqual("", proc.stderr)

    def test_cli_malformed_stdin_fails_open(self):
        env = dict(os.environ)
        env[self.mod.STATE_DIR_ENV] = self.dir
        proc = subprocess.run([sys.executable, _SCRIPT], input="not json",
                              capture_output=True, text=True, env=env, check=False)
        self.assertEqual(0, proc.returncode)


if __name__ == "__main__":
    unittest.main()
