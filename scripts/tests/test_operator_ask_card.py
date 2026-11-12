#!/usr/bin/env python3
"""Tests for scripts/operator-ask-card.py — SC1's three-case fixture set.

The properties worth pinning are the ones where a plausible implementation looks right
and is wrong:

  * **The join is the SESSION, not an id parsed out of the closer.** A `pick` closer
    usually names no card id, and its mirror card is in the store under an id the closer
    never mentions. A checker that reads the closer's text and calls it NO-CARD when it
    finds no id flags every mirror card as missing — the defect the case-(b) fixture pins.
  * **`--closer` is the WHOLE `👤 You:` line.** Anchoring the verb at the raw string's start
    classified the documented input shape as NOT-AN-ASK, hiding every `pick` park.
  * **Case (c) asks about the CITED id, not the session.** A live card for the session does
    not make a dead cited id answerable.
  * **An unreadable store is UNKNOWN, never NO-CARD** — and that includes a MALFORMED body,
    not only an unreachable one. NO-CARD accuses the operator of being unable to answer; a
    read the code cannot parse cannot support that.
  * **Every assertion reads the printed verdict, not just the exit code.** `main` exits 0
    for every verdict, so an exit-code-only test passes on NO-CARD.

Run: python3 -m unittest discover -s scripts/tests
"""

import contextlib
import importlib.util
import io
import json
import os
import tempfile
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


def _run(argv):
    """Run `main`, returning (exit code, verdict token, whole line)."""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = oac.main(argv)
    line = buf.getvalue().strip()
    return rc, (line.split("\t")[0] if line else ""), line


def _fixture(rows):
    f = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8")
    json.dump(rows, f)
    f.close()
    return f.name


class ThreeCaseSet(unittest.TestCase):
    """SC1 (a), (b), (c) — the three real shapes, one fixture each."""

    def test_a_waiting_verb_with_no_card_for_the_session_is_no_card(self):
        # (a) a parked ask closer carrying no card id with no card in the store. The fixture
        # holds ANOTHER session's open card, so an "any open card" implementation fails here.
        v, detail = oac.verdict(
            "👤 You: pick — 1. Re-scope SC1/SC2 now · 2. Leave them and close this session",
            SID_A, _items("a-no-card.json"))
        self.assertEqual(v, oac.NO_CARD, detail)

    def test_a_pick_whose_mirror_card_is_in_the_store_is_not_no_card(self):
        # (b) the id-less pick whose Stop-hook mirror card IS in the store. ⚠️ This is the
        # case an id-parsing checker gets wrong: the closer names nothing, the card exists.
        v, detail = oac.verdict(
            "👤 You: pick — 1. hold prod · 2. ship it", SID_B, _items("b-mirror-card.json"))
        self.assertEqual(v, oac.CARD, detail)
        self.assertIn("22222222222222222222222222222222", detail)

    def test_a_closer_naming_an_absent_id_is_no_card(self):
        # (c) the cited id is not in the store — even though the session has another card.
        v, detail = oac.verdict(
            "👤 You: approve: reply `go` — card deadbeefdeadbeefdeadbeefdeadbeef",
            SID_C, _items("c-absent-id.json"))
        self.assertEqual(v, oac.NO_CARD, detail)
        self.assertIn("deadbeefdeadbeefdeadbeefdeadbeef", detail)

    def test_a_closer_naming_a_live_id_is_a_card(self):
        # The positive control for (c): the same fixture, the id it actually holds.
        v, detail = oac.verdict(
            "👤 You: approve: reply `go` — card 33333333333333333333333333333333",
            SID_C, _items("c-absent-id.json"))
        self.assertEqual(v, oac.CARD, detail)


