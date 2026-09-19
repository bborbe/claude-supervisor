#!/usr/bin/env python3
"""Tests for scripts/who-needs-me.py.

Covers the logic that decides which sessions land in `Needs you`. The feed
answers "was a gate raised", never "is a gate open", and it is triaged
oldest-first -- so a false positive is not merely noise, it is the first thing a
manager reaches. Measured 2026-09-19: 19 listed, roughly 7 real.

Four false-positive classes, each reproduced against the frozen corpus in
`fixtures/attention-feed-baseline.json`:

  * Class 1 -- HTML-entity padding. A closer written `👤 You: &nbsp;&nbsp;nothing`
    is six literal characters, not whitespace, so `startswith("nothing")` is
    False and the parked session is reclassified as an open question.
  * Class 2 -- `later (on <trigger>):` is a parked wait, not a gate. It does not
    begin with `nothing`, so it enters the feed and stays there for hours.
  * Class 3 -- peer-gate restatement. A closer naming another pane while
    describing that pane's gate is not a second INDEPENDENT gate.
  * Class 4 -- a close gate (`approve: /vault-cli:session-close`) whose anchored
    task already reads `status: completed` is reapable, not an operator decision.

The corpus is load-bearing and its provenance is recorded per case: `live` means
copied verbatim from a real `*.needs.json`, `constructed` means hand-written
because no live instance survived. A constructed case is a parser unit case and
carries no claim about the live fleet -- see the fixture's `provenance_legend`.

The no-regression bar is the `genuine` cases: a real, currently-open gate must
still appear. A fix that suppresses everything passes every defect assertion and
fails these.

Run: python3 -m unittest discover -s scripts/tests -v
"""

import importlib.util
import json
import os
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_SCRIPT = os.path.join(os.path.dirname(_HERE), "who-needs-me.py")
_FIXTURE = os.path.join(_HERE, "fixtures", "attention-feed-baseline.json")

_spec = importlib.util.spec_from_file_location("who_needs_me", _SCRIPT)
wnm = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(wnm)

with open(_FIXTURE, encoding="utf-8") as _handle:
    CORPUS = json.load(_handle)

CASES = CORPUS["cases"]


def classify(case):
    """Run one corpus case through the parser and report feed membership.

    Mirrors main(): reclassify_idle() re-derives an `idle` record from the
    transcript, then is_open_gate() decides whether it belongs in `Needs you`.
    Idle cases carry their transcript tail inline, so the transcript read is
    stubbed rather than reaching for a real file.
    """
    record = dict(case["record"])
    wnm.last_assistant_text = lambda _rec, _t=case.get("last_assistant_text"): _t or ""
    record = wnm.reclassify_idle(record)

    extra = {}
    if "peer_pane" in case:
        pane = case["peer_pane"]
        extra["pmap"] = {pane: {"pane_id": int(pane)}}
    if "anchored_task" in case:
        extra["task_status"] = lambda _sid, _c=case: _c["anchored_task"]["status"]

    try:
        return wnm.is_open_gate(record, **extra)
    except TypeError as exc:
        raise AssertionError(
            f"is_open_gate() has no seam for {case['class']} "
            f"(needs {sorted(extra)}): {exc}"
        ) from exc


class ParserNormalization(unittest.TestCase):
    """Classes 1 and 2 -- pure parser defects, no extra context needed."""

    def closer(self, text):
        return {"session_id": "unit", "pane": "1", "cwd": "/tmp", "kind": "idle",
                "detail": "OK", "ts": 0, "state": "open"}

    def feed(self, closer_line, kind="idle"):
        wnm.last_assistant_text = lambda _rec, _t=closer_line: _t
        return wnm.is_open_gate(wnm.reclassify_idle(self.closer(closer_line)))

    def test_plain_nothing_is_not_a_gate(self):
        """The control: already correct today, and must stay correct."""
        self.assertFalse(self.feed("👤 You: nothing"))

    def test_nbsp_padded_nothing_is_not_a_gate(self):
        """Class 1. `&nbsp;` is not whitespace, so strip() does not remove it."""
        self.assertFalse(self.feed("👤 You: &nbsp;&nbsp;&nbsp;&nbsp;nothing"))

    def test_nbsp_without_space_is_not_a_gate(self):
        """Class 1, second shape: no space between the colon and the entity."""
        self.assertFalse(self.feed("👤 You:&nbsp;nothing"))

    def test_later_on_trigger_is_not_a_gate(self):
        """Class 2. A parked wait is not an unanswered gate."""
        self.assertFalse(self.feed("👤 You: later (on flying it): refresh head/module stock"))

    def test_later_on_trigger_with_leading_space_is_not_a_gate(self):
        self.assertFalse(self.feed("👤 You:  later (on the next live Discord call): ask a question"))

    def test_pick_is_a_gate(self):
        self.assertTrue(self.feed("👤 You: pick — 1. do the thing (recommended) · 2. skip"))

    def test_you_run_is_a_gate(self):
        self.assertTrue(self.feed("👤 You: you run: reopen the broker console"))

    def test_approve_is_a_gate(self):
        self.assertTrue(self.feed("👤 You: approve: apply to prod"))


class BaselineCorpus(unittest.TestCase):
    """Every case in the frozen 2026-09-19 corpus, one test each."""


def _corpus_test(case):
    def test(self):
        self.assertEqual(
            classify(case),
            case["expect"]["in_feed"],
            f"{case['id']} [{case['class']}/{case['provenance']}]: {case['why']}",
        )
    test.__name__ = "test_" + case["id"].replace("-", "_")
    test.__doc__ = f"[{case['class']}/{case['provenance']}] {case['why']}"
    return test


for _case in CASES:
    setattr(BaselineCorpus, _corpus_test(_case).__name__, _corpus_test(_case))


class CorpusIntegrity(unittest.TestCase):
    """The corpus must stay able to discriminate -- a corpus that cannot fail
    is not evidence."""

    def test_every_defect_class_is_represented(self):
        classes = {c["class"] for c in CASES}
        self.assertEqual(classes, {"class1", "class2", "class3", "class4", "genuine"})

    def test_both_outcomes_are_represented(self):
        outcomes = {c["expect"]["in_feed"] for c in CASES}
        self.assertEqual(outcomes, {True, False})

    def test_provenance_is_recorded_for_every_case(self):
        for case in CASES:
            self.assertIn(case["provenance"], ("live", "constructed"), case["id"])

    def test_constructed_cases_are_flagged_in_their_rationale(self):
        """A constructed case must never read as live evidence."""
        for case in CASES:
            if case["provenance"] == "constructed":
                self.assertIn("CONSTRUCTED", case["why"], case["id"])

    def test_genuine_controls_exist_for_multiple_closer_verbs(self):
        """No-regression coverage: a fix that suppresses everything must fail."""
        genuine = [c for c in CASES if c["class"] == "genuine" and c["expect"]["in_feed"]]
        self.assertGreaterEqual(len(genuine), 5, "too few genuine controls to catch over-suppression")


if __name__ == "__main__":
    unittest.main()
