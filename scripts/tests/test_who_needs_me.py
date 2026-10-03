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

import contextlib
import importlib.util
import io
import json
import os
import re
import shutil
import tempfile
import time
import time
import unittest
from unittest import mock

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

    [[a separate task]] fixed file-existence as
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
    leaves its item open forever on a pane that still exists.

    The fix applies the liveness rule's `quiet` verdict, which is **both** signals:
    absent from the session registry AND a stale transcript. Either one alone is
    wrong, and each has its own case below -- registry absence alone drops live
    headless workers, and transcript staleness alone drops a live-but-idle session.

    `is_live()` stays a pure predicate (records, pane map, quiet set); the two
    machine-reading halves are `live_session_ids()` and `quiet_session_ids()`.
    """

    LIVE = "aaaaaaaa-1111-4111-8111-aaaaaaaaaaaa"   # registered, interactive
    HEADLESS = "cccccccc-3333-4333-8333-cccccccccccc"  # live worker, NO registry entry
    DEAD = "bbbbbbbb-2222-4222-8222-bbbbbbbbbbbb"   # no registry entry, stale transcript

    def setUp(self):
        self._age = wnm.session_transcript_age
        wnm._AGE_CACHE.clear()
        self.addCleanup(self._restore)

    def _restore(self):
        wnm.session_transcript_age = self._age
        wnm._AGE_CACHE.clear()

    def ages(self, mapping):
        """Stub the transcript read: sid -> age in seconds (absent => inf)."""
        wnm.session_transcript_age = lambda sid: mapping.get(sid, float("inf"))

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

    def quiet(self, records, registry_ids, ages):
        self.ages(ages)
        return wnm.quiet_session_ids(records, wnm.live_session_ids(self.registry(registry_ids)))

    # --- SC1: the orphan is not rendered -------------------------------------

    def test_dead_session_with_live_pane_is_not_rendered(self):
        """The whole defect: pane present, session gone (absent + stale) -> no row."""
        rec = self.rec(self.DEAD)
        q = self.quiet([rec], [], {self.DEAD: 10 * 3600})
        self.assertEqual(q, {self.DEAD})
        self.assertFalse(wnm.is_live(rec, {"7": {}}, q))

    def test_store_vouched_session_is_never_quiet(self):
        """The correction: a row the STORE vouched for is not re-judged here.

        The store resolves producer liveness server-side and drops dead askers as a
        side effect of the read, so a row that came back from it has ALREADY had its
        liveness decided -- by the component that owns that question, with a better
        signal than this reader has. Re-deciding it on transcript staleness is a
        *worse* opinion, and it is wrong for exactly the class this feed protects: a
        session blocked on a gate writes nothing, so its transcript ages past
        LIVE_WINDOW, so the reader called it `provably finished` and dropped the row
        the operator was being waited on -- the ask vanishing BECAUSE nobody answered
        it. Measured 2026-09-28 on pane 2555: row present 09:46:48, absent 09:51:48,
        back 10:01:49, while the store held its open item throughout.
        """
        rec = self.rec(self.DEAD)
        rec["store_vouched"] = True
        q = self.quiet([rec], [], {self.DEAD: 10 * 3600})
        self.assertEqual(q, set())
        self.assertTrue(wnm.is_live(rec, {"7": {}}, q))

    def test_unvouched_row_keeps_the_orphan_rule(self):
        """The protection the correction must NOT break -- same shape, no voucher.

        A log-fallback row was never liveness-filtered by anything, so registry
        absence plus staleness is still the best signal available and the orphan must
        still be dropped. ⚠️ Widening the correction to "a session holding an open
        gate is never quiet" fails this test and the one above it -- that was the
        first attempt, and blocked-live and dead are indistinguishable under these
        two signals alone. Only the store's own verdict separates them.
        """
        rec = self.rec(self.DEAD)
        q = self.quiet([rec], [], {self.DEAD: 10 * 3600})
        self.assertEqual(q, {self.DEAD})
        self.assertFalse(wnm.is_live(rec, {"7": {}}, q))

    def test_live_session_with_live_pane_is_rendered(self):
        """SC2, the asymmetry: a fix that drops every unmatched open fails here."""
        rec = self.rec(self.LIVE)
        q = self.quiet([rec], [self.LIVE], {self.LIVE: 10 * 3600})
        self.assertEqual(q, set())
        self.assertTrue(wnm.is_live(rec, {"7": {}}, q))

    def test_pane_check_stays_necessary(self):
        """SC1 third clause: a live session with no live pane -> not rendered.

        The jump line is this feed's payload; a row the operator cannot jump to is
        not actionable.
        """
        rec = self.rec(self.LIVE)
        q = self.quiet([rec], [self.LIVE], {self.LIVE: 0})
        self.assertFalse(wnm.is_live(rec, {}, q))

    # --- the two halves of `quiet`, each of which alone is wrong --------------

    def test_live_headless_worker_is_not_quiet_despite_no_registry_entry(self):
        """THE REGRESSION CASE -- registry absence alone is not death.

        A headless worker is an in-process SDK `query()` in the supervisor server: it
        holds no socket, so Claude Code writes it no registry entry at all. Measured
        2026-09-21: of 21 workers the ledger called `running`, 0 were in the registry.
        A filter keyed on registry absence alone therefore drops every gate a headless
        worker raises -- including ones it is asking right now. The fresh transcript is
        what separates it from a finished session.
        """
        rec = self.rec(self.HEADLESS)
        q = self.quiet([rec], [], {self.HEADLESS: 30})   # absent from registry, fresh
        self.assertEqual(q, set(), "a live headless worker must not read as quiet")
        self.assertTrue(wnm.is_live(rec, {"7": {}}, q))

    def test_live_but_idle_session_is_not_quiet_despite_stale_transcript(self):
        """The mirror case -- transcript staleness alone is not death either.

        A registered session with an idle transcript is `live` per the rule's
        `stale + alive` row. This is why transcript recency alone was rejected as the
        source: it would drop a genuinely parked gate.
        """
        rec = self.rec(self.LIVE)
        q = self.quiet([rec], [self.LIVE], {self.LIVE: 10 * 3600})   # stale, registered
        self.assertEqual(q, set())
        self.assertTrue(wnm.is_live(rec, {"7": {}}, q))

    def test_fresh_transcript_absent_from_registry_is_indeterminate_not_quiet(self):
        """The rule's `fresh + none` row is `indeterminate`, and cannot prove death."""
        rec = self.rec(self.HEADLESS)
        q = self.quiet([rec], [], {self.HEADLESS: 1})
        self.assertNotIn(self.HEADLESS, q)

    # --- SC3: the registry is read, and its absence is not death --------------

    def test_unreadable_registry_reads_as_live_and_drops_nothing(self):
        """The degradation clause -- and the trap that made it necessary.

        `glob` on a missing directory returns `[]` rather than raising, so an absent
        registry reads as "no session is live" and sweeps the entire feed. Absence is
        not evidence of death; it is evidence the probe cannot run. Mirrors
        `session-liveness-checker.go:59-77` (a failed read returns live, not gone).
        """
        self.assertIsNone(wnm.live_session_ids("/nonexistent/registry/path"),
                          "unreadable registry must be None, not an empty set")
        rec = self.rec(self.DEAD)
        q = wnm.quiet_session_ids([rec], None)   # None => nothing provably quiet
        self.assertEqual(q, set())
        self.assertTrue(wnm.is_live(rec, {"7": {}}, q),
                        "an unreadable registry must drop nothing")

    def test_empty_registry_is_not_none(self):
        """A real but empty registry IS evidence -- distinct from an unreadable one."""
        self.assertEqual(wnm.live_session_ids(self.registry([])), set())

    def test_registry_reader_uses_the_documented_field(self):
        """`sessionId` is the registry's key; a renamed field must not read as dead."""
        self.assertEqual(wnm.live_session_ids(self.registry([self.LIVE])), {self.LIVE})


    def test_reader_delegates_to_the_registry_rather_than_reimplementing_it(self):
        """SC3's evidence is the CALL, not verdict agreement.

        A hand-duplicated check that happens to match on fixtures passes a
        verdict-agreement reading and is exactly the drift the criterion exists to
        prevent. This asserts the seam: `quiet_session_ids()` consults the registry
        set it is handed, and `main()` sources that set from the registry reader.

        The seam moved from `live_session_ids()` to `read_registry()` on 2026-09-21,
        when the ownership check needed the registry's *names* and not only its ids.
        One read now answers both questions, so `main()` takes the map and derives
        the set from it — the call is still a delegation, not a reimplementation.
        """
        rec = self.rec(self.DEAD)
        q = self.quiet([rec], [], {self.DEAD: 10 * 3600})
        self.assertIn(self.DEAD, q)
        with open(_SCRIPT, encoding="utf-8") as handle:
            src = handle.read()
        self.assertIn("read_registry()", src,
                      "main() must source liveness from the registry reader")


