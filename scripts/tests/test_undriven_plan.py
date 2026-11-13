"""The undriven-box planner routes each row by its door and refuses what would over-ask.

Pinned behaviour: a two-way row opens below the soft cap and is held at it; a one-way
self-contained row goes on the round's one card with blast radius and rollback; a
wide-reach row gets a talk-through card carrying no approve option; a re-scope proposal is
never filed; at most one row is filed per round, SC before DoD; and only a row this step
filed and labelled two-way is approved by a manager.

Every class below carries a POSITIVE assertion — the route it must take — so a planner
that never routes anything cannot pass by producing nothing.
"""
import json
import os
import subprocess
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.dirname(HERE)
SCRIPT = os.path.join(SCRIPTS, "undriven-plan.py")


def plan(payload):
    out = subprocess.run(
        [sys.executable, SCRIPT, "decide"],
        input=json.dumps(payload), capture_output=True, text=True,
    )
    if out.returncode != 0:
        raise AssertionError(f"exit {out.returncode}: {out.stderr}")
    return json.loads(out.stdout)


def row(box, door="two-way", reason="vault edit, revert undoes it", kind="sc", order=1,
        proposal="create", driving="none", **extra):
    r = {"box": box, "kind": kind, "order": order, "driving": driving,
         "proposal": proposal, "door": door, "door_reason": reason}
    r.update(extra)
    return r


ONE_WAY = {"blast_radius": "nuke prod cluster", "rollback": "revert the PR and re-apply"}


def base(rows=(), pending=(), live=3, soft=50, card_open=False,
         goal_phase="execution", coverage_ran=True):
    return {
        "coverage_ran": coverage_ran,
        "goal_phase": goal_phase,
        "rows": list(rows),
        "pending": list(pending),
        "cap": {"mode": "headless", "live": live, "soft": soft},
        "card": {"open": card_open},
    }


def by_box(result):
    return {d["box"]: d for d in result["decisions"]}


class NoChangeTick(unittest.TestCase):
    def test_coverage_not_run_means_zero_actions(self):
        r = plan(base(rows=[row("SC1: thing")], coverage_ran=False))
        self.assertEqual(r["decisions"], [])
        self.assertEqual(r["file"], [])
        self.assertIsNone(r["card"])


class FilingIsRankedNeverBulk(unittest.TestCase):
    def test_one_file_per_goal_per_round_sc_before_dod(self):
        r = plan(base(rows=[
            row("DoD: docs", kind="dod", order=1),
            row("SC2: second", order=2),
            row("SC1: first", order=1),
        ]))
        self.assertEqual([f["box"] for f in r["file"]], ["SC1: first"])
        d = by_box(r)
        self.assertEqual(d["SC2: second"]["action"], "next-round")
        self.assertEqual(d["DoD: docs"]["action"], "next-round")

    def test_driven_box_is_ignored(self):
        r = plan(base(rows=[row("SC1: driven", driving="Some Task")]))
        self.assertEqual(r["decisions"], [])

    def test_duplicate_links_instead_of_filing(self):
        r = plan(base(rows=[row("SC1: dup", dup_of="Existing Task")]))
        self.assertEqual(r["file"], [])
        self.assertEqual(by_box(r)["SC1: dup"]["action"], "dedup-linked")


class DoorLabel(unittest.TestCase):
    """SC2: the label must carry a reason, and unsure is one-way."""

    def test_unsure_routes_to_card_never_open(self):
        r = plan(base(rows=[row("SC1: unsure", door="unsure", **ONE_WAY)]))
        f = r["file"][0]
        self.assertEqual(f["route"], "card")
        self.assertEqual(f["door"], "one-way-self-contained")

    def test_missing_reason_is_refused(self):
        r = plan(base(rows=[row("SC1: bare", reason="")]))
        self.assertEqual(r["file"], [])
        d = by_box(r)["SC1: bare"]
        self.assertEqual((d["action"], d["reason"]), ("skip", "door-reason-missing"))

    def test_unknown_door_is_refused(self):
        r = plan(base(rows=[row("SC1: odd", door="sideways")]))
        self.assertEqual(by_box(r)["SC1: odd"]["reason"], "door-unknown")


