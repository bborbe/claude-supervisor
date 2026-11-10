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
              append-only, written here, read by the round's render. A
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
import shlex
import sys
import urllib.parse
import urllib.request
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


def _page_path(vault, tasks_dir, name):
    """The board page for `name`, or None — the SAME predicate the reader resolves with.

    Delegates to `Board.find` rather than re-implementing it: the reader
    (`plan_candidates` → `Board.read` → `Board.find`) searches task/goal/objective/topic
    and carries a containment guard, so a local copy diverges both ways — a name this
    check accepts and the reader rejects is exactly the inert-block defect again.
    """
    kind, path = nf.Board(vault, tasks_dir).find(name)
    return path if kind is not None else None


def _task_state(vault, tasks_dir, name):
    """`(status, phase)` for the row, or empty strings when no page carries it.

    Both board dirs are tried: a proposal row is a task OR a blocked goal, and a
    tasks-only read recorded a goal's `hold` as nothing at all.
    """
    page = _page_path(vault, tasks_dir, name)
    if page is None:
        return "", ""
    text, err = nf.read_text(page)
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


def _picked_table(lines):
    """`(heading_index, row_indices)` for the `## ✅ Picked` table, or `(None, [])`.

    ⚠️ The row number and the heading count come from THIS table's own rows — never
    from the `### N.` block number. A weekly note routinely carries far more blocks
    than Picked rows (W44: 72 blocks, 42 rows), so reusing the block number appends
    `| 73 |` after row 42 and rewrites the heading to `(73)`.
    """
    heading = next((i for i, line in enumerate(lines) if PICKED_RE.match(line)), None)
    if heading is None:
        return None, []
    end = len(lines)
    for i in range(heading + 1, len(lines)):
        if HEADING_RE.match(lines[i]):
            end = i
            break
    return heading, [i for i in range(heading + 1, end) if ROW_RE.match(lines[i])]


def _insert_picked_row(lines, name, why):
    """`(lines, number)` — append a row, numbered from the table itself."""
    heading, rows = _picked_table(lines)
    if heading is None:
        return lines, None
    if not rows:
        # A heading with no data rows yet. Real notes put a BLANK line between the
        # heading and the table header (verified on the live 2026-W44 shape), so blanks
        # are skipped FIRST: skipping only `|` lines from `heading + 1` exits on that
        # blank and inserts the row ABOVE the header, outside the table.
        at = heading + 1
        while at < len(lines) and not lines[at].strip():
            at += 1
        start = at
        while at < len(lines) and lines[at].lstrip().startswith("|"):
            at += 1
        if at == start:
            # The heading has no table under it — refuse rather than writing a row line
            # into whatever prose follows the heading.
            return lines, None
        number = int(PICKED_RE.match(lines[heading]).group(1)) + 1
        return lines[:at] + [f"| {number} | 📌 {name} | {why} |"] + lines[at:], number
    number = max(int(ROW_RE.match(lines[i]).group(1)) for i in rows) + 1
    row = f"| {number} | 📌 {name} | {why} |"
    at = rows[-1] + 1
    return lines[:at] + [row] + lines[at:], number


def _picked_item_names(lines):
    """The item cell of every `## ✅ Picked` row, leading emoji stripped.

    Scoped to the table and compared whole: a substring scan over every numbered row
    in the note reports "already present" when one task's name is a prefix of another's.
    """
    _, rows = _picked_table(lines)
    names = []
    for i in rows:
        cells = lines[i].split("|")
        if len(cells) < 3:
            continue
        names.append(re.sub(r"^\S+\s+", "", cells[2].strip()).strip())
    return names


def _bump_picked_count(lines, number):
    heading, _ = _picked_table(lines)
    if heading is None:
        return lines
    lines[heading] = f"## ✅ Picked ({number})"
    return lines