class PaneResolutionWithoutALogLine(unittest.TestCase):
    """A store row whose pane the event log cannot locate must still be rendered.

    ⚠️ **The defect, and why the pane is absent rather than wrong.** A store row takes
    its pane from the hook's own event log, joined on `dedup_key`
    (`normalize_store_item`). When no line exists under that key the row carries no
    pane at all, and `is_live()`'s pane clause -- `str(rec.get("pane")) not in pmap` --
    then drops the row before any classifier sees it. The operator is never told the
    session is parked, so it waits indefinitely: the ask disappears *because* nobody
    answered it. Measured 2026-10-01 on a parked pane 479 (session `29d4464c`), whose
    store item was open and whose modal footer was on screen, while the feed rendered
    the pane `absent`.

    Two live producers write no line under their own `dedup_key`, so the row has no
    pane to record: `attention-push.py` (a `session:`-marked declaration, not a hook
    event) and, after `owned_pane()` began refusing an unprovable pane, any session
    that **inherits** `WEZTERM_PANE` from its spawner -- a headless worker does this by
    construction, and its item now carries an empty pane by design.

    **The fix is the join the board already uses.** `attention-controller` resolved
    this same class with a second resolution source beside the log: the item's session
    is named from its registry entry, and that name is matched against the live panes'
    glyph-stripped titles. The feed has the identical join already -- `is_routable()`
    proves a *recorded* pane by exactly that test -- so this is the same predicate
    reached from the other side, not a new one.

    **Why a name-keyed join is the right instrument here.** The registry carries
    `sessionId` and `name`; a pane carries `pane_id` and `title`. The glyph-stripped
    title-vs-name comparison is the only session->pane join either side can observe,
    and it makes no claim when the session cannot be named -- which is what keeps a
    nameless row from being routed to a plausible-looking wrong pane.
    """

    SID = "dddddddd-4444-4444-8444-dddddddddddd"
    NAME = "Parked Worker"

    def setUp(self):
        self._age = wnm.session_transcript_age
        wnm._AGE_CACHE.clear()
        self.addCleanup(self._restore)

    def _restore(self):
        wnm.session_transcript_age = self._age
        wnm._AGE_CACHE.clear()

    def pane_map(self, title=None, pane_id=479):
        """The live panes as `wezterm cli list --format json` reports them."""
        return {str(pane_id): {"pane_id": pane_id, "title": title or f"◐ {self.NAME}"}}

    #: Distinguishes "caller passed nothing" from "caller passed an empty string",
    #: which the unnamed-session case needs to express.
    _UNSET = object()

    def registry(self, name=_UNSET, sid=_UNSET):
        """A registry map in the real shape; the no-arg call is the EMPTY one.

        `{}` and `None` are different answers and the tests below use both: an empty
        registry means no session is registered, while `None` is an *unreadable* one
        that proves nothing. Collapsing them is the `pane-reads.md` defect.
        """
        if name is self._UNSET and sid is self._UNSET:
            return {}
        return {(self.SID if sid is self._UNSET else sid):
                (self.NAME if name is self._UNSET else name)}

    def live_registry(self):
        """The registry holding this session under its real name."""
        return self.registry(sid=self.SID, name=self.NAME)

    def store_row(self, pane=None, sid=_UNSET):
        """A store row as `normalize_store_item` yields it when the join found no line.

        `store_vouched` is set, as `needs_source()` sets it on every store row: the
        store resolved the producer's liveness server-side, so this reader must not
        re-judge it -- the row's absence from the feed is the pane clause's doing, not
        the quiet set's, and the fixture must not be able to pass for the wrong reason.
        """
        return {"session_id": self.SID if sid is self._UNSET else sid, "pane": pane, "cwd": "/tmp",
                "kind": "permission", "detail": "Bash: make build -n",
                "state": "open", "ts": 0, "store_vouched": True,
                "store_item_id": "75264206"}

    # --- the defect: no pane -> dropped --------------------------------------

    def test_store_row_without_a_pane_is_dropped_today(self):
        """The control: this is the current, wrong behaviour the fix must change.

        The row is live by every other signal -- the session is registered, its
        transcript is fresh, and the pane its name resolves to is on screen. Only the
        absent pane drops it.
        """
        rec = self.store_row()
        self.assertIsNone(rec["pane"], "the fixture must carry NO pane to be the case")
        self.assertFalse(wnm.is_live(rec, self.pane_map(), set()),
                         "an absent pane drops the row before the fix")

    def test_resolving_the_pane_by_name_makes_the_row_live(self):
        """SC2: the resolved pane survives `is_live()`, so the row reaches the feed."""
        rec = wnm.resolve_missing_panes([self.store_row()], self.pane_map(),
                                        self.live_registry())[0]
        self.assertEqual(str(rec["pane"]), "479", "the pane is resolved by name")
        self.assertTrue(wnm.is_live(rec, self.pane_map(), set()))

    def test_resolved_pane_is_routable(self):
        """The jump line is this feed's payload -- a resolved pane must carry one.

        A pane filled in for liveness but refused by `is_routable()` would render the
        row with no way to reach it, which is the dead end `answer_handover()` exists
        to cover and not the outcome this fix claims.
        """
        rec = wnm.resolve_missing_panes([self.store_row()], self.pane_map(),
                                        self.live_registry())[0]
        self.assertTrue(wnm.is_routable(rec, self.pane_map(), self.live_registry()))

    def test_resolved_pane_renders_under_needs_you(self):
        """SC2, end to end through the classifier the feed's `Needs you` list uses."""
        rec = wnm.resolve_missing_panes([self.store_row()], self.pane_map(),
                                        self.live_registry())[0]
        self.assertTrue(wnm.is_open_gate(rec))

    # --- the guards: resolution must not invent a pane -----------------------

    def test_recorded_pane_is_left_untouched(self):
        """The fallback fills a gap; it never overrides what the log recorded.

        A recorded pane is the event-time fact, and re-resolving it would replace it
        with a name-guess -- the confident-wrong-direction defect `is_routable()`
        refuses. The controller draws the same line: the logged path wins wherever a
        line exists.
        """
        rec = wnm.resolve_missing_panes([self.store_row(pane="999")], self.pane_map(),
                                        self.live_registry())[0]
        self.assertEqual(rec["pane"], "999", "a recorded pane is not re-resolved")

    def test_unnamed_session_claims_no_pane(self):
        """An unresolvable session yields NO pane -- an absence, never a guess.

        This is the load-bearing safety property, and it is why the join is keyed on
        the registry's name: handing an unnamed session the first pane on screen would
        mark every pane as its own and stamp a confident route onto a row the reader
        knows nothing about.
        """
        rec = wnm.resolve_missing_panes([self.store_row()], self.pane_map(),
                                        self.registry(name="", sid=""))[0]
        self.assertIsNone(rec["pane"])
        self.assertFalse(wnm.is_live(rec, self.pane_map(), set()))

    def test_no_matching_pane_claims_nothing(self):
        """A named session whose pane is not on screen resolves to nothing."""
        rec = wnm.resolve_missing_panes([self.store_row()], self.pane_map(title="◐ Other"),
                                        self.registry())[0]
        self.assertIsNone(rec["pane"])

    def test_unreadable_pane_list_claims_nothing(self):
        """`None` panes cannot prove a pane exists -- same rule as `is_live()`.

        `{}` (readable, empty) and `None` (unreadable) are different answers, and only
        the second is an absence of one. Neither may produce a pane here.
        """
        self.assertIsNone(wnm.resolve_missing_panes(
            [self.store_row()], None, self.live_registry())[0]["pane"])

    def test_unreadable_registry_claims_nothing(self):
        """An unreadable registry names no session, so no pane can be resolved.

        `None` is the unreadable answer -- distinct from `{}`, a real read that found
        no session. Neither resolves a pane; this one because nothing can be looked
        up, the next because the session genuinely is not there.
        """
        self.assertIsNone(wnm.resolve_missing_panes(
            [self.store_row()], self.pane_map(), None)[0]["pane"])

    def test_empty_registry_claims_nothing(self):
        """A readable-but-empty registry holds no name, so no pane is claimed."""
        self.assertIsNone(wnm.resolve_missing_panes(
            [self.store_row()], self.pane_map(), {})[0]["pane"])

    def test_row_without_a_session_is_left_alone(self):
        """A cron job or agent item names no session; there is nothing to look up."""
        rec = self.store_row(sid="")
        self.assertIsNone(wnm.resolve_missing_panes([rec], self.pane_map(),
                                                    self.live_registry())[0]["pane"])

