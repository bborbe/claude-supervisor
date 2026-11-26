"""The criterion gate opens a row only when it closes an unticked goal criterion.

Pinned behaviour, one class per Success Criterion of the task that added this gate:

  * SC1 — three fixture rows: one closing an open criterion (opened), one closing a ticked
    criterion (not opened), one with no trace (not opened, reported `untraced`).
  * SC2 — a goal whose every SC and DoD box is ticked: both open tasks read `aborted`
    (the `to_abort` list), and neither is ever opened.
  * SC3 — an approved row whose goal carries no trace at all is reported `untraced`,
    exactly once per round.
  * SC4 — the approval card names only the producers of the goal's next open criterion in
    page order, each line naming the criterion; a second identical round yields the same
    dedupe key, so nothing is posted twice.

Every class carries a POSITIVE assertion — the verdict it must take — so a gate that never
decides anything cannot pass by producing nothing.
"""
import json
import os
import subprocess
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.dirname(HERE)
SCRIPT = os.path.join(SCRIPTS, "criterion-gate.py")

GOAL = "A Goal With Criteria"


def decide(payload):
    out = subprocess.run(
        [sys.executable, SCRIPT, "decide"],
        input=json.dumps(payload), capture_output=True, text=True,
    )
    if out.returncode != 0:
        raise AssertionError(f"exit {out.returncode}: {out.stderr}")
    return json.loads(out.stdout)


def verdict(result, task):
    for v in result["verdicts"]:
        if v["task"] == task:
            return v
    raise AssertionError(f"no verdict for {task!r}: {result['verdicts']}")


class CriterionGateTest(unittest.TestCase):
    def test_sc1_opens_only_a_row_closing_an_unticked_criterion(self):
        body = """# Success Criteria

- [ ] SC1: the first criterion
- [x] SC2: the second, already met
- [ ] SC3: the third

# Definition of Done

- [ ] DoD thing

# Tasks

- [ ] [[Closes Open]] — closes SC1: worker tier
- [ ] [[Closes Ticked]] — closes SC2: done
- [ ] [[No Trace]]
"""
        result = decide({"goal": GOAL, "goal_text": body, "rows": [
            {"task": "Closes Open", "phase": "planning", "approved": True, "live": False},
            {"task": "Closes Ticked", "phase": "planning", "approved": True, "live": False},
            {"task": "No Trace", "phase": "planning", "approved": True, "live": False},
        ]})
        self.assertEqual(verdict(result, "Closes Open")["verdict"], "open")
        self.assertEqual(verdict(result, "Closes Open")["criterion"], "SC1")
        self.assertEqual(verdict(result, "Closes Ticked")["verdict"], "ticked")
        self.assertEqual(verdict(result, "No Trace")["verdict"], "untraced")
        self.assertEqual([u["task"] for u in result["untraced"]], ["No Trace"])
        self.assertEqual(result["to_abort"], [])

    def test_sc2_criteria_met_goal_aborts_leftovers_and_opens_nothing(self):
        body = """# Success Criteria

- [x] SC1: done
- [x] SC2: done

# Definition of Done

- [x] DoD done

# Tasks

- [ ] [[Left One]] — closes SC1: stale
- [ ] [[Left Two]] — closes SC2: stale
"""
        result = decide({"goal": GOAL, "goal_text": body, "rows": [
            {"task": "Left One", "phase": "planning", "approved": True, "live": False},
            {"task": "Left Two", "phase": "planning", "approved": True, "live": False},
        ]})
        self.assertTrue(result["criteria_met"])
        self.assertEqual([v["verdict"] for v in result["verdicts"]],
                         ["criteria-met", "criteria-met"])
        self.assertNotIn("open", [v["verdict"] for v in result["verdicts"]])
        self.assertEqual(
            [a["task"] for a in result["to_abort"]], ["Left One", "Left Two"])
        self.assertEqual(
            result["to_abort"][0]["reason"], f"goal criteria met — {GOAL}")
        self.assertIsNone(result["card"])

    def test_sc3_untraced_row_is_reported_once_and_not_opened(self):
        body = """# Success Criteria

- [ ] SC1: only criterion

# Definition of Done

- [ ] DoD thing

# Tasks

- [ ] [[Orphan Row]]
"""
        result = decide({"goal": GOAL, "goal_text": body, "rows": [
            {"task": "Orphan Row", "phase": "planning", "approved": True, "live": False},
        ]})
        self.assertEqual(verdict(result, "Orphan Row")["verdict"], "untraced")
        self.assertEqual(len(result["untraced"]), 1)
        self.assertIsNone(result["card"])

    def test_sc4_card_names_only_next_criterion_producers(self):
        body = """# Success Criteria

- [x] SC1: met
- [ ] SC2: the next open one
- [ ] SC3: a later one

# Definition of Done

- [ ] DoD thing

# Tasks

- [ ] [[Producer SC2]] — closes SC2: needs building
- [ ] [[Producer SC3]] — closes SC3: later
"""
        payload = {"goal": GOAL, "goal_text": body, "rows": [
            {"task": "Producer SC2", "phase": "todo", "approved": False, "live": False},
            {"task": "Producer SC3", "phase": "todo", "approved": False, "live": False},
        ]}
        first = decide(payload)
        self.assertEqual(first["next_criterion"], "SC2")
        self.assertEqual(
            first["card"]["producers"],
            [{"task": "Producer SC2", "criterion": "SC2"}])
        # A second, unchanged round yields the SAME dedupe key -> nothing posted again.
        second = decide(payload)
        self.assertEqual(first["card"]["dedupe_key"], second["card"]["dedupe_key"])

    def test_covered_by_a_live_task_is_not_opened(self):
        body = """# Success Criteria

- [ ] SC1: open

# Definition of Done

- [ ] DoD thing

# Tasks

- [ ] [[Running Row]] — closes SC1: in flight
- [ ] [[Waiting Row]] — closes SC1: queued
"""
        result = decide({"goal": GOAL, "goal_text": body, "rows": [
            {"task": "Running Row", "phase": "execution", "approved": True, "live": True},
            {"task": "Waiting Row", "phase": "planning", "approved": True, "live": False},
        ]})
        self.assertEqual(verdict(result, "Running Row")["verdict"], "open")
        self.assertEqual(verdict(result, "Waiting Row")["verdict"], "covered")

    def test_malformed_payload_exits_two(self):
        out = subprocess.run(
            [sys.executable, SCRIPT, "decide"],
            input="{}", capture_output=True, text=True,
        )
        self.assertEqual(out.returncode, 2)
        self.assertIn("missing key", out.stderr)


if __name__ == "__main__":
    unittest.main()