def add_to_plan(vault, tasks_dir, name, day, why_text=""):
    """`(detail, notes)` — write both weekly-note artifacts, idempotent by task name.

    Idempotency is by NAME, not by card key: the same task can be proposed under a
    re-worded card, and a second `add it to the plan` must not add a second block.
    """
    weekly = nf.plan_path(vault, "weekly", nf.iso_week(day))
    text, err = nf.read_text(weekly)
    if err:
        raise Refused(f"weekly note unreadable: {weekly} ({err})")

    # A block whose `**Task:** [[name]]` resolves to no page is INERT: `plan_candidates`
    # drops it with `no page in tasks/goals/topics`, the row never enters `planned_keys`,
    # and it re-proposes — while the note carries a block that reads as planned. Measured
    # 2026-11-10: 33 of 110 live blocks named no page and every one of those rows was
    # still proposed. So a name that resolves to nothing is never written.
    if _page_path(vault, tasks_dir, name) is None:
        raise Refused(f"no page for {name!r} on the task/goal/objective/topic board — "
                      f"refusing to write a plan row that can never resolve")

    lines = text.splitlines()
    already_block = any(name in row["names"] for row in nf.parse_weekly(text))
    already_row = name in _picked_item_names(lines)
    if already_block and already_row:
        return {"weekly": str(weekly), "block": None, "row": None}, ["already present"]

    notes = []
    block = None
    if not already_block:
        block = _block_number(lines)
        lines = _insert_block(lines, block, name)
    else:
        notes.append("block already present")

    status, phase = _task_state(vault, tasks_dir, name)
    # The card's own `why` — segment 2 of `<name> — <why> — <file>:<line>` — so a
    # firing-alert or blocked-goal row is not recorded as if it were a P1 task.
    why = (f"Added {day.isoformat()} on the operator's answer — "
           f"{why_text or 'in no current plan'}.")
    if status or phase:
        # Joined over the non-empty parts: a `status: backlog` row with no `phase:`
        # (the vault's normal shape) otherwise wrote a malformed `` `backlog` / ``.
        why += " `" + " / ".join(p for p in (status, phase) if p) + "`."

    row = None
    if not already_row:
        lines, row = _insert_picked_row(lines, name, why)
        if row is None:
            notes.append("no `## ✅ Picked` table — row not written")
        else:
            lines = _bump_picked_count(lines, row)
    else:
        notes.append("picked row already present")

    _write_atomic(weekly, "\n".join(lines) + "\n")
    return {"weekly": str(weekly), "block": block, "row": row}, notes


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


def fetch_history(store, timeout, limit):
    """Proposal cards only — the store narrows server-side on `dedup_key_prefix`.

    The unfiltered history read is ~19k items / ~16 MB and was measured at 41 % of the
    store's allocation (`manager-attention-watch.py` added the same prefix for the same
    reason). This caller runs on every fleet tick, so it narrows to its own key space
    instead of pulling the whole board each time.
    """
    query = ("?limit=%d&dedup_key_prefix=%s"
             % (limit, urllib.parse.quote(PROPOSAL_PREFIX, safe="")))
    with urllib.request.urlopen(
            f"{store}/api/1.0/attention/history{query}", timeout=timeout) as resp:
        return lookup.items_of(json.loads(resp.read().decode("utf-8")))


def unquote_payload(payload):
    """The raw payload text, un-shell-quoted when the store kept the quoted form.

    `next-focus.py` shell-quotes the payload (`shlex.quote`) so the round can paste it
    bare into a command line, and the store sometimes keeps that quoted text. Reading
    it raw left the leading `'` and the `'"'"'` escape in the row name — 33 of 110
    live blocks named no page and the rows kept re-proposing.

    A quoted payload is ONE `shlex` token; an unquoted one splits into many (or raises
    on an apostrophe). Only the single-token case is unquoted — anything else is
    returned verbatim rather than guessed at.
    """
    text = str(payload or "")
    try:
        parts = shlex.split(text)
    except ValueError:
        return text
    return parts[0] if len(parts) == 1 else text


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
        # Unquote ONCE and derive both fields from it: reading `why_text` off the raw
        # payload left a trailing `'` in the row for any quoted multi-segment payload.
        payload = unquote_payload(card.get("payload", ""))
        segments = payload.split(" — ")
        name = segments[0].strip()
        why_text = segments[1] if len(segments) > 1 else ""
        kind = kind_of_key(card_key)
        if not name:
            notes.append(f"{card_key}: card carries no payload name — not consumed")
            continue

        if answer == ADD:
            if kind == "alert":
                # An alert's name is a raw `prometheus-alerts.sh` line, so it resolves to
                # no page: a `### N.` block for it is inert noise in the operator's plan,
                # and `plan_candidates` drops it anyway. The durable record is the only
                # load-bearing artifact, so an `add` on an alert writes that and nothing
                # else rather than littering the weekly note.
                write_dismissal(dismissed_dir, card_key, name, kind, item_id, now)
                notes.append(
                    "alert row: no plan block (it can never resolve to a page) — "
                    "recorded as dismissed so it stops re-proposing"
                )
                detail = {"weekly": None, "block": None, "row": None}
            else:
                try:
                    detail, extra = add_to_plan(
                        vault, tasks_dir, name, now.date(), why_text)
                except Refused as exc:
                    # One unaddable card must not starve the rest of the tick: a
                    # `leave it out` answer needs nothing from the weekly note, and a
                    # fresh-week note legitimately carries no `### N.` blocks yet.
                    notes.append(f"{card_key}: refused — {exc}")
                    # RECORD the refusal before moving on. Without it `pending` never
                    # drains: an unresolvable name is a permanent property, so the same
                    # card re-rendered the same note every tick forever (measured on
                    # three consecutive live ticks, `pending=1` each time). No dismissal
                    # record — that would suppress the row if its page is created later.
                    append_consumed(log_path, {
                        "at": now.isoformat(), "card_key": card_key,
                        "item_id": item_id, "name": name, "kind": kind,
                        "action": "refused", "reason": str(exc),
                    })
                    done.add((card_key, item_id))
                    continue
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
        done.add((card_key, item_id))

    # Answered cards still awaiting consumption — the count the empty render reports.
    # Computed from the same `done` set the loop used, so a note-only run cannot report
    # a clean zero while an answer sits unconsumed.
    pending = 0
    for card_key, cards in by_key.items():
        card, err = lookup.select_latest(cards)
        if err or card is None:
            continue
        if (card_key, str(card.get("item_id", ""))) not in done:
            pending += 1

    return consumed, notes, pending