class CloserShape(unittest.TestCase):
    """The documented input is the whole panel line, so the verb is not at position 0."""

    def test_the_documented_panel_line_is_an_ask(self):
        for closer in ("👤 You: pick — 1. X · 2. Y",
                       "**pick** — 1. X",
                       "👤 You: &nbsp;&nbsp;pick — 1. X",
                       "⚠️ pick — 1. X",
                       "  pick — 1. X",
                       "PICK — 1. X"):
            with self.subTest(closer=closer):
                self.assertTrue(oac.is_waiting_ask(closer), closer)

    def test_a_later_on_closer_is_not_a_waiting_ask(self):
        v, _ = oac.verdict("👤 You: later (on either blocker landing): re-read the delta", SID_A, [])
        self.assertEqual(v, oac.NOT_AN_ASK)

    def test_a_nothing_closer_is_not_a_waiting_ask(self):
        self.assertEqual(oac.verdict("👤 You: nothing", SID_A, [])[0], oac.NOT_AN_ASK)

    def test_a_finished_review_line_is_not_an_ask(self):
        # `review\\b` matched this and turned a FINISHED session into a NO-CARD accusation —
        # the direction this must never fail in. The colon is what makes it a waiting verb.
        self.assertEqual(
            oac.verdict("👤 You: Review complete — nothing needed", SID_A, [])[0],
            oac.NOT_AN_ASK)

    def test_a_whole_panel_is_out_of_contract_and_reads_not_an_ask(self):
        # `--closer` takes the LINE, not the panel. Pinned so a future wiring cannot feed a
        # whole panel by accident and have every session silently read NOT-AN-ASK.
        panel = "📌 Task: foo — execution\n👤 You: pick — 1. X"
        self.assertEqual(oac.verdict(panel, SID_A, [])[0], oac.NOT_AN_ASK)

    def test_a_status_line_is_deliberately_not_an_ask(self):
        # Narrower than `closer-classes.md`'s `ask` class on purpose: neither waits on a card.
        for closer in ("done — all green", "waiting for CI"):
            with self.subTest(closer=closer):
                self.assertEqual(oac.verdict(closer, SID_A, [])[0], oac.NOT_AN_ASK)


class NeverAccuseOnAFailedRead(unittest.TestCase):
    """Every assertion here reads the VERDICT — `main` exits 0 for all of them."""

    def test_an_unreachable_store_is_unknown(self):
        _, verdict, line = _run(["--closer", "👤 You: pick — 1. x", "--session", SID_A,
                                 "--store", "http://127.0.0.1:1"])
        self.assertEqual(verdict, oac.UNKNOWN, line)

    def test_a_malformed_body_is_unknown_not_no_card(self):
        # A list of non-objects is a shape the code cannot read. Reading it as "no cards"
        # would turn a malformed response into an accusation.
        _, verdict, line = _run(["--closer", "👤 You: pick — 1. x", "--session", SID_A,
                                 "--fixture", _fixture(["oops", "oops"])])
        self.assertEqual(verdict, oac.UNKNOWN, line)

    def test_an_open_item_without_an_item_id_is_unknown(self):
        _, verdict, line = _run(["--closer", "👤 You: pick — 1. x", "--session", SID_A,
                                 "--fixture", _fixture([{"state": "open", "producer_id": SID_A}])])
        self.assertEqual(verdict, oac.UNKNOWN, line)

    def test_a_dict_wrapped_fixture_is_unknown(self):
        _, verdict, line = _run(["--closer", "👤 You: pick — 1. x", "--session", SID_A,
                                 "--fixture", _fixture({"items": []})])
        self.assertEqual(verdict, oac.UNKNOWN, line)

    def test_no_session_is_unknown_not_no_card(self):
        # No session means no join at all — nothing supports an accusation.
        _, verdict, line = _run(["--closer", "👤 You: pick — 1. x", "--session", "",
                                 "--fixture", _fixture([])])
        self.assertEqual(verdict, oac.UNKNOWN, line)


class LoadersShareOneFilter(unittest.TestCase):
    """⚠️ The two loaders were byte-identical copies and diverged once already (a fixture
    certifying behaviour the live read cannot produce). One helper, so they cannot drift."""

    def test_both_loaders_apply_the_same_filter(self):
        rows = [{"item_id": "5" * 32, "state": "answered", "producer_id": SID_A},
                {"item_id": "6" * 32, "state": "open", "producer_id": SID_A}]
        path = _fixture(rows)
        self.addCleanup(os.unlink, path)
        self.assertEqual(oac.load_fixture(path), oac._open_items(rows))
        self.assertEqual([i["item_id"] for i in oac.load_fixture(path)], ["6" * 32])


class FixtureMatchesTheLivePath(unittest.TestCase):
    def test_a_non_open_row_is_filtered_exactly_as_the_store_filters(self):
        # A fixture that certifies behaviour the live path cannot produce is worse than none.
        rows = [{"item_id": "3" * 32, "state": "answered", "producer_id": SID_A}]
        _, verdict, line = _run(["--closer", "👤 You: pick — 1. x", "--session", SID_A,
                                 "--fixture", _fixture(rows)])
        self.assertEqual(verdict, oac.NO_CARD, line)

    def test_a_session_id_field_alone_does_not_match(self):
        # `producer_id` is the store's field; a literal `session_id` would more plausibly mean
        # the SPAWNER, so matching it could attribute a manager's card to a worker.
        rows = [{"item_id": "4" * 32, "state": "open", "session_id": SID_A}]
        self.assertEqual(oac.verdict("👤 You: pick — 1. x", SID_A, rows)[0], oac.NO_CARD)


if __name__ == "__main__":
    unittest.main()
