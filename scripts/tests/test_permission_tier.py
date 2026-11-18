#!/usr/bin/env python3
"""Cases for `scripts/permission-tier.py`, driven by the shared fixture both classifiers run.

The fixture (`server/permission-tier.fixtures.json`) is the contract: `server/permission-tier.mjs`
runs the same cases, so a case changed here moves both suites. Every case ASSERTS its tier, and its
form when the fixture names one — a fixture with named cases and no assertion would pin nothing.

Run: python3 -m unittest discover -s scripts/tests -v
"""

import importlib.util
import json
import os
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(os.path.dirname(_HERE))
_SCRIPT = os.path.join(_ROOT, "scripts", "permission-tier.py")
_FIXTURE = os.path.join(_ROOT, "server", "permission-tier.fixtures.json")

_spec = importlib.util.spec_from_file_location("permission_tier", _SCRIPT)
tier = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(tier)

with open(_FIXTURE, encoding="utf-8") as fh:
    CASES = json.load(fh)["cases"]
ENTRIES = tier.load_allowlist()


class FixtureCases(unittest.TestCase):
    def test_every_case(self):
        self.assertGreater(len(CASES), 40)
        for case in CASES:
            with self.subTest(case=case["id"]):
                got = tier.tier_for(case["tool"], case["input"], ENTRIES)
                self.assertEqual(got["tier"], case["tier"], got["reason"])
                if "form" in case:
                    self.assertEqual(got["form"], case["form"], got["reason"])

    def test_bad_examples_are_manager(self):
        bad = [c for c in CASES if c["id"].startswith("bad-example-")]
        self.assertEqual(len(bad), 3)
        for case in bad:
            self.assertEqual(tier.tier_for(case["tool"], case["input"], ENTRIES)["tier"], "manager")


class EveryFormIsCovered(unittest.TestCase):
    """A form the classifier can return but no fixture case exercises is an untested gate."""

    def test_every_returned_form_has_a_case(self):
        import re
        with open(_SCRIPT, encoding="utf-8") as fh:
            source = fh.read()
        returned = set(re.findall(r'return "([a-z][a-z/-]+)"', source)) - {"manager", "operator", "allowlist"}
        returned |= {f for f, _ in tier._TEXT_FORMS} | {"unverifiable"}
        covered = {c["form"] for c in CASES if "form" in c}
        self.assertEqual(sorted(returned - covered), [])


class NoGitOnTheAllowlist(unittest.TestCase):
    """Git reads are code execution (`core.fsmonitor`), so none may ever be `allowlist`."""

    def test_git_reads_never_allowlist(self):
        for cmd in ("git status", "git log", "git diff", "git show", "git rev-parse HEAD"):
            self.assertEqual(tier.tier_for("Bash", {"command": cmd}, ENTRIES)["tier"], "manager", cmd)

    def test_shipped_allowlist_has_no_git_or_kubectl(self):
        heads = {e["prefix"].split()[0] for e in ENTRIES}
        self.assertFalse({h for h in heads if h == "git" or h.startswith("kubectl")})


class FailClosed(unittest.TestCase):
    def test_missing_allowlist_file_yields_no_entries(self):
        self.assertEqual(tier.load_allowlist("/nonexistent/manager-allowlist.json"), [])

    def test_allowlist_entry_needs_its_confinement(self):
        got = tier.tier_for("Bash", {"command": "make test"}, ENTRIES, {})
        self.assertEqual(got["tier"], "manager")
        got = tier.tier_for("Bash", {"command": "make test"}, ENTRIES, {"runnerConfigClean": True})
        self.assertEqual(got["tier"], "allowlist")

    def test_truncated_payload_is_operator(self):
        payload = "Bash: echo " + "x" * (tier.PAYLOAD_CAP - 11)
        self.assertEqual(len(payload), tier.PAYLOAD_CAP)
        self.assertEqual(tier.tier_for_payload(payload)["tier"], "operator")

    def test_payload_parse(self):
        self.assertEqual(tier.tier_for_payload("Bash: git status")["tier"], "manager")
        self.assertEqual(tier.tier_for_payload("Write: /Users/x/.ssh/id_rsa")["form"], "secret")
        self.assertEqual(tier.tier_for_payload("no tool here")["tier"], "operator")


