"""pane-wake.py — who the wake may target, and what counts as delivered.

The target selection is the load-bearing half: it is what keeps a keystroke out of the Fleet
Manager, out of the triage session itself, and out of any session parked on the operator.
`PlanTest` pins each refusal. `SendTest` pins that delivery is positive-only — a cleared
composer without a spinner is `unverified`, never `delivered` — and that the guard is re-run
after activation, so a modal that opened since the plan read is not typed into.
"""
import importlib.util
import pathlib
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
    def _plan(self, sids, records, screens=None, cards=(), self_session=None, self_pane=None):
        panes = {"10": {"tty": "/dev/ttys010"}, "11": {"tty": "/dev/ttys011"}, "12": {"tty": "/dev/ttys012"}}
        pid_ttys = {100: "ttys010", 101: "ttys011", 102: "ttys012"}
        screens = screens or {}
        return wake.plan(sids, records=records, panes=panes, pid_ttys=pid_ttys,
                         read=lambda p: screens.get(p, IDLE),
                         card=lambda s: (s + ".open.json") if s in cards else None,
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
    def _run(self, screens_after_activate):
        calls = []
        seq = list(screens_after_activate)

        def run(cmd, **kw):
            calls.append(cmd)
            return types.SimpleNamespace(returncode=0, stdout="", stderr="")

        def read(_pane):
            return seq.pop(0) if len(seq) > 1 else seq[0]

        outcome = wake.send("10", "hello", run=run, read=read, sleep=lambda s: None)
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


if __name__ == "__main__":
    unittest.main()
