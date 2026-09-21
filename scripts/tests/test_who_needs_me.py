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
import json
import os
import tempfile
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
    """Run one corpus case through the parser and report how it classifies.

    Mirrors main(): reclassify_idle() re-derives an `idle` record from the
    transcript, then is_open_gate() decides `Needs you` membership and
    is_reapable() decides `Reapable` membership. Both are reported and both are
    asserted, because a record leaves `Needs you` *by becoming* reapable — a fix
    that dropped it from the feed without landing it in `Reapable` would hide a
    decision the operator owns, and an `in_feed`-only corpus cannot tell that apart
    from a correct fix. Idle cases carry their transcript tail inline, so the
    transcript read is stubbed rather than reaching for a real file.
    """
    record = dict(case["record"])
    wnm.last_assistant_text = lambda _rec, _t=case.get("last_assistant_text"): _t or ""
    record = wnm.reclassify_idle(record)

    extra = {}
    if "peer_pane" in case:
        extra["open_panes"] = {case["peer_pane"]}
    if "anchored_task" in case:
        extra["task_status"] = lambda _rec, _c=case: _c["anchored_task"]["status"]

    try:
        return {"in_feed": wnm.is_open_gate(record, **extra),
                "reapable": wnm.is_reapable(record, extra.get("task_status")),
                # The third surface: a rendered closer panel is listed but never
                # counted as a block. Asserting only `in_feed` would let a fix that
                # dropped panels entirely pass -- which is the regression SC2 forbids.
                "is_panel": wnm.is_rendered_panel(record)}
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


class ContextDependentClasses(unittest.TestCase):
    """Classes 3 and 4, plus the Class 3 over-suppression guard.

    Neither class is decidable from the record alone: one needs to know which
    panes hold an open gate, the other whether the anchored task is finished.
    """

    def rec(self, detail, pane="1"):
        return {"session_id": "unit", "pane": pane, "cwd": "/tmp", "kind": "question",
                "detail": detail, "ts": 0, "state": "open"}

    def test_peer_restatement_is_not_a_gate(self):
        """Class 3 — pane 285 carries this gate; the restater is a duplicate."""
        self.assertFalse(wnm.is_open_gate(
            self.rec("The dev-promotion approval in pane 285 — yours alone"),
            open_panes={"285"}))

    def test_mentioning_a_pane_that_holds_no_gate_is_still_a_gate(self):
        """Class 3 guard — a mere mention must never be suppressed."""
        self.assertTrue(wnm.is_open_gate(
            self.rec("The dev-promotion approval in pane 285 — yours alone"),
            open_panes=set()))

    def test_referencing_your_own_pane_is_not_a_peer(self):
        self.assertTrue(wnm.is_open_gate(
            self.rec("pane 285 needs you", pane="285"), open_panes={"285"}))

    def test_close_gate_on_a_finished_task_is_not_a_gate(self):
        """Class 4 — reapable, so it is not counted under Needs you."""
        self.assertFalse(wnm.is_open_gate(
            self.rec("approve: /vault-cli:session-close"),
            task_status=lambda _rec: "completed"))

    def test_close_gate_on_an_open_task_is_still_a_gate(self):
        self.assertTrue(wnm.is_open_gate(
            self.rec("approve: /vault-cli:session-close"),
            task_status=lambda _rec: "in_progress"))

    def test_close_gate_without_a_resolver_is_still_a_gate(self):
        """Fails safe — no evidence, no suppression."""
        self.assertTrue(wnm.is_open_gate(
            self.rec("approve: /vault-cli:session-close"), task_status=None))

    def test_only_the_close_gate_is_reapable(self):
        """A production approval is not made reapable by a finished task."""
        self.assertTrue(wnm.is_open_gate(
            self.rec("approve: apply the merged template to prod"),
            task_status=lambda _rec: "completed"))