class AttentionStoreSource(unittest.TestCase):
    """The store is a THIRD producer of one record shape, not a second path.

    Every case pins a boundary the reader must not cross. The store carries
    exactly the schema's fourteen fields, so a name, a pane and an age all have
    to come from somewhere else -- and the failure mode of getting that wrong is
    not an error, it is a plausible-looking row that routes the operator to
    another session's pane.
    """

    def setUp(self):
        wnm._ENRICHED_CACHE.clear()
        self._store_items = wnm.store_items
        self._load_events = wnm.load_events

    def tearDown(self):
        wnm.store_items = self._store_items
        wnm.load_events = self._load_events
        wnm._ENRICHED_CACHE.clear()

    def item(self, **over):
        base = {
            "item_id": "store-1",
            "producer_id": "sess-a",
            "producer_kind": "session",
            "liveness_ref": "heartbeat:/tmp/hb",
            "dedup_key": "log-1",
            "interrupt_class": "pick",
            "payload": "Which liveness source should live() consult?",
            "answer_mechanism": "message",
            "state": "open",
            "created_at": "2026-09-21T09:00:00.000000000+02:00",
        }
        base.update(over)
        return base

    def test_mechanism_maps_to_the_readers_kind_vocabulary(self):
        self.assertEqual("question", wnm.normalize_store_item(self.item(), {})["kind"])
        rec = wnm.normalize_store_item(self.item(answer_mechanism="permission"), {})
        self.assertEqual("permission", rec["kind"])

    def test_ack_is_not_a_gate(self):
        """`ack` is the enum's third value and is not a block.

        The reader's vocabulary is {permission, question}; rendering an
        acknowledgement as a gate would invent a decision nobody was asked for.
        """
        self.assertIsNone(wnm.normalize_store_item(self.item(answer_mechanism="ack"), {}))

    def test_event_fields_join_on_dedup_key(self):
        events = {"log-1": {"pane": 494, "cwd": "/tmp/x", "transcript": "/tmp/t.jsonl"}}
        rec = wnm.normalize_store_item(self.item(), events)
        self.assertEqual(494, rec["pane"])
        self.assertEqual("/tmp/x", rec["cwd"])

    def test_missing_event_leaves_the_pane_absent_rather_than_defaulted(self):
        """A defaulted pane is worse than an absent one -- it routes somewhere."""
        self.assertNotIn("pane", wnm.normalize_store_item(self.item(), {}))

    def test_enrichment_supplies_the_session_name_and_the_render_uses_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.makedirs(os.path.join(tmp, "enriched"))
            with open(os.path.join(tmp, "enriched", "sess-a.json"), "w") as handle:
                json.dump({"session_id": "sess-a", "session_name": "Attention Routing"}, handle)
            original, wnm.ENRICHED = wnm.ENRICHED, os.path.join(tmp, "enriched")
            try:
                rec = wnm.normalize_store_item(self.item(), {})
            finally:
                wnm.ENRICHED = original
        self.assertEqual("Attention Routing", wnm.name_of(rec, {}))

    def test_absent_enrichment_renders_absent(self):
        with tempfile.TemporaryDirectory() as tmp:
            original, wnm.ENRICHED = wnm.ENRICHED, os.path.join(tmp, "enriched")
            try:
                rec = wnm.normalize_store_item(self.item(), {})
            finally:
                wnm.ENRICHED = original
        self.assertNotIn("session_name", rec)

    def test_unreachable_store_falls_back_and_says_so(self):
        """A silent fallback is indistinguishable from a working store."""
        def down():
            raise wnm.StoreUnreachable("connection refused")

        wnm.store_items = down
        wnm.load_events = lambda: [{"item_id": "log-1", "session_id": "sess-a"}]
        self.assertEqual(1, len(wnm.needs_source()))
        self.assertEqual("store unreachable — reading event log", wnm.SOURCE_NOTE)

    def test_fallback_rows_are_stamped_replayed(self):
        """The note is printed once; the ROW is what a manager scans.

        Without the stamp every fallback row renders through the same `row()` a
        live read uses, so a replayed gate and a live one differ only in the age
        column -- and age is not provenance, since a live gate can be hours old
        and a replayed one minutes old. Measured 2026-09-27: 18 replayed rows
        against 2 live, with nothing on any row to tell them apart.
        """
        def down():
            raise wnm.StoreUnreachable("connection refused")

        wnm.store_items = down
        wnm.load_events = lambda: [{"item_id": "log-1", "session_id": "sess-a"}]
        rows = wnm.needs_source()
        self.assertTrue(all(r.get("replayed") for r in rows))

    def test_store_rows_are_not_stamped_replayed(self):
        """The stamp means "the fallback produced this", not "this looks old".

        A live read must carry no stamp at all: a marker present on every row
        would change every live row and fail the no-regression requirement that
        a responsive store's output is unchanged.
        """
        wnm.store_items = lambda: [self.item()]
        wnm.load_events = lambda: []
        rows = wnm.needs_source()
        self.assertTrue(all(not r.get("replayed") for r in rows))

    def test_reachable_store_prints_no_note(self):
        wnm.store_items = lambda: [self.item()]
        wnm.load_events = lambda: []
        self.assertEqual(1, len(wnm.needs_source()))
        self.assertIsNone(wnm.SOURCE_NOTE)

    def test_idle_records_survive_the_store_path(self):
        """The store carries only the kinds the watcher pushes.

        `idle` is not one of them, and it is not dead weight: the reader
        promotes an idle record to a real gate by reading its transcript. A
        store-only read would silently drop every gate arriving that way.
        """
        wnm.store_items = lambda: [self.item()]
        wnm.load_events = lambda: [
            {"item_id": "log-1", "session_id": "sess-a", "kind": "question"},
            {"item_id": "idle-1", "session_id": "sess-b", "kind": "idle"},
        ]
        kinds = sorted(r.get("kind") for r in wnm.needs_source())
        self.assertEqual(["idle", "question"], kinds)

    def test_idle_record_is_not_duplicated_when_the_store_also_carries_it(self):
        wnm.store_items = lambda: []
        wnm.load_events = lambda: [{"item_id": "idle-1", "session_id": "s", "kind": "idle"}]
        self.assertEqual(1, len(wnm.needs_source()))

    def test_unvalidatable_pane_is_marked_not_presented(self):
        """Pane ids are recycled, so a stale one is another session's pane."""
        rec = {"pane": 999, "ts": 0, "cwd": "/tmp/x", "kind": "question", "detail": "d"}
        self.assertFalse(wnm.is_routable(rec, {"1": {}}))
        rendered = wnm.row(rec, {"1": {}}, "d")
        self.assertIn("unroutable", rendered)
        self.assertNotIn("activate-pane", rendered)

    def test_valid_pane_renders_a_jump_line(self):
        rec = {"pane": 7, "ts": 0, "cwd": "/tmp/x", "kind": "question", "detail": "d"}
        self.assertTrue(wnm.is_routable(rec, {"7": {}}))
        self.assertIn("activate-pane --pane-id 7", wnm.row(rec, {"7": {}}, "d"))

    def test_store_rows_still_render_an_age(self):
        """SC7(a) is a regression guard, not new behaviour.

        `row()` aged every row before the store existed; the store path must not
        be the one that stops.
        """
        rec = {"pane": 7, "ts": time.time() - 3600, "cwd": "/tmp/x",
               "kind": "question", "detail": "d"}
        self.assertIn("1h00m", wnm.row(rec, {"7": {}}, "d"))

    def test_cap_withholds_and_counts(self):
        rows = list(range(30))
        shown, withheld = wnm.capped(rows, show_all=False)
        self.assertEqual(wnm.CAP, len(shown))
        self.assertEqual(30 - wnm.CAP, withheld)

    def test_cap_is_lifted_by_all(self):
        shown, withheld = wnm.capped(list(range(30)), show_all=True)
        self.assertEqual(30, len(shown))
        self.assertEqual(0, withheld)

    def test_cap_is_silent_below_the_threshold(self):
        shown, withheld = wnm.capped(list(range(3)), show_all=False)
        self.assertEqual(3, len(shown))
        self.assertEqual(0, withheld)


