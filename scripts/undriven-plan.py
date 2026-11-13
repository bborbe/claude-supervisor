#!/usr/bin/env python3
"""Decide what a goal round does with each SC/DoD box nothing drives — one owner.

Why this exists
---------------
The coverage step (`commands/steps/goal-coverage.md`) reports a box whose driving cell is
`none`, with a proposal, and is read-only by design: *"surface it, never decide it"*. No
step decided it, so the operator had to visit every manager and ask "what is the next task
we should create/approve/open to complete the next SC/DoD?" (2026-11-13). Step 4d
(`commands/steps/goal-plan-undriven.md`) now files the task and routes it by its door; this
script is that decision, so the routing has one derivation instead of one per manager.

The door is the manager's judgment — this script never classifies. It takes the label the
manager wrote (with its reason) and turns it into a route:

    two-way                 -> open        (below the soft cap of its spawn mode)
                            -> held-on-cap (at or above it — never carded)
    one-way-self-contained  -> card        (the round's ONE approve card: task, blast
                                            radius, rollback)
    one-way-wide            -> talk-through (a `### Decision:` block, Status: proposed, and
                                            a card that asks for NO decision)
    unsure                  -> treated as one-way-self-contained
    proposal re-scope       -> operator-decision (never filed: it changes an SC)

Operator rulings this encodes
-----------------------------
- 2026-11-13, the three tiers: *"there is a card that says, 'Hey, I want to restructure
  everything.' And I maybe don't read that well and click 'Yes.' And this should be
  prevented by the decision records that need more attention from me."* Hence the
  talk-through card carries no approve option.
- 2026-11-13, caps: *"the softcap is for unflagged tasks and the hardcap is for flagged
  tasks. So I guess we only start workers for tasks automatically through softcap."* A
  manager-filed row is unflagged, so `open` needs `live < soft` on its mode's axis.
- 2026-10-01, ranked never bulk: at most ONE file per goal per round, SC boxes before DoD
  boxes, page order within each.
- 2026-09-30, the approval boundary: `phase: todo` is the operator's inbox. Only a row this
  step itself filed (`planned_by: goal-plan-undriven`) AND labelled two-way may be approved
  by a manager; every other todo row is left exactly as it is.

Usage
-----
    python3 undriven-plan.py decide < payload.json

stdin (JSON):
    coverage_ran   bool   — false on a no-change tick (the gate exited 0): zero actions.
    goal_phase     str    — the subject goal's phase; two-way opens only at `execution`.
    rows[]         — this round's coverage rows: box, kind (sc|dod), order, driving,
                     proposal, door, door_reason, optional dup_of, blast_radius, rollback.
    pending[]      — todo rows already on disk: task, planned_by, door, phase.
    cap            — {mode, live, soft} for the spawn mode a manager-opened row uses.
    card           — {open: bool} — whether the round's waiting-approval card is still open.
                     A box the operator turned down needs no skip list: it is closed by
                     `dup_of`, because the filed task quotes the box and the next tick's
                     search finds it.

stdout (JSON): decisions[], file[], open[], approve_by_manager[], held_on_cap[], card.
Exit 2 on malformed input — a planner that guesses on a bad payload files the wrong task.
"""
import json
import sys

PLANNED_BY = "goal-plan-undriven"
FILE_PROPOSALS = {"create", "split", "diagnosis-task"}
DOORS = {"two-way", "one-way-self-contained", "one-way-wide", "unsure"}
MAX_FILES_PER_ROUND = 1
TALK_THROUGH_OPTIONS = ["talk-through scheduled", "not now"]


def fail(msg):
    print(f"undriven-plan: {msg}", file=sys.stderr)
    sys.exit(2)


def below_soft_cap(cap):
    return cap["live"] < cap["soft"]


def route_for(door, goal_phase, cap):
    if door == "two-way":
        if goal_phase != "execution":
            return "card"
        return "open" if below_soft_cap(cap) else "held-on-cap"
    if door == "one-way-wide":
        return "talk-through"
    return "card"


