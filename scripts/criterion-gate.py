#!/usr/bin/env python3
"""Decide which approved rows a goal round may OPEN — the criterion gate, one owner.

Why this exists
---------------
Approval means a task is **allowed** to start, not that it **should** start now. The
reason to start a row is an open Success Criterion or DoD item on its parent goal that
the row closes, with no live task already covering it. A goal is done when its criteria
are met, not when every task is done — leftover tasks of a criteria-met goal are aborted
with a reason, never opened. The operator's rule is recorded on the vault page
[[Development Pipeline Throughput]] § What We Improve and How We Measure:

    "A manager is proactive: its job is to complete the goal — every SC and DoD ticked."
    "Criteria drive the work, not the task list."

Before this script the act leg (`agents/goal-drive.md` clause (6)) opened on approval
alone: the criterion a row served was used only for *ranking*, never for *opening*. So an
approved row that closes nothing still started, and one whose criterion was already met
still started. This is that decision's single home, so the rule lives in code with a test
instead of in prose a run can narrate and then ignore.

What it decides, per row
------------------------
    open          the row's trace names an UNTICKED criterion, and no live task's row
                  names that same criterion -> hand it over as `To open`
    ticked        the row's trace names a criterion already ticked -> hold, never open
    covered       a live task's row already names that same criterion -> hold, never open
    untraced      the goal's row for the task carries no `— closes SC<n>` / `DoD<n>`
                  -> report `untraced — run /unblock`, never open, never skip silently
    criteria-met  the goal's every SC and DoD box is ticked -> never open; the row is a
                  leftover, and `commands/steps/goal-closure.md` Step 0b aborts it

The card (clause (7)) is narrowed the same way: it names only the producers of the goal's
**next open criterion in page order** (the first unticked SC, then DoD), each line naming
the criterion it closes, deduped on the goal plus the set of task names — so a second
round with nothing changed posts no second card.

Usage
-----
    python3 criterion-gate.py decide < payload.json

stdin (JSON):
    goal       str   — the subject goal's name (used in the abort reason and dedupe key)
    goal_text  str   — the goal page body (frontmatter tolerated); the reader finds
                       `# Success Criteria`, `# Definition of Done` and `# Tasks`
    rows[]     — the round's candidate rows: task (name), phase (todo|planning|
                 execution|...), approved (bool), live (bool), optional status
    skip       — optional bool: true on a no-change tick (the closure path only); the
                 verdict pass still runs, the card is never built

stdout (JSON):
    criteria_met   bool
    next_criterion str|null
    verdicts[]     {task, verdict, criterion|null, reason}
    untraced[]     {task}
    to_abort[]     {task, reason}   — only when criteria_met
    card           {next_criterion, producers[], dedupe_key}|null

Exit 2 on malformed input — a planner that guesses on a bad payload opens the wrong row.
"""
import hashlib
import json
import re
import sys

# A checkbox bullet, the only shape a Success Criterion, DoD item or traced task row
# takes on a goal page. Prose bullets (`1. [[X]] — done; delivers SC1's …`) are skipped
# on purpose: they are history, not rows.
BOX_RE = re.compile(r"^\s*[-*]\s*\[([ xX])\]\s*(.*)$")
LINK_RE = re.compile(r"\[\[([^\]|]+)(?:\|[^\]]+)?\]\]")
# The trace the gate reads. Em dash (—), en dash (–) or hyphen tolerated; `closes SC<n>` or
# `closes DoD<n>`. Never a bare "SC1" mention — only the explicit `closes` construct.
CLOSES_RE = re.compile(r"[—–-]\s*closes\s+(SC|DoD)\s*(\d+)", re.IGNORECASE)
SC_ID_RE = re.compile(r"^(SC\d+)\s*[:.]", re.IGNORECASE)
HEADING_RE = re.compile(r"^#\s+(.*)$")


def fail(msg):
    print(f"criterion-gate: {msg}", file=sys.stderr)
    sys.exit(2)


def section(body, name):
    """Return the lines under `# <name>` up to the next `# ` heading."""
    want = name.strip().lower()
    out, active = [], False
    for line in body.splitlines():
        m = HEADING_RE.match(line)
        if m:
            active = m.group(1).strip().lower() == want
            continue
        if active:
            out.append(line)
    return "\n".join(out)


def parse_criteria(body):
    """Return (success_criteria, definition_of_done) as ordered lists of boxes."""
    sc, dod = [], []
    for line in section(body, "Success Criteria").splitlines():
        m = BOX_RE.match(line)
        if not m:
            continue
        text = m.group(2).strip()
        idm = SC_ID_RE.match(text)
        sc.append({"id": idm.group(1).upper() if idm else f"SC{len(sc) + 1}",
                   "ticked": m.group(1).lower() == "x", "text": text})
    for line in section(body, "Definition of Done").splitlines():
        m = BOX_RE.match(line)
        if not m:
            continue
        dod.append({"id": f"DoD{len(dod) + 1}", "ticked": m.group(1).lower() == "x",
                    "text": m.group(2).strip()})
    return sc, dod


