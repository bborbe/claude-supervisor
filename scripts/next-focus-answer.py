#!/usr/bin/env python3
"""Consume the operator's answer to a `next-focus` proposal card.

Why this exists
---------------
The fleet round posts one card per proposed row and offers two answers —
`add it to the plan` and `leave it out`. Until this script existed **nothing
consumed either**: an approved row was never written into the weekly plan, so
`next-focus.py` kept seeing it as "in no current plan" and proposed it again;
a declined row left no record at all. The same three cards returned every round.

`next-focus.py` cannot own this. Its own contract says so: *"It is read-only: it
opens nothing, posts nothing and edits nothing. The round renders its output and
owns every act."* Making it write would break an invariant `commands/steps/
readings.md` relies on. So the round owns the act, through this script, and the
reader stays a reader.

Two records, two readers
------------------------
  dismissal   ~/.claude/state/next-focus-dismissed/<digest>.json
              written here, READ by `next-focus.py` (a read, so its read-only
              contract survives). One file per card key, because the store's
              suppression is per key.

  consumption ~/.claude/state/next-focus-consumed/<vault-slug>.jsonl
              append-only, written here, read by the round's render (SC4). A
              per-key state file cannot answer "what did THIS round consume".

The two are not one thing: a dismissal is durable state, a consumption entry is
an audit line.

The block is the load-bearing artifact
--------------------------------------
`parse_weekly` reads only `### N.` blocks carrying `**Task:** [[name]]` — never
the `## ✅ Picked` table. Measured 2026-10-27: appending three rows to the table
did NOT stop the re-post, and hand-writing the matching `### N.` blocks did. So
`add it to the plan` writes BOTH: the row the operator reads, and the block the
reader parses.

Usage
-----
    next-focus-answer.py --vault <vault> --consume
    next-focus-answer.py --vault <vault> --consume --json

Exit codes
----------
  0  consumed one or more answers, or there was nothing to consume
  2  REFUSED — the store was unreadable, the weekly note could not be read, or
     the note carries no `### N.` blocks to number from. Never read as "nothing
     to consume": a round that could not look must not look like a quiet round.
"""
import argparse
import hashlib
import importlib.util
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent


