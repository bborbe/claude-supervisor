#!/usr/bin/env python3
"""Tests for scripts/rebase-agent.py.

The cases are the four ways this agent can be wrong in a way that looks like success:

  * a branch carrying a MERGE COMMIT must come out LINEAR. This is the negative control
    for the whole script: `git merge origin/master` produces exactly that merge commit and
    every visible PR check stays green, so a rebase that leaves one behind would report
    success on the state the goal exists to prevent.
  * a CHANGELOG-only conflict is resolved mechanically and passes the probes — the
    mechanical class the agent exists to clear.
  * a conflict in any other file is REFUSED, never resolved. Guessing at a content
    conflict is how an agent silently picks one side of a real decision.
  * the resolution never produces a SECOND `## Unreleased` heading. Two sections read as
    one to the heading probes, so the duplicate has to be caught on its own.
  * a bullet the branch adds is placed under `## Unreleased`, not under the released
    section a pre-cut branch still carries.

Each end-to-end case builds a throwaway repo, so nothing here touches a real one.

Run: python3 -m unittest discover -s scripts/tests -v
"""

import contextlib
import importlib.util
import io
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

_HERE = os.path.dirname(os.path.abspath(__file__))
_SCRIPT = os.path.join(os.path.dirname(_HERE), "rebase-agent.py")

_spec = importlib.util.spec_from_file_location("rebase_agent", _SCRIPT)
ra = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ra)


def _isolated_env(extra=None):
    """A developer's global git config must not decide what these tests measure."""
    env = dict(
        os.environ,
        GIT_CONFIG_GLOBAL=os.devnull,
        GIT_CONFIG_SYSTEM=os.devnull,
        GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.com",
        GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@example.com",
    )
    if extra:
        env.update(extra)
    return env


def git(repo, *args, env=None):
    proc = subprocess.run(
        ["git", "-C", repo, *args], capture_output=True, text=True,
        env=_isolated_env(env),
    )
    if proc.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed: {proc.stderr}")
    return proc.stdout


def write(repo, name, text):
    path = os.path.join(repo, name)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)


def commit(repo, message):
    env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.com",
               GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@example.com")
    git(repo, "add", "-A")
    git(repo, "commit", "-m", message, env=env)


def make_repo():
    """A repo with `origin/master` and a branch, no network involved."""
    root = tempfile.mkdtemp(prefix="rebase-agent-test-")
    git(root, "init", "-q", "-b", "master")
    git(root, "config", "user.email", "t@example.com")
    git(root, "config", "user.name", "t")
    write(root, "CHANGELOG.md",
          "# Changelog\n\n## Unreleased\n\n- fix: base bullet\n\n"
          "## v1.0.0\n\n- fix: released bullet\n")
    write(root, "code.py", "value = 1\n")
    commit(root, "base")
    git(root, "update-ref", "refs/remotes/origin/master", "HEAD")
    return root


def add_branch_commit(repo, message, files):
    for name, text in files.items():
        write(repo, name, text)
    commit(repo, message)
    return git(repo, "rev-parse", "HEAD").strip()


class InsertBulletsTest(unittest.TestCase):
    def test_creates_unreleased_above_the_top_released_heading(self):
        text = "# Changelog\n\n## v1.0.0\n\n- fix: shipped\n"
        out = ra.insert_bullets(text, ["- fix: mine"])
        self.assertIn("## Unreleased", out)
        self.assertLess(out.index("## Unreleased"), out.index("## v1.0.0"))
        self.assertLess(out.index("- fix: mine"), out.index("## v1.0.0"))

    def test_appends_to_an_existing_unreleased_rather_than_duplicating_it(self):
        text = "# Changelog\n\n## Unreleased\n\n- fix: theirs\n\n## v1.0.0\n\n- fix: shipped\n"
        out = ra.insert_bullets(text, ["- fix: mine"])
        self.assertEqual(out.count("## Unreleased"), 1)
        self.assertIn("- fix: theirs", out)
        self.assertIn("- fix: mine", out)
        self.assertLess(out.index("- fix: mine"), out.index("## v1.0.0"))

    def test_never_repeats_a_bullet_already_present(self):
        text = "# Changelog\n\n## Unreleased\n\n- fix: mine\n"
        out = ra.insert_bullets(text, ["- fix: mine"])
        self.assertEqual(out.count("- fix: mine"), 1)

    def test_refuses_a_changelog_with_no_section_heading(self):
        with self.assertRaises(ra.Refusal):
            ra.insert_bullets("# Changelog\n\nnothing here\n", ["- fix: mine"])