class PaneOwnership(unittest.TestCase):
    """Existence is not ownership -- the recycled pane that a live list still carries.

    `is_routable()` was `str(pane) in pmap`, which rejects a pane that is *gone* and
    never one that exists and belongs to a different session. A headless worker
    inherits its spawner's `WEZTERM_PANE`, so its item carries the spawner's pane id
    — which exists, so the row rendered a confident jump to the wrong tab.
    Reproduced 2026-09-21 and recorded at the attention-item schema § Silence 7.

    Ownership is proven by the name: the pane's title against the session's current
    name, both glyph-stripped. The rule fires on a *mismatch* — a session with no name
    to compare keeps its row, because stripping the jump from an unprovable row is a
    regression dressed as a safety fix.
    """

    def setUp(self):
        wnm._REGISTRY_CACHE.clear()
        self.addCleanup(wnm._REGISTRY_CACHE.clear)

    def rec(self, sid="s", pane="7", name=None):
        rec = {"session_id": sid, "pane": pane, "ts": 0, "cwd": "/tmp/x",
               "kind": "question", "detail": "d"}
        if name is not None:
            rec["session_name"] = name
        return rec

    def test_a_pane_belonging_to_another_session_is_not_routable(self):
        """THE DEFECT -- the pane exists, and it is not this session's."""
        rec = self.rec(name="worker-verify")
        pmap = {"7": {"title": "◐ some other session"}}
        self.assertFalse(wnm.is_routable(rec, pmap, {"s": "worker-verify"}))
        rendered = wnm.row(rec, pmap, "d", {"s": "worker-verify"})
        self.assertIn("unroutable", rendered)
        self.assertNotIn("activate-pane", rendered)

    def test_a_matching_title_is_routable(self):
        """The positive control: the check can pass, so it is not a constant."""
        rec = self.rec(name="worker-verify")
        pmap = {"7": {"title": "◐ worker-verify"}}
        self.assertTrue(wnm.is_routable(rec, pmap, {"s": "worker-verify"}))
        self.assertIn("activate-pane --pane-id 7",
                      wnm.row(rec, pmap, "d", {"s": "worker-verify"}))

    def test_the_status_glyph_is_stripped_from_both_sides(self):
        """The name carries a glyph the pane title may lack.

        Measured 2026-09-21: 17 of 27 rendered rows disagreed on the leading `⚙`
        alone. Stripping one side only would have marked two thirds of the feed
        unroutable.
        """
        rec = self.rec(name="⚙ Test Worker")
        pmap = {"7": {"title": "✳ ⚙ Test Worker"}}
        self.assertTrue(wnm.is_routable(rec, pmap, {"s": "⚙ Test Worker"}))

    def test_a_renamed_session_is_routable_from_the_registry(self):
        """The registry rewrites `name`; the enrichment snapshot does not.

        Measured 2026-09-21: one live session read `Sample Task` in the registry against
        `Renamed Sample Task` in its enrichment record, the old name sitting in the
        registry's own `formerNames`. Comparing against the snapshot marked that row
        unroutable while its pane was genuinely correct — a false positive on every
        rename, and renames are routine.
        """
        rec = self.rec(name="Renamed Sample Task")     # the stale snapshot
        pmap = {"7": {"title": "◐ Sample Task"}}       # the current title
        self.assertTrue(wnm.is_routable(rec, pmap, {"s": "Sample Task"}))

    def test_a_headless_worker_with_no_registry_entry_still_mismatches(self):
        """No registry entry is not a licence to skip the check.

        A headless worker is an in-process SDK query holding no entry at all, so the
        enrichment record is the only name available — and it is exactly what makes
        its inherited pane detectable.
        """
        rec = self.rec(name="worker-verify")
        pmap = {"7": {"title": "◐ the spawner's session"}}
        self.assertFalse(wnm.is_routable(rec, pmap, {}))

    def test_unprovable_ownership_keeps_the_row(self):
        """A mismatch is required; an absence is not a verdict."""
        rec = self.rec()
        self.assertTrue(wnm.is_routable(rec, {"7": {"title": "◐ anything"}}, {}))

    def test_a_gone_pane_is_still_not_routable(self):
        """Existence is added to, not replaced -- a pane that is gone still fails."""
        rec = self.rec(name="worker-verify")
        self.assertFalse(wnm.is_routable(rec, {"1": {}}, {"s": "worker-verify"}))


class UnroutableHandover(unittest.TestCase):
    """An unroutable row hands over the way to answer, and never a borrowed identity.

    Two holes behind one another, both measured 2026-09-25 by deliberately parking a
    headless worker (session `50193c14`). It inherited its spawner's `WEZTERM_PANE`
    (pane 0, the Fleet Manager's tab), so:

      * `is_routable()` proves ownership by comparing the pane title to the session's
        *name* — a test a NAMELESS session cannot run, so it fell to `return True` by
        design and kept a confident jump to a pane it did not own;
      * `name_of()` had no name either, so it borrowed the pane title and rendered the
        SPAWNER's name as the row's identity.

    The fixtures below therefore build the NO-NAME state deliberately. A *named*
    headless worker passes against the pre-fix script (it mismatches on the name and is
    already refused), so a named fixture would assert nothing.
    """

    BORROWED = {"session_id": "50193c14-4729-41be-90c4-ee84f24e76cd", "pane": "0", "ts": 0,
                "cwd": "/tmp/x", "kind": "permission", "detail": "d",
                "store_item_id": "0f701f0b4b654e3338dcc97d9be8b314"}
    # The spawner's pane. Its title must DIFFER from the session id, or the
    # "never the spawner's pane title" assertion would pass vacuously.
    SPAWNER_PANE = {"0": {"title": "◐ Fleet Manager"}}
    SPAWNER_REG = {"spawner-session": "Fleet Manager"}

    def test_a_nameless_session_keeps_no_jump_to_another_sessions_pane(self):
        """The hole the name-mismatch test cannot see: no name, so no comparison."""
        self.assertFalse(wnm.is_routable(self.BORROWED, self.SPAWNER_PANE, self.SPAWNER_REG))

    def test_a_nameless_row_wears_its_own_session_id_not_the_pane_title(self):
        """The identity half. Asserts the parsed field, not a substring of the row."""
        self.assertEqual("session 50193c14",
                         wnm.name_of(self.BORROWED, self.SPAWNER_PANE, self.SPAWNER_REG))

    def test_an_unroutable_row_hands_over_the_store_keyed_answer_command(self):
        """The handover half: the STORE id, since that is what the endpoint resolves."""
        rendered = wnm.row(self.BORROWED, self.SPAWNER_PANE, "d", self.SPAWNER_REG)
        self.assertIn("attention-answer.py answer 0f701f0b4b654e3338dcc97d9be8b314", rendered)
        self.assertIn("--decision allow|deny", rendered)
        self.assertNotIn("activate-pane", rendered)

    def test_an_unprovable_owner_still_keeps_its_row(self):
        """Absence is not a verdict — an empty registry must not strip the jump."""
        rec = dict(self.BORROWED, pane="9")
        self.assertTrue(wnm.is_routable(rec, {"9": {"title": "◐ unregistered"}}, {}))

    def test_a_routable_row_is_untouched(self):
        """The positive control: one jump line, and no answer line bolted onto it."""
        rec = {"session_id": "s2", "pane": "7", "ts": 0, "cwd": "/tmp/y", "kind": "permission",
               "detail": "d", "session_name": "worker-verify", "store_item_id": "abc123"}
        pmap, reg = {"7": {"title": "◐ worker-verify"}}, {"s2": "worker-verify"}
        rendered = wnm.row(rec, pmap, "d", reg)
        self.assertIn("wezterm cli activate-pane --pane-id 7", rendered)
        self.assertNotIn("attention-answer.py", rendered)

    def test_the_store_id_is_carried_apart_from_the_log_join_key(self):
        """`item_id` stays the LOG key the classifiers join on; the store id rides beside it."""
        item = {"answer_mechanism": "permission", "producer_id": "s", "dedup_key": "LOGID",
                "item_id": "STOREID", "payload": "d", "state": "open",
                "created_at": "2026-09-25T15:24:07Z"}
        rec = wnm.normalize_store_item(item, {})
        self.assertEqual("LOGID", rec["item_id"])
        self.assertEqual("STOREID", rec["store_item_id"])


