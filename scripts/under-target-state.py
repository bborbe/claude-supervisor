#!/usr/bin/env python3
"""The under-target branch's state file, its dedup key, and its suppression line — one owner.

Why this exists
---------------
`commands/manager-loop.md` § Act's `under-target` bullet tells a manager to record the card
it just posted at `~/.claude/state/manager-under-target/<topic>.json`, to derive the card's
`--dedup-key` from the round's row set, and to print a named line when it withholds a card
because the operator already declined that ask. It described all three and **owned none of
them**, so every manager hand-rolled its own — and they diverged.

Measured 2026-10-02, four subjects live in one state directory:

    attention-routing              attention-routing-under-target-2026-10-02T17:32
    bro-21546-…                    BRO-21546 under-target 2026-10-02T17:20 approval question: …
    manager-layer                  under-target:manager-layer:75514df80c93f70c
    managers-spawn-…               under-target:<topic>:add-a-cluster-spawn-mode|the-parked-…

Four derivations, no two alike — and **two carry a timestamp**. A timestamped key changes
every minute, so a re-post of an *unchanged* row set is a different key: the store's
open-scoped suppression never matches, a new row is written, and the operator is asked the
same question twice. The rule's own guard ("re-posting an unchanged set returns the same
item to poll") silently does not hold for those subjects.

The same drift hit the line. Two live failures on one subject: a line reading
`row set unchanged` where the rule specifies the row names, and a suppressed tick that
dropped the capacity line and the ranked rows **along with the ask** — the one thing the
rule says suppression must never take. Both came from a session that had read and quoted
the rule, which is the argument for a producer over a better description.

So this script owns the file, the key and the line. It cannot emit a non-canonical key or
a line missing its parts, because it does not accept them as free text.

Why the key sorts
-----------------
The key is over the row set's **membership**, not its order. A re-ranking that names the
same rows is not a changed ask, so the key must not move — and `set-change`, not order,
is what the rule's re-post bound is written against.

Usage
-----
    under-target-state.py key      --topic T --row R [--row R …]
    under-target-state.py write    --topic T --item-id ID --row R [--row R …] [--now ISO]
    under-target-state.py read     --topic T [--json]
    under-target-state.py clear    --topic T
    under-target-state.py suppress --topic T --item-id ID --live N --target N --row R …

Exit codes: 0 ok · 2 refused (malformed input, nothing written) · 3 no state recorded.
"""

import argparse
import hashlib
import json
import os
import re
import sys
from datetime import datetime, timezone

STATE_DIR = os.path.expanduser("~/.claude/state/manager-under-target")
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")


def canonical_key(topic, rows):
    """The one derivation. Membership only — order must not move the key."""
    digest = hashlib.sha256("\n".join(sorted(rows)).encode()).hexdigest()[:16]
    return "under-target:%s:%s" % (topic, digest)


def refuse(msg):
    print("REFUSED: %s" % msg, file=sys.stderr)
    return 2


def state_path(topic):
    return os.path.join(STATE_DIR, "%s.json" % topic)

def validate(topic, rows):
    """Returns an error string, or None. Every caller runs this before touching disk."""
    if not topic or not SLUG_RE.match(topic):
        return "topic %r is not a slug (want ^[a-z0-9][a-z0-9._-]*$)" % topic
    if not rows:
        return "no --row given; an empty row set has no membership to key on"
    for r in rows:
        if not r or not r.strip():
            return "a --row is empty; the row set must name rows"
    if len(set(rows)) != len(rows):
        return "the row set repeats a row; membership is a set"
    return None


def write_state(topic, item_id, rows, now=None):
    """Atomic: a refused or interrupted write leaves the previous state intact."""
    os.makedirs(STATE_DIR, exist_ok=True)
    stamp = now or datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
    payload = {"card_item_id": item_id, "row_set": rows, "posted_at": stamp}
    path = state_path(topic)
    tmp = path + ".tmp.%d" % os.getpid()
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    os.replace(tmp, path)
    return path


def cmd_key(args):
    err = validate(args.topic, args.row)
    if err:
        return refuse(err)
    print(canonical_key(args.topic, args.row))
    return 0


def cmd_write(args):
    err = validate(args.topic, args.row)
    if err:
        return refuse(err)
    if not args.item_id or not args.item_id.strip():
        return refuse("--item-id is empty; the state records which card was posted")
    path = write_state(args.topic, args.item_id, args.row, args.now)
    print("wrote %s" % path)
    return 0


def cmd_read(args):
    path = state_path(args.topic)
    if not os.path.exists(path):
        print("no state recorded for %s" % args.topic, file=sys.stderr)
        return 3
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    if args.json:
        print(json.dumps(data, indent=2, ensure_ascii=False))
    else:
        print("card_item_id: %s" % data.get("card_item_id", ""))
        print("posted_at:    %s" % data.get("posted_at", ""))
        print("key:          %s" % canonical_key(args.topic, data.get("row_set", [])))
    return 0


def cmd_clear(args):
    path = state_path(args.topic)
    if os.path.exists(path):
        os.remove(path)
    print("cleared %s" % args.topic)
    return 0


def cmd_suppress(args):
    """The line the rule specifies — item id, the withheld row NAMES, and the information.

    Emitting all three here is the point: the two live failures on 2026-10-02 were a line
    that dropped the names and a tick that dropped the capacity line and the rows with it.
    Neither is expressible through this function.
    """
    err = validate(args.topic, args.row)
    if err:
        return refuse(err)
    if not args.item_id or not args.item_id.strip():
        return refuse("--item-id is empty; the line must name the declined ask")
    if args.live is None or args.target is None:
        return refuse("--live and --target are required; suppression never withholds the "
                      "capacity line, and a line without it cannot be told from a tick "
                      "that found nothing")
    print("under-target: suppressed — declined ask %s withheld, row set %s"
          % (args.item_id, " | ".join(args.row)))
    print("workers: %d/%d" % (args.live, args.target))
    print("ranked: %s" % " | ".join(args.row))
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="The under-target branch's state file, dedup key and suppression line")
    sub = parser.add_subparsers(dest="cmd", required=True)

    def common(p):
        p.add_argument("--topic", required=True)
        # Never argparse-required: an empty row set is a *refusal* with a reason, not a
        # usage error. `validate()` owns it so the message is the script's, not argparse's.
        p.add_argument("--row", action="append", default=[])
        p.add_argument("--state-dir", default=None,
                       help="override the state directory (tests; defaults to the live store)")

    common(sub.add_parser("key", help="print the canonical dedup key"))
    w = sub.add_parser("write", help="record the posted card (atomic, refuses malformed)")
    common(w)
    w.add_argument("--item-id", required=True)
    w.add_argument("--now", default=None, help="ISO-8601 override, for tests")
    r = sub.add_parser("read", help="print the recorded state")
    common(r)
    r.add_argument("--json", action="store_true")
    c = sub.add_parser("clear", help="remove the recorded state")
    common(c)
    s = sub.add_parser("suppress", help="print the suppression line (ask withheld, information kept)")
    common(s)
    s.add_argument("--item-id", required=True)
    s.add_argument("--live", type=int, required=True, help="live workers this tick")
    s.add_argument("--target", type=int, required=True, help="the fleet-wide worker target")

    args = parser.parse_args(argv)
    if getattr(args, "state_dir", None):
        global STATE_DIR
        STATE_DIR = args.state_dir
    return {
        "key": cmd_key, "write": cmd_write, "read": cmd_read,
        "clear": cmd_clear, "suppress": cmd_suppress,
    }[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
