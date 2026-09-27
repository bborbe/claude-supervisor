#!/usr/bin/env python3
"""Tests for scripts/answered-attribution.py.

The fixtures are the record shapes actually measured on the live store — the
2026-09-26 census (`GET /api/1.0/attention/history`, 9,454 rows) for the shape
list, and a 2026-09-27 re-measure on a copy of `bolt.db` (12,331 items) for the
counts the 2026-09-27 rules move:

    closed, no actor at all              11,838   a reap — not an act by anyone
    closed + actor, no answered_at           65   a CLEAR, not an answer
    closed + answered_at                    336   answered, then closed — stands
    answered + answered_at + actor           90   of which only 4 carry resolved_by
    open                                      2

⚠️ **Two rules landed on 2026-09-27, and both change what this module answers.**

  ⚠️ **A close without an answer is not a release.** The close path stamps
  `answered_by` and never `answered_at`. It stays an *act* — the card moved —
  but nothing was routed back, so the asking session is still frozen and
  releasing on it opens a gate nobody answered. `answered_at` is the
  discriminator, which is why an item answered and closed *afterwards* still
  releases. 65 rows move, 63 of them `attention-board`.

  ⚠️ **A gate releases only on `resolved_by`.** Attribution cannot carry this:
  a real Playwright/CDP click stores the operator's own Chrome UA with
  `automation: false`, byte-for-byte the operator's own click. `resolved_by` is
  sourced by the arm from its own `CLAUDE_CODE_SESSION_ID` and the board's
  JavaScript never sends it, so a click on any control the board renders cannot
  produce it. 86 of 90 `answered` rows move.

⚠️ The 11,838 reaps are unaffected by either rule — they carry no actor and
already read as NOTHING — which is why the count that moves is 151, not 12,000.

Run: python3 -m unittest discover -s scripts/tests -v
"""

import importlib.util
import os
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_SCRIPT = os.path.join(os.path.dirname(_HERE), "answered-attribution.py")

_spec = importlib.util.spec_from_file_location("answered_attribution", _SCRIPT)
attribution = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(attribution)


OPERATOR_CLIENT = {
    "user_agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/153.0.0.0 Safari/537.36",
    "remote_addr": "127.0.0.1:63415",
    "automation": False,
}
CURL_CLIENT = {"user_agent": "curl/8.7.1", "remote_addr": "127.0.0.1:55022"}
AUTOMATED_CLIENT = dict(OPERATOR_CLIENT, automation=True)