class CloseGateForms(unittest.TestCase):
    """Both sanctioned close forms count; neither trap does.

    `vault-cli:sync-progress` Phase 6 emits the `approve:` form, while the
    operator's global DONE rule prescribes the `pick` form. Matching only the first
    left `Reapable (0)` while a finished session using the second sat in
    `Needs you` in the same run (measured 2026-09-19, pane 17).
    """

    def test_approve_form_is_a_close_gate(self):
        self.assertTrue(wnm.is_close_gate_closer("approve: /vault-cli:session-close"))

    def test_pick_form_is_a_close_gate(self):
        self.assertTrue(wnm.is_close_gate_closer(
            "pick — 1. /vault-cli:sync-progress, then /vault-cli:session-close "
            "(recommended) · 2. /vault-cli:session-close directly"))

    def test_pick_leading_with_session_close_is_a_close_gate(self):
        self.assertTrue(wnm.is_close_gate_closer(
            "pick — 1. /vault-cli:session-close (recommended) · 2. keep working"))

    def test_pick_offering_other_work_is_not_a_close_gate(self):
        """First disposition is other work, so the operator is not being asked to close."""
        self.assertFalse(wnm.is_close_gate_closer(
            "pick — 1. keep SC3 as designed; label when you can · 2. rewrite SC3"))

    def test_later_mentioning_session_close_is_not_a_close_gate(self):
        """The widening trap -- a deferral that merely mentions session-close.

        Widening a prefix test into a containment test is exactly how a
        false-negative fix becomes a false positive.
        """
        self.assertFalse(wnm.is_close_gate_closer(
            "later (on the next live call): run /vault-cli:session-close"))

    def test_production_approval_is_not_a_close_gate(self):
        self.assertFalse(wnm.is_close_gate_closer("approve: apply the merged template to prod"))


class StaleDetailIsNotTheLiveCloser(unittest.TestCase):
    """`detail` is hook-written once and never refreshed.

    A hook-written `question` record goes stale the moment its session clears one
    gate and raises another. Measured 2026-09-19: panes 338 and 254 carried a
    cleared `you run:` push gate in `detail` while their live closer was the close
    gate, and both were rejected before their task file was ever opened.
    """

    def rec(self, detail):
        return {"session_id": "unit", "pane": "1", "cwd": "/tmp", "kind": "question",
                "detail": detail, "ts": 0, "state": "open"}

    def stale(self, transcript_closer,
              detail="you run: ! cd ~/.claude && git push origin master"):
        wnm.last_assistant_text = lambda _rec, _t=transcript_closer: f"👤 You: {_t}"
        return self.rec(detail)

    def test_stale_detail_with_a_live_close_gate_is_reapable(self):
        self.assertTrue(wnm.is_reapable(
            self.stale("approve: /vault-cli:session-close"),
            task_status=lambda _rec: "completed"))

    def test_stale_detail_with_a_live_close_gate_leaves_the_feed(self):
        self.assertFalse(wnm.is_open_gate(
            self.stale("approve: /vault-cli:session-close"),
            task_status=lambda _rec: "completed"))

    def test_stale_detail_with_a_live_pick_close_gate_is_reapable(self):
        self.assertTrue(wnm.is_reapable(
            self.stale("pick — 1. /vault-cli:session-close (recommended) · 2. keep working"),
            task_status=lambda _rec: "completed"))

    def test_cleared_gate_alone_is_not_reapable(self):
        """The cached text is a cleared gate, not a close gate -- no evidence."""
        self.assertFalse(wnm.is_reapable(
            self.rec("you run: ! cd ~/.claude && git push origin master"),
            task_status=lambda _rec: "completed"))

    def test_missing_transcript_falls_back_to_detail(self):
        """A record whose session file is gone is classified on what was recorded."""
        wnm.last_assistant_text = lambda _rec: ""
        self.assertTrue(wnm.is_reapable(
            self.rec("approve: /vault-cli:session-close"),
            task_status=lambda _rec: "completed"))


