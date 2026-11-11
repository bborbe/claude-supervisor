#!/usr/bin/env python3
"""Tests for scripts/wait-reprobe.py — the round's join of declared waits to a probe.

The properties worth pinning are the ones where a plausible implementation looks right
and is wrong:

  * **A resolved declaration is not silently dropped.** Every input record must produce
    a row; a join that skips the ones it cannot resolve makes the block look clean while
    sessions stay parked.
  * **`unprobeable` never reads as a clearance.** A missing repo, an unreadable
    transcript and a failed probe all keep the row where it was.
  * **The repo comes from the session's own `cwd`**, read from the FIRST transcript line
    only — reading a whole transcript per session would make one round cost the fleet's
    entire history.
  * **A refused reader is not an empty round.** Exit 2 with a reason, never a bare
    `(none)`.

Run: python3 -m unittest discover -s scripts/tests
"""

import importlib.util
import json
import os
import tempfile
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_SCRIPT = os.path.join(os.path.dirname(_HERE), "wait-reprobe.py")

_spec = importlib.util.spec_from_file_location("wait_reprobe", _SCRIPT)
wr = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(wr)

SID = "aaaa1111-0000-0000-0000-000000000001"


def _rec(ends, sid=SID, mechanism="external"):
    return {"session_id": sid, "ends": ends, "verdict": "declared", "mechanism": mechanism}


def _probe(payload):
    return lambda repo, number: payload


MERGED = {"state": "MERGED", "reviewDecision": "APPROVED", "mergeStateStatus": "UNKNOWN"}
OPEN = {"state": "OPEN", "reviewDecision": "REVIEW_REQUIRED", "mergeStateStatus": "BLOCKED"}


class Reprobe(unittest.TestCase):
    def test_every_record_produces_a_row(self):
        rows = wr.reprobe(
            [_rec("PR #1 merged"), _rec("PR #2 merged")],
            probe=_probe(MERGED), repo_for=lambda sid: "o/r",
        )
        self.assertEqual(len(rows), 2)

    def test_a_merged_pr_resumes(self):
        rows = wr.reprobe([_rec("PR #786")], probe=_probe(MERGED), repo_for=lambda s: "o/r")
        self.assertEqual(rows[0]["verdict"], "resumed")
        self.assertIn("MERGED", rows[0]["reason"])

    def test_an_open_pr_stays_blocked(self):
        rows = wr.reprobe([_rec("PR #786")], probe=_probe(OPEN), repo_for=lambda s: "o/r")
        self.assertEqual(rows[0]["verdict"], "still-blocked")

    def test_an_unresolvable_repo_is_unprobeable_not_resumed(self):
        rows = wr.reprobe([_rec("PR #786")], probe=_probe(MERGED), repo_for=lambda s: None)
        self.assertEqual(rows[0]["verdict"], "unprobeable")

    def test_a_failed_probe_is_unprobeable_not_resumed(self):
        def boom(repo, number):
            raise RuntimeError("gh: boom")
        rows = wr.reprobe([_rec("PR #786")], probe=boom, repo_for=lambda s: "o/r")
        self.assertEqual(rows[0]["verdict"], "unprobeable")

    def test_a_clock_declaration_is_unprobeable(self):
        rows = wr.reprobe([_rec("by ~00:03")], probe=_probe(MERGED), repo_for=lambda s: "o/r")
        self.assertEqual(rows[0]["verdict"], "unprobeable")

    def test_a_qualified_declaration_needs_no_repo_resolution(self):
        # Cross-repo waits are written qualified; they must probe even when the session's
        # own repo cannot be resolved.
        rows = wr.reprobe([_rec("bborbe/nuke#939")], probe=_probe(MERGED), repo_for=lambda s: None)
        self.assertEqual(rows[0]["verdict"], "resumed")
        self.assertIn("bborbe/nuke#939", rows[0]["reason"])


class Render(unittest.TestCase):
    def test_block_lists_each_row_and_counts(self):
        rows = [
            {"session_id": SID, "ends": "PR #1", "verdict": "resumed", "reason": "o/r#1 MERGED"},
            {"session_id": "bbbb2222-x", "ends": "PR #2", "verdict": "still-blocked", "reason": "o/r#2 OPEN"},
            {"session_id": "cccc3333-x", "ends": "clock", "verdict": "unprobeable", "reason": "no PR"},
        ]
        block = wr.render_block(rows)
        self.assertIn("REPROBED", block)
        self.assertIn("[aaaa1111]", block)
        self.assertIn("1 resumed · 1 still blocked · 1 unprobeable", block)

    def test_empty_renders_none_not_a_blank(self):
        self.assertIn("(none)", wr.render_block([]))


class Cwd(unittest.TestCase):
    def test_reads_cwd_from_the_first_line(self):
        with tempfile.TemporaryDirectory() as root:
            proj = os.path.join(root, "proj")
            os.makedirs(proj)
            with open(os.path.join(proj, f"{SID}.jsonl"), "w", encoding="utf-8") as f:
                f.write(json.dumps({"cwd": "/tmp/somewhere"}) + "\n")
                f.write("{ this line is never parsed\n")
            self.assertEqual(wr.session_cwd(SID, projects_root=root), "/tmp/somewhere")

    def test_missing_transcript_is_none(self):
        with tempfile.TemporaryDirectory() as root:
            self.assertIsNone(wr.session_cwd(SID, projects_root=root))

    def test_a_sid_is_not_a_path(self):
        self.assertIsNone(wr.session_cwd("../../etc/passwd", projects_root="/tmp"))


class Origin(unittest.TestCase):
    def test_ssh_origin(self):
        self.assertEqual(wr._ORIGIN.search("git@github.com:bborbe/nuke.git").group(1, 2), ("bborbe", "nuke"))

    def test_https_origin(self):
        self.assertEqual(
            wr._ORIGIN.search("https://github.com/bborbe/claude-supervisor").group(1, 2),
            ("bborbe", "claude-supervisor"),
        )

    def test_a_missing_cwd_is_none(self):
        self.assertIsNone(wr.repo_for_cwd(None))
        self.assertIsNone(wr.repo_for_cwd("/definitely/not/a/dir"))


if __name__ == "__main__":
    unittest.main()