class ClassifyTest(unittest.TestCase):
    def test_behind_and_dirty_are_candidates(self):
        self.assertTrue(ra.needs_rebase({"mergeStateStatus": "BEHIND"}))
        self.assertTrue(ra.needs_rebase({"mergeStateStatus": "DIRTY"}))
        self.assertTrue(ra.needs_rebase({"mergeable": "CONFLICTING"}))

    def test_clean_and_blocked_are_not(self):
        # BLOCKED is a PR waiting on review, not a conflict — reading it as one tells the
        # operator to rebase and invalidates the review they are waiting for.
        self.assertFalse(ra.needs_rebase({"mergeStateStatus": "CLEAN"}))
        self.assertFalse(ra.needs_rebase({"mergeStateStatus": "BLOCKED",
                                          "mergeable": "MERGEABLE"}))

    def test_prod_pin_path_is_refused(self):
        reason = ra.prod_pin_reason({"title": "bump", "headRefName": "feat/x"},
                                    ["k8s/values-prod.yaml"])
        self.assertIn("values-prod.yaml", reason)

    def test_prod_pin_branch_is_refused(self):
        self.assertIsNotNone(ra.prod_pin_reason(
            {"title": "bump", "headRefName": "feat/prod-pin-nuke"}, []))

    def test_ordinary_path_is_not_a_prod_pin(self):
        self.assertIsNone(ra.prod_pin_reason(
            {"title": "docs", "headRefName": "docs/x"},
            ["docs/production-guide.md", "scripts/x.py"]))


class SingleUnreleasedGateTest(unittest.TestCase):
    def _dir(self, text):
        path = tempfile.mkdtemp(prefix="rebase-agent-test-")
        self.addCleanup(shutil.rmtree, path, True)
        write(path, "CHANGELOG.md", text)
        return path

    def test_a_duplicate_heading_is_caught(self):
        path = self._dir("# Changelog\n\n## Unreleased\n\n- fix: a\n\n"
                         "## Unreleased\n\n- fix: b\n")
        self.assertIn("2", ra.gate_single_unreleased(path))

    def test_exactly_one_heading_passes(self):
        path = self._dir("# Changelog\n\n## Unreleased\n\n- fix: a\n")
        self.assertIsNone(ra.gate_single_unreleased(path))


class PushLineTest(unittest.TestCase):
    def test_a_hostile_branch_name_stays_one_shell_word(self):
        """The line is pasted into a shell; a branch name must never split or execute."""
        line = ra.push_line("/tmp/r", "feat/x; rm -rf ~", "abc123", 7)
        self.assertEqual(shlex.split(line), [
            "git", "-C", "/tmp/r", "push",
            "--force-with-lease=refs/heads/feat/x; rm -rf ~:abc123",
            "origin", "refs/rebase-agent/pr/7:refs/heads/feat/x; rm -rf ~",
        ])

    def test_an_ordinary_branch_name_reads_normally(self):
        line = ra.push_line("/tmp/r", "feat/plain", "abc123", 7)
        self.assertEqual(
            line,
            "git -C /tmp/r push --force-with-lease=refs/heads/feat/plain:abc123 "
            "origin refs/rebase-agent/pr/7:refs/heads/feat/plain",
        )


