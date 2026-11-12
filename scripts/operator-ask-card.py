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
into the other turns a store outage into a fleet-wide false alarm. **That covers a
malformed body, not only an unreachable one**: a list of non-objects, or an open item with
no `item_id`, is a shape the code cannot read, and reading it as "no cards" would accuse on
garbage.

⚠️ **`--closer` takes the whole `👤 You:` line, so the verb is NOT at position 0.** Panes
render `👤 You: pick — …`, sometimes with markdown emphasis or `&nbsp;` padding, and
`manager-attention-watch.py` carries a `normalize_closer()` for exactly that. Anchoring the
verb at the raw string's start classified the documented input shape as NOT-AN-ASK — the
loophole that would hide every `pick` parked session. `normalize_closer()` strips the
prefix, the entities and the leading marker characters first.

⚠️ **`NOT-AN-ASK` is deliberately NARROWER than `docs/closer-classes.md`'s `ask` class.**
That class also admits a plain status line (`done — all green`, `waiting for CI`). Those
are real asks for the fleet's purposes, but neither waits on a **card**: a `done` session
is not parked, and `waiting for CI` waits on a machine. Reporting either as NO-CARD would
accuse a session that is not waiting on an operator answer at all.

Usage:
    operator-ask-card.py --closer "👤 You: pick — 1. X · 2. Y" --session <sid>
    operator-ask-card.py --closer "..." --session <sid> --fixture items.json

Prints one line, `<verdict>\\t<detail>`. Exit 0 for any verdict; 2 on a usage error.
"""
import argparse
import html
import json
import os
import re
import sys
import urllib.request

STORE = os.environ.get("ATTENTION_STORE_URL") or "http://localhost:18080"
try:
    TIMEOUT = float(os.environ.get("ATTENTION_STORE_TIMEOUT") or 5)
except ValueError:
    TIMEOUT = 5.0

NO_CARD = "NO-CARD"
CARD = "CARD"
NOT_AN_ASK = "NOT-AN-ASK"
UNKNOWN = "UNKNOWN"

#: The four verbs whose turn end leaves a session waiting on an operator answer. A
#: `later (on …)` closer is deliberately NOT one: it waits on a named event, not a card.
WAITING_VERB_RE = re.compile(r"^(?:pick|review|you\s+run|approve)\b", re.IGNORECASE)

#: A full 32-hex item id — the only shape the store keys on. Case-insensitive because a
#: closer may uppercase it; store ids are lowercase, so the lookup lowercases too.
ITEM_ID_RE = re.compile(r"\b[0-9a-f]{32}\b", re.IGNORECASE)

#: `👤 You:` and the markdown/entity noise panes wrap it in. The leading-noise class is
#: "any non-word character" rather than a list of markers: panes emit emoji (`⚠️`),
#: emphasis (`**`), dashes and non-breaking space, and an enumerated list misses the next
#: one — the failure mode is silent, because the closer then reads NOT-AN-ASK.
_CLOSER_PREFIX_RE = re.compile(r"^\s*(?:\*\*)?\s*👤\s*You:\s*(?:\*\*)?", re.IGNORECASE)
_LEADING_NOISE_RE = re.compile(r"^[^\w]+")


def normalize_closer(text):
    """The closer body with its `👤 You:` prefix, entities and leading markers removed.

    ⚠️ Load-bearing, not cosmetic: the verb is anchored at the body's start, and the
    documented input is the whole panel line. `manager-attention-watch.py` carries the same
    normalisation for the same reason — panes have emitted `👤 You: &nbsp;&nbsp;nothing`.
    """
    s = html.unescape(text or "")
    s = _CLOSER_PREFIX_RE.sub("", s)
    return _LEADING_NOISE_RE.sub("", s)


def is_waiting_ask(closer):
    """Whether this closer parks the session on an operator answer."""
    return bool(WAITING_VERB_RE.match(normalize_closer(closer)))


def _require_readable(items):
    """Raise unless every element is an object — a shape the code cannot read is UNKNOWN.

    ⚠️ Silently dropping a non-object turns a malformed body into "no cards", i.e. into an
    accusation. The safe direction is to refuse to answer.
    """
    if not isinstance(items, list):
        raise ValueError("store did not return a list")
    for i, item in enumerate(items):
        if not isinstance(item, dict):
            raise ValueError(f"item {i} is not an object")


def fetch_open(store=STORE, timeout=None):
    """The store's open items. Raises on any failure — the caller turns that into UNKNOWN."""
    url = f"{(store or STORE).rstrip('/')}/api/1.0/attention"
    with urllib.request.urlopen(url, timeout=TIMEOUT if timeout is None else timeout) as resp:
        items = json.load(resp)
    _require_readable(items)
    open_items = [i for i in items if i.get("state") == "open"]
    for item in open_items:
        if not item.get("item_id"):
            raise ValueError("an open item carries no item_id")
    return open_items


