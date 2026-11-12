#!/usr/bin/env python3
"""Tests for scripts/operator-ask-card.py — SC1's three-case fixture set.

The properties worth pinning are the ones where a plausible implementation looks right
and is wrong:

  * **The join is the SESSION, not an id parsed out of the closer.** A `pick` closer
    usually names no card id, and its mirror card is in the store under an id the closer
    never mentions. A checker that reads the closer's text and calls it NO-CARD when it
    finds no id flags every mirror card as missing — the defect the case-(b) fixture pins.
  * **Case (c) asks about the CITED id, not the session.** A live card for the session does
    not make a dead cited id answerable, so a closer naming an id the store lacks is
    NO-CARD even when the session has another card open.
  * **An unreadable store is UNKNOWN, never NO-CARD.** NO-CARD accuses the operator of
    being unable to answer; a failed read cannot support that.
  * **A `later (on …)` closer is not a waiting ask.** It waits on a named event, not a
    card, so reporting it as NO-CARD would be a false accusation.

Run: python3 -m unittest discover -s scripts/tests
"""

import importlib.util
import json
import os
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_SCRIPT = os.path.join(os.path.dirname(_HERE), "operator-ask-card.py")
_FIX = os.path.join(_HERE, "fixtures", "operator-ask-card")

_spec = importlib.util.spec_from_file_location("operator_ask_card", _SCRIPT)
oac = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(oac)

SID_A = "aaaaaaaa-0000-0000-0000-000000000001"
SID_B = "aaaaaaaa-0000-0000-0000-000000000002"
SID_C = "aaaaaaaa-0000-0000-0000-000000000003"


def _items(name):
    with open(os.path.join(_FIX, name), encoding="utf-8") as f:
        return json.load(f)


class ThreeCaseSet(unittest.TestCase):
    """SC1 (a), (b), (c) — the three real shapes, one fixture each."""

    def test_a_waiting_verb_with_no_card_for_the_session_is_no_card(self):
        # (a) a parked ask closer carrying no card id with no card in the store.
        v, detail = oac.verdict(
            "pick — 1. Re-scope SC1/SC2 now · 2. Leave them and close this session",
            SID_A, _items("a-no-card.json"))
        self.assertEqual(v, oac.NO_CARD, detail)

    def test_a_pick_whose_mirror_card_is_in_the_store_is_not_no_card(self):
        # (b) the id-less pick whose Stop-hook mirror card IS in the store. ⚠️ This is the
        # case an id-parsing checker gets wrong: the closer names nothing, the card exists.
        v, detail = oac.verdict(
            "pick — 1. hold prod · 2. ship it", SID_B, _items("b-mirror-card.json"))
        self.assertEqual(v, oac.CARD, detail)
        self.assertIn("22222222222222222222222222222222", detail)

    def test_a_closer_naming_an_absent_id_is_no_card(self):
        # (c) the cited id is not in the store — even though the session has another card.
        v, detail = oac.verdict(
            "approve: reply `go` — card deadbeefdeadbeefdeadbeefdeadbeef",
            SID_C, _items("c-absent-id.json"))
        self.assertEqual(v, oac.NO_CARD, detail)
        self.assertIn("deadbeefdeadbeefdeadbeefdeadbeef", detail)

    def test_a_closer_naming_a_live_id_is_a_card(self):
        # The positive control for (c): the same fixture, the id it actually holds.
        v, detail = oac.verdict(
            "approve: reply `go` — card 33333333333333333333333333333333",
            SID_C, _items("c-absent-id.json"))
        self.assertEqual(v, oac.CARD, detail)


class NotAWaitingAsk(unittest.TestCase):
    def test_a_later_on_closer_is_not_a_waiting_ask(self):
        # It waits on a named event, not a card. Calling it NO-CARD would accuse a session
        # that is correctly parked.
        v, _ = oac.verdict("later (on either blocker landing): re-read the delta", SID_A, [])
        self.assertEqual(v, oac.NOT_AN_ASK)

    def test_a_nothing_closer_is_not_a_waiting_ask(self):
        self.assertEqual(oac.verdict("nothing", SID_A, [])[0], oac.NOT_AN_ASK)

    def test_a_routine_closer_is_not_a_waiting_ask(self):
        self.assertEqual(oac.verdict("", SID_A, [])[0], oac.NOT_AN_ASK)


class NeverAccuseOnAFailedRead(unittest.TestCase):
    def test_an_unreadable_store_is_unknown_not_no_card(self):
        # NO-CARD accuses the operator of being unable to answer; a failed read cannot
        # support that, so it must never be folded in.
        code = oac.main(["--closer", "pick — 1. x", "--session", SID_A,
                         "--store", "http://127.0.0.1:1"])
        self.assertEqual(code, 0)

    def test_a_malformed_fixture_is_unknown(self):
        import tempfile
        f = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8")
        f.write('{"not": "a list"}')
        f.close()
        self.addCleanup(os.unlink, f.name)
        self.assertEqual(
            oac.main(["--closer", "pick — 1. x", "--session", SID_A, "--fixture", f.name]), 0)


if __name__ == "__main__":
    unittest.main()