def parse_rows(body):
    """Return {task name: {"closes": "SC2"|"DoD1"|None, "ticked": bool}} from `# Tasks`."""
    rows = {}
    for line in section(body, "Tasks").splitlines():
        m = BOX_RE.match(line)
        if not m:
            continue
        rest = m.group(2)
        lm = LINK_RE.search(rest)
        if not lm:
            continue
        name = lm.group(1).strip()
        cm = CLOSES_RE.search(rest)
        closes = None
        if cm:
            kind = "SC" if cm.group(1).upper() == "SC" else "DoD"
            closes = f"{kind}{cm.group(2)}"
        rows[name] = {"closes": closes, "ticked": m.group(1).lower() == "x"}
    return rows


def next_open_criterion(sc, dod):
    """The first unticked SC in page order, then the first unticked DoD item."""
    for c in sc:
        if not c["ticked"]:
            return c["id"]
    for c in dod:
        if not c["ticked"]:
            return c["id"]
    return None


def dedupe_key(goal, names):
    seed = goal + "|" + ",".join(sorted(names))
    return hashlib.sha1(seed.encode("utf-8")).hexdigest()[:12]


def decide(p):
    for key in ("goal", "goal_text", "rows"):
        if key not in p:
            fail(f"missing key: {key}")
    goal = p["goal"]
    body = p["goal_text"]
    sc, dod = parse_criteria(body)
    rows = parse_rows(body)
    ticked = {c["id"]: c["ticked"] for c in sc + dod}
    all_met = bool(sc or dod) and all(c["ticked"] for c in sc + dod)

    candidates = p["rows"]
    # A criterion already named by a LIVE task's row is covered: opening a second row for
    # it would spend a worker on work already in flight.
    live_criteria = set()
    for r in candidates:
        if r.get("live"):
            row = rows.get(r.get("task"))
            if row and row.get("closes"):
                live_criteria.add(row["closes"])

    verdicts, untraced, to_abort = [], [], []
    for r in candidates:
        name = r.get("task")
        if not isinstance(name, str) or not name:
            fail("a row carries no task name")
        row = rows.get(name)
        if all_met:
            reason = f"goal criteria met — {goal}"
            verdicts.append({"task": name, "verdict": "criteria-met",
                             "criterion": None, "reason": reason})
            to_abort.append({"task": name, "reason": reason})
            continue
        if not row or not row.get("closes"):
            verdicts.append({"task": name, "verdict": "untraced",
                             "criterion": None,
                             "reason": "no — closes SC/DoD trace on the goal's row"})
            untraced.append({"task": name})
            continue
        crit = row["closes"]
        if ticked.get(crit, False):
            verdicts.append({"task": name, "verdict": "ticked", "criterion": crit,
                             "reason": f"criterion {crit} already met"})
            continue
        if crit in live_criteria and not r.get("live"):
            verdicts.append({"task": name, "verdict": "covered", "criterion": crit,
                             "reason": f"criterion {crit} already covered by a live task"})
            continue
        verdicts.append({"task": name, "verdict": "open", "criterion": crit,
                         "reason": f"closes open criterion {crit}"})

    nxt = None if all_met else next_open_criterion(sc, dod)
    card = None
    if nxt and not p.get("skip"):
        producers = []
        for r in candidates:
            row = rows.get(r.get("task"))
            if not row or row.get("closes") != nxt:
                continue
            # Only a row still in the operator's inbox is a card candidate.
            if r.get("phase") != "todo" or r.get("approved"):
                continue
            producers.append({"task": r["task"], "criterion": nxt})
        if producers:
            card = {"next_criterion": nxt, "producers": producers,
                    "dedupe_key": dedupe_key(goal, [x["task"] for x in producers])}

    return {"criteria_met": all_met, "next_criterion": nxt, "verdicts": verdicts,
            "untraced": untraced, "to_abort": to_abort, "card": card}


def main(argv):
    if len(argv) != 2 or argv[1] != "decide":
        fail("usage: criterion-gate.py decide < payload.json")
    try:
        payload = json.load(sys.stdin)
    except json.JSONDecodeError as e:
        fail(f"stdin is not JSON: {e}")
    if not isinstance(payload, dict):
        fail("stdin must be a JSON object")
    try:
        result = decide(payload)
    except (KeyError, TypeError, AttributeError) as e:
        fail(f"malformed payload entry: {type(e).__name__}: {e}")
    json.dump(result, sys.stdout, indent=1)
    print()


if __name__ == "__main__":
    main(sys.argv)
