#!/usr/bin/env python3
"""Resolve a dedup key to its latest-answered attention card.

The gap this closes: the drive leg's hold line must name the card the operator
already answered for a held row — `agents/manager-drive.md` says "quote the
existing `item-id` instead" — but a dedup key can carry SEVERAL cards. The
store's suppression is open-scoped, so once an item is answered the same key
writes a NEW row rather than returning the old one. Quoting an arbitrary member
of that set reports a superseded answer, and it reports it in the direction that
suppresses a raise: a reader concludes the operator declined a row they in fact
raised.

⚠️ There is no lookup-by-dedup-key anywhere else, and this is why each candidate
is wrong rather than merely inconvenient:

  - `GET /api/1.0/attention` returns **open** items only, so an answered card is
    absent from it by construction.
  - `attention-ask.py` exposes only `post` / `post-batch` / `poll <item-id>`, and
    `poll` refuses anything that is not a 32-hex item id — a key is not pollable.
  - `waiting-approval-state.py` and `under-target-state.py` each carry a single
    `card_item_id` for THEIR OWN card, one per topic. Neither is the below-bar
    per-task card, which has no state file at all.

So the only read that can see a key's answered cards is
`GET /api/1.0/attention/history`, and this script is its filter.

⚠️ The history read is PAGED, and the default page is a trap: called without
`limit` the store returns the **1000** most recent items (measured 2026-10-05 —
`?limit=50000` returned 24,032 against a default-page 1,000). A key whose cards
all predate that window then reads as `NO_CARDS`, which is the false negative
that makes a caller re-post the very card this lookup exists to find. So the
limit is raised deliberately and can be tuned with `--limit` /
`$ATTENTION_HISTORY_LIMIT`.

⚠️ Ordering is by `answered_at`, and an undecidable ordering is REFUSED rather
than guessed. Two cards sharing the maximum timestamp — or a prefix that matches
more than one full key — make "latest-answered" undecidable, and quoting either
member would report an answer the operator may never have given. The caller's
contract is to report the key as ambiguous in that case, never to pick.

Usage:
    attention-card-lookup.py latest --dedup-key K
    attention-card-lookup.py list   --dedup-key K

Exit codes: 0 ok · 3 no cards for the key · 2 refused (ambiguous or bad input).
"""
import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone

STORE = os.environ.get("ATTENTION_STORE_URL", "http://localhost:18080").rstrip("/")
STORE_TIMEOUT = float(os.environ.get("ATTENTION_STORE_TIMEOUT", "10"))

# ⚠️ The default page is 1000 items and a key's cards routinely fall outside it
# (measured 2026-10-05: a default read showed 2 cards for a key that carries 9).
# Raised here because the failure mode of a short window is a false `NO_CARDS`,
# which reads as "nothing to quote" and invites the caller to re-post.
HISTORY_LIMIT = int(os.environ.get("ATTENTION_HISTORY_LIMIT", "50000"))

# ⚠️ A prefix shorter than this is not evidence of anything. The open-items
# ledger displays 8-char ids and the runbook's own trap is explicit: "A short id
# is never evidence the card is absent". The same bound applies here in the
# other direction — a short prefix would match an unrelated key and return its
# card as though it were this one's.
MIN_PREFIX = 8

# The states in which a card has been answered by the operator. `closed` is the
# store's terminal state and `answered` the one that precedes it; both carry an
# `answered_at`. Read as a set rather than a single value because the store
# moves an item through them and the history read can catch either.
ANSWERED_STATES = {"answered", "closed"}


def refuse(msg, code=2):
    print(f"REFUSED: {msg}")
    return code


def items_of(payload):
    """The item list from a store read, tolerating a bare list or a wrapper."""
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        items = payload.get("items")
        if isinstance(items, list):
            return items
    return []


def fetch_history(store, timeout, limit=HISTORY_LIMIT):
    with urllib.request.urlopen(
        f"{store}/api/1.0/attention/history?limit={limit}", timeout=timeout
    ) as resp:
        return items_of(json.loads(resp.read().decode("utf-8")))


