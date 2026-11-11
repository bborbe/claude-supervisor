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
import urllib.error

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
    "09-gh-pr-merge.jsonl": 2,
    "10-pick.jsonl": 0,
    "11-no-closer.jsonl": 0,
    "12-truncated.jsonl": 0,
    "13-vault-cli-session-close.jsonl": 2,
    "14-gh-pr-merge-admin.jsonl": 0,
    "15-session-close-with-admin-merge.jsonl": 0,
    "16-compact-stays-gated.jsonl": 0,
    "17-worktree-remove-stays-gated.jsonl": 0,
    # ⚠️ The partial-write pair: a well-formed fixture cannot reproduce the stale read
    # at any scan bound, so these carry an unparseable final line (see the test below).
    "18-partial-write-gh-pr-merge.jsonl": 2,
    "19-partial-write-session-close.jsonl": 2,
    # Both exit 0, so only their `reason` distinguishes them from a stale fallback —
    # pinned by the two tests below.
    "20-unreadable-assistant-no-closer.jsonl": 0,
    "21-partial-tool-result-quoting-a-closer.jsonl": 0,
    "22-partial-quoted-subject-before-admin.jsonl": 0,
    "23-partial-tool-use-quoting-a-closer.jsonl": 0,
    "24-partial-cut-inside-the-closer.jsonl": 0,
    "25-partial-literal-backslash-before-admin.jsonl": 0,
    # ⚠️ The `⏰ Ends:` check: one positive per session-bound class (26 is pane 619's
    # recorded line verbatim), then six negatives that each carry a session-bound watcher
    # in their `(mechanism, id)` slot — so only the durable observable keeps them clean.
    "26-ends-background-agents-pane-619.jsonl": 2,
    "27-ends-monitor.jsonl": 2,
    "28-ends-run-in-background-shell.jsonl": 2,
    "29-ends-schedule-wakeup.jsonl": 2,
    "30-ends-cron-create.jsonl": 2,
    "31-ends-file-path.jsonl": 0,
    "32-ends-card-id.jsonl": 0,
    "33-ends-defer-date.jsonl": 0,
    "34-ends-external-probe.jsonl": 0,
    "35-ends-monitor-in-path.jsonl": 0,
    "36-ends-pr-watch-compliant.jsonl": 0,
    # ⚠️ `gh -R <owner>/<repo> pr ready N` is the form CLAUDE.md mandates, and the entry
    # originally required `gh` and `pr ready` to be adjacent — so the mandated form fell
    # through as `not-allow-listed` while the bare form was released.
    "37-gh-pr-ready-with-repo-flag.jsonl": 2,
    # ⚠️ A full ISO datetime is a legal clock (CLAUDE.md § Async State Closer names
    # "a clock (`defer_date` / timestamp)"), and the trailing `\b` after `\d{2}` cannot
    # match between `9` and `T` — both word characters — so this closer was blocked.
    # The watcher stays in the slot, so only the datetime keeps it clean.
    "38-ends-iso-datetime.jsonl": 0,
    # ⚠️ The ownerless-blocked pair, and it is a PAIR on purpose: the same `🔴 BLOCKED`
    # state line with only the `👤 You:` body changed. A check that blocked both would be
    # refusing the state rather than the missing owner.
    "39-ownerless-blocked.jsonl": 2,
    "40-blocked-with-manager-owner.jsonl": 0,
    # ⚠️ The pane-only fixtures under a TAB environment (`environ={}`): every one passes,
    # because a tab has an operator at the keyboard. PANELESS_EXPECTED below is the
    # same set under `CLAUDE_CODE_ENTRYPOINT=sdk-cli`.
    "41-paneless-shift-tab-recorded.jsonl": 0,
    "42-paneless-reload-in-fleet-manager-tab.jsonl": 0,
    "43-paneless-pick-shift-tab.jsonl": 0,
    "44-paneless-prose-mention-only.jsonl": 0,
    "45-paneless-path-not-a-slash-command.jsonl": 0,
}

PANELESS_ENV = {"CLAUDE_CODE_ENTRYPOINT": "sdk-cli"}

# The verdicts that CHANGE under a headless entrypoint. Every fixture not listed here must
# keep its EXPECTED verdict under PANELESS_ENV too — pinned by
# `test_paneless_env_changes_only_the_pane_only_fixtures`.
PANELESS_EXPECTED = {
    "41-paneless-shift-tab-recorded.jsonl": 2,
    "42-paneless-reload-in-fleet-manager-tab.jsonl": 2,
    "43-paneless-pick-shift-tab.jsonl": 2,
}