def decide(p):
    for key in ("coverage_ran", "goal_phase", "rows", "pending", "cap", "card"):
        if key not in p:
            fail(f"missing key: {key}")
    cap = p["cap"]
    if not all(k in cap for k in ("mode", "live", "soft")):
        fail("cap needs mode, live, soft")

    out = {"decisions": [], "file": [], "open": [], "approve_by_manager": [],
           "held_on_cap": [], "card": None}
    card_open = bool(p["card"].get("open"))

    # Pending rows: only this step's own two-way rows may be approved by a manager.
    for t in p["pending"]:
        if t.get("phase") != "todo" or t.get("planned_by") != PLANNED_BY:
            continue
        if t.get("door") != "two-way" or p["goal_phase"] != "execution":
            continue
        if below_soft_cap(cap):
            out["approve_by_manager"].append(t["task"])
            out["open"].append({"task": t["task"], "mode": cap["mode"]})
        else:
            out["held_on_cap"].append(t["task"])

    if not p["coverage_ran"]:
        return out

    undriven = [r for r in p["rows"] if r.get("driving") == "none"]
    undriven.sort(key=lambda r: (0 if r.get("kind") == "sc" else 1, r.get("order", 0)))

    card_items, talk_items = [], []
    for r in undriven:
        box = r["box"]
        proposal = r.get("proposal")
        if proposal == "re-scope":
            out["decisions"].append({"box": box, "action": "operator-decision",
                                     "reason": "re-scope changes an SC"})
            continue
        if proposal not in FILE_PROPOSALS:
            out["decisions"].append({"box": box, "action": "skip",
                                     "reason": "proposal-unknown"})
            continue
        if r.get("dup_of"):
            out["decisions"].append({"box": box, "action": "dedup-linked",
                                     "reason": r["dup_of"]})
            continue
        door = r.get("door")
        if door not in DOORS:
            out["decisions"].append({"box": box, "action": "skip", "reason": "door-unknown"})
            continue
        if not (r.get("door_reason") or "").strip():
            out["decisions"].append({"box": box, "action": "skip",
                                     "reason": "door-reason-missing"})
            continue
        if door == "unsure":
            door = "one-way-self-contained"
        route = route_for(door, p["goal_phase"], cap)
        if route in ("card", "talk-through") and not (r.get("blast_radius") and r.get("rollback")):
            out["decisions"].append({"box": box, "action": "skip",
                                     "reason": "card-fields-missing"})
            continue
        if len(out["file"]) >= MAX_FILES_PER_ROUND:
            out["decisions"].append({"box": box, "action": "next-round",
                                     "reason": "one file per goal per round"})
            continue
        if route == "card" and card_open:
            out["decisions"].append({"box": box, "action": "card-pending",
                                     "reason": "the round's card is still open"})
            continue
        entry = {"box": box, "door": door, "door_reason": r["door_reason"],
                 "route": route, "planned_by": PLANNED_BY,
                 "decision_block": route == "talk-through"}
        out["file"].append(entry)
        out["decisions"].append({"box": box, "action": "file", "reason": route})
        if route == "card":
            card_items.append({"box": box, "blast_radius": r["blast_radius"],
                               "rollback": r["rollback"]})
        elif route == "talk-through":
            talk_items.append({"box": box})

    if card_items:
        out["card"] = {"kind": "approve", "items": card_items}
    elif talk_items:
        out["card"] = {"kind": "talk-through", "items": talk_items,
                       "options": list(TALK_THROUGH_OPTIONS)}
    return out


def main(argv):
    if len(argv) != 2 or argv[1] != "decide":
        fail("usage: undriven-plan.py decide < payload.json")
    try:
        payload = json.load(sys.stdin)
    except json.JSONDecodeError as e:
        fail(f"stdin is not JSON: {e}")
    if not isinstance(payload, dict):
        fail("stdin must be a JSON object")
    json.dump(decide(payload), sys.stdout, indent=1)
    print()


if __name__ == "__main__":
    main(sys.argv)
