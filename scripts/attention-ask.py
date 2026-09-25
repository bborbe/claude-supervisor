#!/usr/bin/env python3
"""Ask the operator a question on the attention stack, and read the answer back.

This is the asking half of the board-answers pair; `attention-answer.py` is the
answering half. A manager uses this one to post a question and end its turn,
instead of blocking its whole turn on AskUserQuestion.

  post   POST /api/1.0/attention, then print the item id it created
  poll   GET /api/1.0/attention/{ITEM_ID}, print the answer, or OPEN

**Why a manager polls rather than being sent to.** The asking session is the
item's `producer_id`, so it can read its own item back; no cross-session
SendMessage is involved. A browser button cannot send one, and neither can a
cron tick, so polling is the honest mechanism rather than a degraded one. The
cost is latency, and it is worth naming: the answer arrives on the manager's
next loop tick (~5 min for manager-loop, ~15 for fleet-loop), not instantly.

**Posting does not block.** The command returns as soon as the store has the
item. Nothing here waits for the operator.

Every field the producer owns is a declaration, so they are all flags: this
script never infers a dedup key, an interrupt class, or a liveness model from
the prose. The two that would be silently wrong if guessed are defaulted from
the environment instead, and both defaults are visible in `--help`:

  --producer-id    defaults to $CLAUDE_CODE_SESSION_ID
  --liveness-ref   defaults to session:<producer-id>

A spawned child is stripped of CLAUDE_CODE_SESSION_ID, so a missing producer id
is a real case rather than a bug, and it is refused rather than posted: an item
with no producer can never be polled back by anyone, so it would be a question
asked into a void.

Run: python3 attention-ask.py post --dedup-key KEY --payload "..." [--option L]...
     python3 attention-ask.py poll ITEM_ID
"""

import argparse
import json
import os
import sys
import urllib.error
import urllib.request

STORE = os.environ.get("ATTENTION_STORE_URL", "http://localhost:18080").rstrip("/")
# Local store; a hung one must cost a clear failure, never a stalled loop tick.
STORE_TIMEOUT = float(os.environ.get("ATTENTION_STORE_TIMEOUT", "3"))

# The answer mechanisms this script posts. It asks questions, so `message` is
# the only one it offers: a `permission` item is approve-shaped and only the
# operator may answer it, in the session that raised it, and an `ack` item asks
# nothing. Offering either here would let a manager post a gate it cannot
# legally answer.
MECHANISM = "message"


def resolved_session_id(environ=None):
    """The session id of whoever is running this arm, or "".

    Claude Code exports CLAUDE_CODE_SESSION_ID into any Bash a session runs, so
    the asker is resolvable here without the caller passing a flag. "" is a real
    case (a spawned child is deliberately stripped of it) and is refused by the
    caller rather than papered over.
    """
    return (os.environ if environ is None else environ).get("CLAUDE_CODE_SESSION_ID", "")


def build_options(labels, recommended):
    """Return the `options` list, or raise ValueError naming the bad input.

    The store enforces the same two rules (a label is required; at most one is
    recommended), but checking here costs no round trip and lets the error name
    the fix rather than quoting a store payload.
    """
    options = []
    for label in labels:
        if not label.strip():
            raise ValueError("--option must not be empty")
        options.append({"label": label, "recommended": label == recommended})
    if recommended:
        if recommended not in labels:
            raise ValueError(f"--recommend {recommended!r} is not one of the --option labels")
    return options


def post_question(
    producer_id,
    producer_kind,
    liveness_ref,
    dedup_key,
    interrupt_class,
    payload,
    context,
    options,
    expires_at,
):
    body = {
        "producer_id": producer_id,
        "producer_kind": producer_kind,
        "liveness_ref": liveness_ref,
        "dedup_key": dedup_key,
        "interrupt_class": interrupt_class,
        "payload": payload,
        "answer_mechanism": MECHANISM,
    }
    # Omitted rather than sent empty. The schema treats an absent `context` and
    # an absent `expires_at` as pre-change/optional values, while "" would be a
    # *present* field holding nothing — the same distinction attention-answer.py
    # draws for resolved_by and decision.
    if context:
        body["context"] = context
    if options:
        body["options"] = options
    if expires_at:
        body["expires_at"] = expires_at
    req = urllib.request.Request(
        f"{STORE}/api/1.0/attention",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=STORE_TIMEOUT) as resp:
        return json.load(resp)


