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

    def _closer(self, text):
        path = os.path.join(self.dir, "t.jsonl")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(json.dumps({"type": "assistant", "timestamp": "2026-11-05T20:00:00Z",
                                 "message": {"role": "assistant", "content": [
                                     {"type": "text", "text": "done\n👤 You: " + text}]}}) + "\n")
        return self.mod.evaluate({"transcript_path": path, "stop_hook_active": False,
                                  "session_id": "test-session"}, environ={})

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
        code, text, _record = self._closer("review: card " + "7e55adaf" * 4 + " — reply `seen`")
        self.assertEqual(0, code)
        self.assertEqual("", text)

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
        # ⚠️ The trailing `\b` after `\d{2}` cannot match between `9` and `T` — both are
        # word characters — so `2026-11-09T15:30:00+01:00`, which CLAUDE.md § Async State
        # Closer names as a legal clock ("a clock (`defer_date` / timestamp)"), did not
        # match and the closer was blocked. Two-sided on purpose: the datetime arm is
        # optional, so the date-only and `defer_date` forms must keep matching.
        for body in (
            "⏰ Ends:   2026-11-09T15:30:00+01:00 (CronCreate j4f2 as early wake)",
            "⏰ Ends:   2026-11-09 15:30 (CronCreate j4f2 as early wake)",
            "⏰ Ends:   2026-11-09 (CronCreate j4f2 as early wake)",
            "⏰ Ends:   defer_date 2026-11-09 (CronCreate j4f2 as early wake)",
        ):
            with self.subTest(body=body):
                self.assertIsNone(self.mod.session_bound_class(body))
        # The other half, and the guard against a permissive fix: `2026-11-9` is not a
        # date `defer-date.py`'s `parse_defer_date` accepts, so the clock class must NOT
        # bless it and the session-bound watcher still blocks. This keeps the class a
        # subset of the parser — the lint must never release a clock every fail-closed
        # reader rejects as invalid.
        self.assertEqual("cron-create", self.mod.session_bound_class(
            "⏰ Ends:   2026-11-9 (CronCreate j4f2 as early wake)"))

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
