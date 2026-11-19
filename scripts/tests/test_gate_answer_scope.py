#!/usr/bin/env python3
"""Tests for the subject-scoped ownership predicate.

`own-park-check.verdict()` used to admit a worker only when THIS session had spawned it, so a
manager holding a card raised by a peer's worker escalated every time — measured 2026-11-19,
0 cards answered in 24 dispatches while the operator answered 14. `session-scope.Scope`
supplies the durable second input: the vault says whose work a session's page is.

⚠️ **The two defaults point opposite ways, and that is the whole design.** `Scope.exclusion()`
feeds the FEED and fails open, because hiding a row on a failed read silences the fleet.
`Scope.admission()` feeds the ANSWERER and fails closed, because admitting on a failed read is
the blanket widening the manager contract forbids. Each refusal below is paired with the
admission it must not collapse into.

Run: python3 -m unittest discover -s scripts/tests -v
"""

import importlib.util
import os
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_SCRIPTS = os.path.dirname(_HERE)


def _load(name, filename):
    spec = importlib.util.spec_from_file_location(name, os.path.join(_SCRIPTS, filename))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


ss = _load("session_scope", "session-scope.py")
opc = _load("own_park_check", "own-park-check.py")
gac = _load("gate_answer_check", "gate-answer-check.py")

SELF = "11111111-1111-1111-1111-111111111111"
PEER = "22222222-2222-2222-2222-222222222222"
IN_SUBJECT = "33333333-3333-3333-3333-333333333333"
OTHER_SUBJECT = "44444444-4444-4444-4444-444444444444"
NO_PAGE = "55555555-5555-5555-5555-555555555555"
TWO_PAGES = "66666666-6666-6666-6666-666666666666"

GOAL = "Managers Unblock Their Own Workers"
OTHER_GOAL = "Some Other Goal"


def _scope(sessions=None, own_titles=None, reason="", self_goals=None, manager=True):
    return ss.Scope(
        sessions if sessions is not None else {},
        {},
        set(own_titles or ()),
        list(self_goals or ()),
        reason,
        manager,
    )


def _resolved_scope():
    return _scope(
        sessions={
            IN_SUBJECT: [ss.Entry("Task In Subject", "task", [GOAL])],
            OTHER_SUBJECT: [ss.Entry("Task Elsewhere", "task", [OTHER_GOAL])],
            TWO_PAGES: [
                ss.Entry("Task A", "task", [GOAL]),
                ss.Entry("Task B", "task", [GOAL]),
            ],
        },
        own_titles={"Task In Subject", GOAL},
        self_goals=[GOAL],
    )


class AdmissionTest(unittest.TestCase):
    """`Scope.admission()` — the answerer's half of the predicate."""

    def setUp(self):
        self.scope = _resolved_scope()

    def test_admits_a_worker_whose_task_is_in_the_subject(self):
        admit, why = self.scope.admission(IN_SUBJECT, SELF)
        self.assertTrue(admit, why)
        self.assertIn("in this manager's subject", why)

    def test_refuses_a_worker_from_another_subject(self):
        admit, why = self.scope.admission(OTHER_SUBJECT, SELF)
        self.assertFalse(admit)
        self.assertEqual(why, "foreign-goal:%s" % OTHER_GOAL)

    def test_refuses_a_session_with_no_page(self):
        admit, why = self.scope.admission(NO_PAGE, SELF)
        self.assertFalse(admit)
        self.assertIn("no vault page", why)

    def test_refuses_a_session_stamped_on_two_pages(self):
        admit, why = self.scope.admission(TWO_PAGES, SELF)
        self.assertFalse(admit)
        self.assertIn("2 vault pages", why)

    def test_refuses_everything_when_the_manager_has_no_page(self):
        """The Fleet Manager's case: no task page of its own means no subject at all."""
        unresolved = _scope(
            sessions={IN_SUBJECT: [ss.Entry("Task In Subject", "task", [GOAL])]},
            reason="session 11111111 is stamped on no task or goal page",
        )
        self.assertFalse(unresolved.resolved)
        for sid in (IN_SUBJECT, OTHER_SUBJECT, NO_PAGE):
            admit, why = unresolved.admission(sid, SELF)
            self.assertFalse(admit, sid)
            self.assertIn("scope unresolved", why)

    def test_refuses_this_sessions_own_gate(self):
        """`exclusion()` keeps a manager's own row; admission refuses it — self-approval."""
        admit, why = self.scope.admission(SELF, SELF)
        self.assertFalse(admit)

    def test_refuses_a_worker_scoped_session(self):
        """A task-stamped session inherits its goal's task set but may not answer with it."""
        worker = _scope(
            sessions=self.scope.sessions,
            own_titles={"Task In Subject", GOAL},
            self_goals=[GOAL],
            manager=False,
        )
        admit, why = worker.admission(IN_SUBJECT, SELF)
        self.assertFalse(admit)
        self.assertIn("not a manager", why)

    def test_unresolved_scope_refuses_to_answer_but_keeps_the_feed_row(self):
        """The one asymmetry: same input, opposite defaults, and neither may drift."""
        unresolved = _scope()
        self.assertEqual(unresolved.admission(IN_SUBJECT, SELF)[0], False)
        self.assertEqual(unresolved.exclusion(IN_SUBJECT, SELF), (False, ""))


