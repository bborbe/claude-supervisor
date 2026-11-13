"""pane-wake.py — who the wake may target, and what counts as delivered.

The target selection is the load-bearing half: it is what keeps a keystroke out of the Fleet
Manager, out of the triage session itself, and out of any session parked on the operator.
`PlanTest` pins each refusal. `SendTest` pins that delivery is positive-only — a cleared
composer without a spinner is `unverified`, never `delivered` — and that the guard is re-run
after activation, so a modal that opened since the plan read is not typed into.
"""
import importlib.util
import pathlib
import subprocess
import types
import unittest

SCRIPT = pathlib.Path(__file__).resolve().parent.parent / "pane-wake.py"
spec = importlib.util.spec_from_file_location("pane_wake", SCRIPT)
wake = importlib.util.module_from_spec(spec)
spec.loader.exec_module(wake)

NBSP = " "
STATUS = "  ⏵⏵ auto mode on"
IDLE = "⏺ Done.\n❯" + NBSP + "\n" + STATUS
MODAL = "Pick:\n❯ 1. Yes\n  2. No\nEnter to select · Esc to cancel"
WORKING = "✶ Determining… (2s · esc to interrupt)\n❯" + NBSP + "\n" + STATUS


def rec(sid, pid, name="worker", alive=True):
    return {"sessionId": sid, "pid": pid, "name": name, "alive": alive}


class PlanTest(unittest.TestCase):
    def _plan(self, sids, records, screens=None, cards=(), self_session=None, self_pane=None,
              states=None):
        panes = {"10": {"tty": "/dev/ttys010"}, "11": {"tty": "/dev/ttys011"}, "12": {"tty": "/dev/ttys012"}}
        pid_ttys = {100: "ttys010", 101: "ttys011", 102: "ttys012"}
        screens = screens or {}
        states = states or {}
        return wake.plan(sids, records=records, panes=panes, pid_ttys=pid_ttys,
                         read=lambda p: screens.get(p, IDLE),
                         card=lambda s: (s + ".open.json") if s in cards else None,
                         state=lambda s: states.get(s),
                         self_session=self_session, self_pane=self_pane)

    def test_idle_tab_session_is_a_target_with_its_join(self):
        [row] = self._plan(["a"], [rec("a", 100)])
        self.assertEqual((row["pid"], row["tty"], row["pane"], row["verdict"]), (100, "ttys010", "10", "wake"))

    def test_fleet_manager_is_never_a_target(self):
        [row] = self._plan(["fm"], [rec("fm", 100, name="Fleet Manager")])
        self.assertEqual(row["verdict"], "skip:fleet-manager")

    def test_another_pusher_is_never_a_target(self):
        [row] = self._plan(["p"], [rec("p", 100, name="Fleet Pusher 2")])
        self.assertEqual(row["verdict"], "skip:pusher")

    def test_self_session_and_self_pane_are_never_targets(self):
        rows = self._plan(["me", "b"], [rec("me", 100), rec("b", 101)], self_session="me", self_pane="11")
        self.assertEqual([r["verdict"] for r in rows], ["skip:self", "skip:self"])

    def test_open_card_is_never_a_target(self):
        [row] = self._plan(["a"], [rec("a", 100)], cards={"a"})
        self.assertEqual(row["verdict"], "skip:open-card")

    def test_dead_and_unknown_sessions_skip(self):
        rows = self._plan(["dead", "ghost"], [rec("dead", 100, alive=False)])
        self.assertEqual([r["verdict"] for r in rows], ["skip:not-live", "skip:not-live"])

    def test_no_pane_for_pid_skips(self):
        [row] = self._plan(["h"], [rec("h", 999)])
        self.assertEqual(row["verdict"], "skip:no-pane")

    def test_modal_pane_carries_the_guard_verdict(self):
        [row] = self._plan(["a"], [rec("a", 100)], screens={"10": MODAL})
        self.assertIn(row["verdict"], ("skip:modal-select", "skip:option-list", "skip:modal-cancel"))

    def test_sleeping_subject_is_never_a_target(self):
        # The whole defect: an idle screen plus a SLEEPING subject used to read `wake`.
        [row] = self._plan(["a"], [rec("a", 100)], states={"a": "SLEEPING"})
        self.assertEqual((row["verdict"], row["subject_state"]), ("skip:sleeping", "SLEEPING"))

    def test_no_subject_falls_through_to_the_screen_verdict(self):
        # `None` means NO subject resolved — a hand-opened tab — not "the read failed".
        # A failed read is the sentinel and is refused; this case must still wake.
        [row] = self._plan(["a"], [rec("a", 100)], states={})
        self.assertEqual((row["verdict"], row["subject_state"]), ("wake", None))

    def test_unreadable_state_is_never_a_target(self):
        sentinel = wake.guard.STATE_UNREADABLE
        [row] = self._plan(["a"], [rec("a", 100)], states={"a": sentinel})
        self.assertEqual((row["verdict"], row["subject_state"]),
                         ("skip:state-unreadable", sentinel))


