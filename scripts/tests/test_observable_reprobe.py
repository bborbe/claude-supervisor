#!/usr/bin/env python3
"""Tests for scripts/observable-reprobe.py — the `⏰ Ends:` observable probe.

The properties worth pinning are the ones where a plausible implementation looks right
and is wrong:

  * **A merged PR and an approved-but-unmerged PR must not share a verdict.** They are
    the case the caller splits on, so a probe that answered "is it still open" would
    resume neither — or both. `test_merged_and_unmerged_differ` is that discriminator,
    and it asserts the two rows differ in BOTH the verdict and the reason.
  * **A declaration naming two PRs clears only when BOTH have cleared.** Probing the
    first match alone reports `resumed` off a merged PR while a second is still open —
    a clearance on partial evidence, the same false positive by a different route.
  * **A failed probe is not a clearance.** `gh` raising, a missing repo, or an
    unrecognised observable must all leave the row standing.
  * **A bare `PR #<n>` with no repo is unprobeable, never a guess.** A wrong repo's
    PR #786 is a real PR, and its state would be reported as this row's evidence.
  * **`CLOSED` is a clearance but not a merge.** It re-drives the session; it must not
    read as "your PR merged".

Run: python3 -m unittest discover -s scripts/tests
"""

import contextlib
import importlib.util
import io
import os
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_SCRIPT = os.path.join(os.path.dirname(_HERE), "observable-reprobe.py")

_spec = importlib.util.spec_from_file_location("observable_reprobe", _SCRIPT)
orx = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(orx)

MERGED = {"state": "MERGED", "reviewDecision": "APPROVED", "mergeStateStatus": "UNKNOWN"}
CLOSED = {"state": "CLOSED", "reviewDecision": "", "mergeStateStatus": ""}
OPEN_APPROVED = {"state": "OPEN", "reviewDecision": "APPROVED", "mergeStateStatus": "CLEAN"}
OPEN_IN_REVIEW = {"state": "OPEN", "reviewDecision": "REVIEW_REQUIRED", "mergeStateStatus": "BLOCKED"}


def _probe(payload):
    """A probe stub returning `payload`, so no test touches the network."""
    return lambda repo, number: payload


def _by_number(payloads):
    """A probe stub answering per PR number, for the multi-PR cases."""
    return lambda repo, number: payloads[number]


def _raising(exc):
    def boom(repo, number):
        raise exc
    return boom


class ObservableParsing(unittest.TestCase):
    def test_url_shape(self):
        self.assertEqual(
            orx.observables("https://github.com/bborbe/claude-supervisor/pull/786 merged"),
            [{"kind": orx.PR, "repo": "bborbe/claude-supervisor", "number": 786}],
        )

    def test_qualified_shape(self):
        self.assertEqual(
            orx.observables("bborbe/claude-supervisor#786 MERGED"),
            [{"kind": orx.PR, "repo": "bborbe/claude-supervisor", "number": 786}],
        )

    def test_bare_shape_carries_no_repo(self):
        self.assertEqual(
            orx.observables("PR #786 merged and the release cut"),
            [{"kind": orx.PR, "repo": None, "number": 786}],
        )

    def test_pull_request_spelling(self):
        self.assertEqual(orx.observables("pull request 12")[0]["number"], 12)

    def test_two_prs_are_both_collected(self):
        self.assertEqual([o["number"] for o in orx.observables("PR #786 and PR #791")], [786, 791])

    def test_an_unprefixed_second_pr_after_a_pr_token_is_collected(self):
        # The shape the fleet actually writes, measured in live declarations. A
        # prefix-only matcher probed #119 and never saw #939, so a merged #119 cleared a
        # declaration whose second PR was still open.
        self.assertEqual(
            [o["number"] for o in orx.observables(
                "bot verdict on PR #119 head `ee0fd9c` or nuke #939 head `2ed9c4c`"
            )],
            [119, 939],
        )

    def test_a_counter_before_any_pr_token_is_not_a_pr(self):
        # "audit #4" is an ordinal, not a PR. A blanket `#<n>` matcher would probe it as
        # one and could clear on it; the position rule drops it.
        self.assertEqual(
            [o["number"] for o in orx.observables(
                "audit #4 returns a score → I flip the phase; PR #536 merges → I rebase"
            )],
            [536],
        )

    def test_a_text_with_no_pr_token_is_unprobeable(self):
        # Nothing identifies these as PRs, so the declaration is not cleared — the safe
        # direction, stated in the docstring.
        self.assertEqual(orx.observables("a review appears on #89 or #92"), [])

    def test_a_url_fragment_is_not_a_second_observable(self):
        self.assertEqual(
            orx.observables("https://github.com/bborbe/claude-supervisor/pull/786#1"),
            [{"kind": orx.PR, "repo": "bborbe/claude-supervisor", "number": 786}],
        )

    def test_a_bare_repeat_of_a_qualified_pr_is_not_probed_twice(self):
        # Probing it again would add a second call for one PR, and an unresolvable
        # duplicate would fail a declaration the qualified reference had settled.
        self.assertEqual(len(orx.observables("o/r#1 and PR #1")), 1)

    def test_a_clock_is_not_an_observable(self):
        self.assertEqual(orx.observables("2026-11-12T09:00:00+01:00"), [])

    def test_a_person_is_not_an_observable(self):
        self.assertEqual(orx.observables("a reply from the vendor"), [])

    def test_empty_and_non_string(self):
        self.assertEqual(orx.observables(""), [])
        self.assertEqual(orx.observables(None), [])