class ChangelogDeltaTest(unittest.TestCase):
    def test_an_unreadable_delta_is_refused_not_returned_empty(self):
        """Empty bullets would resolve the conflict to master's side and drop the branch's."""
        repo = tempfile.mkdtemp(prefix="rebase-agent-test-")
        self.addCleanup(shutil.rmtree, repo, True)
        git(repo, "init", "-q", "-b", "master")
        git(repo, "config", "user.email", "t@example.com")
        git(repo, "config", "user.name", "t")
        write(repo, "CHANGELOG.md", "# Changelog\n\n## Unreleased\n\n- fix: base\n")
        commit(repo, "base")
        with self.assertRaises(ra.Refusal):
            ra.changelog_delta(repo, "0000000000000000000000000000000000000000",
                               "1111111111111111111111111111111111111111")


class RebaseEndToEndTest(unittest.TestCase):
    def setUp(self):
        self.repo = make_repo()
        self.master = git(self.repo, "rev-parse", "HEAD").strip()
        # Registered FIRST so it runs LAST: the scratch worktrees are children of this
        # repo, and removing the repo first leaves `git worktree remove` with nothing to
        # change into.
        self.addCleanup(shutil.rmtree, self.repo, True)

    def _scratch(self, sha):
        scratch = tempfile.mkdtemp(prefix="rebase-agent-scratch-")
        git(self.repo, "worktree", "add", "--detach", scratch, sha)
        self.addCleanup(
            lambda: subprocess.run(
                ["git", "-C", self.repo, "worktree", "remove", "--force", scratch],
                check=False, capture_output=True,
            )
        )
        return scratch

    def test_a_merge_commit_comes_out_linear(self):
        """The #652 negative control: a merge commit in, zero out."""
        git(self.repo, "checkout", "-q", "-b", "feat/x", self.master)
        write(self.repo, "code.py", "value = 2\n")
        commit(self.repo, "feat: change")
        git(self.repo, "checkout", "-q", "master")
        write(self.repo, "other.py", "other = 1\n")
        commit(self.repo, "master moves")
        git(self.repo, "update-ref", "refs/remotes/origin/master", "HEAD")
        git(self.repo, "checkout", "-q", "feat/x")
        git(self.repo, "merge", "-q", "--no-ff", "-m", "sync with master", "master")
        head = git(self.repo, "rev-parse", "HEAD").strip()
        before = git(self.repo, "log", "--merges", "--oneline",
                     f"refs/remotes/origin/master..{head}").strip().splitlines()
        self.assertEqual(len(before), 1, "fixture must start with one merge commit")

        scratch = self._scratch(head)
        ra.rebase_onto_master(scratch, self.repo, head)

        after = git(scratch, "log", "--merges", "--oneline",
                    "origin/master..HEAD").strip().splitlines()
        self.assertEqual(after, [])
        self.assertIsNone(ra.gate_linear(scratch))

    def test_changelog_only_conflict_resolves_and_passes_every_probe(self):
        git(self.repo, "checkout", "-q", "-b", "feat/cl", self.master)
        head = add_branch_commit(self.repo, "feat: mine", {
            "CHANGELOG.md": "# Changelog\n\n## Unreleased\n\n- fix: base bullet\n"
                            "- fix: mine\n\n## v1.0.0\n\n- fix: released bullet\n",
        })
        # A release cut on master that also adds a bullet in the SAME hunk, so the
        # rebase stops on a real conflict rather than auto-merging the bullet into the
        # released section (which is the fold, and is covered by its own test).
        git(self.repo, "checkout", "-q", "master")
        write(self.repo, "CHANGELOG.md",
              "# Changelog\n\n## v1.1.0\n\n- fix: base bullet\n- fix: master bullet\n\n"
              "## v1.0.0\n\n- fix: released bullet\n")
        commit(self.repo, "release v1.1.0")
        git(self.repo, "update-ref", "refs/remotes/origin/master", "HEAD")

        scratch = self._scratch(head)
        base = ra.rebase_onto_master(scratch, self.repo, head)
        bullets, nonbullet = ra.changelog_delta(self.repo, base, head)
        self.assertFalse(nonbullet, "the branch only adds bullets")
        self.assertIn("- fix: mine", bullets)

        self.assertIsNone(ra.gate_linear(scratch))
        self.assertIsNone(ra.gate_heading_guard(scratch))
        self.assertIsNone(ra.gate_dropped_bullet(scratch))
        self.assertIsNone(ra.gate_placement(scratch, bullets))
        self.assertIsNone(ra.gate_single_unreleased(scratch))

    def test_a_real_content_conflict_is_refused_never_resolved(self):
        git(self.repo, "checkout", "-q", "-b", "feat/conflict", self.master)
        add_branch_commit(self.repo, "feat: mine", {"code.py": "value = 2\n"})
        git(self.repo, "checkout", "-q", "master")
        write(self.repo, "code.py", "value = 3\n")
        commit(self.repo, "master moves")
        git(self.repo, "update-ref", "refs/remotes/origin/master", "HEAD")
        head = git(self.repo, "rev-parse", "feat/conflict").strip()

        scratch = self._scratch(head)
        with self.assertRaises(ra.Refusal) as caught:
            ra.rebase_onto_master(scratch, self.repo, head)
        self.assertIn("code.py", str(caught.exception))
        self.assertIn("content conflict", str(caught.exception))

    def test_a_clean_rebase_that_folds_a_bullet_is_still_refused(self):
        """No conflict at all — git silently splices the bullet into the released section.

        This is the case the gates exist for: the rebase succeeds, every visible signal
        is green, and only the placement assertion can see the fold.
        """
        git(self.repo, "checkout", "-q", "-b", "feat/fold", self.master)
        head = add_branch_commit(self.repo, "feat: mine", {
            "CHANGELOG.md": "# Changelog\n\n## Unreleased\n\n- fix: base bullet\n"
                            "- fix: mine\n\n## v1.0.0\n\n- fix: released bullet\n",
        })
        git(self.repo, "checkout", "-q", "master")
        write(self.repo, "CHANGELOG.md",
              "# Changelog\n\n## v1.1.0\n\n- fix: base bullet\n\n"
              "## v1.0.0\n\n- fix: released bullet\n")
        commit(self.repo, "release v1.1.0")
        git(self.repo, "update-ref", "refs/remotes/origin/master", "HEAD")

        scratch = self._scratch(head)
        base = ra.rebase_onto_master(scratch, self.repo, head)
        bullets, _ = ra.changelog_delta(self.repo, base, head)
        placement = ra.gate_placement(scratch, bullets)
        self.assertIsNotNone(placement, "a bullet under a released heading must fail")
        self.assertIn("v1.1.0", placement)