class ClassifyTest(unittest.TestCase):
    def verdict(self, item):
        return attribution.classify(item)[0]

    # --- the dominant shape, and the one a naive `closed` read gets wrong -----

    def test_reaped_close_is_not_an_act_by_anyone(self):
        self.assertEqual(self.verdict({"state": "closed"}), attribution.NOTHING)

    def test_open_item_records_nothing(self):
        self.assertEqual(self.verdict({"state": "open"}), attribution.NOTHING)

    def test_missing_item_records_nothing(self):
        self.assertEqual(self.verdict(None), attribution.NOTHING)

    # --- the attributed cases, both paths ------------------------------------

    def test_operator_answer_is_attributed(self):
        self.assertEqual(
            self.verdict(
                {
                    "state": "answered",
                    "answered_at": "2026-09-26T20:19:18Z",
                    "answered_by": "attention-board",
                    "resolved_by": "4df6f20a-e3f6-4943-8b0a-ec550465360a",
                    "answered_client": OPERATOR_CLIENT,
                }
            ),
            attribution.ATTRIBUTED,
        )

    def test_a_board_answer_without_resolved_by_does_not_release(self):
        """⚠️ The whole point, 2026-09-27: the board's JavaScript never sends
        `resolved_by`, so the record an operator's own click produces and the
        record a Playwright click produces are the same record — and neither
        releases the gate."""
        verdict, reason = attribution.classify(
            {
                "state": "answered",
                "answered_at": "2026-09-26T20:19:18Z",
                "answered_by": "attention-board",
                "answered_client": OPERATOR_CLIENT,
            }
        )
        self.assertEqual(verdict, attribution.UNATTRIBUTED)
        self.assertIn("resolved_by", reason)

    def test_a_close_is_a_clear_not_an_answer(self):
        """⚠️ The close path stamps `answered_by` and never `answered_at`. It is
        still an *act* — the card moved — but it is not a *release*: nothing was
        routed back, so the asking session is still frozen and releasing on it
        opens a gate nobody answered. ⚠️ **Provenance does not rescue it**, which
        is why the second case below carries `resolved_by` and still fails: the
        defect is the transition, not the actor."""
        for item in (
            {
                "state": "closed",
                "answered_by": "attention-board",
                "answered_client": OPERATOR_CLIENT,
            },
            {
                "state": "closed",
                "answered_by": "attention-board",
                "resolved_by": "4df6f20a-e3f6-4943-8b0a-ec550465360a",
                "answered_client": OPERATOR_CLIENT,
            },
        ):
            with self.subTest(item=item):
                self.assertEqual(self.verdict(item), attribution.UNATTRIBUTED)

    def test_an_answered_item_closed_later_still_releases(self):
        """⚠️ `answered_at` is the discriminator, and it is why the rule is not
        "closes never count": an item that was genuinely answered and closed
        afterwards carries the timestamp, and its answer stands."""
        self.assertEqual(
            self.verdict(
                {
                    "state": "closed",
                    "answered_at": "2026-09-26T20:19:18Z",
                    "answered_by": "attention-board",
                    "resolved_by": "4df6f20a-e3f6-4943-8b0a-ec550465360a",
                    "answered_client": OPERATOR_CLIENT,
                }
            ),
            attribution.ATTRIBUTED,
        )

    def test_a_curl_answer_with_resolved_by_is_attributed(self):
        # A deliberate API call by an arm is not the threat — the threat is a
        # synthesised click inside the operator's own browser. `curl` carries a
        # client record, reports no automation, and can carry the arm's own
        # provenance; that is the shape that legitimately releases.
        self.assertEqual(
            self.verdict(
                {
                    "state": "answered",
                    "answered_at": "2026-09-26T15:41:58Z",
                    "answered_by": "some-arm",
                    "resolved_by": "4df6f20a-e3f6-4943-8b0a-ec550465360a",
                    "answered_client": CURL_CLIENT,
                }
            ),
            attribution.ATTRIBUTED,
        )

    def test_a_curl_answer_without_resolved_by_does_not_release(self):
        # ⚠️ A direct API POST is no more evidence of the operator than a click
        # is. It carries no provenance, so it fails closed like everything else
        # that cannot produce `resolved_by`.
        self.assertEqual(
            self.verdict(
                {
                    "state": "answered",
                    "answered_at": "2026-09-26T15:41:58Z",
                    "answered_by": "some-arm",
                    "answered_client": CURL_CLIENT,
                }
            ),
            attribution.UNATTRIBUTED,
        )

    # --- the fail-closed cases ----------------------------------------------

    def test_answer_with_no_client_is_unattributed(self):
        self.assertEqual(
            self.verdict(
                {
                    "state": "answered",
                    "answered_at": "2026-09-26T15:14:13Z",
                    "answered_by": "attention-board",
                }
            ),
            attribution.UNATTRIBUTED,
        )

    def test_closed_answer_with_no_client_is_unattributed(self):
        self.assertEqual(
            self.verdict(
                {
                    "state": "closed",
                    "answered_at": "2026-09-26T15:14:13Z",
                    "answered_by": "attention-board",
                }
            ),
            attribution.UNATTRIBUTED,
        )

    def test_automation_true_is_automated(self):
        self.assertEqual(
            self.verdict(
                {
                    "state": "answered",
                    "answered_at": "2026-09-26T20:33:45Z",
                    "answered_by": "attention-board",
                    "answered_client": AUTOMATED_CLIENT,
                }
            ),
            attribution.AUTOMATED,
        )

    def test_malformed_client_is_unattributed_not_a_crash(self):
        # A sweep must not raise on a shape it did not expect, and a value it
        # cannot read is not evidence — so it fails closed rather than open.
        for bad in ("a string", 7, [], None):
            with self.subTest(client=bad):
                self.assertEqual(
                    self.verdict(
                        {
                            "state": "answered",
                            "answered_at": "2026-09-26T20:19:18Z",
                            "answered_client": bad,
                        }
                    ),
                    attribution.UNATTRIBUTED,
                )

    def test_a_scripted_browser_click_no_longer_takes_the_gate(self):
        """⚠️ The boundary, pinned as a test so it cannot be over-read later.

        This is the record a REAL Playwright/CDP click produced on the deployed
        board on 2026-09-26: the operator's Chrome user-agent and
        `automation: false`, because `navigator.webdriver` is `false` under
        Playwright. It is byte-for-byte the shape the operator's own board click
        produces.

        ⚠️ **This spec used to assert the opposite, and its own docstring asked
        for this day:** *"A future change that makes this fail is a real
        improvement, and it should fail loudly here rather than be discovered by
        re-deriving the whole probe."* 2026-09-27 is that change. The assertion
        is **inverted rather than deleted**, so the falsified expectation stays
        on the record: attribution still cannot tell these two apart — what
        changed is that the gate no longer rests on attribution.
        """
        verdict, reason = attribution.classify(
            {
                "state": "answered",
                "answered_at": "2026-09-26T21:16:33Z",
                "answered_by": "attention-board",
                "answered_client": {
                    "user_agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                                  "AppleWebKit/537.36 (KHTML, like Gecko) "
                                  "Chrome/153.0.0.0 Safari/537.36",
                    "remote_addr": "127.0.0.1:50694",
                    "automation": False,
                },
            }
        )
        self.assertEqual(verdict, attribution.UNATTRIBUTED)
        self.assertIn("resolved_by", reason)

    def test_automation_false_does_not_exonerate(self):
        # ⚠️ The boundary, pinned so it is not over-read later: a `false` only
        # fails to incriminate. It is not proof the operator answered — and it is
        # no longer sufficient to release a gate either; the answer must also
        # carry the arm's provenance.
        verdict, reason = attribution.classify(
            {
                "state": "answered",
                "answered_at": "2026-09-26T20:19:18Z",
                "resolved_by": "4df6f20a-e3f6-4943-8b0a-ec550465360a",
                "answered_client": dict(OPERATOR_CLIENT, automation=False),
            }
        )
        self.assertEqual(verdict, attribution.ATTRIBUTED)
        self.assertIn("no positive automation flag", reason)

    def test_automation_false_without_provenance_does_not_release(self):
        verdict, _ = attribution.classify(
            {
                "state": "answered",
                "answered_at": "2026-09-26T20:19:18Z",
                "answered_client": dict(OPERATOR_CLIENT, automation=False),
            }
        )
        self.assertEqual(verdict, attribution.UNATTRIBUTED)

    def test_every_reason_names_the_evidence_that_was_missing(self):
        for item in (
            {"state": "open"},
            {"state": "answered", "answered_at": "2026-09-26T20:19:18Z"},
            {
                "state": "answered",
                "answered_at": "2026-09-26T20:19:18Z",
                "answered_client": AUTOMATED_CLIENT,
            },
        ):
            with self.subTest(item=item):
                _, reason = attribution.classify(item)
                self.assertTrue(reason.strip())


class OperatorAnsweredTest(unittest.TestCase):
    def test_only_the_attributed_verdict_passes(self):
        cases = [
            ({"state": "open"}, False),
            ({"state": "closed"}, False),
            ({"state": "answered", "answered_at": "x"}, False),
            (
                {
                    "state": "answered",
                    "answered_at": "x",
                    "answered_client": AUTOMATED_CLIENT,
                },
                False,
            ),
            (
                {
                    "state": "answered",
                    "answered_at": "x",
                    "resolved_by": "arm-1",
                    "answered_client": OPERATOR_CLIENT,
                },
                True,
            ),
            # Provenance alone is not enough either: an arm answer with no client
            # record is still unattributed, exactly as before.
            (
                {
                    "state": "answered",
                    "answered_at": "x",
                    "resolved_by": "arm-1",
                },
                False,
            ),
            # The close path, however well attributed, is never an act.
            (
                {
                    "state": "closed",
                    "answered_by": "attention-board",
                    "resolved_by": "arm-1",
                    "answered_client": OPERATOR_CLIENT,
                },
                False,
            ),
        ]
        for item, expected in cases:
            with self.subTest(item=item):
                self.assertIs(attribution.operator_answered(item), expected)


if __name__ == "__main__":
    unittest.main()