class Verdicts(unittest.TestCase):
    def test_merged_resumes(self):
        v, reason = orx.verdict(orx.observables("PR #786"), probe=_probe(MERGED), repo="o/r")
        self.assertEqual(v, orx._RESUMED)
        self.assertIn("MERGED", reason)
        self.assertIn("o/r#786", reason)

    def test_closed_is_a_clearance_but_not_a_merge(self):
        v, reason = orx.verdict(
            orx.observables("PR #786"), probe=_probe(CLOSED), repo="o/r"
        )
        self.assertEqual(v, orx._RESUMED)
        self.assertIn("CLOSED", reason)
        self.assertNotIn("MERGED", reason)

    def test_approved_but_unmerged_stays_blocked_and_quotes_the_merge_state(self):
        v, reason = orx.verdict(
            orx.observables("PR #786"), probe=_probe(OPEN_APPROVED), repo="o/r"
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
            orx.observables("o/r#1"), probe=_probe(MERGED), repo=None
        )
        open_v, open_r = orx.verdict(
            orx.observables("o/r#2"), probe=_probe(OPEN_APPROVED), repo=None
        )
        self.assertNotEqual(merged_v, open_v)
        self.assertNotEqual(merged_r, open_r)

    def test_still_blocked_does_not_hide_an_unreviewed_pr(self):
        v, reason = orx.verdict(
            orx.observables("PR #786"), probe=_probe(OPEN_IN_REVIEW), repo="o/r"
        )
        self.assertEqual(v, orx._STILL)
        self.assertIn("review REVIEW_REQUIRED", reason)

    def test_one_open_pr_blocks_the_whole_declaration(self):
        # The defect this guards: probing the FIRST match alone reports `resumed` off
        # the merged one while the second is still open.
        v, reason = orx.verdict(
            orx.observables("PR #786 and PR #791"),
            probe=_by_number({786: MERGED, 791: OPEN_APPROVED}),
            repo="o/r",
        )
        self.assertEqual(v, orx._STILL)
        self.assertIn("o/r#791", reason)

    def test_all_prs_cleared_resumes_and_names_each(self):
        v, reason = orx.verdict(
            orx.observables("PR #786 and PR #791"),
            probe=_by_number({786: MERGED, 791: CLOSED}),
            repo="o/r",
        )
        self.assertEqual(v, orx._RESUMED)
        self.assertIn("o/r#786", reason)
        self.assertIn("o/r#791", reason)


class NeverAClearance(unittest.TestCase):
    """Every unreadable input leaves the row standing. The safe direction."""

    def test_no_observable_is_unprobeable(self):
        v, _ = orx.verdict([])
        self.assertEqual(v, orx._UNPROBEABLE)

    def test_bare_pr_without_repo_is_unprobeable(self):
        v, reason = orx.verdict(orx.observables("PR #786"), probe=_probe(MERGED))
        self.assertEqual(v, orx._UNPROBEABLE)
        self.assertIn("names no repo", reason)

    def test_probe_error_is_unprobeable_not_resumed(self):
        v, reason = orx.verdict(
            orx.observables("PR #786"), probe=_raising(RuntimeError("gh: not authenticated")),
            repo="o/r",
        )
        self.assertEqual(v, orx._UNPROBEABLE)
        self.assertIn("probe failed", reason)
        self.assertIn("gh: not authenticated", reason)

    def test_one_failed_probe_in_a_pair_is_unprobeable(self):
        def flaky(repo, number):
            if number == 791:
                raise RuntimeError("gh: boom")
            return MERGED
        v, _ = orx.verdict(orx.observables("PR #786 and PR #791"), probe=flaky, repo="o/r")
        self.assertEqual(v, orx._UNPROBEABLE)


class Cli(unittest.TestCase):
    def test_bad_repo_exits_2(self):
        self.assertEqual(orx.main(["--ends", "PR #1", "--repo", "not-a-repo"]), 2)

    def test_output_contract_is_verdict_tab_reason(self):
        # Every verdict path exits 0, so an exit-code-only assertion would pass even if
        # the line printed were wrong. The caller parses this line, so pin its shape.
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = orx.main(["--ends", "2026-11-12T09:00:00+01:00"])
        self.assertEqual(rc, 0)
        verdict, sep, reason = buf.getvalue().rstrip("\n").partition("\t")
        self.assertEqual(sep, "\t", f"no tab separator in {buf.getvalue()!r}")
        self.assertEqual(verdict, orx._UNPROBEABLE)
        self.assertTrue(reason.strip())


if __name__ == "__main__":
    unittest.main()