class RunRecordTest(unittest.TestCase):
    """The record is the only thing a CronJob pod leaves behind.

    The state directory is an emptyDir in the cluster, so a run whose record exists only
    there is a run nothing can read afterwards. These cases pin the two halves that make
    it durable and honest: the trigger travels with the payload, and the printed line is
    a single parseable line naming every row.
    """

    def _dir(self):
        path = tempfile.mkdtemp(prefix="rebase-agent-test-")
        self.addCleanup(shutil.rmtree, path, True)
        return path

    def test_the_payload_carries_the_trigger(self):
        rows = [{"repo": "bborbe/x", "pr": 7, "branch": "feat/a", "verdict": "PUSH",
                 "reason": None, "behind": 2, "push_line": "git -C /r push"}]
        path, payload = ra.write_state(rows, [], "schedule", state_dir=self._dir())
        self.assertEqual(payload["trigger"], "schedule")
        self.assertTrue(os.path.exists(path))
        with open(os.path.join(os.path.dirname(path), "latest.json"),
                  encoding="utf-8") as handle:
            self.assertEqual(json.load(handle)["trigger"], "schedule")

    def test_the_record_line_is_one_line_json_naming_every_row(self):
        rows = [{"repo": "bborbe/x", "pr": 7, "branch": "feat/a", "verdict": "PUSH",
                 "reason": None, "behind": 2, "push_line": "line"}]
        payload = {"generated_at": "2026-11-13T00:00:00+00:00", "trigger": "schedule",
                   "errors": [], "rows": rows}
        line = ra.record_line(payload, "/state/2026.json")
        self.assertTrue(line.startswith(ra.RECORD_PREFIX))
        self.assertNotIn("\n", line)
        parsed = json.loads(line[len(ra.RECORD_PREFIX):])
        self.assertEqual(parsed["trigger"], "schedule")
        self.assertEqual(parsed["state"], "/state/2026.json")
        self.assertEqual(parsed["rows"][0]["pr"], 7)
        self.assertEqual(parsed["rows"][0]["push_line"], "line")

    def test_a_hand_started_run_records_manual(self):
        """A hand-started run must never be readable as a scheduled one."""
        self.assertEqual(ra.build_parser().parse_args([]).trigger, "manual")
        self.assertEqual(
            ra.build_parser().parse_args(["--trigger", "schedule"]).trigger, "schedule")


