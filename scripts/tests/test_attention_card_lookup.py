"""Tests for scripts/attention-card-lookup.py.

The properties worth pinning, because each is a way the lookup could look like
it works while being wrong:

  - **The selection is by `answered_at`, not by position.** A lookup that
    returns whichever card it happened to see first passes every test written
    against a single-card key and fails the task's whole point.
  - **A tie is REFUSED, never broken.** Quoting either member of a tie reports
    an answer the operator may not have given — the defect this exists to
    remove — so the refusal is the correct output, not a degraded one.
  - **An unanswered card never outranks an answered one.** A card can sit in
    `closed` with no `answered_at` at all (observed 2026-10-05, item
    `ee9bd781`); ranking it above a real answer would invert the report.
  - **Two distinct keys are never unioned.** `7fbee982` resolves to two full
    keys sharing an 8-char prefix (measured 2026-10-05). Their cards cannot be
    ordered against each other, so a prefix match over both is refused rather
    than silently merged.
"""
import importlib.util
import io
import os
import unittest

SCRIPTS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

KEY_A = "a86e919c-617c-4a2a-8aff-7f1de66e9287"
KEY_B1 = "7fbee982-860f-4eb4-bbc2-b5b15ff2f9d7"
KEY_B2 = "7fbee982-860f-4ebf-bbc2-b5b15ff2f9d7"


def load(name, filename):
    path = os.path.join(SCRIPTS, filename)
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


lookup = load("attention_card_lookup", "attention-card-lookup.py")


def card(item_id, key, answered, state="closed", answer="raise it now", created=None):
    return {
        "item_id": item_id,
        "dedup_key": key,
        "state": state,
        "created_at": created,
        "answered_at": answered,
        "answer": {"kind": "option", "value": answer} if answer else None,
    }


class CardsForKeyTest(unittest.TestCase):
    def test_exact_match_returns_only_that_key(self):
        items = [card("aa", KEY_A, "2026-10-05T09:00:00Z"), card("bb", KEY_B1, "2026-10-05T08:00:00Z")]
        cards, err = lookup.cards_for_key(items, KEY_A)
        self.assertIsNone(err)
        self.assertEqual(["aa"], [c["item_id"] for c in cards])

    def test_prefix_of_two_distinct_keys_is_refused(self):
        items = [card("aa", KEY_B1, "2026-10-05T08:00:00Z"), card("bb", KEY_B2, "2026-10-05T09:00:00Z")]
        cards, err = lookup.cards_for_key(items, "7fbee982")
        self.assertEqual([], cards)
        self.assertIsNotNone(err)
        self.assertIn(KEY_B1, err)
        self.assertIn(KEY_B2, err)

    def test_short_prefix_matches_nothing(self):
        # A sub-MIN_PREFIX string is not evidence of anything; matching on it
        # would return an unrelated key's card as though it were this one's.
        items = [card("aa", KEY_A, "2026-10-05T09:00:00Z")]
        cards, err = lookup.cards_for_key(items, "a86e")
        self.assertEqual([], cards)
        self.assertIsNone(err)


class SelectLatestTest(unittest.TestCase):
    def test_picks_max_answered_at_not_position(self):
        cards = [
            card("early", KEY_A, "2026-10-04T15:59:56Z", answer="skip"),
            card("late", KEY_A, "2026-10-04T16:03:52Z"),
        ]
        chosen, err = lookup.select_latest(cards)
        self.assertIsNone(err)
        self.assertEqual("late", chosen["item_id"])

    def test_tie_is_refused_not_broken(self):
        cards = [
            card("aa", KEY_A, "2026-10-05T09:00:00Z"),
            card("bb", KEY_A, "2026-10-05T09:00:00Z"),
        ]
        chosen, err = lookup.select_latest(cards)
        self.assertIsNone(chosen)
        self.assertIsNotNone(err)
        self.assertIn("undecidable", err)

    def test_unanswered_closed_card_never_outranks_an_answer(self):
        cards = [
            card("noanswer", KEY_A, None, state="closed", answer=None),
            card("answered", KEY_A, "2026-10-04T16:03:52Z"),
        ]
        chosen, err = lookup.select_latest(cards)
        self.assertIsNone(err)
        self.assertEqual("answered", chosen["item_id"])

    def test_open_card_is_not_a_candidate(self):
        cards = [
            card("open", KEY_A, None, state="open", answer=None),
            card("answered", KEY_A, "2026-10-04T16:03:52Z"),
        ]
        chosen, _ = lookup.select_latest(cards)
        self.assertEqual("answered", chosen["item_id"])


