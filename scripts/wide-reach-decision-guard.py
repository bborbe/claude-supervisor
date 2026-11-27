#!/usr/bin/env python3
"""Refuse an attention card that asks for a wide-reach decision.

The rule lives in the operator's vault (`One-Way and Two-Way Door Decisions`
§ Where the decision is made): a one-way door with wide reach gets a written
decision record and an interactive talk-through, and *"Never a board card"*.
The card that reaches the board only announces the decision.

Nothing enforced that. On 2026-11-27 a worker put the IBKR weekly-candle choice
on a card, took the click as the decision, and closed the task with no record.
This module is the post-time half: given the card's options and the asking
session, decide whether the card is asking for a decision it must not ask.

Two questions, kept separate so each is testable without a vault:

  * `classify_card` — is this card ASKING (deciding) or merely ANNOUNCING?
  * `pending_decision_marks` — does the asking session's task have a wide-reach
    decision pending, and by which mark?

`refusal_for` combines them. Every I/O edge is injectable, so the tests drive
real fixtures rather than the operator's live vaults.
"""

from __future__ import annotations

import os
import re
import sys

# An option is a pure ack/dismiss when choosing it decides nothing — it only
# acknowledges a statement or postpones it. Deliberately EXCLUDES yes/no: a
# yes/no answer to a real question IS the decision, and letting it through
# would re-open exactly the hole this guard closes.
PURE_ACK = frozenset({
    "ok", "okay", "ack", "acknowledged", "noted", "understood", "got it",
    "dismiss", "dismissed", "later", "no action", "nothing", "fine",
    "leave it", "leave it unchanged",
})

_DECISION_HEADING = re.compile(r"(?m)^#\s+Decision\s*$")


def _label(option):
    """The label of an option, accepting both the dict and bare-string forms."""
    if isinstance(option, dict):
        return str(option.get("label", ""))
    return str(option)


def classify_card(options):
    """Return `"deciding"` or `"announcement"`.

    `announcement` when the card offers no choice at all, or every choice is a
    pure ack/dismiss. `deciding` when any choice would select an outcome.
    """
    for option in options or []:
        core = _label(option).strip().lower().rstrip(".!")
        if core not in PURE_ACK:
            return "deciding"
    return "announcement"


def _truthy(value):
    return str(value).strip().lower() in ("true", "yes", "1")


def decision_record_is_pending(frontmatter):
    """True unless the record was accepted by `vault-cli decision ack`.

    `ack` is what writes `reviewed: true` / `status: accepted`. The IBKR TDR
    carries neither while `proposed`, so absence means not accepted — the
    fail-closed direction, because treating an unreadable record as accepted
    would let the very card this guard refuses through.
    """
    if _truthy(frontmatter.get("reviewed", False)):
        return False
    if str(frontmatter.get("status", "")).strip().lower() == "accepted":
        return False
    return True


def pending_decision_marks(frontmatter, body):
    """Return the list of marks showing this task waits on a wide-reach decision.

    Three marks, per the task's SC1: an explicit frontmatter flag, a `# Decision`
    block, or a linked Decision Record in `proposed` state (resolved by the
    caller and passed in as `frontmatter["_pending_records"]`, a list of names).
    """
    marks = []
    if str(frontmatter.get("wide_reach_decision", "")).strip().lower() == "pending":
        marks.append("wide_reach_decision: pending")
    if _DECISION_HEADING.search(body or ""):
        marks.append("# Decision block")
    for name in frontmatter.get("_pending_records") or []:
        marks.append(f"Decision Record {name} (proposed)")
    return marks