def render(consumed, notes, open_cards, pending):
    if not consumed:
        lines = [
            f"next-focus consume: nothing to consume "
            f"({pending} answered card(s) awaiting; {open_cards} open)"
        ]
    else:
        lines = []
        for entry in consumed:
            card = entry["item_id"][:8]
            if entry["action"] == "added":
                block, row = entry.get("block"), entry.get("row")
                # Guard on BOTH: the live 2026-W46 shape has `### N.` blocks and no
                # Picked table, so `block` alone printed "picked row None" to the operator.
                if block and row:
                    where = f"block {block}, picked row {row}"
                elif block:
                    where = f"block {block}"
                elif row:
                    where = f"picked row {row}"
                else:
                    where = "recorded"
                lines.append(
                    f'next-focus consume: added "{entry["name"]}" ({where}) — card {card}'
                )
            else:
                lines.append(f'next-focus consume: dismissed "{entry["name"]}" — card {card}')
    # ⚠️ The notes print on BOTH branches. Dropping them on the empty branch swallowed
    # exactly the failures this script exists to surface — an unhandled answer, a card
    # carrying no payload name, an unreadable log line, an undecidable "latest answer" —
    # and left "nothing to consume" reading as a clean tick.
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
    # The flag is a real gate, not a label: every invocation writes the operator's weekly
    # note, so a caller that omitted `--consume` is asking for an inspection and must not
    # mutate anything.
    if not args.consume:
        print("next-focus-answer: REFUSED — pass --consume to apply answers "
              "(nothing was written)", file=sys.stderr)
        return 2
    try:
        now = nf.defer_date.as_now(args.now)
    except ValueError:
        print(f"next-focus-answer: REFUSED — bad --now: {args.now}", file=sys.stderr)
        return 2

    try:
        items = fetch_history(args.store, args.timeout, args.limit)
    except Exception as exc:  # noqa: BLE001 — a store read failure is a refusal
        print(f"next-focus-answer: REFUSED — store unreadable: "
              f"{exc.__class__.__name__}: {exc}", file=sys.stderr)
        return 2

    try:
        consumed, notes, pending = consume_items(
            items, args.vault, args.tasks_dir,
            args.dismissed_dir, args.consumed_dir, now,
        )
    except Refused as exc:
        print(f"next-focus-answer: REFUSED — {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001 — a write failure is a refusal too
        # The round renders "a refused consume (exit 2)"; an escaped traceback exits 1,
        # a code its caller has no render for. Same mapping `next-focus.py`'s `main` uses.
        print(f"next-focus-answer: REFUSED — "
              f"{exc.__class__.__name__}: {exc}", file=sys.stderr)
        return 2

    open_cards = sum(
        1 for item in items
        if str(item.get("dedup_key", "")).startswith(PROPOSAL_PREFIX)
        and item.get("state") not in lookup.ANSWERED_STATES
    )
    if args.json:
        print(json.dumps({"consumed": consumed, "notes": notes,
                          "open": open_cards, "pending": pending}, indent=2))
    else:
        print(render(consumed, notes, open_cards, pending))
    return 0


if __name__ == "__main__":
    sys.exit(main())
