#!/usr/bin/env python3
"""Answer an item on the attention stack and name the session it routes back to.

The store records the answer; this script never delivers it. Delivery is a
cross-session SendMessage, which only a Claude session can send, so the command
wrapping this script does it from the TARGET line printed here.

  next              list open items, oldest first, each with its delivery target
  answer ITEM_ID    POST /api/1.0/attention/{ITEM_ID}/answer, then print the target

Order matters in `answer`: the store's open -> answered compare-and-set runs
first, and a caller that loses it (409) must NOT route its answer -- another arm
already did. Only the winner prints a TARGET.

Target resolution: the item's `producer_id` is a session id; the session
registry (`~/.claude/sessions/<pid>.json`) maps it to the session `name`, which
is the address `SendMessage` takes. One line, three shapes:

  TARGET: <name>                     deliver to this name
  UNDELIVERABLE: session exited      no registry entry -- the asker is gone
  UNDELIVERABLE: name ambiguous ...  >1 session shares the name; a bare name
                                     would reach the wrong one, so refuse

Only `message`-class items route an answer. `permission` and `ack` items are
listed but refused by `answer` -- they have their own paths.

Run: python3 attention-answer.py next | answer ITEM_ID [--by ARM]
"""

import argparse
import glob
import json
import os
import sys
import urllib.error
import urllib.request

STORE = os.environ.get("ATTENTION_STORE_URL", "http://localhost:18080").rstrip("/")
# Local store; a hung one must cost a clear failure, never a stalled session.
STORE_TIMEOUT = float(os.environ.get("ATTENTION_STORE_TIMEOUT", "3"))
SESSIONS_DIR = os.environ.get("CLAUDE_SESSIONS_DIR") or os.path.expanduser("~/.claude/sessions")
DEFAULT_ARM = "supervisor:attention-next"
PAYLOAD_WIDTH = 160


def load_registry(sessions_dir=None):
    """Return every readable registry entry. An unreadable file is skipped, not fatal."""
    entries = []
    for path in glob.glob(os.path.join(sessions_dir or SESSIONS_DIR, "*.json")):
        try:
            with open(path, encoding="utf-8") as fh:
                entries.append(json.load(fh))
        except (OSError, ValueError):
            continue
    return entries


def resolve_target(producer_id, registry):
    """Return (name, None) when deliverable, else (None, reason)."""
    matches = [e for e in registry if e.get("sessionId") == producer_id]
    if not matches:
        return None, "session exited"
    name = matches[0].get("name") or ""
    if not name:
        return None, "session has no name"
    sharing = [e for e in registry if e.get("name") == name]
    if len(sharing) > 1:
        return None, f"name ambiguous ({len(sharing)} sessions named {name!r})"
    return name, None


def target_line(producer_id, registry):
    name, reason = resolve_target(producer_id, registry)
    return f"TARGET: {name}" if name else f"UNDELIVERABLE: {reason}"


def fetch_open():
    with urllib.request.urlopen(f"{STORE}/api/1.0/attention", timeout=STORE_TIMEOUT) as resp:
        items = json.load(resp)
    items = [i for i in items if i.get("state") == "open"]
    return sorted(items, key=lambda i: i.get("created_at", ""))


def fetch_item(item_id):
    with urllib.request.urlopen(f"{STORE}/api/1.0/attention/{item_id}", timeout=STORE_TIMEOUT) as resp:
        return json.load(resp)


def post_answer(item_id, answered_by):
    req = urllib.request.Request(
        f"{STORE}/api/1.0/attention/{item_id}/answer",
        data=json.dumps({"answered_by": answered_by}).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=STORE_TIMEOUT) as resp:
        return json.load(resp)


def cmd_next(registry, out=sys.stdout):
    items = fetch_open()
    if not items:
        print("No open items.", file=out)
        return 0
    for n, item in enumerate(items, 1):
        payload = " ".join(str(item.get("payload", "")).split())[:PAYLOAD_WIDTH]
        mech = item.get("answer_mechanism", "?")
        if mech == "message":
            route = target_line(item.get("producer_id", ""), registry)
        else:
            route = f"not answerable here ({mech})"
        print(f"{n}. [{item.get('interrupt_class', '?')}] {item.get('item_id')} -- {payload}", file=out)
        print(f"   {route}", file=out)
    return 0


def cmd_answer(item_id, answered_by, registry, out=sys.stdout):
    item = fetch_item(item_id)
    mech = item.get("answer_mechanism")
    if mech != "message":
        print(f"REFUSED: {item_id} is {mech}-class; only message-class items route an answer", file=out)
        return 2
    try:
        answered = post_answer(item_id, answered_by)
    except urllib.error.HTTPError as err:
        if err.code == 409:
            # 409 covers both a lost compare-and-set and an item no longer open;
            # either way this arm did not win, so it must not route.
            print(f"LOST: {item_id} already answered or no longer open -- do not send", file=out)
        else:
            print(f"FAILED: store returned {err.code} for {item_id}", file=out)
        return 1
    print(f"ANSWERED: {item_id} at {answered.get('answered_at')} by {answered.get('answered_by')}", file=out)
    print(target_line(item.get("producer_id", ""), registry), file=out)
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("next")
    ans = sub.add_parser("answer")
    ans.add_argument("item_id")
    ans.add_argument("--by", default=DEFAULT_ARM)
    args = parser.parse_args(argv)
    registry = load_registry()
    try:
        if args.cmd == "next":
            return cmd_next(registry)
        return cmd_answer(args.item_id, args.by, registry)
    except urllib.error.HTTPError as err:
        print(f"FAILED: store returned {err.code}")
        return 1
    except (urllib.error.URLError, OSError) as err:
        print(f"FAILED: attention store unreachable at {STORE} ({err})")
        return 1


if __name__ == "__main__":
    sys.exit(main())