class Lexer(unittest.TestCase):
    def test_redirect_ampersand_is_not_a_separator(self):
        segs, _, _ = tier.lex("make test 2>&1 | tail -3")
        self.assertEqual(segs, ["make test 2>&1", "tail -3"])

    def test_quoted_separator_stays_in_segment(self):
        segs, _, _ = tier.lex('git commit -m "a; b && c"')
        self.assertEqual(len(segs), 1)

    def test_nested_substitution_collected(self):
        _, nested, _ = tier.lex('echo "$(git rev-parse HEAD)" `date`')
        self.assertEqual(nested, ["git rev-parse HEAD", "date"])


class QuotedBraces(unittest.TestCase):
    """Only an UNQUOTED, unescaped `{a,b}` is brace expansion; quoted or escaped braces are literal."""

    def _got(self, cmd):
        return tier.tier_for("Bash", {"command": cmd}, ENTRIES, {"runnerConfigClean": True})

    def test_real_brace_expansion_stays_unverifiable(self):
        for cmd in ("cat ~/{.netrc,x}", "echo {a,b}", "echo {a,'b'}", "echo x{a,b}y", "echo 'unterminated {a,b}"):
            got = self._got(cmd)
            self.assertEqual(got.get("form"), "unverifiable", cmd)

    def test_quoted_braces_are_not_unverifiable(self):
        for cmd in (
            "gh -R bborbe/x pr view 1 --json a,b --jq '{a: .a, b: .b}'",
            "jq '{\"a\":1,\"b\":2}' f.json",
            'echo "{a,b}"',
            "echo \\{a,b\\}",
            "echo {'a,b'}",
        ):
            got = self._got(cmd)
            self.assertNotEqual(got.get("form"), "unverifiable", cmd)

    def test_quoted_gh_jq_is_manager_tier(self):
        got = self._got("gh -R bborbe/x pr view 1 --json a,b --jq '{a: .a, b: .b}'")
        self.assertEqual(got["tier"], "manager", got["reason"])

    def test_substitution_inside_double_quotes_is_unverifiable(self):
        for cmd in (
            'cat "$(: "\'")"/Users/bborbe/{.netrc,x}"$(: "\'")"',
            'cat "`: "\'"`"/Users/bborbe/{.netrc,x}"`: "\'"`"',
        ):
            self.assertEqual(self._got(cmd).get("form"), "unverifiable", cmd)

    def test_sequence_expansion_is_unverifiable(self):
        self.assertEqual(self._got("cat ~/.netr{c..c}").get("form"), "unverifiable")

    def test_brace_after_quoted_word_is_unverifiable(self):
        for cmd in ("echo 'x'{a,b}", 'echo "a"{b,c}', 'echo "a\\"b"{c,d}'):
            self.assertEqual(self._got(cmd).get("form"), "unverifiable", cmd)

    def test_nested_quoted_braces_are_literal(self):
        self.assertNotEqual(self._got('echo "$(echo "{a,b}")"').get("form"), "unverifiable")

    def test_masker_fails_closed_on_unterminated_input(self):
        for seg in ("echo 'abc", 'echo "abc', "echo abc\\"):
            self.assertTrue(tier._unquoted_brace_expansion(seg), seg)

    def test_parameter_expansion_cannot_end_a_brace_list_early(self):
        for cmd in ("cat ~/{${x}.netrc,y}", "cat ~/.{${x}netrc,y}", "cat ~/{${x:-.netrc},y}", "cat ~/{${x},.netrc}"):
            self.assertEqual(self._got(cmd).get("form"), "unverifiable", cmd)

    def test_plain_parameter_expansion_is_not_a_brace_list(self):
        for cmd in ("cat ${HOME}/x", "echo ${x:-{a,b}}"):
            self.assertNotEqual(self._got(cmd).get("form"), "unverifiable", cmd)

    def test_ansi_c_quote_stays_fail_closed(self):
        # `$'…'` is rejected as unverifiable elsewhere (shlex does not model it); not loosened here.
        self.assertEqual(self._got("echo $'{a,b}'").get("form"), "unverifiable")


if __name__ == "__main__":
    unittest.main()