def _load(name, filename):
    spec = importlib.util.spec_from_file_location(name, HERE / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


nf = _load("next_focus", "next-focus.py")
lookup = _load("attention_card_lookup", "attention-card-lookup.py")

PROPOSAL_PREFIX = "next-focus:"
ADD = "add it to the plan"
LEAVE = "leave it out"

CONSUMED_DIR = os.path.expanduser("~/.claude/state/next-focus-consumed")

BLOCK_RE = re.compile(r"^###\s+(\d+)\.\s")
PICKED_RE = re.compile(r"^##\s+✅\s+Picked\s+\((\d+)\)")
ROW_RE = re.compile(r"^\|\s*(\d+)\s*\|")
HEADING_RE = re.compile(r"^#{1,2}\s")


class Refused(Exception):
    """A read that failed. Never collapsed into an empty result."""


# --------------------------------------------------------------- the two writes


def _task_state(vault, tasks_dir, name):
    """`(status, phase)` for the row, or empty strings when the file is unreadable."""
    text, err = nf.read_text(Path(vault) / tasks_dir / f"{name}.md")
    if err:
        return "", ""
    fm = nf.frontmatter(text)
    return str(fm.get("status", "")), str(fm.get("phase", ""))


def _block_number(lines):
    numbers = [int(m.group(1)) for m in (BLOCK_RE.match(line) for line in lines) if m]
    return (max(numbers) + 1) if numbers else 1


def _insert_block(lines, number, name):
    """Insert a `### N.` block after the LAST existing one, inside its section.

    `parse_weekly` closes a block on any `^#{1,2} ` heading, so the block must sit
    before the next heading — appending it at EOF would put it outside the section
    a reader lands in.
    """
    starts = [i for i, line in enumerate(lines) if BLOCK_RE.match(line)]
    if not starts:
        raise Refused("the weekly note carries no `### N.` block to number from")
    last = starts[-1]
    end = len(lines)
    for i in range(last + 1, len(lines)):
        if HEADING_RE.match(lines[i]):
            end = i
            break
    block = [f"### {number}. 📌 {name}", "", f"**Task:** [[{name}]]", ""]
    return lines[:end] + block + lines[end:]


def _insert_picked_row(lines, number, name, why):
    """Insert a `| N | … |` row after the last row of the `## ✅ Picked` table."""
    heading = next((i for i, line in enumerate(lines) if PICKED_RE.match(line)), None)
    if heading is None:
        return lines, False
    end = len(lines)
    for i in range(heading + 1, len(lines)):
        if HEADING_RE.match(lines[i]):
            end = i
            break
    rows = [i for i in range(heading + 1, end) if ROW_RE.match(lines[i])]
    if not rows:
        return lines, False
    row = f"| {number} | 📌 {name} | {why} |"
    return lines[: rows[-1] + 1] + [row] + lines[rows[-1] + 1:], True


def _bump_picked_count(lines, number):
    for i, line in enumerate(lines):
        if PICKED_RE.match(line):
            lines[i] = f"## ✅ Picked ({number})"
            return lines
    return lines


def add_to_plan(vault, tasks_dir, name, day):
    """`(detail, notes)` — write both weekly-note artifacts, idempotent by task name.

    Idempotency is by NAME, not by card key: the same task can be proposed under a
    re-worded card, and a second `add it to the plan` must not add a second block.
    """
    weekly = nf.plan_path(vault, "weekly", nf.iso_week(day))
    text, err = nf.read_text(weekly)
    if err:
        raise Refused(f"weekly note unreadable: {weekly} ({err})")

    lines = text.splitlines()
    already = any(name in row["names"] for row in nf.parse_weekly(text))
    already_row = any(name in line for line in lines if ROW_RE.match(line))
    if already and already_row:
        return {"weekly": str(weekly), "block": None, "row": None}, ["already present"]

    number = _block_number(lines)
    notes = []
    if not already:
        lines = _insert_block(lines, number, name)
    else:
        notes.append("block already present")

    status, phase = _task_state(vault, tasks_dir, name)
    why = (
        f"P1, in no current plan. Added {day.isoformat()} on the operator's answer"
        + (f" — `{status}` / `{phase}`." if status or phase else ".")
    )
    if not already_row:
        lines, inserted = _insert_picked_row(lines, number, name, why)
        if not inserted:
            notes.append("no `## ✅ Picked` table — row not written")
        else:
            lines = _bump_picked_count(lines, number)
    else:
        notes.append("picked row already present")

    _write_atomic(weekly, "\n".join(lines) + "\n")
    return {"weekly": str(weekly), "block": number, "row": number}, notes


def write_dismissal(state_dir, card_key, name, kind, item_id, now):
    """The durable `leave it out` record `next-focus.py` reads back."""
    directory = Path(state_dir)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / (hashlib.sha256(card_key.encode("utf-8")).hexdigest()[:16] + ".json")
    record = {
        "card_key": card_key,
        "name": name,
        "kind": kind,
        "item_id": item_id,
        "dismissed_at": now.isoformat(),
    }
    _write_atomic(path, json.dumps(record, indent=2, sort_keys=True) + "\n")
    return path


def _write_atomic(path, text):
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        handle.write(text)
    os.replace(tmp, path)


# ------------------------------------------------------------- the consumption log


def consumed_path(consumed_dir, vault):
    slug = re.sub(r"[^a-z0-9]+", "-", Path(vault).name.lower()).strip("-")
    return Path(consumed_dir) / f"{slug}.jsonl"


def load_consumed(path):
    """`(set of (card_key, item_id), notes)` — what has already been consumed."""
    if not path.is_file():
        return set(), []
    seen, notes = set(), []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
        except ValueError:
            notes.append(f"consumed log line {number}: unreadable")
            continue
        seen.add((entry.get("card_key", ""), entry.get("item_id", "")))
    return seen, notes


def append_consumed(path, entry):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry, sort_keys=True) + "\n")


# ------------------------------------------------------------------- the consume


def split_payload(payload):
    """The row name out of `<name> — <why> — <file>:<line>`.

    The name is the first segment: `next-focus.py` builds the payload with ` — ` as
    the separator, and a task filename may itself contain a dash but never that
    spaced em dash pair.
    """
    return str(payload or "").split(" — ")[0].strip()


def kind_of_key(card_key):
    parts = card_key.split(":")
    return parts[2] if len(parts) > 2 else ""