class RepoSetTest(unittest.TestCase):
    """A --repo outside DEFAULT_REPOS must never read as a clean queue.

    `cfgs` is filtered by the argument, so an out-of-set repo empties the list and the
    old code printed the clean-queue line — "nothing is behind" for a repo it never
    looked at. Measured 2026-11-13 on bborbe/git-rest#81 and
    bborbe/agent-task-executor#161, both CONFLICTING and both reported clean.
    """

    def test_out_of_set_repo_is_named_and_never_reported_clean(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = ra.main(["--repo", "bborbe/not-in-the-set",
                          "--state-dir", tempfile.mkdtemp()])
        out = buf.getvalue()
        self.assertNotIn(
            "no open PR is behind master", out,
            "an unexamined repo must never print the clean-queue line",
        )
        self.assertIn("repo not in the rebase set", out)
        self.assertIn("bborbe/not-in-the-set", out)
        self.assertEqual(2, rc, "an unexamined repo is not a success")

    def test_empty_queue_message_still_used_when_nothing_is_wrong(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            ra.render([])
        self.assertIn("no open PR is behind master", buf.getvalue())


class BoardTest(unittest.TestCase):
    """A push line is the operator's to run, so a record nobody reads is not delivery.

    The declarations are the load-bearing half, not the transport: `owner:` liveness is
    what lets the store's duplicate suppression collapse a re-sweep onto one card (a pod
    is never resolvable as live under `session:` or `heartbeat:`), and `expires_at` is the
    only thing that ever removes that card again. Getting either wrong produces a board
    that floods every 15 minutes or one that never clears.
    """

    ROW = {
        "repo": "bborbe/claude-supervisor",
        "pr": 7,
        "branch": "feat/x",
        "verdict": "PUSH",
        "push_line": "git -C /r push --force-with-lease=refs/heads/feat/x:a origin b:refs/heads/feat/x",
    }
    AT = "2026-11-13T20:00:00+00:00"

    def test_dedup_key_is_per_pr_so_a_resweep_refreshes_one_card(self):
        body = ra.board_body(self.ROW, self.AT)
        self.assertEqual(body["dedup_key"], "rebase-agent:bborbe/claude-supervisor#7")

    def test_the_payload_carries_the_push_line_verbatim(self):
        """The task's criterion reads the card for this exact line, so it must be in it."""
        body = ra.board_body(self.ROW, self.AT)
        self.assertIn(self.ROW["push_line"], body["payload"])
        self.assertIn("bborbe/claude-supervisor#7", body["payload"])

    def test_the_card_declares_owner_liveness_and_carries_its_only_bound(self):
        body = ra.board_body(self.ROW, self.AT)
        self.assertEqual(body["liveness_ref"], "owner:operator")
        self.assertEqual(body["expires_at"], self.AT)

    def test_the_card_is_an_ack_so_nothing_routes_back_to_the_pod(self):
        body = ra.board_body(self.ROW, self.AT)
        self.assertEqual(body["answer_mechanism"], "ack")
        self.assertEqual(body["producer_kind"], "cron")
        self.assertEqual(body["provenance_class"], "explicit")

    def test_a_remote_store_without_a_token_is_refused(self):
        reason = ra.board_refusal(
            "http://attention-controller.agent.svc.cluster.local:18081", "")
        self.assertIsNotNone(reason)
        self.assertIn("REFUSED", reason)

    def test_a_loopback_store_needs_no_token(self):
        self.assertIsNone(ra.board_refusal("http://127.0.0.1:18081", ""))

    def test_an_unset_store_means_no_post_at_all(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(ra.board_store(), "")
            item_id, error = ra.post_push_line(self.ROW)
        self.assertIsNone(item_id)
        self.assertIn("unset", error)

    def test_post_sends_the_body_and_returns_the_item_id(self):
        seen = {}

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802 — the stdlib's spelling
                length = int(self.headers.get("Content-Length") or 0)
                seen["path"] = self.path
                seen["body"] = json.loads(self.rfile.read(length) or b"{}")
                seen["auth"] = self.headers.get("Authorization")
                payload = json.dumps({"item_id": "abc123"}).encode()
                self.send_response(201)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *args):  # silence the per-request stderr line
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        url = f"http://127.0.0.1:{server.server_address[1]}"
        with mock.patch.dict(
            os.environ,
            {ra.BOARD_STORE_ENV: url, ra.BOARD_TOKEN_ENV: "secret"},
            clear=False,
        ):
            item_id, error = ra.post_push_line(self.ROW)
        self.assertIsNone(error)
        self.assertEqual(item_id, "abc123")
        self.assertEqual(seen["path"], "/api/1.0/attention")
        self.assertEqual(seen["auth"], "Bearer secret")
        self.assertIn(self.ROW["push_line"], seen["body"]["payload"])
        self.assertEqual(seen["body"]["dedup_key"],
                         "rebase-agent:bborbe/claude-supervisor#7")

    def test_a_failing_store_returns_an_error_and_never_raises(self):
        """A board that is down must not turn a rebase verdict into an ERROR."""
        with mock.patch.dict(os.environ, {ra.BOARD_STORE_ENV: "http://127.0.0.1:1"},
                             clear=False):
            item_id, error = ra.post_push_line(self.ROW)
        self.assertIsNone(item_id)
        self.assertIn("unreachable", error)

    # ---- the wiring: the post must never reach process_pr's verdict ----------------

    def test_a_successful_post_records_the_item_id_on_the_row(self):
        row = dict(self.ROW, board_item_id=None, board_error=None)
        with mock.patch.object(ra, "post_push_line", return_value=("abc123", None)):
            ra.board_post_row(row)
        self.assertEqual(row["board_item_id"], "abc123")
        self.assertIsNone(row["board_error"])

    def test_a_raising_post_leaves_the_verdict_alone(self):
        """The load-bearing claim: the post sits outside process_pr's try."""
        row = dict(self.ROW, board_item_id=None, board_error=None)
        with mock.patch.object(ra, "post_push_line", side_effect=RuntimeError("boom")):
            ra.board_post_row(row)
        self.assertEqual(row["verdict"], "PUSH")
        self.assertIsNone(row["board_item_id"])
        self.assertIn("boom", row["board_error"])

    def test_render_prints_the_board_item_id_and_any_board_error(self):
        row = dict(self.ROW, reason=None, behind=2, board_item_id="abc123")
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            ra.render([row])
        self.assertIn("board: abc123", buf.getvalue())

        failed = dict(self.ROW, reason=None, behind=2, board_item_id=None,
                      board_error="board unreachable at http://x")
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            ra.render([failed])
        self.assertIn("board: board unreachable at http://x", buf.getvalue())


if __name__ == "__main__":
    unittest.main()