def parse_ts(value):
    """The store's RFC3339 stamps, as aware datetimes. None when unparseable."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def answered_at(item):
    """When the operator answered this card, or None if it never was.

    ⚠️ Deliberately reads the field rather than inferring from `state`: a card
    can sit in `closed` with no answer recorded (observed 2026-10-05, item
    `ee9bd781`), and treating that as an answer would let it outrank a real one.
    """
    return parse_ts(item.get("answered_at"))


def cards_for_key(items, want):
    """(cards, err) — the items under `want`, or a refusal naming the ambiguity.

    Exact key match, or a prefix match of at least MIN_PREFIX characters. A
    prefix resolving to more than one distinct full key is refused rather than
    unioned: two keys' cards cannot be ordered against each other, so the
    "latest-answered" answer would be meaningless even though it would look
    concrete.
    """
    matched_keys = {
        str(it.get("dedup_key", ""))
        for it in items
        if str(it.get("dedup_key", "")) == want
        or (
            len(want) >= MIN_PREFIX
            and str(it.get("dedup_key", "")).startswith(want)
        )
    }
    matched_keys.discard("")
    if not matched_keys:
        return [], None
    if len(matched_keys) > 1:
        return [], (
            f"{want!r} is a prefix of {len(matched_keys)} distinct dedup keys "
            f"({', '.join(sorted(matched_keys))}) — their cards cannot be "
            "ordered against each other. Pass a full key."
        )
    key = next(iter(matched_keys))
    return [it for it in items if str(it.get("dedup_key", "")) == key], None


def select_latest(cards):
    """(card, err) — the max-`answered_at` answered card, or a refusal.

    Only answered cards compete: an open card has no answer to supersede
    anything, so including it would let an unanswered row outrank the answer the
    operator actually gave.
    """
    answered = [
        (it, answered_at(it))
        for it in cards
        if it.get("state") in ANSWERED_STATES and answered_at(it) is not None
    ]
    if not answered:
        return None, None
    top = max(ts for _, ts in answered)
    leaders = [it for it, ts in answered if ts == top]
    if len(leaders) > 1:
        ids = ", ".join(sorted(str(it.get("item_id", ""))[:8] for it in leaders))
        return None, (
            f"{len(leaders)} cards share the maximum answered_at "
            f"({top.isoformat()}) — {ids}. The latest-answered card is "
            "undecidable, so no card id is quoted."
        )
    return leaders[0], None


def cmd_latest(args, items, out=sys.stdout):
    cards, err = cards_for_key(items, args.dedup_key)
    if err:
        print(f"REFUSED: {err}", file=out)
        return 2
    if not cards:
        print(f"NO_CARDS: no card carries dedup key {args.dedup_key!r}", file=out)
        return 3
    card, err = select_latest(cards)
    if err:
        print(f"REFUSED: {err}", file=out)
        return 2
    if card is None:
        print(
            f"NO_ANSWERED: {len(cards)} card(s) under {args.dedup_key!r}, "
            "none answered",
            file=out,
        )
        return 3
    answer = (card.get("answer") or {}).get("value", "")
    print(f"ITEM_ID: {card.get('item_id', '')}", file=out)
    print(f"ANSWERED_AT: {card.get('answered_at', '')}", file=out)
    print(f"ANSWER: {answer}", file=out)
    print(f"CARDS: {len(cards)}", file=out)
    return 0


def cmd_list(args, items, out=sys.stdout):
    cards, err = cards_for_key(items, args.dedup_key)
    if err:
        print(f"REFUSED: {err}", file=out)
        return 2
    if not cards:
        print(f"NO_CARDS: no card carries dedup key {args.dedup_key!r}", file=out)
        return 3
    ordered = sorted(
        cards,
        key=lambda it: (answered_at(it) or datetime.min.replace(tzinfo=timezone.utc)),
    )
    for it in ordered:
        ts = answered_at(it)
        answer = (it.get("answer") or {}).get("value", "")
        print(
            f"{str(it.get('item_id', ''))[:8]}  "
            f"state={it.get('state', ''):9s} "
            f"answered={ts.isoformat() if ts else '-':25s} "
            f"answer={answer}",
            file=out,
        )
    print(f"CARDS: {len(cards)}", file=out)
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Resolve a dedup key to its latest-answered attention card"
    )
    sub = parser.add_subparsers(dest="cmd", required=True)
    for name, helptext in (
        ("latest", "print the key's latest-answered card id"),
        ("list", "print every card under the key, oldest answer first"),
    ):
        p = sub.add_parser(name, help=helptext)
        p.add_argument("--dedup-key", required=True)
        p.add_argument("--store", default=STORE)
        p.add_argument("--limit", type=int, default=HISTORY_LIMIT)

    args = parser.parse_args(argv)
    if not args.dedup_key.strip():
        return refuse("--dedup-key is empty")
    try:
        items = fetch_history(args.store, STORE_TIMEOUT, args.limit)
    except (urllib.error.URLError, OSError) as err:
        # ⚠️ Reported as its own failure, never as "no cards". A store that is
        # down must not read as a key with nothing under it — that is the
        # distinction `attention-ask.py poll` was fixed to preserve, and it
        # matters more here because the caller's next act is to quote a card.
        print(f"FAILED: attention store unreachable at {args.store} ({err})")
        return 1
    except json.JSONDecodeError as err:
        print(f"FAILED: attention store returned unparseable history ({err})")
        return 1
    return (cmd_latest if args.cmd == "latest" else cmd_list)(args, items)


if __name__ == "__main__":
    sys.exit(main())