ENDS_POSITIVE = {
    "26-ends-background-agents-pane-619.jsonl": "background-sub-agent",
    "27-ends-monitor.jsonl": "monitor",
    "28-ends-run-in-background-shell.jsonl": "run-in-background-shell",
    "29-ends-schedule-wakeup.jsonl": "schedule-wakeup",
    "30-ends-cron-create.jsonl": "cron-create",
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

    def test_paneless_env_changes_only_the_pane_only_fixtures(self):
        for name, expected in EXPECTED.items():
            with self.subTest(fixture=name):
                code, _text, record = self.mod.evaluate(self.payload(name),
                                                        environ=PANELESS_ENV)
                self.assertEqual(PANELESS_EXPECTED.get(name, expected), code)
                if name in PANELESS_EXPECTED:
                    self.assertEqual("paneless-pane-only-ask", record["reason"])

    def test_paneless_block_names_the_phrase_and_the_routes(self):
        code, text, record = self.mod.evaluate(
            self.payload("41-paneless-shift-tab-recorded.jsonl"), environ=PANELESS_ENV)
        self.assertEqual(2, code)
        self.assertEqual("Shift+Tab", record["phrase"])
        for needle in ("CLAUDE_CODE_ENTRYPOINT=sdk-cli", "approve-run.py",
                       "SendMessage", "manager", "shipping: true"):
            self.assertIn(needle, text)

    def test_paneless_reload_phrase_is_the_slash_command(self):
        _code, _text, record = self.mod.evaluate(
            self.payload("42-paneless-reload-in-fleet-manager-tab.jsonl"),
            environ=PANELESS_ENV)
        self.assertEqual("/reload-plugins", record["phrase"])

    def test_an_unknown_or_cli_entrypoint_is_a_tab(self):
        for env in ({}, {"CLAUDE_CODE_ENTRYPOINT": "cli"},
                    {"CLAUDE_CODE_ENTRYPOINT": ""}, {"CLAUDE_CODE_ENTRYPOINT": "SDK-CLI "}):
            with self.subTest(env=env):
                code, _text, _record = self.mod.evaluate(
                    self.payload("41-paneless-shift-tab-recorded.jsonl"), environ=env)
                self.assertEqual(0, code)

    def test_the_recorded_e4fd1919_closer_is_blocked_only_when_paneless(self):
        text = "you run: switch this tab to accept edits (Shift+Tab), then reply go"
        self.assertEqual(0, self._closer(text)[0])
        code, _t, record = self._closer(text, environ=PANELESS_ENV)
        self.assertEqual(2, code)
        self.assertEqual("paneless-pane-only-ask", record["reason"])

    def test_paneless_block_is_bounded_by_the_loop_guard(self):
        state = {"SUPERVISOR_CLOSER_GATE_LINT_STATE_DIR": self.dir, **PANELESS_ENV}
        payload = self.payload("41-paneless-shift-tab-recorded.jsonl")
        first = self.mod.evaluate(payload, environ=state)
        self.assertEqual(2, first[0])
        self.mod.write_record(first[2], directory=self.dir)
        retry = self.mod.evaluate(dict(payload, stop_hook_active=True), environ=state)
        self.assertEqual(2, retry[0])
        self.assertEqual("blocked-retry", retry[2]["reason"])
        self.mod.write_record(retry[2], directory=self.dir)
        third = self.mod.evaluate(dict(payload, stop_hook_active=True), environ=state)
        self.assertEqual(0, third[0])

    def _closer(self, text, card_probe=None, prefix="done\n", environ=None):
        path = os.path.join(self.dir, "t.jsonl")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(json.dumps({"type": "assistant", "timestamp": "2026-11-05T20:00:00Z",
                                 "message": {"role": "assistant", "content": [
                                     {"type": "text", "text": prefix + "👤 You: " + text}]}}) + "\n")
        return self.mod.evaluate({"transcript_path": path, "stop_hook_active": False,
                                  "session_id": "test-session"},
                                 environ={} if environ is None else environ,
                                 card_probe=card_probe)

    def test_truncated_card_handle_is_refused_under_review(self):
        code, text, record = self._closer("review: dismiss card `7e55adaf` — reply `ok`")
        self.assertEqual(2, code)
        self.assertIn("7e55adaf", text)
        self.assertEqual("unpollable-handle", record["reason"])

    def test_truncated_card_handle_is_refused_under_you_run(self):
        code, text, _record = self._closer("you run: python3 attention-ask.py poll 7e55adaf")
        self.assertEqual(2, code)
        self.assertIn("7e55adaf", text)

    def test_full_store_id_passes(self):
        # ⚠️ The shape check passes; existence is a SEPARATE check (`absent-card`), so the
        # probe is injected here — otherwise this asserts a store round-trip, not a shape.
        code, text, _record = self._closer(
            "review: card " + "7e55adaf" * 4 + " — reply `seen`",
            card_probe=lambda _item, _env: True)
        self.assertEqual(0, code)
        self.assertEqual("", text)

    def test_a_full_store_id_the_store_does_not_hold_is_refused(self):
        code, text, record = self._closer(
            "review: card " + "7e55adaf" * 4 + " — reply `seen`",
            card_probe=lambda _item, _env: False)
        self.assertEqual(2, code)
        self.assertIn("7e55adaf", text)
        self.assertEqual("absent-card", record["reason"])

    def test_an_unaskable_store_never_blocks(self):
        # Fail-open: "cannot tell" is not "absent". A store outage must not refuse a turn.
        code, _text, _record = self._closer(
            "review: card " + "7e55adaf" * 4 + " — reply `seen`",
            card_probe=lambda _item, _env: None)
        self.assertEqual(0, code)

    def test_a_blocked_closer_naming_no_owner_is_refused(self):
        code, text, record = self._closer(
            "nothing",
            prefix="🔴 BLOCKED · nothing running · needs intervention\n")
        self.assertEqual(2, code)
        self.assertIn("names no owner", text)
        self.assertEqual("ownerless-blocked", record["reason"])

    def test_a_blocked_closer_naming_an_owner_passes(self):
        for body in ("approve: reply `go` — card " + "7e55adaf" * 4,
                     "you run: wait on the manager to re-arm the loop"):
            with self.subTest(body=body):
                code = self._closer(
                    body,
                    card_probe=lambda _item, _env: True,
                    prefix="🔴 BLOCKED · nothing running · needs intervention\n")[0]
                self.assertEqual(0, code)

    def test_the_recorded_e4fd1919_blocked_closer_passes(self):
        # ⚠️ The exact shape that made the bot request changes on #778. `fixtures/
        # e4fd1919-transcript.jsonl` is a RECORDED real closer: an operator verb IS an
        # owner, because only the operator can take the step it names.
        code, _text, _record = self._closer(
            "you run: switch this tab to `accept edits` (Shift+Tab), then reply `go`",
            prefix="🔴 BLOCKED · nothing running · needs intervention\n")
        self.assertEqual(0, code)

    def test_a_blocked_closer_naming_a_separate_line_ends_observable_passes(self):
        # ⚠️ The conventional layout: the observable is its OWN line below the verb. A
        # body-scoped owner search refused exactly this shape (bot finding on #778).
        code, _text, _record = self._closer(
            "nothing",
            prefix="🔴 BLOCKED · nothing running · needs intervention\n"
                   "⏰ Ends: PR #480 merged\n")
        self.assertEqual(0, code)

    def test_an_absent_card_probe_does_not_run_on_a_non_gate_closer(self):
        # ⚠️ A closer that merely CITES a 32-hex token in prose is not a gate nobody can
        # answer; probing it would refuse the session for the citation.
        code, _text, _record = self._closer(
            "nothing — see the uuid " + "7e55adaf" * 4 + " in the log",
            card_probe=lambda _item, _env: False)
        self.assertEqual(0, code)

    def test_card_in_store_404_is_absent(self):
        self.assertEqual(False, self._probe(urllib.error.HTTPError("u", 404, "no", {}, None)))

    def test_card_in_store_other_http_error_is_unknown(self):
        self.assertEqual(None, self._probe(urllib.error.HTTPError("u", 500, "boom", {}, None)))

    def test_card_in_store_connection_error_is_unknown(self):
        self.assertEqual(None, self._probe(urllib.error.URLError("down")))

    def test_card_in_store_200_is_present(self):
        self.assertEqual(True, self._probe(None, status=200))

    def test_card_in_store_off_switch_skips_the_probe(self):
        self.assertIsNone(self.mod.card_in_store(
            "7e55adaf" * 4, {self.mod.STORE_PROBE_ENV: "off"}))

    def _probe(self, raises, status=200):
        """`card_in_store` with `urlopen` replaced by a stub that raises or answers."""
        class _Resp:
            def __init__(self, status):
                self.status = status

            def __enter__(self):
                return self

            def __exit__(self, *_a):
                return False

        def fake(_request, timeout=None):
            if raises is not None:
                raise raises
            return _Resp(status)

        original = self.mod.urllib.request.urlopen
        self.mod.urllib.request.urlopen = fake
        try:
            return self.mod.card_in_store("7e55adaf" * 4, {})
        finally:
            self.mod.urllib.request.urlopen = original

    def test_a_bare_status_line_is_still_not_a_state_line(self):
        # ⚠️ The negative half of the pair above: without the `·` separator this is prose.
        code, _text, _record = self._closer(
            "you run: switch this tab to `accept edits`",
            prefix="🔴 BLOCKED\n")
        self.assertEqual(0, code)

    def test_prose_mentioning_a_blocked_row_is_not_a_state_line(self):
        code, _text, _record = self._closer(
            "nothing",
            prefix="The round rendered 3 rows: a 🔴 BLOCKED row for pane 809.\n")
        self.assertEqual(0, code)

    def test_handle_spelled_with_id_is_refused(self):
        for body in ("review: card id 7e55adaf — reply `ok`",
                     "review: ITEM_ID: 7e55adaf — reply `ok`",
                     "review: card `e5af` — reply `ok`"):
            with self.subTest(body=body):
                self.assertEqual(2, self._closer(body)[0])

    def test_an_all_digit_number_is_not_a_card_handle(self):
        self.assertEqual(0, self._closer("review: item 202611 in the queue — reply `ok`")[0])

    def test_a_commit_sha_is_not_a_card_handle(self):
        code, _text, _record = self._closer("review: commit 4af7bc13 on fix/x — reply `ok`")
        self.assertEqual(0, code)

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

    def test_gh_pr_merge_on_a_green_pr_is_allow_listed(self):
        # Operator decision 2026-10-30: a plain merge names work § After Opening a PR
        # already authorises, so it must not cost the operator a gate.
        _code, text, record = self.mod.evaluate(
            self.payload("09-gh-pr-merge.jsonl"), environ={})
        self.assertEqual("gh-pr-merge", record["matched"])
        self.assertIn("gh-pr-merge", text)

    def test_gh_pr_merge_admin_stays_gated(self):
        # ⚠️ The negative arm, and the one fixture this pair exists for: --admin
        # bypasses branch protection. It must never enter the allow-list, so the lint
        # allows the stop (exit 0) and the operator's gate stands. An exit 2 here is
        # the regression.
        code, text, record = self.mod.evaluate(
            self.payload("14-gh-pr-merge-admin.jsonl"), environ={})
        self.assertEqual(0, code)
        self.assertIsNone(record["matched"])
        self.assertEqual("not-allow-listed", record["reason"])
        self.assertEqual("", text)

    def test_vault_cli_session_close_is_allow_listed(self):
        # CLAUDE.md § Async State Closer: the session runs its own close, and offering
        # `approve: /vault-cli:session-close` is named there as the defect.
        _code, text, record = self.mod.evaluate(
            self.payload("13-vault-cli-session-close.jsonl"), environ={})
        self.assertEqual("vault-cli-session-close", record["matched"])
        self.assertIn("vault-cli-session-close", text)

    def test_admin_deny_survives_a_co_occurring_allow_entry(self):
        # ⚠️ The bypass the per-pattern lookahead could not close. `match_allow` returns
        # the FIRST match, so a body naming both session-close and an --admin merge was
        # released by the session-close entry. The deny is global for exactly this case.
        code, _text, record = self.mod.evaluate(
            self.payload("15-session-close-with-admin-merge.jsonl"), environ={})
        self.assertEqual(0, code)
        self.assertIsNone(record["matched"])
        self.assertEqual("not-allow-listed", record["reason"])

    def test_the_dropped_classes_stay_gated(self):
        # The other half of the 2026-10-30 decision: compaction and `git worktree remove`
        # were measured and deliberately left out, so each must still block nothing and
        # let the operator's gate stand.
        for name in ("16-compact-stays-gated.jsonl", "17-worktree-remove-stays-gated.jsonl"):
            with self.subTest(fixture=name):
                code, _text, record = self.mod.evaluate(self.payload(name), environ={})
                self.assertEqual(0, code)
                self.assertEqual("not-allow-listed", record["reason"])

    def test_a_partial_final_line_still_yields_the_turns_closer(self):
        # ⚠️ The measured defect (2026-10-31, session 95e9b174). The harness had not
        # finished writing the final message, so its line is unparseable; the extractor
        # skipped it and returned the PREVIOUS message's `👤 You: nothing`, recording
        # `not-a-gate` 266 ms after a real `gh pr merge` gate. A well-formed fixture
        # cannot reproduce this at any scan bound: `tail_lines` scans backwards from EOF,
        # so reaching the earlier message implies reaching the later one. Only a partial
        # final line reproduces it — measured, see the task's `# Results`.
        for name, matched in (
            ("18-partial-write-gh-pr-merge.jsonl", "gh-pr-merge"),
            ("19-partial-write-session-close.jsonl", "vault-cli-session-close"),
        ):
            with self.subTest(fixture=name):
                code, _text, record = self.mod.evaluate(self.payload(name), environ={})
                self.assertEqual(2, code)
                self.assertEqual("allow-listed", record["reason"])
                self.assertEqual(matched, record["matched"])

    def test_an_unreadable_final_line_never_falls_back_to_an_older_closer(self):
        # ⚠️ The `return None` branch. Without it the scan falls through to the earlier
        # `👤 You: nothing` and reports `not-a-gate` — the same stale-closer defect one
        # line further out. Both outcomes exit 0, so only the reason distinguishes them.
        _code, _text, record = self.mod.evaluate(
            self.payload("20-unreadable-assistant-no-closer.jsonl"), environ={})
        self.assertEqual("no-closer", record["reason"])
        self.assertIsNone(record["closer"])

    def test_a_partial_tool_result_quoting_a_closer_is_not_a_gate(self):
        # ⚠️ `_partial_closer` requires a `role`/`type` of `assistant`, so a tool result
        # that happens to quote a closer never becomes one — it falls through to the
        # earlier message instead.
        _code, _text, record = self.mod.evaluate(
            self.payload("21-partial-tool-result-quoting-a-closer.jsonl"), environ={})
        self.assertEqual("not-a-gate", record["reason"])
        self.assertIsNone(record["matched"])

    def test_a_quoted_subject_does_not_truncate_away_a_trailing_admin(self):
        # ⚠️ Cutting the salvaged segment at the first `"` stops at a JSON-ESCAPED quote,
        # so `--subject "Release 1.2" --admin --merge` truncated to `--subject \` and the
        # `--admin` deny at closer-gate-lint.py never fired — an admin merge released on
        # the salvage path, which is exactly what fixtures 14/15 exist to prevent.
        _code, _text, record = self.mod.evaluate(
            self.payload("22-partial-quoted-subject-before-admin.jsonl"), environ={})
        self.assertEqual("not-allow-listed", record["reason"])
        self.assertIsNone(record["matched"])
        self.assertIn("--admin", record["closer"])

    def test_a_closer_quoted_in_a_tool_use_input_is_not_salvaged(self):
        # ⚠️ The parsed path joins only `type == "text"` blocks, so the salvage must too —
        # a closer quoted in a `tool_use` input is not the operator's gate.
        _code, _text, record = self.mod.evaluate(
            self.payload("23-partial-tool-use-quoting-a-closer.jsonl"), environ={})
        self.assertEqual("no-closer", record["reason"])
        self.assertIsNone(record["closer"])

    def test_a_closer_cut_mid_command_is_not_recovered(self):
        # ⚠️ The write can stop INSIDE the closer. `--admi` is not `--admin`, so salvaging
        # it would let the deny miss and the lint release a branch-protection bypass. A
        # closer line that runs into an unterminated tail is therefore unrecoverable.
        _code, _text, record = self.mod.evaluate(
            self.payload("24-partial-cut-inside-the-closer.jsonl"), environ={})
        self.assertEqual("no-closer", record["reason"])
        self.assertIsNone(record["closer"])

    def test_a_literal_backslash_does_not_read_as_a_newline(self):
        # ⚠️ A literal backslash is `\\` in the raw body, so `C:\new` carries the same
        # `\n` pair as a real newline escape. Splitting on it truncated the closer before
        # `--admin`, so the deny never fired.
        _code, _text, record = self.mod.evaluate(
            self.payload("25-partial-literal-backslash-before-admin.jsonl"), environ={})
        self.assertEqual("not-allow-listed", record["reason"])
        self.assertIn("--admin", record["closer"])

    def test_the_message_never_calls_the_step_reversible(self):
        # ⚠️ A merge is irreversible. The old text said "authorise for a reversible,
        # in-scope, non-production step" AND "keep the line if the action is
        # irreversible" — self-contradictory on the exact closer the allow-list now
        # releases, so a careful model kept the gate and the operator still paid it.
        _code, text, _record = self.mod.evaluate(
            self.payload("09-gh-pr-merge.jsonl"), environ={})
        self.assertNotIn("reversible", text.lower())
        # The precondition replaces the escape hatch, and it names the merge's own rule.
        self.assertIn("bot-approved", text)
        self.assertIn("CI is green", text)

    def test_prod_deploy_stays_the_operators(self):
        _code, _text, record = self.mod.evaluate(
            self.payload("08-prod-buca.jsonl"), environ={})
        self.assertEqual("not-allow-listed", record["reason"])

    def test_stop_hook_active_allows_when_nothing_was_blocked(self):
        # The loop guard: without it a continued turn ending on the same closer re-blocks
        # forever. With no prior block on record the retry is allowed, exactly as before.
        code, _text, record = self.mod.evaluate(
            self.payload("01-github-pr-watch.jsonl", stop_hook_active=True),
            environ={self.mod.STATE_DIR_ENV: self.dir})
        self.assertEqual(0, code)
        self.assertEqual("stop-hook-active", record["reason"])

    def test_a_re_emitted_closer_is_blocked_once_more_then_allowed(self):
        # ⚠️ The SC2 fixture: a closer the lint blocked once must NOT be admitted on the
        # retry. The bound comes from the hook's own record, so the sequence is
        # block → blocked-retry → allow. Three asserts, because a fix that blocks the
        # retry but never releases it would loop, and that is the guard's whole purpose.
        env = {self.mod.STATE_DIR_ENV: self.dir}
        payload = self.payload("37-gh-pr-ready-with-repo-flag.jsonl")

        first = self.mod.evaluate(payload, environ=env)
        self.assertEqual(2, first[0])
        self.assertEqual("allow-listed", first[2]["reason"])
        self.mod.write_record(first[2], directory=self.dir)

        retry = self.mod.evaluate(dict(payload, stop_hook_active=True), environ=env)
        self.assertEqual(2, retry[0], "the retry must be blocked, not released")
        self.assertEqual("blocked-retry", retry[2]["reason"])
        self.assertIn("re-emitted unchanged", retry[1])
        self.mod.write_record(retry[2], directory=self.dir)

        third = self.mod.evaluate(dict(payload, stop_hook_active=True), environ=env)
        self.assertEqual(0, third[0], "a third identical stop is allowed — the guard is bounded")
        self.assertEqual("stop-hook-active", third[2]["reason"])

    def test_a_different_closer_on_the_retry_is_allowed(self):
        # The bound keys on the closer, not the session: a continued turn that ends on a
        # *different* gate is the guard's normal case and must not be re-blocked.
        env = {self.mod.STATE_DIR_ENV: self.dir}
        first = self.mod.evaluate(
            self.payload("37-gh-pr-ready-with-repo-flag.jsonl"), environ=env)
        self.mod.write_record(first[2], directory=self.dir)
        code, _text, record = self.mod.evaluate(
            self.payload("01-github-pr-watch.jsonl", stop_hook_active=True), environ=env)
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

    def test_each_session_bound_class_is_flagged_with_class_and_phrase(self):
        for name, cls in ENDS_POSITIVE.items():
            with self.subTest(fixture=name):
                code, text, record = self.mod.evaluate(self.payload(name), environ={})
                self.assertEqual(2, code)
                self.assertEqual("session-bound-ends", record["reason"])
                self.assertEqual("block", record["decision"])
                self.assertEqual(cls, record["class"])
                self.assertIn(record["phrase"], text)
                self.assertIn("must outlive the session", text)

    def test_pane_619_closer_is_the_regression(self):
        # ⚠️ The measured shape: `👤 You: nothing`, which the gate checks read as
        # `not-a-gate`. The `⏰ Ends:` check must run first or it never sees this line.
        _code, _text, record = self.mod.evaluate(
            self.payload("26-ends-background-agents-pane-619.jsonl"), environ={})
        self.assertEqual("chunk 7a and 7c completion notices (background agents)",
                         record["phrase"])
        self.assertIn("nothing", record["closer"])

    def test_a_durable_observable_clears_a_watcher_in_the_slot(self):
        # The operator's "observable only" scope: the watcher may stay in the slot.
        for name in ("31-ends-file-path.jsonl", "32-ends-card-id.jsonl",
                     "33-ends-defer-date.jsonl", "34-ends-external-probe.jsonl",
                     "35-ends-monitor-in-path.jsonl", "36-ends-pr-watch-compliant.jsonl",
                     "38-ends-iso-datetime.jsonl"):
            with self.subTest(fixture=name):
                code, text, record = self.mod.evaluate(self.payload(name), environ={})
                self.assertEqual(0, code)
                self.assertEqual("", text)
                self.assertIsNone(record["class"])
                self.assertIsNone(record["phrase"])

    def test_a_watcher_timeout_is_not_a_clock_observable(self):
        # ⚠️ `times out ~18:40` dies with the watcher; only defer_date / an ISO date count.
        self.assertEqual("background-sub-agent", self.mod.session_bound_class(
            "⏰ Ends:   chunk 7a done (background agent a1, times out ~18:40)"))

    def test_an_iso_datetime_is_a_clock_observable(self):
        # ⚠️ Three boundary regressions, each caught on its own review round: a trailing
        # `\b` failed between `9` and `T`, then between the final digit and a `Z`, and a
        # `(?![\w-])` terminator newly blocked `2026-11-09-15:30` and
        # `2026-11-09-rollout` — `-` is a valid `fromisoformat` separator, and blocking a
        # compliant closer is the exact false positive this class exists to remove.
        # CLAUDE.md § Async State Closer names a clock as legal, so every form clears.
        for body in (
            "⏰ Ends:   2026-11-09T15:30:00+01:00 (CronCreate j4f2 as early wake)",
            "⏰ Ends:   2026-11-09T15:30Z (CronCreate j4f2 as early wake)",
            "⏰ Ends:   2026-11-09T15:30:00Z (CronCreate j4f2 as early wake)",
            "⏰ Ends:   2026-11-09T00:00:00Z (CronCreate j4f2 as early wake)",
            "⏰ Ends:   2026-11-09T15:30:00.5+01:00 (CronCreate j4f2 as early wake)",
            "⏰ Ends:   2026-11-09T15 (CronCreate j4f2 as early wake)",
            "⏰ Ends:   2026-11-09t15:30:00 (CronCreate j4f2 as early wake)",
            "⏰ Ends:   2026-11-09T15:30:00z (CronCreate j4f2 as early wake)",
            "⏰ Ends:   2026-11-09-15:30 (CronCreate j4f2 as early wake)",
            "⏰ Ends:   2026-11-09-rollout (CronCreate j4f2 as early wake)",
            "⏰ Ends:   2026-11-09 15:30 (CronCreate j4f2 as early wake)",
            "⏰ Ends:   2026-11-09 (CronCreate j4f2 as early wake)",
            "⏰ Ends:   defer_date 2026-11-09 (CronCreate j4f2 as early wake)",
            # ⚠️ Malformed time suffix, still a durable clock: its `T15` prefix is a
            # parser-accepted hour, so this clears on the same fail-open rule that lets
            # `2026-11-09 15:30abc` clear via its bare date. Blocking it would mean
            # rejecting a real date+hour the writer plainly meant.
            "⏰ Ends:   2026-11-09T15:30abc (CronCreate j4f2 as early wake)",
        ):
            with self.subTest(body=body):
                self.assertIsNone(self.mod.session_bound_class(body))
        # The other half, and the guard against a permissive fix: a truncated date or a
        # trailing digit run is not a clock `parse_defer_date` accepts, so the
        # session-bound watcher still blocks.
        for body in (
            "⏰ Ends:   2026-11-9 (CronCreate j4f2 as early wake)",
            "⏰ Ends:   2026-11-09123 (CronCreate j4f2 as early wake)",
        ):
            with self.subTest(body=body):
                self.assertEqual("cron-create", self.mod.session_bound_class(body))

    def test_a_representative_sample_of_parser_accepted_clocks_clears_the_clock_class(self):
        # ⚠️ A SAMPLE, not the parser's full accepted set. `parse_defer_date` accepts an
        # unbounded family — `fromisoformat` takes any single separator character and any
        # fraction precision — so no literal list can be exhaustive and this test must not
        # claim parity. What it pins is that the two sides agree on every shape seen so
        # far, including the three that each cost a review round (`…T15:30`, `…T15:30Z`,
        # `…-15:30`). The assertion is two-sided by construction: each value must be
        # parser-accepted AND lint-cleared, so an entry the parser rejects fails here
        # rather than silently widening the gate.
        spec = importlib.util.spec_from_file_location(
            "defer_date", os.path.join(os.path.dirname(_HERE), "defer-date.py"))
        defer = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(defer)
        sample = (
            "2026-11-09",
            "2026-11-09T00:00:00Z",
            "2026-11-09T15",
            "2026-11-09T15:30",
            "2026-11-09T15:30Z",
            "2026-11-09T15:30:00Z",
            "2026-11-09T15:30:00z",
            "2026-11-09t15:30:00",
            "2026-11-09T15:30:00+01:00",
            "2026-11-09T15:30:00-05:00",
            "2026-11-09T15:30:00",
            "2026-11-09 15:30:00",
            "2026-11-09-15:30",
        )
        for value in sample:
            with self.subTest(value=value):
                _parsed, reason = defer.parse_defer_date(value)
                self.assertIsNone(reason, f"{value!r} is not parser-accepted — fix the sample")
                self.assertIsNone(
                    self.mod.session_bound_class(
                        f"⏰ Ends:   {value} (CronCreate j4f2 as early wake)"),
                    f"{value!r} is parser-accepted but the lint blocks it")

    def test_lowercase_monitor_in_a_path_is_not_the_monitor_class(self):
        self.assertIsNone(self.mod.session_bound_class(
            "⏰ Ends:   the monitor-check job reports done (polled 30s, b77)"))

    def test_ends_from_an_older_message_is_never_read(self):
        # ⚠️ The latest closer has no `⏰ Ends:`; an older WAITING message does. Reading
        # the older one would judge a wait the session already left.
        path = os.path.join(self.dir, "t.jsonl")
        older = {"type": "assistant", "message": {"content": [{"type": "text", "text":
                 "🟡 WAITING\n👤 You: nothing\n⏰ Ends:   notices (background agents)"}]}}
        newer = {"type": "assistant", "message": {"content": [{"type": "text", "text":
                 "🔵 READY\n👤 You: pick — 1. X (recommended) · 2. Y"}]}}
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(older) + "\n" + json.dumps(newer) + "\n")
        code, _text, record = self.mod.evaluate(
            {"transcript_path": path, "stop_hook_active": False, "session_id": "s"},
            environ={})
        self.assertEqual(0, code)
        self.assertIsNone(record["class"])

    def test_stop_hook_active_allows_a_session_bound_ends(self):
        code, _text, record = self.mod.evaluate(
            self.payload("26-ends-background-agents-pane-619.jsonl", stop_hook_active=True),
            environ={})
        self.assertEqual(0, code)
        self.assertEqual("stop-hook-active", record["reason"])

    def test_record_carries_class_and_phrase_on_allow_and_block(self):
        env = dict(os.environ)
        env[self.mod.STATE_DIR_ENV] = self.dir
        for name, expected in (("26-ends-background-agents-pane-619.jsonl", 2),
                               ("36-ends-pr-watch-compliant.jsonl", 0)):
            proc = subprocess.run(
                [sys.executable, _SCRIPT], input=json.dumps(self.payload(name)),
                capture_output=True, text=True, env=env, check=False)
            self.assertEqual(expected, proc.returncode)
        with open(os.path.join(self.dir, "test-session.jsonl"), encoding="utf-8") as fh:
            rows = [json.loads(line) for line in fh]
        self.assertEqual("background-sub-agent", rows[0]["class"])
        self.assertIn("background agents", rows[0]["phrase"])
        self.assertIn("class", rows[1])
        self.assertIsNone(rows[1]["class"])

    def test_cli_malformed_stdin_fails_open(self):
        env = dict(os.environ)
        env[self.mod.STATE_DIR_ENV] = self.dir
        proc = subprocess.run([sys.executable, _SCRIPT], input="not json",
                              capture_output=True, text=True, env=env, check=False)
        self.assertEqual(0, proc.returncode)


if __name__ == "__main__":
    unittest.main()
