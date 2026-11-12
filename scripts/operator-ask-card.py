#!/usr/bin/env python3
"""operator-ask-card — does a session's parked operator ask have a live card?

The defect this answers: a session that ends its turn on a waiting closer (`pick` /
`review:` / `you run:` / `approve:`) is parked until someone answers a card — and 5 of 18
such sessions measured 2026-11-10 had **no card at all**, so nothing could ever answer
them and the operator believed the fleet was handling it.

⚠️ **Resolve by SESSION, never by parsing an id out of the closer.** A `pick` closer
usually names no card id; the id-less form is the common one, not the exception. A checker
that reads the closer's text and calls it NO-CARD when it finds no id would flag every
mirror card as missing — the mirror lives in the store under an id the closer never
mentions. The store item carries `producer_id`, which IS the asking session, so that is
the join.

⚠️ **Three cases, and they are not one rule** (SC1):
  (a) a waiting-verb closer naming no id, with no card for that session → NO-CARD
  (b) a `pick` naming no id whose mirror card IS in the store      → CARD
  (c) a closer naming an id the store does not hold                 → NO-CARD

⚠️ **An unreadable store is UNKNOWN, never NO-CARD.** NO-CARD is an accusation — it says
the operator cannot answer this session. A failed read cannot support it, and folding one
into the other turns a store outage into a fleet-wide false alarm.

Usage:
    operator-ask-card.py --closer "<the 👤 You: line>" --session <sid> [--store URL]
    operator-ask-card.py --closer "..." --session <sid> --fixture items.json

Prints one line, `<verdict>\\t<detail>`. Exit 0 for any verdict; 2 on a usage error.
"""
import argparse
import json
import re
import sys
import urllib.error
import urllib.request

STORE = "http://localhost:18080"
TIMEOUT = 5

NO_CARD = "NO-CARD"
CARD = "CARD"
NOT_AN_ASK = "NOT-AN-ASK"
UNKNOWN = "UNKNOWN"

#: The four verbs whose turn end leaves a session waiting on an operator answer. A
#: `later (on …)` closer is deliberately NOT one: it waits on a named event, not a card.
WAITING_VERB_RE = re.compile(r"^\s*(?:pick|review|you\s+run|approve)\b", re.IGNORECASE)

#: A full 32-hex item id, the only shape the store keys on.
ITEM_ID_RE = re.compile(r"\b[0-9a-f]{32}\b")


def is_waiting_ask(closer):
    """Whether this closer parks the session on an operator answer.

    Only a waiting VERB counts. A `nothing` / routine closer parks on nothing, and a
    `later (on …)` waits on an event — reporting either as NO-CARD would be a false
    accusation against a session that is not waiting on a card at all.
    """
    return bool(WAITING_VERB_RE.match(closer or ""))


def fetch_open(store=STORE, timeout=TIMEOUT):
    """The store's open items. Raises on any failure — the caller turns that into UNKNOWN."""
    with urllib.request.urlopen(f"{store}/api/1.0/attention", timeout=timeout) as resp:
        items = json.load(resp)
    if not isinstance(items, list):
        raise ValueError("store did not return a list")
    return [i for i in items if isinstance(i, dict) and i.get("state") == "open"]


def card_for_session(items, sid):
    """The open item whose `producer_id` is `sid`, or None."""
    if not sid:
        return None
    for item in items:
        if item.get("producer_id") == sid or item.get("session_id") == sid:
            return item
    return None


def verdict(closer, sid, items):
    """`(verdict, detail)` — never raises, so the caller always prints one line."""
    if not is_waiting_ask(closer):
        return NOT_AN_ASK, "the closer does not park the session on an operator answer"
    named = ITEM_ID_RE.findall(closer or "")
    if named:
        # Case (c): the closer names an id. It is the operator's own claim about which card
        # they must answer, so the store is asked about THAT id rather than about the session
        # — a live card for the session does not make a dead cited id answerable.
        held = {i.get("item_id") for i in items}
        missing = [n for n in named if n not in held]
        if missing:
            return NO_CARD, f"closer names {missing[0]}, which the store does not hold"
        return CARD, f"closer names {named[0]}, which is open"
    # Case (b) before case (a): with no id in the closer, the session is the only join.
    item = card_for_session(items, sid)
    if item is not None:
        return CARD, f"open card {item.get('item_id')} for this session"
    return NO_CARD, "no card in the store for this session"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--closer", required=True, help="the closer's 👤 You: line")
    parser.add_argument("--session", required=True, help="the asking session id")
    parser.add_argument("--store", default=STORE, help="attention store base URL")
    parser.add_argument("--fixture", default=None, help="read items from a JSON file instead")
    args = parser.parse_args(argv)

    try:
        if args.fixture:
            items = json.load(open(args.fixture, encoding="utf-8"))
            if not isinstance(items, list):
                raise ValueError("fixture is not a list")
        else:
            items = fetch_open(args.store)
    except Exception as exc:  # an unreadable store is UNKNOWN, never NO-CARD
        print(f"{UNKNOWN}\tstore unreadable ({exc})")
        return 0

    v, detail = verdict(args.closer, args.session, items)
    print(f"{v}\t{detail}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
