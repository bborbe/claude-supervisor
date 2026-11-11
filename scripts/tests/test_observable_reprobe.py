#!/usr/bin/env python3
"""Tests for scripts/observable-reprobe.py — the `⏰ Ends:` observable probe.

The properties worth pinning are the ones where a plausible implementation looks right
and is wrong:

  * **A merged PR and an approved-but-unmerged PR must not share a verdict.** They are
    the case the caller splits on, so a probe that answered "is it still open" would
    resume neither — or both. `test_merged_and_unmerged_differ` is that discriminator,
    and it asserts the two rows differ in BOTH the verdict and the reason.
  * **A failed probe is not a clearance.** `gh` raising, a missing repo, or an
    unrecognised observable must all leave the row standing. Folding any of them into
    `resumed` resumes a session on no evidence.
  * **A bare `PR #<n>` with no repo is unprobeable, never a guess.** A wrong repo's
    PR #786 is a real PR, and its state would be reported as this row's evidence.
  * **`CLOSED` is a clearance but not a merge.** It re-drives the session; it must not
    read as "your PR merged".

Run: python3 -m unittest discover -s scripts/tests
"""

import importlib.util
import os
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_SCRIPT = os.path.join(os.path.dirname(_HERE), "observable-reprobe.py")

_spec = importlib.util.spec_from_file_location("observable_reprobe", _SCRIPT)
orx = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(orx)

MERGED = {"state": "MERGED", "reviewDecision": "APPROVED", "mergeStateStatus": "UNKNOWN"}
OPEN_APPROVED = {"state": "OPEN", "reviewDecision": "APPROVED", "mergeStateStatus": "CLEAN"}
OPEN_IN_REVIEW = {"state": "OPEN", "reviewDecision": "REVIEW_REQUIRED", "mergeStateStatus": "BLOCKED"}


def _probe(payload):
    """A probe stub returning `payload`, so no test touches the network."""
    return lambda repo, number: payload


def _raising(exc):
    def boom(repo, number):
        raise exc
    return boom


class ObservableParsing(unittest.TestCase):
    def test_url_shape(self):
        self.assertEqual(
            orx.observable("https://github.com/bborbe/claude-supervisor/pull/786 merged"),
            {"kind": orx.PR, "repo": "bborbe/claude-supervisor", "number": 786},
        )

    def test_qualified_shape(self):
        self.assertEqual(
            orx.observable("bborbe/claude-supervisor#786 MERGED"),
            {"kind": orx.PR, "repo": "bborbe/claude-supervisor", "number": 786},
        )

    def test_bare_shape_carries_no_repo(self):
        self.assertEqual(
            orx.observable("PR #786 merged and the release cut"),
            {"kind": orx.PR, "repo": None, "number": 786},
        )

    def test_pull_request_spelling(self):
        self.assertEqual(orx.observable("pull request 12")["number"], 12)

    def test_a_clock_is_not_an_observable(self):
        self.assertIsNone(orx.observable("2026-11-12T09:00:00+01:00"))

    def test_a_person_is_not_an_observable(self):
        self.assertIsNone(orx.observable("a reply from the vendor"))

    def test_empty_and_non_string(self):
        self.assertIsNone(orx.observable(""))
        self.assertIsNone(orx.observable(None))


class Verdicts(unittest.TestCase):
    def test_merged_resumes(self):
        v, reason = orx.verdict(orx.observable("PR #786"), probe=_probe(MERGED), repo="o/r")
        self.assertEqual(v, orx._RESUMED)
        self.assertIn("MERGED", reason)
        self.assertIn("o/r#786", reason)

    def test_closed_is_a_clearance_but_not_a_merge(self):
        v, reason = orx.verdict(
            orx.observable("PR #786"),
            probe=_probe({"state": "CLOSED", "reviewDecision": "", "mergeStateStatus": ""}),
            repo="o/r",
        )
        self.assertEqual(v, orx._RESUMED)
        self.assertIn("CLOSED", reason)
        self.assertNotIn("MERGED", reason)

    def test_approved_but_unmerged_stays_blocked_and_quotes_the_merge_state(self):
        v, reason = orx.verdict(
            orx.observable("PR #786"), probe=_probe(OPEN_APPROVED), repo="o/r"
        )
        self.assertEqual(v, orx._STILL)
        self.assertIn("OPEN", reason)
        self.assertIn("review APPROVED", reason)
        self.assertIn("merge CLEAN", reason)

    def test_merged_and_unmerged_differ_in_both_verdict_and_reason(self):
        # SC3's discriminator: a probe that only asked "is it open" would satisfy one
        # row and silently break the other. Both fields are asserted so a change that
        # made them share a verdict could not pass.
        merged_v, merged_r = orx.verdict(
            orx.observable("o/r#1"), probe=_probe(MERGED), repo=None
        )
        open_v, open_r = orx.verdict(
            orx.observable("o/r#2"), probe=_probe(OPEN_APPROVED), repo=None
        )
        self.assertNotEqual(merged_v, open_v)
        self.assertNotEqual(merged_r, open_r)

    def test_still_blocked_does_not_hide_an_unreviewed_pr(self):
        v, reason = orx.verdict(
            orx.observable("PR #786"), probe=_probe(OPEN_IN_REVIEW), repo="o/r"
        )
        self.assertEqual(v, orx._STILL)
        self.assertIn("review REVIEW_REQUIRED", reason)


class NeverAClearance(unittest.TestCase):
    """Every unreadable input leaves the row standing. The safe direction."""

    def test_no_observable_is_unprobeable(self):
        v, _ = orx.verdict(None)
        self.assertEqual(v, orx._UNPROBEABLE)

    def test_bare_pr_without_repo_is_unprobeable(self):
        v, reason = orx.verdict(orx.observable("PR #786"), probe=_probe(MERGED))
        self.assertEqual(v, orx._UNPROBEABLE)
        self.assertIn("names no repo", reason)

    def test_probe_error_is_unprobeable_not_resumed(self):
        v, reason = orx.verdict(
            orx.observable("PR #786"), probe=_raising(RuntimeError("gh: not authenticated")),
            repo="o/r",
        )
        self.assertEqual(v, orx._UNPROBEABLE)
        self.assertIn("probe failed", reason)
        self.assertIn("gh: not authenticated", reason)

    def test_a_verdict_is_never_both_resumed_and_unprobeable(self):
        # The one pairing that would make the caller resume on a failed read.
        for payload in (MERGED, OPEN_APPROVED):
            v, _ = orx.verdict(
                orx.observable("PR #786"), probe=_probe(payload), repo="o/r"
            )
            self.assertIn(v, (orx._RESUMED, orx._STILL))


class Cli(unittest.TestCase):
    def test_bad_repo_exits_2(self):
        self.assertEqual(orx.main(["--ends", "PR #1", "--repo", "not-a-repo"]), 2)

    def test_clock_ends_prints_unprobeable(self):
        self.assertEqual(orx.main(["--ends", "2026-11-12T09:00:00+01:00"]), 0)


if __name__ == "__main__":
    unittest.main()