def load_fixture(path):
    """A fixture, filtered exactly as the live path filters — never more permissively.

    ⚠️ The same `state == "open"` filter and the same shape check as `fetch_open`. A
    fixture that certifies behaviour the store path cannot produce is worse than no
    fixture: it passes a test the live read would fail.
    """
    with open(path, encoding="utf-8") as f:
        items = json.load(f)
    _require_readable(items)
    open_items = [i for i in items if i.get("state") == "open"]
    for item in open_items:
        if not item.get("item_id"):
            raise ValueError("an open item carries no item_id")
    return open_items


def card_for_session(items, sid):
    """The open item whose `producer_id` is `sid`, or None.

    ⚠️ `producer_id` ONLY. The store emits that field (`server/open-cards.mjs` joins on it,
    `notify-gate.py` calls it "the only one available"), and `who-needs-me.py` maps it to its
    own `session_id` key. A store item that ever carried a literal `session_id` would more
    plausibly mean the SPAWNER (`server/park-refusal.mjs` distinguishes the two), so falling
    back to it could attribute a manager's card to a worker's session.
    """
    if not sid:
        return None
    for item in items:
        if item.get("producer_id") == sid:
            return item
    return None


def verdict(closer, sid, items):
    """`(verdict, detail)` — never raises for well-formed `items`, so one line always prints."""
    if not sid:
        # No session means no join at all, so nothing supports an accusation.
        return UNKNOWN, "no session id to join on"
    if not is_waiting_ask(closer):
        return NOT_AN_ASK, "the closer does not park the session on an operator answer"
    named = ITEM_ID_RE.findall(closer or "")
    if named:
        # Case (c): the closer names an id. It is the operator's own claim about which card
        # they must answer, so the store is asked about THAT id rather than about the session
        # — a live card for the session does not make a dead cited id answerable.
        held = {str(i.get("item_id")).lower() for i in items}
        missing = [n for n in named if n.lower() not in held]
        if missing:
            return NO_CARD, f"closer names {missing[0].lower()}, which the store does not hold open"
        return CARD, f"closer names {named[0].lower()}, which is open"
    # Case (b) before case (a): with no id in the closer, the session is the only join.
    item = card_for_session(items, sid)
    if item is not None:
        return CARD, f"open card {item.get('item_id')} for this session"
    return NO_CARD, "no open card in the store for this session"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--closer", required=True, help="the closer's whole 👤 You: line")
    parser.add_argument("--session", required=True, help="the asking session id")
    parser.add_argument("--store", default=STORE, help="attention store base URL")
    parser.add_argument("--fixture", default=None, help="read items from a JSON file instead")
    args = parser.parse_args(argv)

    try:
        items = load_fixture(args.fixture) if args.fixture else fetch_open(args.store)
    except Exception as exc:  # an unreadable store is UNKNOWN, never NO-CARD
        print(f"{UNKNOWN}\tstore unreadable ({exc})")
        return 0

    v, detail = verdict(args.closer, args.session, items)
    print(f"{v}\t{detail}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