class TwoWay(unittest.TestCase):
    """SC3: opened below the soft cap of its mode, no card; held at the cap."""

    def test_two_way_routes_open_below_cap(self):
        r = plan(base(rows=[row("SC1: easy")], live=3, soft=50))
        self.assertEqual(r["file"][0]["route"], "open")
        self.assertIsNone(r["card"])

    def test_two_way_held_at_cap(self):
        r = plan(base(rows=[row("SC1: easy")], live=50, soft=50))
        self.assertEqual(r["file"][0]["route"], "held-on-cap")
        self.assertIsNone(r["card"])

    def test_two_way_on_goal_not_in_execution_goes_to_card(self):
        r = plan(base(rows=[row("SC1: easy", **ONE_WAY)], goal_phase="planning"))
        self.assertEqual(r["file"][0]["route"], "card")

    def test_two_way_on_goal_not_in_execution_without_fields_is_refused(self):
        """A two-way row on a non-execution goal routes to `card`, which needs the card
        fields whatever the door. Without them the row is refused, not carded blind."""
        r = plan(base(rows=[row("SC1: easy")], goal_phase="planning"))
        self.assertEqual(r["file"], [])
        self.assertEqual(by_box(r)["SC1: easy"]["reason"], "card-fields-missing")

    def test_two_way_on_execution_goal_needs_no_card_fields(self):
        r = plan(base(rows=[row("SC1: easy")], goal_phase="execution"))
        self.assertEqual(r["file"][0]["route"], "open")


class OneWaySelfContained(unittest.TestCase):
    """SC4a: one card, naming task, blast radius and rollback."""

    def test_card_payload_carries_the_three_fields(self):
        r = plan(base(rows=[row("SC1: deploy", door="one-way-self-contained", **ONE_WAY)]))
        self.assertEqual(r["file"][0]["route"], "card")
        card = r["card"]
        self.assertEqual(card["kind"], "approve")
        item = card["items"][0]
        self.assertEqual(item["box"], "SC1: deploy")
        self.assertEqual(item["blast_radius"], ONE_WAY["blast_radius"])
        self.assertEqual(item["rollback"], ONE_WAY["rollback"])

    def test_card_without_blast_radius_is_refused(self):
        r = plan(base(rows=[row("SC1: deploy", door="one-way-self-contained")]))
        self.assertEqual(r["file"], [])
        self.assertEqual(by_box(r)["SC1: deploy"]["reason"], "card-fields-missing")


class CardDiscipline(unittest.TestCase):
    """SC4b: an open card is not re-posted, a skip is not re-asked, two-way keeps going."""

    def test_open_card_is_not_reposted_and_two_way_still_opens(self):
        r = plan(base(
            rows=[row("SC1: deploy", door="one-way-self-contained", **ONE_WAY)],
            pending=[{"task": "Easy Task", "planned_by": "goal-plan-undriven",
                      "door": "two-way", "phase": "todo"}],
            card_open=True,
        ))
        self.assertIsNone(r["card"])
        self.assertEqual(by_box(r)["SC1: deploy"]["action"], "card-pending")
        self.assertEqual([p["task"] for p in r["open"]], ["Easy Task"])

    def test_turned_down_box_is_closed_by_dup_of(self):
        """A box the operator already declined carries `dup_of` to the task that quotes it,
        so the next tick reports `dedup-linked` instead of re-asking."""
        r = plan(base(
            rows=[row("SC1: deploy", door="one-way-self-contained", **ONE_WAY,
                      dup_of="Deploy Task")],
        ))
        self.assertIsNone(r["card"])
        self.assertEqual(r["file"], [])
        d = by_box(r)["SC1: deploy"]
        self.assertEqual((d["action"], d["reason"]), ("dedup-linked", "Deploy Task"))