class SubjectStateTest(unittest.TestCase):
    def test_failed_read_is_the_unreadable_sentinel_not_a_state(self):
        # A subject RESOLVED, so the read failing must fail CLOSED — 2026-11-13: three
        # wakes landed in a SLEEPING window because this returned None and fell through.
        def run(cmd, **kw):
            return types.SimpleNamespace(returncode=1, stdout="", stderr="boom")
        self.assertEqual(
            wake.read_subject_state("a", subject_of=lambda s: ("T", "goal"), run=run),
            wake.guard.STATE_UNREADABLE)

    def test_empty_output_is_the_unreadable_sentinel(self):
        def run(cmd, **kw):
            return types.SimpleNamespace(returncode=0, stdout="", stderr="")
        self.assertEqual(
            wake.read_subject_state("a", subject_of=lambda s: ("T", "goal"), run=run),
            wake.guard.STATE_UNREADABLE)

    def test_timeout_is_the_unreadable_sentinel(self):
        def run(cmd, **kw):
            raise subprocess.SubprocessError("timed out")
        self.assertEqual(
            wake.read_subject_state("a", subject_of=lambda s: ("T", "goal"), run=run),
            wake.guard.STATE_UNREADABLE)

    def test_unresolvable_subject_is_none(self):
        self.assertIsNone(wake.read_subject_state("a", subject_of=lambda s: None))

    def test_state_is_the_first_field(self):
        def run(cmd, **kw):
            return types.SimpleNamespace(returncode=0, stdout="SLEEPING\tslug\tuntil X\n", stderr="")
        self.assertEqual(
            wake.read_subject_state("a", subject_of=lambda s: ("T", "goal"), run=run), "SLEEPING")

    def test_record_without_subject_or_branch_is_none(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            p = pathlib.Path(tmp, "a.json")
            p.write_text('{"subject": "T"}', encoding="utf-8")
            self.assertIsNone(wake.subject_of("a", state_dir=tmp))
            p.write_text('{"subject": "T", "branch": "goal"}', encoding="utf-8")
            self.assertEqual(wake.subject_of("a", state_dir=tmp), ("T", "goal"))

    def test_missing_record_is_none(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(wake.subject_of("nobody", state_dir=tmp))


class OpenCardTest(unittest.TestCase):
    def _card(self, records, poll=lambda i: "OPEN"):
        import json
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            for i, r in enumerate(records):
                pathlib.Path(tmp, f"s1-{i}.open.json").write_text(json.dumps(r))
            return wake.open_card("s1", attention_dir=tmp, poll=poll)

    def test_cleared_record_is_not_a_card(self):
        self.assertIsNone(self._card([{"item_id": "x", "cleared_by": {"event": "UserPromptSubmit"}}]))

    def test_uncleared_record_polled_open_is_a_card(self):
        self.assertEqual(self._card([{"item_id": "x", "cleared_by": None}]), "x")

    def test_uncleared_record_polled_answered_is_not_a_card(self):
        self.assertIsNone(self._card([{"item_id": "x"}], poll=lambda i: "ANSWERED: option: 1"))

    def test_unrunnable_poll_counts_as_open(self):
        self.assertEqual(self._card([{"item_id": "x"}], poll=lambda i: None), "x")

    def test_no_file_is_no_card(self):
        self.assertIsNone(self._card([]))


class SendTest(unittest.TestCase):
    def _run(self, screens_after_activate, subject_state=None):
        calls = []
        seq = list(screens_after_activate)

        def run(cmd, **kw):
            calls.append(cmd)
            return types.SimpleNamespace(returncode=0, stdout="", stderr="")

        def read(_pane):
            return seq.pop(0) if len(seq) > 1 else seq[0]

        outcome = wake.send("10", "hello", run=run, read=read, sleep=lambda s: None,
                            subject_state=subject_state)
        typed = [c for c in calls if c[:3] == ["wezterm", "cli", "send-text"]]
        return outcome, typed

    def test_delivered_needs_a_spinner_after_enter(self):
        outcome, typed = self._run([IDLE, WORKING])
        self.assertEqual(outcome, "delivered")
        self.assertEqual(typed[0][-1], "hello")
        self.assertEqual(typed[1][-1], "\r")

    def test_cleared_composer_without_spinner_is_unverified(self):
        outcome, typed = self._run([IDLE, IDLE])
        self.assertEqual(outcome, "unverified")
        self.assertEqual(len(typed), 1 + wake.ENTER_ATTEMPTS)

    def test_modal_opened_since_plan_types_nothing(self):
        outcome, typed = self._run([MODAL])
        self.assertTrue(outcome.startswith("failed:guard-flipped"))
        self.assertEqual(typed, [])

    def test_sleeping_subject_types_nothing(self):
        # The re-check in `send` must carry the plan's state, not re-read it: a sleeper
        # that became sleeping after the plan is still never typed into.
        outcome, typed = self._run([IDLE], subject_state="SLEEPING")
        self.assertTrue(outcome.startswith("failed:guard-flipped skip:sleeping"))
        self.assertEqual(typed, [])


if __name__ == "__main__":
    unittest.main()