def fetch_item(item_id):
    with urllib.request.urlopen(f"{STORE}/api/1.0/attention/{item_id}", timeout=STORE_TIMEOUT) as resp:
        return json.load(resp)


def describe_answer(item):
    """Render the stored answer, or None when the item is still open.

    The shape is the schema's `answer`: `{kind: option|skip|text, value}`. A
    `skip` carries no value, so it is rendered as the bare word rather than as
    an empty one — printing `skip ` with nothing after it reads as a truncated
    option answer.
    """
    answer = item.get("answer") or {}
    kind = answer.get("kind")
    if not kind:
        return None
    value = answer.get("value") or ""
    return f"{kind}: {value}" if value else kind


def cmd_post(args, out=sys.stdout):
    producer_id = args.producer_id or resolved_session_id()
    if not producer_id:
        print(
            "REFUSED: no --producer-id and CLAUDE_CODE_SESSION_ID is unset, so this item "
            "would have no producer to poll it back. Pass --producer-id explicitly.",
            file=out,
        )
        return 2
    liveness_ref = args.liveness_ref or f"session:{producer_id}"
    try:
        options = build_options(args.option, args.recommend)
    except ValueError as err:
        print(f"REFUSED: {err}", file=out)
        return 2
    try:
        item = post_question(
            producer_id=producer_id,
            producer_kind=args.producer_kind,
            liveness_ref=liveness_ref,
            dedup_key=args.dedup_key,
            interrupt_class=args.interrupt_class,
            payload=args.payload,
            context=args.context,
            options=options,
            expires_at=args.expires_at,
        )
    except urllib.error.HTTPError as err:
        detail = err.read().decode(errors="replace")
        print(f"FAILED: store returned {err.code} for the push -- {detail}", file=out)
        return 1
    print(f"ITEM_ID: {item.get('item_id')}", file=out)
    print(f"POLL: python3 attention-ask.py poll {item.get('item_id')}", file=out)
    return 0


def cmd_poll(item_id, out=sys.stdout):
    try:
        item = fetch_item(item_id)
    except urllib.error.HTTPError as err:
        # Handled here rather than in main so the message can name the item and
        # carry the store's explanation. A poll runs unattended on a loop tick,
        # so "FAILED: store returned 500" naming nothing is not something a
        # manager can act on. The 404 is the ordinary case: the item was
        # answered, closed or pruned between ticks.
        if err.code == 404:
            print(f"FAILED: no such item {item_id}", file=out)
        else:
            detail = err.read().decode(errors="replace")
            print(f"FAILED: store returned {err.code} for {item_id} -- {detail}", file=out)
        return 1
    described = describe_answer(item)
    if described is None:
        print("OPEN", file=out)
        return 0
    print(f"ANSWERED: {described}", file=out)
    answered_by = item.get("answered_by") or ""
    if answered_by:
        print(f"ANSWERED_BY: {answered_by}", file=out)
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="cmd", required=True)

    post = sub.add_parser("post")
    post.add_argument("--dedup-key", required=True)
    post.add_argument("--payload", required=True)
    post.add_argument("--context", default="")
    post.add_argument("--option", action="append", default=[])
    post.add_argument("--recommend", default="")
    post.add_argument("--producer-id", default="")
    post.add_argument("--producer-kind", default="session")
    post.add_argument("--liveness-ref", default="")
    post.add_argument("--interrupt-class", default="pick")
    post.add_argument("--expires-at", default="")

    poll = sub.add_parser("poll")
    poll.add_argument("item_id")

    args = parser.parse_args(argv)
    try:
        if args.cmd == "post":
            return cmd_post(args)
        return cmd_poll(args.item_id)
    except urllib.error.HTTPError as err:
        # Each command handles its own HTTP errors, so reaching here means the
        # failure predates the item lookup — which is why no item is named: this
        # branch cannot know one. It previously read `args.item_id`, which only
        # `poll` carries; that was unreachable rather than live, because
        # `cmd_post` catches its own HTTPError first. It was one refactor away
        # from an AttributeError, not a defect in the shipped path.
        print(f"FAILED: store returned {err.code}")
        return 1
    except (urllib.error.URLError, OSError) as err:
        print(f"FAILED: attention store unreachable at {STORE} ({err})")
        return 1


if __name__ == "__main__":
    sys.exit(main())