class SiblingRegressionBar(unittest.TestCase):
    """The opposite defect — a real gate omitted.

    [[who-needs-me.py Omits a Pane That Has an Open Gate]] fixed file-existence as
    the gate test: the hook deleted the record on ANY later event, so a background
    task completing erased an open gate and the pane vanished with nothing left to
    classify. `state: answered` is now the only clearing signal, and this pins both
    halves of it — a fix that over-suppresses must fail here.
    """

    def rec(self, state):
        r = {"session_id": "unit", "pane": "1", "cwd": "/tmp", "kind": "question",
             "detail": "pick — 1. do the thing (recommended) · 2. skip", "ts": 0}
        if state is not None:
            r["state"] = state
        return r

    def test_answered_record_is_not_a_gate(self):
        self.assertFalse(wnm.is_open_gate(self.rec("answered")))

    def test_open_record_is_a_gate(self):
        self.assertTrue(wnm.is_open_gate(self.rec("open")))

    def test_record_without_state_is_still_a_gate(self):
        """Fail-open: a missing clearing signal is not a clearing signal."""
        self.assertTrue(wnm.is_open_gate(self.rec(None)))


class BaselineCorpus(unittest.TestCase):
    """Every case in the frozen 2026-09-19 corpus, one test each."""


def _corpus_test(case):
    def test(self):
        got = classify(case)
        for surface in ("in_feed", "reapable", "is_panel"):
            self.assertEqual(
                got[surface],
                case["expect"][surface],
                f"{case['id']} [{case['class']}/{case['provenance']}] {surface}: {case['why']}",
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
        self.assertEqual(classes, {"class1", "class2", "class3", "class4", "class5", "genuine"})

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

    def test_both_reapable_outcomes_are_represented(self):
        """A corpus that cannot tell reapable from not is not evidence for this fix."""
        outcomes = {c["expect"]["reapable"] for c in CASES}
        self.assertEqual(outcomes, {True, False})

    def test_reapable_and_in_feed_are_mutually_exclusive(self):
        """A record is either the operator's to close or in the queue -- never both."""
        for case in CASES:
            if case["expect"]["reapable"]:
                self.assertFalse(case["expect"]["in_feed"], case["id"])

    def test_panel_and_elicitation_are_both_represented(self):
        """Class 5's whole point. Both sides must be in the corpus, or it cannot
        tell the correct fix from either wrong one: a fix that counts panels as
        blocks (over-reporting) and a fix that drops them entirely (hiding panes)
        each need a case that goes red."""
        panels = [c for c in CASES if c["expect"]["is_panel"]]
        real_gates = [c for c in CASES if c["class"] == "class5" and c["expect"]["in_feed"]]
        self.assertGreaterEqual(
            len(panels), 2, "no panel cases -- a fix that drops panels entirely would pass")
        self.assertGreaterEqual(
            len(real_gates), 1,
            "no class5 no-regression bar -- a fix that suppresses every question would pass")

    def test_both_defects_and_both_traps_are_represented(self):
        """A fix for one half must not be able to pass on the others."""
        ids = {c["id"] for c in CASES}
        for required in ("class4-constructed-stale-detail",
                         "class4-constructed-pick-close-form",
                         "class4-guard-later-mentions-session-close",
                         "class4-guard-pick-offering-other-work"):
            self.assertIn(required, ids)


class OrphanLiveness(unittest.TestCase):
    """Class 5 -- an item whose session is gone, rendered because its pane outlived it.

    The feed's render filter was `str(rec["pane"]) in pmap`: pane existence standing
    in for session liveness. A pane id is a lease, not an identifier -- WezTerm
    renumbers and reuses them -- and a session killed without emitting `SessionEnd`
    leaves its item open forever on a pane that still exists. Measured 2026-09-21
    against the live store: 4 such orphans rendered, every one on a live pane.

    Liveness now comes from the session registry (`~/.claude/sessions/<pid>.json`),
    the source named by `vault-cli/docs/session-liveness.md` and read by the attention
    store's `pkg/session-liveness-checker.go`.

    These cases exercise `is_live()` and `live_session_ids()` directly rather than
    through `classify()`: the corpus's `record` dicts carry no pane map or registry,
    so a corpus case could only assert the filter by stubbing both -- which would test
    the stub. The corpus classes stay as they are; this is a separate surface.
    """

    LIVE = "aaaaaaaa-1111-4111-8111-aaaaaaaaaaaa"
    DEAD = "bbbbbbbb-2222-4222-8222-bbbbbbbbbbbb"

    def rec(self, sid, pane="7"):
        return {"session_id": sid, "pane": pane, "cwd": "/tmp",
                "kind": "question", "detail": "pick — 1. do the thing"}

    def registry(self, ids):
        """A temp registry dir holding one `<pid>.json` per id, in the real shape."""
        d = tempfile.mkdtemp(prefix="attn-registry-")
        for i, sid in enumerate(ids):
            with open(os.path.join(d, f"{1000 + i}.json"), "w", encoding="utf-8") as f:
                json.dump({"pid": 1000 + i, "sessionId": sid, "cwd": "/tmp",
                           "status": "idle"}, f)
        return d

    # --- SC1: the orphan is not rendered -------------------------------------

    def test_dead_session_with_live_pane_is_not_rendered(self):
        """The whole defect in one case: pane present, session gone -> no row."""
        reg = self.registry([self.LIVE])
        live = wnm.live_session_ids(reg)
        self.assertEqual(live, {self.LIVE})
        self.assertFalse(wnm.is_live(self.rec(self.DEAD), {"7": {}}, live))

    def test_live_session_with_live_pane_is_rendered(self):
        """SC2, the asymmetry: a fix that drops every unmatched open fails here."""
        reg = self.registry([self.LIVE])
        live = wnm.live_session_ids(reg)
        self.assertTrue(wnm.is_live(self.rec(self.LIVE), {"7": {}}, live))

    def test_pane_check_stays_necessary(self):
        """SC1 third clause: registry-present but no live pane -> not rendered.

        Discriminates a registry-AND-pane filter from a registry-only one. The jump
        line is this feed's payload; a row the operator cannot jump to is not
        actionable.
        """
        reg = self.registry([self.LIVE])
        live = wnm.live_session_ids(reg)
        self.assertFalse(wnm.is_live(self.rec(self.LIVE), {}, live))

    # --- SC3: the registry is read, and its absence is not death --------------

    def test_unreadable_registry_reads_as_live_and_drops_nothing(self):
        """The degradation clause -- and the trap that made it necessary.

        `glob` on a missing directory returns `[]` rather than raising, so an absent
        registry reads as "no session is live" and sweeps the entire feed. Absence is
        not evidence of death; it is evidence the probe cannot run. Mirrors
        `session-liveness-checker.go:59-77` (a failed read returns live, not gone).
        """
        live = wnm.live_session_ids("/nonexistent/registry/path")
        self.assertIsNone(live, "unreadable registry must be None, not an empty set")
        self.assertTrue(wnm.is_live(self.rec(self.DEAD), {"7": {}}, live),
                        "an unreadable registry must drop nothing")
        self.assertTrue(wnm.is_live(self.rec(self.LIVE), {"7": {}}, live))

    def test_empty_registry_is_not_none(self):
        """A real but empty registry IS evidence -- distinct from an unreadable one."""
        reg = self.registry([])
        live = wnm.live_session_ids(reg)
        self.assertEqual(live, set())
        self.assertFalse(wnm.is_live(self.rec(self.DEAD), {"7": {}}, live))

    def test_registry_reader_uses_the_documented_field(self):
        """`sessionId` is the registry's key; a renamed field must not read as dead."""
        reg = self.registry([self.LIVE])
        self.assertEqual(wnm.live_session_ids(reg), {self.LIVE})

    def test_reader_delegates_to_the_registry_rather_than_reimplementing_it(self):
        """SC3's evidence is the CALL, not verdict agreement.

        A hand-duplicated check that happens to match on fixtures passes a
        verdict-agreement reading and is exactly the drift the criterion exists to
        prevent. This asserts the seam: `is_live()` consults the registry set it is
        handed, and `main()` sources that set from `live_session_ids()`.
        """
        reg = self.registry([self.LIVE])
        live = wnm.live_session_ids(reg)
        # Same record, same pane map, opposite verdicts -- decided only by the registry.
        self.assertTrue(wnm.is_live(self.rec(self.LIVE), {"7": {}}, live))
        self.assertFalse(wnm.is_live(self.rec(self.DEAD), {"7": {}}, live))
        src = open(_SCRIPT, encoding="utf-8").read()
        self.assertIn("live_session_ids()", src,
                      "main() must source liveness from the registry reader")


if __name__ == "__main__":
    unittest.main()
