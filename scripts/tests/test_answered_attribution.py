#!/usr/bin/env python3
"""Tests for scripts/answered-attribution.py.

The fixtures are the record shapes actually measured on the live store on
2026-09-26 (`GET /api/1.0/attention/history`, 9,454 rows), with their counts, so
the rule is pinned to the shapes that exist rather than to shapes invented here:

    closed, no answered_at/by/client      9,339   a reap — not an act by anyone
    closed + answered_at + by, no client     44   answered before the field landed
    closed + answered_at + by + client       43   answered, then closed
    answered + answered_at + by + client     34   answered after the field landed
    closed + by + client, no answered_at     30   the Acknowledge path
    answered + answered_at + by, no client   21   answered before the field landed
    closed + by, no answered_at, no client    3   an older Acknowledge

⚠️ The two large numbers are the ones that make this rule non-obvious. Reading
`closed` as "answered" would classify 9,339 reaps as operator acts; reading
`answered` as "the operator saw it" would classify the 21+44 pre-field answers
as operator-verified when nothing about their client is known.

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
                    "answered_client": OPERATOR_CLIENT,
                }
            ),
            attribution.ATTRIBUTED,
        )

    def test_ack_close_with_a_client_is_attributed(self):
        # The close path stamps `answered_by` and never `answered_at`; it is
        # still an act, and it carries a client, so it is attributable.
        self.assertEqual(
            self.verdict(
                {
                    "state": "closed",
                    "answered_by": "attention-board",
                    "answered_client": OPERATOR_CLIENT,
                }
            ),
            attribution.ATTRIBUTED,
        )

    def test_curl_answer_is_attributed(self):
        # A deliberate API call by a session is not the threat: the threat is a
        # synthesised click inside the operator's own browser. `curl` carries a
        # client record and reports no automation.
        self.assertEqual(
            self.verdict(
                {
                    "state": "answered",
                    "answered_at": "2026-09-26T15:41:58Z",
                    "answered_by": "some-arm",
                    "answered_client": CURL_CLIENT,
                }
            ),
            attribution.ATTRIBUTED,
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

    def test_a_scripted_browser_click_passes_the_predicate(self):
        """⚠️ The boundary, pinned as a test so it cannot be over-read later.

        This is the record a REAL Playwright/CDP click produced on the deployed
        board on 2026-09-26: the operator's Chrome user-agent and
        `automation: false`, because `navigator.webdriver` is `false` under
        Playwright. It is byte-for-byte the shape an operator click produces, so
        the predicate returns ATTRIBUTED and the gate IS released.

        The assertion is deliberately that it passes. A future change that makes
        this fail is a real improvement, and it should fail loudly here rather
        than be discovered by re-deriving the whole probe.
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
        self.assertEqual(verdict, attribution.ATTRIBUTED)
        self.assertTrue(reason.strip())

    def test_automation_false_does_not_exonerate_but_does_attributed(self):
        # ⚠️ The boundary, pinned so it is not over-read later: a `false` only
        # fails to incriminate. It is not proof the operator answered, and this
        # module does not claim it is — it returns ATTRIBUTED, which is the
        # weaker claim that the answer is attributable at all.
        verdict, reason = attribution.classify(
            {
                "state": "answered",
                "answered_at": "2026-09-26T20:19:18Z",
                "answered_client": dict(OPERATOR_CLIENT, automation=False),
            }
        )
        self.assertEqual(verdict, attribution.ATTRIBUTED)
        self.assertIn("no positive automation flag", reason)

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
                    "answered_client": OPERATOR_CLIENT,
                },
                True,
            ),
        ]
        for item, expected in cases:
            with self.subTest(item=item):
                self.assertIs(attribution.operator_answered(item), expected)


if __name__ == "__main__":
    unittest.main()