def consume_items(items, vault, tasks_dir, dismissed_dir, consumed_dir, now):
    """`(consumed, notes)` — apply every answered proposal card not yet consumed.

    Takes the history items as an argument rather than fetching them, so the
    behaviour under test is the behaviour that runs — no store needed.
    """
    proposals = [
        item for item in items
        if str(item.get("dedup_key", "")).startswith(PROPOSAL_PREFIX)
    ]
    by_key = {}
    for item in proposals:
        by_key.setdefault(str(item.get("dedup_key", "")), []).append(item)

    log_path = consumed_path(consumed_dir, vault)
    done, notes = load_consumed(log_path)
    consumed = []

    for card_key in sorted(by_key):
        card, err = lookup.select_latest(by_key[card_key])
        if err:
            notes.append(f"{card_key}: {err}")
            continue
        if card is None:
            continue  # nothing answered under this key yet
        item_id = str(card.get("item_id", ""))
        if (card_key, item_id) in done:
            continue

        answer = str((card.get("answer") or {}).get("value", ""))
        name = split_payload(card.get("payload", ""))
        kind = kind_of_key(card_key)
        if not name:
            notes.append(f"{card_key}: card carries no payload name — not consumed")
            continue

        if answer == ADD:
            detail, extra = add_to_plan(vault, tasks_dir, name, now.date())
            notes.extend(extra)
            entry = {
                "at": now.isoformat(), "card_key": card_key, "item_id": item_id,
                "name": name, "kind": kind, "action": "added", **detail,
            }
        elif answer == LEAVE:
            path = write_dismissal(dismissed_dir, card_key, name, kind, item_id, now)
            entry = {
                "at": now.isoformat(), "card_key": card_key, "item_id": item_id,
                "name": name, "kind": kind, "action": "dismissed",
                "record": str(path),
            }
        else:
            notes.append(f"{card_key}: unhandled answer {answer!r} — not consumed")
            continue

        append_consumed(log_path, entry)
        consumed.append(entry)

    return consumed, notes


def render(consumed, notes, open_cards):
    if not consumed:
        return (
            f"next-focus consume: nothing to consume "
            f"(0 answered card(s) awaiting; {open_cards} open)"
        )
    lines = []
    for entry in consumed:
        card = entry["item_id"][:8]
        if entry["action"] == "added":
            where = (
                f"block {entry['block']}, picked row {entry['row']}"
                if entry.get("block") else "already present"
            )
            lines.append(f'next-focus consume: added "{entry["name"]}" ({where}) — card {card}')
        else:
            lines.append(f'next-focus consume: dismissed "{entry["name"]}" — card {card}')
    for note in notes:
        lines.append(f"  note: {note}")
    return "\n".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Consume the operator's answer to a next-focus proposal card.")
    parser.add_argument("--vault", required=True)
    parser.add_argument("--tasks-dir", default="25 Tasks")
    parser.add_argument("--consume", action="store_true",
                        help="apply every answered proposal card not yet consumed")
    parser.add_argument("--dismissed-dir", default=nf.DISMISSED_DIR,
                        help=f"dismissal state dir; default {nf.DISMISSED_DIR}")
    parser.add_argument("--consumed-dir", default=CONSUMED_DIR,
                        help=f"consumption log dir; default {CONSUMED_DIR}")
    parser.add_argument("--store", default=lookup.STORE)
    parser.add_argument("--timeout", type=float, default=10.0)
    parser.add_argument("--limit", type=int, default=lookup.HISTORY_LIMIT)
    parser.add_argument("--now", default=None,
                        help="ISO date or datetime; default now")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    if not Path(args.vault).is_dir():
        print(f"next-focus-answer: REFUSED — vault not found: {args.vault}", file=sys.stderr)
        return 2
    try:
        now = nf.defer_date.as_now(args.now)
    except ValueError:
        print(f"next-focus-answer: REFUSED — bad --now: {args.now}", file=sys.stderr)
        return 2

    try:
        items = lookup.fetch_history(args.store, args.timeout, args.limit)
    except Exception as exc:  # noqa: BLE001 — a store read failure is a refusal
        print(f"next-focus-answer: REFUSED — store unreadable: "
              f"{exc.__class__.__name__}: {exc}", file=sys.stderr)
        return 2

    try:
        consumed, notes = consume_items(
            items, args.vault, args.tasks_dir,
            args.dismissed_dir, args.consumed_dir, now,
        )
    except Refused as exc:
        print(f"next-focus-answer: REFUSED — {exc}", file=sys.stderr)
        return 2

    open_cards = sum(
        1 for item in items
        if str(item.get("dedup_key", "")).startswith(PROPOSAL_PREFIX)
        and item.get("state") not in lookup.ANSWERED_STATES
    )
    if args.json:
        print(json.dumps({"consumed": consumed, "notes": notes,
                          "open": open_cards}, indent=2))
    else:
        print(render(consumed, notes, open_cards))
    return 0


if __name__ == "__main__":
    sys.exit(main())
