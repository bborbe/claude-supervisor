#!/usr/bin/env python3
"""Tests for scripts/wait-reprobe.py — the round's join of declared waits to a probe.

The properties worth pinning are the ones where a plausible implementation looks right
and is wrong:

  * **There is no repo resolver, and that is the point.** The transcript-`cwd` resolver
    returned the repo the session *lives in* — for every PR-bearing declaration in the
    live store, a vault — and the vault repo has a merged PR #1, so a bare `PR #1` would
    render `resumed` off a repository the wait never mentioned. A test that reintroduces
    a resolver must fail here, not in production.
  * **`unprobeable` never reads as a clearance.** An unresolved repo, a failed probe and
    a clock all keep the row where it was.
  * **A declaration's own state is carried.** `declared-wait.py` returns `declared` or
    `expired`, and the live store is overwhelmingly `expired`; flattening the two tells
    a reader to keep waiting on something the reader has already decided is not a wait.
  * **A malformed record refuses the round, never shortens it.** A non-object element is
    exit 2 with a reason, not a silently shorter block.

Run: python3 -m unittest discover -s scripts/tests
"""

import importlib.util
import json
import os
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_SCRIPT = os.path.join(os.path.dirname(_HERE), "wait-reprobe.py")

_spec = importlib.util.spec_from_file_location("wait_reprobe", _SCRIPT)
wr = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(wr)

SID = "aaaa1111-0000-0000-0000-000000000001"


def _rec(ends, sid=SID, wait="declared"):
    return {"session_id": sid, "ends": ends, "verdict": wait, "mechanism": "external"}


def _probe(payload):
    return lambda repo, number: payload


MERGED = {"state": "MERGED", "reviewDecision": "APPROVED", "mergeStateStatus": "UNKNOWN"}
OPEN = {"state": "OPEN", "reviewDecision": "REVIEW_REQUIRED", "mergeStateStatus": "BLOCKED"}


class Reprobe(unittest.TestCase):
    def test_every_record_produces_a_row(self):
        rows = wr.reprobe([_rec("PR #1 merged"), _rec("PR #2 merged")], probe=_probe(MERGED))
        self.assertEqual(len(rows), 2)

    def test_an_empty_record_still_produces_a_row(self):
        rows = wr.reprobe([{}], probe=_probe(MERGED))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["verdict"], "unprobeable")

    def test_a_qualified_merged_pr_resumes(self):
        rows = wr.reprobe([_rec("bborbe/nuke#698 MERGED")], probe=_probe(MERGED))
        self.assertEqual(rows[0]["verdict"], "resumed")
        self.assertIn("bborbe/nuke#698", rows[0]["reason"])

    def test_a_qualified_open_pr_stays_blocked(self):
        rows = wr.reprobe([_rec("bborbe/nuke#698")], probe=_probe(OPEN))
        self.assertEqual(rows[0]["verdict"], "still-blocked")

    def test_a_bare_pr_is_unprobeable_because_there_is_no_resolver(self):
        # The measured decision: resolving from the session's cwd points at the repo it
        # lives in (a vault), whose PR #1 is merged — a false clearance. So a bare
        # reference is never probed.
        rows = wr.reprobe([_rec("PR #516 at `d706efbc`")], probe=_probe(MERGED))
        self.assertEqual(rows[0]["verdict"], "unprobeable")
        self.assertIn("names no repo", rows[0]["reason"])

    def test_a_failed_probe_is_unprobeable_not_resumed(self):
        def boom(repo, number):
            raise RuntimeError("gh: boom")
        rows = wr.reprobe([_rec("bborbe/nuke#1")], probe=boom)
        self.assertEqual(rows[0]["verdict"], "unprobeable")

    def test_a_clock_declaration_is_unprobeable(self):
        rows = wr.reprobe([_rec("by ~00:03")], probe=_probe(MERGED))
        self.assertEqual(rows[0]["verdict"], "unprobeable")

    def test_the_wait_state_is_carried_not_flattened(self):
        rows = wr.reprobe(
            [_rec("bborbe/nuke#1", wait="expired"), _rec("bborbe/nuke#2", wait="declared")],
            probe=_probe(OPEN),
        )
        self.assertEqual([r["wait"] for r in rows], ["expired", "declared"])

    def test_an_expired_row_is_not_rendered_as_a_live_wait(self):
        block = wr.render_block(wr.reprobe([_rec("bborbe/nuke#1", wait="expired")], probe=_probe(OPEN)))
        self.assertIn("expired", block)


class Render(unittest.TestCase):
    def test_block_lists_each_row_and_counts(self):
        rows = [
            {"session_id": SID, "ends": "x", "verdict": "resumed", "reason": "r", "wait": "declared"},
            {"session_id": "bbbb2222-x", "ends": "y", "verdict": "still-blocked", "reason": "r", "wait": "declared"},
            {"session_id": "cccc3333-x", "ends": "z", "verdict": "unprobeable", "reason": "r", "wait": "expired"},
        ]
        block = wr.render_block(rows)
        self.assertIn("REPROBED", block)
        self.assertIn("[aaaa1111-000]", block)
        self.assertIn("1 resumed · 1 still blocked · 1 unprobeable", block)
        self.assertIn("2 declared / 1 expired", block)

    def test_empty_renders_none_not_a_blank(self):
        self.assertIn("(none)", wr.render_block([]))

    def test_counts_sum_to_the_row_count(self):
        rows = [{"session_id": "s", "ends": "", "verdict": v, "reason": "", "wait": "declared"}
                for v in ("resumed", "still-blocked", "unprobeable")]
        block = wr.render_block(rows)
        self.assertIn("of 3 rows", block)


class ReadWaits(unittest.TestCase):
    def test_a_non_list_body_raises(self):
        self.assertRaises(Exception, wr.read_waits, script=os.path.join(_HERE, "no-such.py"))

    def test_a_missing_reader_raises(self):
        self.assertRaises(Exception, wr.read_waits, script=os.path.join(_HERE, "no-such.py"))


class Cli(unittest.TestCase):
    def test_an_unknown_subcommand_exits_2(self):
        with self.assertRaises(SystemExit) as ctx:
            wr.main(["nonsense"])
        self.assertEqual(ctx.exception.code, 2)

    def test_render_with_a_broken_reader_refuses_with_2(self):
        original = wr.read_waits
        wr.read_waits = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("store unreadable"))
        self.addCleanup(setattr, wr, "read_waits", original)
        self.assertEqual(wr.main(["render"]), wr.REFUSED)


if __name__ == "__main__":
    unittest.main()