class RecentTest(unittest.TestCase):
    """`recent` answers a DIFFERENT question from `latest`, and the hold line
    needs this one: the card that carries the hold is usually still OPEN, so a
    lookup restricted to answered cards returns nothing for the common case."""

    def run_recent(self, items, key):
        out = io.StringIO()
        code = lookup.cmd_recent(type("A", (), {"dedup_key": key})(), items, out=out)
        return code, out.getvalue()

    def test_newest_card_wins_even_when_unanswered(self):
        items = [
            card("old", KEY_A, "2026-10-04T16:03:52Z", created="2026-10-04T11:55:19Z"),
            card("open", KEY_A, None, state="open", answer=None, created="2026-10-05T09:26:34Z"),
        ]
        code, text = self.run_recent(items, KEY_A)
        self.assertEqual(0, code)
        self.assertIn("ITEM_ID: open", text)
        self.assertIn("STATE: open", text)
        self.assertIn("ANSWERED_AT: -", text)

    def test_latest_would_not_serve_that_case(self):
        # The negative probe for the split: the same items through `latest` do
        # NOT return the open card, which is why one verb cannot cover both.
        items = [
            card("old", KEY_A, "2026-10-04T16:03:52Z", created="2026-10-04T11:55:19Z"),
            card("open", KEY_A, None, state="open", answer=None, created="2026-10-05T09:26:34Z"),
        ]
        out = io.StringIO()
        lookup.cmd_latest(type("A", (), {"dedup_key": KEY_A})(), items, out=out)
        self.assertNotIn("ITEM_ID: open", out.getvalue())

    def test_tie_on_created_at_is_refused(self):
        items = [
            card("aa", KEY_A, None, created="2026-10-05T09:00:00Z"),
            card("bb", KEY_A, None, created="2026-10-05T09:00:00Z"),
        ]
        code, text = self.run_recent(items, KEY_A)
        self.assertEqual(2, code)
        self.assertIn("REFUSED:", text)
        self.assertIn("undecidable", text)

    def test_no_cards_exits_three(self):
        code, text = self.run_recent([card("aa", KEY_A, None, created="2026-10-05T09:00:00Z")], KEY_B1)
        self.assertEqual(3, code)
        self.assertIn("NO_CARDS:", text)


class CommandExitCodeTest(unittest.TestCase):
    def run_latest(self, items, key):
        out = io.StringIO()
        code = lookup.cmd_latest(type("A", (), {"dedup_key": key})(), items, out=out)
        return code, out.getvalue()

    def test_latest_reports_the_card_and_exits_zero(self):
        items = [
            card("early", KEY_A, "2026-10-04T15:59:56Z", answer="skip"),
            card("late", KEY_A, "2026-10-04T16:03:52Z"),
        ]
        code, text = self.run_latest(items, KEY_A)
        self.assertEqual(0, code)
        self.assertIn("ITEM_ID: late", text)
        self.assertIn("ANSWER: raise it now", text)
        self.assertIn("CARDS: 2", text)

    def test_ambiguous_key_exits_two(self):
        items = [card("aa", KEY_B1, "2026-10-05T08:00:00Z"), card("bb", KEY_B2, "2026-10-05T09:00:00Z")]
        code, text = self.run_latest(items, "7fbee982")
        self.assertEqual(2, code)
        self.assertIn("REFUSED:", text)

    def test_unknown_key_exits_three(self):
        code, text = self.run_latest([card("aa", KEY_A, "2026-10-05T09:00:00Z")], KEY_B1)
        self.assertEqual(3, code)
        self.assertIn("NO_CARDS:", text)

    def test_key_with_no_answered_card_exits_three(self):
        items = [card("aa", KEY_A, None, state="open", answer=None)]
        code, text = self.run_latest(items, KEY_A)
        self.assertEqual(3, code)
        self.assertIn("NO_ANSWERED:", text)


if __name__ == "__main__":
    unittest.main()