class ProvenanceRendering(unittest.TestCase):
    """The row carries its provenance, and an absent value renders as absent.

    The reader resolved `host`, `cwd` and `tool_name` into the record before this
    change, but `row()` rendered none of them — so the operator could see a pane and
    a name and still not know which directory or which tool raised the item, which is
    the whole question the feed exists to answer.

    Absent renders as `—`, never as a blank: a missing host and a host that is
    genuinely empty are different claims, and a blank reads as the second.
    """

    def rec(self, **kw):
        rec = {"pane": 7, "ts": 0, "cwd": "/tmp/x", "kind": "question", "detail": "d"}
        rec.update(kw)
        return rec

    def test_the_row_carries_host_cwd_and_tool(self):
        rec = self.rec(host="burn", cwd="~/Documents/Obsidian/my-vault",
                       tool_name="AskUserQuestion")
        rendered = wnm.row(rec, {"7": {}}, "question: d")
        self.assertIn("burn:~/Documents/Obsidian/my-vault", rendered)
        self.assertIn("AskUserQuestion", rendered)

    def test_an_absent_provenance_field_renders_as_absent(self):
        """`—`, not a blank, and not the payload echoed into the field."""
        rendered = wnm.row(self.rec(), {"7": {}}, "question: d")
        self.assertIn("—:/tmp/x · —", rendered)

    def test_an_absent_field_is_never_defaulted_from_the_payload(self):
        rec = self.rec(detail="raised from burn by Bash")
        rendered = wnm.row(rec, {"7": {}}, "question: " + rec["detail"])
        self.assertNotIn("burn:", rendered)

    def test_a_replayed_row_says_so_on_the_row(self):
        """The marker rides the row, in the scan position a manager reads first.

        SC1 is a per-row claim: a section header printed once, above rows that
        then render identically, is exactly the indistinguishability this exists
        to break -- so asserting on the header would pass on the defect.
        """
        rendered = wnm.row(self.rec(replayed=True), {"7": {}}, "question: d")
        self.assertIn("⟳replay", rendered.splitlines()[0])

    def test_a_live_row_carries_no_replay_marker(self):
        """The positive control, and the no-regression half at row level.

        A responsive store's rows must render exactly as they did before the
        marker existed, so the marker is EMPTY rather than padded when unset --
        and a record that never carried the key renders the same as one that
        sets it False, which is what makes an absent stamp safe.
        """
        rendered = wnm.row(self.rec(), {"7": {}}, "question: d")
        self.assertNotIn("⟳replay", rendered)
        self.assertEqual(
            rendered,
            wnm.row(self.rec(replayed=False), {"7": {}}, "question: d"),
        )


class NoShadowedDefinitions(unittest.TestCase):
    """A top-level definition is never left shadowed by a later one of the same name.

    Observed 2026-09-21 (PR #91): an edit that replaced a function's *body* while
    leaving its `def` line and docstring behind produced a docstring-only stub. That
    is valid Python — the docstring IS the body — so the module imported, the whole
    suite passed, and the reader worked. The stub was silently shadowed by the real
    definition further down, and only a reviewer reading the diff caught it.

    A duplicate top-level name is never intentional in a one-shot script, so the
    guard is a plain uniqueness assertion rather than a judgement call.
    """

    def test_no_top_level_definition_appears_twice(self):
        with open(_SCRIPT, encoding="utf-8") as handle:
            names = re.findall(r"^def ([A-Za-z_][A-Za-z0-9_]*)", handle.read(), re.M)
        dupes = sorted({n for n in names if names.count(n) > 1})
        self.assertEqual([], dupes, f"top-level defs defined more than once: {dupes}")

    def test_the_guard_can_see_a_duplicate(self):
        """The positive control: the regex matches, so an empty result means clean."""
        names = re.findall(r"^def ([A-Za-z_][A-Za-z0-9_]*)",
                           "def a():\n    pass\n\ndef a():\n    pass\n", re.M)
        self.assertEqual(["a", "a"], names)


class PaneFor(unittest.TestCase):
    """`--pane-for` resolves one session id to its pane, or refuses.

    This is the lookup `/supervisor:fleet-drive` runs to give an escalated row its
    jump link when the sweep digest carried no pane for it. Every refusal path is
    asserted on stdout being EMPTY, not just on the exit code: a caller reads the
    pane off stdout, so a diagnostic that leaked there would be handed to
    `jump-link.py` as a pane id and produce a link to a pane that does not exist.

    Two classes of case carry the weight, both measured rather than imagined:

      * **The stale recorded pane.** A pane id is a lease -- WezTerm renumbers and
        reuses them -- so a live session's attention record can name a pane that no
        longer exists. Measured 2026-09-24: `Sample Task` was live in the registry on
        pane 1391 while all eight of its records carried pane 85. Asking whether
        pane 85 still existed called that session dead, hiding a session the
        operator could have jumped to.
      * **The ambiguity guards.** A shared session-id prefix and a shared pane
        title both refuse rather than pick one; either guess hands the operator a
        link to the wrong tab, which reads as a working one.
    """

    SID_A = "11111111-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    SID_B = "22222222-bbbb-4bbb-8bbb-bbbbbbbbbbbb"

    def setUp(self):
        self.patches = []

        def patch(name, value):
            p = mock.patch.object(wnm, name, value)
            self.patches.append(p)
            p.start()

        self.records = [
            {"session_id": self.SID_A, "pane": "204", "kind": "idle", "ts": 1},
            {"session_id": self.SID_B, "pane": "205", "kind": "idle", "ts": 2},
        ]
        self.registry = {self.SID_A: "Session A", self.SID_B: "Session B"}
        self.panes = {
            "204": {"pane_id": 204, "title": "\u2733 Session A"},
            "205": {"pane_id": 205, "title": "\u2733 Session B"},
        }
        patch("load", lambda _suffix: list(self.records))
        patch("wezterm_panes", lambda: None if self.panes is None else dict(self.panes))
        patch("read_registry", lambda: None if self.registry is None else dict(self.registry))
        self.addCleanup(lambda: [p.stop() for p in self.patches])

    def run_pane_for(self, arg):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = wnm.pane_for(arg)
        return rc, out.getvalue(), err.getvalue()

    def test_recorded_pane_is_preferred(self):
        """The sweep already resolved this pane; while it is live it is used as-is."""
        rc, out, _ = self.run_pane_for(self.SID_A[:8])
        self.assertEqual(0, rc)
        self.assertEqual("204\n", out)

    def test_existing_pane_of_another_session_is_not_used(self):
        """A recycled pane id: the pane exists but is titled for another session.

        Existence is not ownership -- pane ids are recycled across tab moves and
        WezTerm restarts. Returning this one would hand the operator a confident
        link to the wrong tab, which reads as a working one.
        """
        self.panes = {
            "204": {"pane_id": 204, "title": "✳ Some Other Session"},
            "1391": {"pane_id": 1391, "title": "✳ Session A"},
        }
        rc, out, _ = self.run_pane_for(self.SID_A[:8])
        self.assertEqual(0, rc)
        self.assertEqual("1391\n", out)

    def test_existing_pane_of_another_session_with_no_match_refuses(self):
        """Not ours, and no pane is titled ours -> refuse, never fall back to it."""
        self.panes = {"204": {"pane_id": 204, "title": "✳ Some Other Session"}}
        rc, out, err = self.run_pane_for(self.SID_A[:8])
        self.assertEqual(1, rc)
        self.assertEqual("", out)
        self.assertIn("no pane resolves", err)

    def test_full_id_resolves(self):
        rc, out, _ = self.run_pane_for(self.SID_A)
        self.assertEqual(0, rc)
        self.assertEqual("204\n", out)

    def test_stale_recorded_pane_falls_back_to_the_current_name(self):
        """The Sample Task case: live session, recorded pane gone, real pane 1391.

        The fallback matches the registry's CURRENT name, which is what makes a
        `/rename` tracked rather than broken.
        """
        self.records[0]["pane"] = "85"
        self.panes = {"1391": {"pane_id": 1391, "title": "\u2733 Session A"}}
        rc, out, _ = self.run_pane_for(self.SID_A[:8])
        self.assertEqual(0, rc)
        self.assertEqual("1391\n", out)

    def test_status_glyph_is_stripped_on_both_sides(self):
        """The pane title carries a glyph; the registry name may carry its own."""
        self.records[0]["pane"] = "85"
        self.registry[self.SID_A] = "\u2699 Session A"
        self.panes = {"1391": {"pane_id": 1391, "title": "\u2733 Session A"}}
        rc, out, _ = self.run_pane_for(self.SID_A[:8])
        self.assertEqual(0, rc)
        self.assertEqual("1391\n", out)

    def test_stale_pane_with_no_matching_title_refuses(self):
        """No title matches -> refuse with the reason, never a guess."""
        self.records[0]["pane"] = "85"
        self.panes = {"1391": {"pane_id": 1391, "title": "\u2733 Some Other Session"}}
        rc, out, err = self.run_pane_for(self.SID_A[:8])
        self.assertEqual(1, rc)
        self.assertEqual("", out)
        self.assertIn("no pane resolves", err)

    def test_ambiguous_title_refuses(self):
        """Two panes titled alike must not pick one -- that is a wrong-tab link."""
        self.records[0]["pane"] = "85"
        self.panes = {
            "1391": {"pane_id": 1391, "title": "\u2733 Session A"},
            "1392": {"pane_id": 1392, "title": "\u2733 Session A"},
        }
        rc, out, err = self.run_pane_for(self.SID_A[:8])
        self.assertEqual(1, rc)
        self.assertEqual("", out)
        self.assertIn("ambiguous", err)

    def test_pane_is_taken_from_a_sibling_record(self):
        """The store returns one record per open item; only some carry a pane."""
        self.records.insert(0, {"session_id": self.SID_A, "kind": "tool", "ts": 0})
        rc, out, _ = self.run_pane_for(self.SID_A[:8])
        self.assertEqual(0, rc)
        self.assertEqual("204\n", out)

    def test_ambiguous_prefix_refuses(self):
        """Two live ids sharing the prefix must refuse rather than pick one."""
        twin = self.SID_A[:8] + "-cccc-4ccc-8ccc-cccccccccccc"
        self.registry = {self.SID_A: "Session A", twin: "Session C"}
        rc, out, err = self.run_pane_for(self.SID_A[:8])
        self.assertEqual(1, rc)
        self.assertEqual("", out)
        self.assertIn("match", err)

    def test_unknown_id_refuses(self):
        rc, out, err = self.run_pane_for("deadbeef")
        self.assertEqual(1, rc)
        self.assertEqual("", out)
        self.assertIn("no live session id matches", err)

    def test_unreadable_registry_refuses(self):
        """An unreadable registry cannot prove liveness, so it must not guess."""
        self.registry = None
        rc, out, err = self.run_pane_for(self.SID_A[:8])
        self.assertEqual(1, rc)
        self.assertEqual("", out)
        self.assertIn("registry unreadable", err)

    def test_unreadable_panes_refuses(self):
        """`None` is a failed read — the only thing that may claim "unreadable"."""
        self.panes = None
        rc, out, err = self.run_pane_for(self.SID_A[:8])
        self.assertEqual(1, rc)
        self.assertEqual("", out)
        self.assertIn("pane list unreadable", err)

    def test_healthy_empty_fleet_is_not_reported_as_unreadable(self):
        """The control: `{}` is a reachable WezTerm holding no panes — a real answer.

        This case previously asserted the opposite (`self.panes = {}` expecting
        "pane list unreadable"), which was the conflation itself: it could not tell a
        broken transport from an empty fleet, so it passed while `pane_for()` refused
        a healthy-but-empty WezTerm. A fix that warns on `not pmap` still passes the
        case above and fails only here.
        """
        self.panes = {}
        rc, out, err = self.run_pane_for(self.SID_A[:8])
        self.assertEqual(1, rc)
        self.assertEqual("", out)
        self.assertNotIn("unreadable", err)
        self.assertIn("no pane resolves", err)

    def test_empty_id_refuses(self):
        rc, out, _ = self.run_pane_for("")
        self.assertEqual(1, rc)
        self.assertEqual("", out)

    def test_the_flag_is_registered(self):
        """The positive control: the mode exists on the CLI, so the command can call it."""
        with open(_SCRIPT, encoding="utf-8") as handle:
            self.assertIn('"--pane-for"', handle.read())