class VerdictTest(unittest.TestCase):
    """`own-park-check.verdict()` — spawn edge OR subject, and the spawn edge alone is enough."""

    def setUp(self):
        self.scope = _resolved_scope()

    def test_spawn_edge_still_admits_without_a_scope(self):
        own, why = opc.verdict(SELF, IN_SUBJECT, {IN_SUBJECT: {"spawner_session_id": SELF}})
        self.assertTrue(own, why)
        self.assertEqual(why, "spawned by this session")

    def test_subject_admits_a_peer_spawned_worker(self):
        own, why = opc.verdict(SELF, IN_SUBJECT,
                               {IN_SUBJECT: {"spawner_session_id": PEER}}, scope=self.scope)
        self.assertTrue(own, why)
        self.assertIn("in this manager's subject", why)

    def test_subject_does_not_admit_another_managers_worker(self):
        own, why = opc.verdict(SELF, OTHER_SUBJECT,
                               {OTHER_SUBJECT: {"spawner_session_id": PEER}}, scope=self.scope)
        self.assertFalse(own)
        self.assertIn("foreign-goal", why)

    def test_subject_admits_when_the_spawn_ledger_is_unreadable(self):
        own, why = opc.verdict(SELF, IN_SUBJECT, None, scope=self.scope)
        self.assertTrue(own, why)

    def test_unresolved_scope_keeps_the_old_refusal(self):
        own, why = opc.verdict(SELF, IN_SUBJECT,
                               {IN_SUBJECT: {"spawner_session_id": PEER}}, scope=_scope())
        self.assertFalse(own)
        self.assertIn("scope unresolved", why)

    def test_no_scope_keeps_the_old_refusal(self):
        own, why = opc.verdict(SELF, IN_SUBJECT, {IN_SUBJECT: {"spawner_session_id": PEER}})
        self.assertFalse(own)
        self.assertIn("spawned by", why)


class _OperatorTier:
    """`permission-tier.py` as the deny-wins case needs it: every card reads operator."""

    @staticmethod
    def tier_for(tool, inp, allowlist):
        return {"tier": "operator", "form": "secret", "reason": "here-string feeds stdin"}

    @staticmethod
    def tier_for_payload(payload, allowlist):
        return {"tier": "operator", "form": "secret", "reason": "payload reads operator"}

    @staticmethod
    def parse_payload(payload):
        return ("Bash", "")

    @staticmethod
    def load_allowlist():
        return {}


def _card(producer):
    return {
        "state": "open",
        "answer_mechanism": "permission",
        "producer_id": producer,
        "payload": "tool=Bash",
        "created_at": "2026-11-19T08:00:00Z",
    }


def _sessions(producer):
    return [{
        "id": producer,
        "cwd": "/tmp",
        "question": {
            "kind": "permission",
            "request_id": "req-1",
            "tool_name": "Bash",
            "input": {"command": "ls"},
            "created_at": "2026-11-19T08:00:00Z",
            "requested_at": "2026-11-19T08:00:00Z",
        },
    }]


class DecideTest(unittest.TestCase):
    """The end-to-end verdict: subject scope widens WHO may answer, never WHAT may be answered."""

    def setUp(self):
        self.scope = _resolved_scope()

    def _decide(self, producer, tier_mod, task_files):
        return gac.decide(
            "item-1", _card(producer), SELF, {}, {}, set(), _sessions(producer),
            task_files, tier_mod, {}, opc, scope=self.scope, is_human=lambda sid, led: False,
        )

    def test_an_in_subject_card_the_classifier_rates_operator_still_escalates(self):
        """Deny-wins survives the widening — SC1's fourth fixture."""
        verdict = self._decide(IN_SUBJECT, _OperatorTier, ["25 Tasks/Task In Subject.md"])
        self.assertEqual(verdict["verdict"], gac.ESCALATE)
        self.assertIn("operator tier", verdict["why"])

    def test_an_out_of_subject_card_escalates_at_ownership_not_at_the_tier(self):
        verdict = self._decide(OTHER_SUBJECT, _OperatorTier, ["25 Tasks/Task Elsewhere.md"])
        self.assertEqual(verdict["verdict"], gac.ESCALATE)
        self.assertIn("not this session's worker", verdict["why"])

    def test_a_card_whose_session_has_no_page_escalates(self):
        verdict = self._decide(NO_PAGE, _OperatorTier, ["25 Tasks/Unrelated.md"])
        self.assertEqual(verdict["verdict"], gac.ESCALATE)
        self.assertIn("no vault page", verdict["why"])


if __name__ == "__main__":
    unittest.main()