class WideReach(unittest.TestCase):
    """SC5: a Decision block and a talk-through card that asks for no decision."""

    def test_wide_reach_routes_talk_through_with_no_approve_option(self):
        r = plan(base(rows=[row("SC1: restructure k8s", door="one-way-wide", **ONE_WAY)]))
        f = r["file"][0]
        self.assertEqual(f["route"], "talk-through")
        self.assertTrue(f["decision_block"])
        card = r["card"]
        self.assertEqual(card["kind"], "talk-through")
        self.assertFalse(any("approve" in o.lower() for o in card["options"]))


class ReScope(unittest.TestCase):
    """SC6: a re-scope proposal is never filed."""

    def test_rescope_is_operator_decision_and_files_nothing(self):
        r = plan(base(rows=[row("SC1: wrong", proposal="re-scope")]))
        self.assertEqual(r["file"], [])
        self.assertEqual(by_box(r)["SC1: wrong"]["action"], "operator-decision")


class ApprovalBoundary(unittest.TestCase):
    """Only a row step 4d itself filed AND labelled two-way is approved by a manager.

    Regression guard for the 2026-09-30 waiting-approval bucket: every other todo row
    stays the operator's inbox — never opened, never flipped."""

    def test_operator_filed_todo_row_is_untouched(self):
        r = plan(base(pending=[
            {"task": "Operator Row", "planned_by": "", "door": "two-way", "phase": "todo"},
        ]))
        self.assertEqual(r["open"], [])
        self.assertEqual(r["approve_by_manager"], [])

    def test_one_way_4d_row_is_not_self_approved(self):
        r = plan(base(pending=[
            {"task": "Risky Row", "planned_by": "goal-plan-undriven",
             "door": "one-way-self-contained", "phase": "todo"},
        ]))
        self.assertEqual(r["approve_by_manager"], [])

    def test_two_way_4d_row_is_approved_and_opened_below_cap(self):
        r = plan(base(pending=[
            {"task": "Easy Row", "planned_by": "goal-plan-undriven",
             "door": "two-way", "phase": "todo"},
        ]))
        self.assertEqual(r["approve_by_manager"], ["Easy Row"])
        self.assertEqual([p["task"] for p in r["open"]], ["Easy Row"])

    def test_two_way_4d_row_at_cap_is_held_not_approved(self):
        r = plan(base(pending=[
            {"task": "Easy Row", "planned_by": "goal-plan-undriven",
             "door": "two-way", "phase": "todo"},
        ], live=50, soft=50))
        self.assertEqual(r["approve_by_manager"], [])
        self.assertEqual(r["open"], [])
        self.assertEqual(r["held_on_cap"], ["Easy Row"])


class InputRefusals(unittest.TestCase):
    def test_malformed_stdin_exits_nonzero(self):
        out = subprocess.run([sys.executable, SCRIPT, "decide"], input="{not json",
                             capture_output=True, text=True)
        self.assertEqual(out.returncode, 2)

    def test_missing_cap_exits_nonzero(self):
        p = base()
        del p["cap"]
        out = subprocess.run([sys.executable, SCRIPT, "decide"], input=json.dumps(p),
                             capture_output=True, text=True)
        self.assertEqual(out.returncode, 2)

    def test_malformed_nested_entry_exits_two_not_a_traceback(self):
        p = base()
        p["card"] = []
        out = subprocess.run([sys.executable, SCRIPT, "decide"], input=json.dumps(p),
                             capture_output=True, text=True)
        self.assertEqual(out.returncode, 2)
        self.assertIn("malformed payload entry", out.stderr)
        self.assertNotIn("Traceback", out.stderr)


if __name__ == "__main__":
    unittest.main()