def refusal_for(options, frontmatter, body):
    """The refusal text, or None when the card is allowed.

    A deciding card on a task with a pending wide-reach decision is refused.
    An announcement card is always allowed — it is the sanctioned channel.
    A deciding card on a task with NO pending decision is allowed: that is an
    ordinary question, and this guard is not a general card censor.
    """
    if classify_card(options) != "deciding":
        return None
    marks = pending_decision_marks(frontmatter or {}, body or "")
    if not marks:
        return None
    return (
        "this card asks for a decision, and the asking task has a wide-reach "
        "decision pending (" + "; ".join(marks) + "). A one-way door with wide "
        "reach gets a written decision record and an interactive talk-through, "
        "never a board card — see `One-Way and Two-Way Door Decisions` § Where "
        "the decision is made. Post an announcement card instead (state that a "
        "decision is waiting and when the talk-through happens), or re-run with "
        "--force if the card is deliberate."
    )


# --- I/O edges (injectable) -------------------------------------------------

def split_frontmatter(text):
    """Return (frontmatter_dict, body). Tolerant: a parse miss yields ({}, text)."""
    if not text.startswith("---"):
        return {}, text
    parts = text.split("---", 2)
    if len(parts) < 3:
        return {}, text
    raw, body = parts[1], parts[2]
    front = {}
    for line in raw.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        front[key.strip()] = value.strip().strip("'\"")
    return front, body


def _default_obsidian_root():
    return os.path.expanduser("~/Documents/Obsidian")


def _task_files(root):
    for vault in sorted(os.listdir(root)):
        for folder in ("24 Tasks", "25 Tasks"):
            directory = os.path.join(root, vault, folder)
            if not os.path.isdir(directory):
                continue
            for name in sorted(os.listdir(directory)):
                if name.endswith(".md"):
                    yield os.path.join(directory, name)


def _decision_files(root):
    for vault in sorted(os.listdir(root)):
        directory = os.path.join(root, vault, "40 Decisions")
        if not os.path.isdir(directory):
            continue
        for name in sorted(os.listdir(directory)):
            if name.endswith(".md"):
                yield os.path.join(directory, name)


def pending_records_for(task_name, root):
    """Names of Decision Records whose `related_task` names this task and are pending."""
    found = []
    try:
        paths = list(_decision_files(root))
    except OSError:
        return found
    needle = f"[[{task_name}]]"
    for path in paths:
        try:
            with open(path, encoding="utf-8") as handle:
                front, _ = split_frontmatter(handle.read())
        except OSError:
            continue
        if str(front.get("related_task", "")).strip() != needle:
            continue
        if decision_record_is_pending(front):
            found.append(os.path.splitext(os.path.basename(path))[0])
    return found


def load_session_task(producer_id, root=None):
    """Find the in-progress task owned by `producer_id`.

    Returns (frontmatter, body) or (None, None). Fail-open: an unreadable or
    absent task is not an error — a card posted by a script has no task, and
    SC1 scopes itself to "a task with a wide-reach decision pending".
    """
    root = root or _default_obsidian_root()
    try:
        candidates = list(_task_files(root))
    except OSError:
        return None, None
    for path in candidates:
        try:
            with open(path, encoding="utf-8") as handle:
                text = handle.read()
        except OSError:
            continue
        front, body = split_frontmatter(text)
        if front.get("claude_session_id") != producer_id:
            continue
        if str(front.get("status", "")).strip().lower() not in ("in_progress", "todo"):
            continue
        name = os.path.splitext(os.path.basename(path))[0]
        front["_name"] = name
        front["_pending_records"] = pending_records_for(name, root)
        return front, body
    return None, None


def main(argv=None):
    """CLI: `wide-reach-decision-guard.py --producer-id <id> [--option L]...`"""
    argv = sys.argv[1:] if argv is None else argv
    producer_id, options = "", []
    it = iter(argv)
    for arg in it:
        if arg == "--producer-id":
            producer_id = next(it, "")
        elif arg == "--option":
            options.append({"label": next(it, "")})
    front, body = load_session_task(producer_id)
    if front is None:
        print("ALLOWED: no in-progress task for this session")
        return 0
    refusal = refusal_for(options, front, body)
    if refusal:
        print(f"REFUSED: {refusal}")
        return 2
    print("ALLOWED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