class SupersededCloserPanel(unittest.TestCase):
    """Class 6 -- a rendered closer whose session has already started a new turn.

    The Rendered-panels pass lists a closer that was true when it was written and is
    stale by the time a manager reads it: the session has since taken a new user or
    tool turn, so nothing is waiting on the operator. Measured 2026-09-25 on 0.56.5 by
    gate-relay-read triage -- one batch listed 5 panes with 2 real gates, another 4
    with 1, a 2-4x overstatement.

    Distinct from the two fixed siblings, and neither fix touches this case:
    [[who-needs-me.py Counts Cleared Gates]] fixed the *headline count* including
    already-answered gates, and the closer-panel work fixed closers
    rendering indistinguishably from blocks. Here the closer is stale, not merely
    unlabelled or uncleared -- so the signal must come from session/turn state, never
    from parsing the closer text. A genuinely blocked session emits an identical string.

    `is_superseded()` stays a pure predicate (record, busy set, resumed set); the two
    machine-reading halves are `busy_session_ids()` and `turn_after_closer()`.

    Scope: this removes *superseded* rows only. The sibling task's SC2 --
    "panel rows stay visible ... the command must not start hiding panes" -- protects a
    **live** closer panel, and a superseded row is no longer a panel row, so dropping
    it is a reclassification rather than a hidden pane. The asymmetry cases below are
    what hold that line: a fix that drops every panel fails them.
    """

    SID = "dddddddd-4444-4444-8444-dddddddddddd"
    OTHER = "eeeeeeee-5555-4555-8555-eeeeeeeeeeee"

    def setUp(self):
        self._turn = wnm.turn_after_closer
        self.addCleanup(self._restore)

    def _restore(self):
        wnm.turn_after_closer = self._turn

    def rec(self, detail="pick — 1. do the thing", event="Stop", state="open"):
        """A `Stop`-written closer panel -- the record class this defect lives on."""
        return {"session_id": self.SID, "pane": "7", "cwd": "/tmp",
                "kind": "question", "event": event, "state": state,
                "detail": detail, "ts": time.time()}

    def listed(self, rec, busy=(), resumed=()):
        """main()'s Rendered-panels membership, through the composed pass it uses.

        Asserted through `rendered_panels()` rather than by re-composing the three
        predicates here: a second copy of that composition would drift from main()'s,
        and both would keep returning a list while they disagreed.
        """
        return [r["session_id"] for r in
                wnm.rendered_panels([rec], frozenset(), None, set(busy), set(resumed))]

    def transcript(self, entries):
        """A real `.jsonl` tail, so the machine reader is exercised, not stubbed."""
        fd, path = tempfile.mkstemp(prefix="wnm-transcript-", suffix=".jsonl")
        os.close(fd)
        with open(path, "w", encoding="utf-8") as handle:
            for entry in entries:
                handle.write(json.dumps(entry) + "\n")
        self.addCleanup(os.unlink, path)
        return path

    def registry(self, entries):
        """A temp registry dir in the real shape: one `<pid>.json` per live session.

        Registered for cleanup like `transcript()` does -- `mkdtemp` is not
        self-cleaning, so without this every run of the two registry cases leaves a
        directory behind.
        """
        d = tempfile.mkdtemp(prefix="wnm-registry-")
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        for i, (sid, status) in enumerate(entries):
            with open(os.path.join(d, f"{2000 + i}.json"), "w", encoding="utf-8") as handle:
                json.dump({"pid": 2000 + i, "sessionId": sid, "status": status}, handle)
        return d

    def closer_line(self, text="…\n\U0001F464 You: pick — 1. do the thing"):
        return {"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}}

    # --- SC1: the defect, closer followed by a new turn ----------------------

    def test_closer_followed_by_a_user_turn_is_not_listed(self):
        """THE REGRESSION CASE. The session took a new user turn after its closer."""
        self.assertEqual(self.listed(self.rec(), resumed={self.SID}), [])

    def test_closer_followed_by_a_tool_turn_is_not_listed(self):
        """A tool turn supersedes it too -- the transcript half's other branch."""
        self.assertEqual(self.listed(self.rec(), resumed={self.SID}), [])

    def test_closer_on_a_busy_session_is_not_listed(self):
        """`busy` is inside-a-turn, so the closer is stale by construction.

        The registry's status vocabulary is `idle` / `shell` / `busy` / `waiting`. A
        session parked on a prompt reads `waiting`, and one that has ended its turn
        reads `idle` -- so `busy` cannot be a session waiting on the operator, which is
        what makes it usable here.
        """
        self.assertEqual(self.listed(self.rec(), busy={self.SID}), [])

    # --- the asymmetry: a fix that drops every panel fails these -------------

    def test_closer_with_nothing_after_it_is_still_listed(self):
        """The sibling's SC2, as a control: a live panel row stays visible."""
        self.assertEqual(self.listed(self.rec()), [self.SID])

    def test_closer_on_an_idle_session_is_still_listed(self):
        """`idle` is the panel's own case -- turn ended, waiting. Not superseded."""
        self.assertEqual(self.listed(self.rec()), [self.SID])

    def test_a_different_sessions_supersession_does_not_leak(self):
        """Keyed by session, not global: another pane's turn must not hide this row."""
        self.assertEqual(self.listed(self.rec(), busy={self.OTHER}, resumed={self.OTHER}),
                         [self.SID])

    def test_answered_record_is_not_listed(self):
        """The pre-existing cleared-gate path still applies, unchanged."""
        self.assertEqual(self.listed(self.rec(state="answered")), [])

    def test_non_panel_gate_is_not_listed(self):
        """A `Notification` elicitation is a parked gate, never a rendered panel."""
        self.assertEqual(self.listed(self.rec(event="Notification")), [])

    def test_supersession_does_not_touch_the_block_predicate(self):
        """Scope guard: `is_open_gate()` takes no busy/resumed input at all.

        The change is scoped to the Rendered-panels list. A parked gate on a session
        that is merely busy must stay answerable, so the block predicate cannot learn
        about supersession by accident.
        """
        self.assertTrue(wnm.is_open_gate(self.rec(), include_panels=True))

    # --- machine-reading half 1: the transcript tail -------------------------

    def test_turn_after_closer_reads_a_real_transcript(self):
        path = self.transcript([
            self.closer_line(),
            {"type": "user", "message": {"content": [{"type": "text", "text": "go on"}]}},
        ])
        self.assertTrue(wnm.turn_after_closer({"session_id": self.SID, "transcript": path}))

    def test_tool_turn_after_closer_is_detected(self):
        path = self.transcript([
            self.closer_line(),
            {"type": "assistant", "message": {"content": [
                {"type": "tool_use", "name": "Bash", "input": {"command": "ls"}}]}},
        ])
        self.assertTrue(wnm.turn_after_closer({"session_id": self.SID, "transcript": path}))

    def test_closer_as_the_last_line_has_no_turn_after_it(self):
        path = self.transcript([
            {"type": "user", "message": {"content": [{"type": "text", "text": "hi"}]}},
            self.closer_line(),
        ])
        self.assertFalse(wnm.turn_after_closer({"session_id": self.SID, "transcript": path}))

    def test_a_turn_before_the_closer_does_not_supersede(self):
        """Position, not mere presence -- a tool turn earlier in the turn is normal.

        This is the case that makes the check a position read rather than a
        transcript-contains-a-tool-call read: every real turn has tool calls in it, so
        the naive version would drop every panel.
        """
        path = self.transcript([
            {"type": "assistant", "message": {"content": [
                {"type": "tool_use", "name": "Bash", "input": {"command": "ls"}}]}},
            {"type": "user", "message": {"content": [
                {"type": "tool_result", "content": "ok"}]}},
            self.closer_line(),
        ])
        self.assertFalse(wnm.turn_after_closer({"session_id": self.SID, "transcript": path}))

    def test_a_later_closer_resets_the_turn(self):
        """A second closer after a turn is a fresh closer, not a superseded one."""
        path = self.transcript([
            self.closer_line(),
            {"type": "user", "message": {"content": [{"type": "text", "text": "again"}]}},
            self.closer_line("…\n\U0001F464 You: approve: /vault-cli:session-close"),
        ])
        self.assertFalse(wnm.turn_after_closer({"session_id": self.SID, "transcript": path}))

    def test_missing_transcript_is_not_superseded(self):
        """An unreadable transcript proves nothing, so the row is kept."""
        self.assertFalse(wnm.turn_after_closer(
            {"session_id": self.SID, "transcript": "/nonexistent/nope.jsonl"}))

    # --- machine-reading half 2: the registry status -------------------------

    def test_busy_session_ids_reads_the_registry(self):
        d = self.registry([(self.SID, "busy"), (self.OTHER, "idle")])
        self.assertEqual(wnm.busy_session_ids(d), {self.SID})

    def test_waiting_session_is_not_busy(self):
        """A session parked on a prompt reads `waiting`; its gate is still real."""
        self.assertEqual(wnm.busy_session_ids(self.registry([(self.SID, "waiting")])), set())

    def test_unreadable_registry_is_not_busy(self):
        """Same rule as `quiet_session_ids()`: a failed read proves nothing."""
        self.assertEqual(wnm.busy_session_ids("/nonexistent/registry"), set())


class FeedTransportTest(unittest.TestCase):
    """The feed's three transport states, end to end through `main()`.

    A failed `wezterm cli list` used to fold into `{}` (`panes()`), so `is_live()`
    dropped every record and the feed printed `Needs you (0)` / `Nothing needs you.`
    and exited 0 -- a clear fleet certified by the success code, for a transport it
    never reached. Measured 2026-09-25 against the installed 0.57.0: with the mux
    socket unreachable the feed lost 4 rendered panels and 16 idle rows and still
    exited 0 with an empty stderr.

    All three states are asserted together because any two of them can be satisfied
    by a wrong implementation: refusing unconditionally passes the broken case and
    fails both healthy ones; rendering unconditionally passes the healthy ones and
    fails the broken one. The third state is the control for the `is None` test --
    a reachable WezTerm holding no panes is an answer, not a broken query, so a fix
    that refused on `not pmap` would wrongly fail it.
    """

    GATES = [c["record"] for c in CASES
             if c["class"] == "genuine" and c["provenance"] == "live"
             and c["expect"]["in_feed"]]

    def run_feed(self, panes, records, argv=()):
        """Run `main()` against a stubbed transport; return (rc, stdout, stderr).

        `argv` appends flags after the program name, so a caller can exercise an
        option (e.g. `--section`) without restating the stubs.
        """
        registry = {r["session_id"]: "Session %s" % r["pane"] for r in records}
        patches = [
            mock.patch.object(wnm, "wezterm_panes", lambda: panes),
            # Takes the optional dir the real `read_registry(sessions_dir=None)` takes:
            # `busy_session_ids()` passes one through, as `live_session_ids()` already
            # did -- so a zero-arg stub would fail on a caller that mirrors the real
            # signature rather than on the code under test.
            mock.patch.object(wnm, "read_registry", lambda *_a, **_k: dict(registry)),
            mock.patch.object(
                wnm, "load",
                lambda suffix: list(records) if suffix == "needs" else []),
            mock.patch.object(wnm, "reclassify_idle", lambda rec: rec),
            mock.patch.object(wnm, "task_status_from_closer", lambda _rec: None),
            mock.patch.object(wnm.sys, "argv", ["who-needs-me.py", *argv]),
        ]
        out, err = io.StringIO(), io.StringIO()
        for patch in patches:
            patch.start()
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                try:
                    wnm.main()
                    rc = 0
                except SystemExit as exc:
                    rc = exc.code if isinstance(exc.code, int) else 1
        finally:
            for patch in patches:
                patch.stop()
        return rc, out.getvalue(), err.getvalue()

    def test_unreadable_transport_refuses_instead_of_reporting_zero(self):
        """`None` from the transport must never render as an empty queue."""
        rc, out, err = self.run_feed(None, self.GATES)
        self.assertNotEqual(0, rc)
        self.assertIn("pane list unreadable", err)
        self.assertIn("mux socket", err)
        # The lie this fix exists to stop: a confident zero, carrying exit 0.
        self.assertNotIn("Needs you (0)", out)
        self.assertNotIn("Nothing needs you.", out)

    def test_healthy_transport_with_gates_still_renders(self):
        """The healthy path is unchanged: the gates print and the exit stays 0."""
        panes = {str(r["pane"]): {"pane_id": int(r["pane"]), "title": "Session"}
                 for r in self.GATES}
        rc, out, err = self.run_feed(panes, self.GATES)
        self.assertEqual(0, rc, err)
        self.assertNotIn("Needs you (0)", out)
        self.assertIn("Needs you (%d)" % len(self.GATES), out)
        self.assertNotIn("Nothing needs you.", out)

    def test_empty_but_reachable_transport_is_not_a_failure(self):
        """A reachable WezTerm with no panes is an answer, not a broken query.

        The control for the `is None` test: refusing on `not pmap` would pass the
        broken-transport case above and wrongly fail this one.
        """
        rc, out, err = self.run_feed({}, [])
        self.assertEqual(0, rc, err)
        self.assertEqual("", err)
        self.assertIn("Needs you (0)", out)
        self.assertIn("Nothing needs you.", out)


class SectionScoping(unittest.TestCase):
    """`--section` scopes the render to the named sections.

    Why it exists: the consumer, `gate-owner-filter.py`'s `panes_from_feed`,
    matches `[<pane>]` on ANY line regardless of the section header above it -- so
    a whole-feed arm fires the watcher on every `Rendered panels` closer, a line
    this feed's own header labels "not a parked gate". Measured 2026-10-01 on the
    Manager Layer arm: 8 wakes in ~55 minutes, 1 actionable, each a full turn.

    Selection is by section NAME, never by position.
    """

    def test_no_flag_selects_every_section(self):
        wanted = wnm.section_filter(None)
        for name in wnm.SECTIONS:
            self.assertTrue(wanted(name), name)

    def test_empty_selection_selects_every_section(self):
        wanted = wnm.section_filter([])
        for name in wnm.SECTIONS:
            self.assertTrue(wanted(name), name)

    def test_one_section_selects_only_that_section(self):
        wanted = wnm.section_filter(["needs-you"])
        self.assertTrue(wanted("needs-you"))
        for name in wnm.SECTIONS:
            if name != "needs-you":
                self.assertFalse(wanted(name), name)

    def test_selection_is_by_name_not_position(self):
        # `idle` is rendered LAST. A filter keyed on position -- or a "first
        # section" shortcut -- cannot express this, and would wrongly answer True
        # for `needs-you`.
        wanted = wnm.section_filter(["idle"])
        self.assertTrue(wanted("idle"))
        self.assertFalse(wanted("needs-you"))

    def test_flag_is_repeatable(self):
        wanted = wnm.section_filter(["needs-you", "reapable"])
        self.assertTrue(wanted("needs-you"))
        self.assertTrue(wanted("reapable"))
        self.assertFalse(wanted("rendered-panels"))

    def test_the_watcher_arm_excludes_the_closer_section(self):
        # The scoped arm's whole point: `rendered-panels` is the section that made
        # the unscoped arm fire 8 times in ~55 minutes with 1 actionable.
        wanted = wnm.section_filter(["needs-you"])
        self.assertTrue(wanted("needs-you"))
        self.assertFalse(wanted("rendered-panels"))


class SectionScopingEndToEnd(unittest.TestCase):
    """The flag through `main()`, not only through the predicate.

    `run_feed` is borrowed from `FeedTransportTest` rather than inherited: a
    subclass would re-run that class's three transport tests under this name.
    """

    run_feed = FeedTransportTest.run_feed

    SECTION_HEADERS = (
        "Needs you (",
        "Rendered panels (",
        "Probably stuck > ",
        "Reapable (",
        "Idle, turn ended: ",
    )

    def assertHeaders(self, out, present):
        for header in self.SECTION_HEADERS:
            if header in present:
                self.assertIn(header, out)
            else:
                self.assertNotIn(header, out)

    def test_unscoped_render_still_prints_all_five_headers(self):
        # The no-regression bar: the default path is unchanged, so the existing
        # consumer sees exactly what it saw before the flag existed.
        rc, out, err = self.run_feed({}, [])
        self.assertEqual(0, rc, err)
        self.assertHeaders(out, self.SECTION_HEADERS)

    def test_scoped_render_prints_only_the_named_header(self):
        rc, out, err = self.run_feed({}, [], argv=["--section", "needs-you"])
        self.assertEqual(0, rc, err)
        self.assertHeaders(out, ("Needs you (",))
        self.assertIn("Nothing needs you.", out)

    def test_scoped_render_can_name_a_later_section(self):
        # `idle` is the last header rendered; naming it proves selection is not
        # "everything up to here".
        rc, out, err = self.run_feed({}, [], argv=["--section", "idle"])
        self.assertEqual(0, rc, err)
        self.assertHeaders(out, ("Idle, turn ended: ",))

    def test_unknown_section_is_rejected(self):
        # argparse choices are bound to SECTIONS, so a typo fails loudly rather
        # than silently rendering the whole feed -- which would re-introduce the
        # unscoped arm under a scoped-looking command line.
        rc, _out, err = self.run_feed({}, [], argv=["--section", "needs_you"])
        self.assertNotEqual(0, rc)
        self.assertIn("invalid choice", err)


class PostedCloserTest(unittest.TestCase):
    """The record that stops a suppressed echo being re-promoted by the reader.

    `reclassify_idle` promotes an `idle` row back to `question` from the
    transcript, so without this the hook's suppression is undone one layer down.
    """

    def setUp(self):
        self._dir = tempfile.mkdtemp()
        self._orig_state = wnm.STATE
        self._orig_ttl = wnm.POSTED_CLOSER_TTL
        wnm.STATE = self._dir

    def tearDown(self):
        wnm.STATE = self._orig_state
        wnm.POSTED_CLOSER_TTL = self._orig_ttl
        shutil.rmtree(self._dir, ignore_errors=True)

    def _write(self, rec, sid="session-a"):
        with open(
            os.path.join(self._dir, f"{sid}.posted.json"), "w", encoding="utf-8"
        ) as f:
            f.write(rec if isinstance(rec, str) else json.dumps(rec))

    def test_no_record_returns_empty(self):
        self.assertEqual(wnm.posted_closer("session-a"), "")

    def test_an_empty_session_id_returns_empty(self):
        self._write({"closer": "pick — 1. x", "ts": time.time()})
        self.assertEqual(wnm.posted_closer(""), "")

    def test_malformed_json_returns_empty(self):
        self._write("{not json")
        self.assertEqual(wnm.posted_closer("session-a"), "")

    def test_a_non_string_closer_returns_empty(self):
        self._write({"closer": 42, "ts": time.time()})
        self.assertEqual(wnm.posted_closer("session-a"), "")

    def test_a_fresh_record_returns_its_normalized_closer(self):
        self._write({"closer": "pick  —  1. x · 2. y", "ts": time.time()})
        self.assertEqual(wnm.posted_closer("session-a"), "pick — 1. x · 2. y")

    def test_an_expired_record_is_ignored(self):
        # Unbounded, the record lives as long as the session id, so an identical
        # closer re-raised on a genuinely later turn is suppressed forever. The
        # window bounds the identity to the turn that produced it.
        self._write({"closer": "pick — 1. x", "ts": time.time() - 7 * 3600})
        self.assertEqual(wnm.posted_closer("session-a"), "")

    def test_a_record_with_no_usable_ts_is_ignored(self):
        # Failing open here means the echo returns, which is the safe direction:
        # the alternative suppresses a closer on an unreadable clock.
        self._write({"closer": "pick — 1. x"})
        self.assertEqual(wnm.posted_closer("session-a"), "")

    def test_a_future_dated_record_is_ignored(self):
        # A `ts` ahead of now makes the delta negative, so it would never exceed
        # the TTL and the record would stay authoritative indefinitely -- the
        # unbounded case the window exists to prevent, reached by a clock-skewed
        # or buggy poster rather than by a typo.
        self._write({"closer": "pick — 1. x", "ts": time.time() + 86400})
        self.assertEqual(wnm.posted_closer("session-a"), "")


class PostedCloserTtlFromEnv(unittest.TestCase):
    """`_ttl_from_env` must cost the window, never the import.

    An unguarded `float()` at module scope raises ValueError at import, which
    kills the reader outright rather than falling back to the default -- and
    `inf`/`nan` parse as floats while making every comparison false, so the
    record would never expire. Both are the unbounded case arriving by a
    different route, which is why only a finite positive window is accepted.
    """

    def _ttl(self, value):
        original = os.environ.get("ATTENTION_POSTED_CLOSER_TTL")
        try:
            os.environ.pop("ATTENTION_POSTED_CLOSER_TTL", None)
            if value is not None:
                os.environ["ATTENTION_POSTED_CLOSER_TTL"] = value
            return wnm._ttl_from_env()
        finally:
            if original is None:
                os.environ.pop("ATTENTION_POSTED_CLOSER_TTL", None)
            else:
                os.environ["ATTENTION_POSTED_CLOSER_TTL"] = original

    def test_unset_uses_the_default(self):
        self.assertEqual(self._ttl(None), 6 * 3600)

    def test_a_non_numeric_override_falls_back(self):
        self.assertEqual(self._ttl("abc"), 6 * 3600)

    def test_infinity_does_not_disable_expiry(self):
        self.assertEqual(self._ttl("inf"), 6 * 3600)

    def test_nan_does_not_disable_expiry(self):
        self.assertEqual(self._ttl("nan"), 6 * 3600)

    def test_zero_and_negative_fall_back(self):
        self.assertEqual(self._ttl("0"), 6 * 3600)
        self.assertEqual(self._ttl("-5"), 6 * 3600)

    def test_a_valid_override_is_honoured(self):
        self.assertEqual(self._ttl("7200"), 7200.0)


if __name__ == "__main__":
    unittest.main()
