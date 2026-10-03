#!/usr/bin/env python3
"""The waiting-approval card's dedup key and its state file — one owner.

Why this exists
---------------
`commands/manager-loop.md` § Act's `waiting-approval` bullet and `agents/manager-drive.md`
clause (7)'s `Card (1)` block both name a `--dedup-key` for the round's waiting-approval
row set, and **neither owned its derivation**. The leg rendered the template's own example
as the value to hand over — `post-batch --dedup-key "<the round's waiting-approval row set>"`
— and across three consecutive ticks (89–91, 2026-10-03, topic `manager-layer`) it returned
a real key once and that literal twice. A placeholder is not a key: no derivation is stable,
the store's open-scoped suppression never matches, and the operator is asked the same
question twice. Two consecutive rounds held the card.

⚠️ Why a second script rather than a mode of `under-target-state.py`
-------------------------------------------------------------------
That script is the **under-target** branch's producer: its key carries an
`under-target:<topic>:` prefix and its state file is
`~/.claude/state/manager-under-target/<topic>.json`. Borrowing it for this card is what
tick 92 measured — the leg returned `under-target:manager-layer:5850b257b5f5ecd2` over the
round's 22 waiting-approval rows and described it as the fix exercised by hand. That key
**round-trips cleanly**: the under-target producer recomputes it from those rows, so every
recompute check passes. A key can be internally consistent and still belong to a different
card's branch, and verification alone cannot tell the two apart. So this card gets its own
key form and its own store, and the two never share a namespace.

Why the key is bare
-------------------
The key is over the row set's **membership**, not its order — a re-ranking that names the
same rows is not a changed ask, so the key must not move. It carries **no prefix** and does
**not** fold in the topic: the namespace is the script, not the string, and a bare 16-hex
digest cannot be confused with `under-target:<topic>:<digest>` at the point of use. Cross-
topic separation is the store's own `producer_id` scoping, not the key's.

Why there is a state file
-------------------------
The posting rule is *poll before you post* — the store's suppression is open-scoped, so a
re-post of an already-answered key writes a fresh row that reads `OPEN` and loses the
answer. `poll` takes an **item id**, not a key, and `attention-ask.py` exposes no lookup
for one, so the id has to be carried between ticks. Nothing carried it. `write` on post,
`read` before posting.

Why `verify` exists
-------------------
The posting rule obliges a caller handed a non-key to derive the real one or refuse and say
so, and a prose obligation cannot be run. `verify` is that obligation as an executable: it
takes the key a caller was handed plus the round's rows and either confirms the two agree or
refuses with a line naming what it was given. Feeding it the `Card (1)` template's
placeholder is the forced input that exercises the path.

Usage
-----
    waiting-approval-state.py key    --row R [--row R …]
    waiting-approval-state.py verify --dedup-key K --row R [--row R …]
    waiting-approval-state.py write  --topic T --item-id ID --row R [--row R …] [--now ISO]
    waiting-approval-state.py read   --topic T [--json]
    waiting-approval-state.py clear  --topic T

Exit codes: 0 ok · 2 refused (malformed input, nothing written) · 3 no state recorded.
"""

import argparse
import hashlib
import json
import os
import re
import sys
from datetime import datetime, timezone

STATE_DIR = os.path.expanduser("~/.claude/state/manager-waiting-approval")
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")

# The template's own example, verbatim from `agents/manager-drive.md`'s `Card (1)` block.
# Named here so the refusal can say what it was handed rather than "invalid key".
PLACEHOLDER = "<the round's waiting-approval row set>"


def canonical_key(rows):
    """The one derivation. Membership only — order must not move the key.

    Deliberately prefixless: the digest half of `under-target-state.py`'s `canonical_key`
    with its `under-target:<topic>:` namespace dropped, so the two can never be mistaken
    for one another by a reader or a `grep`.
    """
    return hashlib.sha256("\n".join(sorted(rows)).encode()).hexdigest()[:16]


def refuse(msg):
    print("REFUSED: %s" % msg, file=sys.stderr)
    return 2


def state_path(topic):
    return os.path.join(STATE_DIR, "%s.json" % topic)


def validate_topic(topic):
    """Every path-building caller runs this first — a topic is a slug, never a path."""
    if not topic or not SLUG_RE.match(topic):
        return "topic %r is not a slug (want ^[a-z0-9][a-z0-9._-]*$)" % topic
    return None


def validate_rows(rows):
    """Returns an error string, or None. Every caller runs this before touching disk."""
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
    payload = {
        "card_item_id": item_id,
        "key": canonical_key(rows),
        "row_set": rows,
        "posted_at": stamp,
    }
    path = state_path(topic)
    tmp = path + ".tmp.%d" % os.getpid()
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    os.replace(tmp, path)
    return path


def cmd_key(args):
    err = validate_rows(args.row)
    if err:
        return refuse(err)
    print(canonical_key(args.row))
    return 0


def cmd_verify(args):
    """The caller's derive-or-refuse obligation, executable.

    Refuses a key that is not this row set's key — the template's placeholder included —
    and names what it was handed. A refusal is the correct outcome for a non-key: the
    alternative is posting a card that no later tick can poll back.
    """
    err = validate_rows(args.row)
    if err:
        return refuse(err)
    given = (args.dedup_key or "").strip()
    if not given:
        return refuse(
            "--dedup-key is empty; derive it with `waiting-approval-state.py key --row …`")
    expected = canonical_key(args.row)
    if given == PLACEHOLDER:
        print(
            "REFUSED: --dedup-key is the `Card (1)` template's placeholder, not a key — "
            "derive the real one: waiting-approval-state.py key --row <row> …",
            file=sys.stderr,
        )
        return 2
    if given != expected:
        print(
            "REFUSED: --dedup-key %r is not this row set's key (%s) — derive it: "
            "waiting-approval-state.py key --row <row> …" % (given, expected),
            file=sys.stderr,
        )
        return 2
    print("OK: %s is this row set's key" % expected)
    return 0


def cmd_write(args):
    err = validate_topic(args.topic) or validate_rows(args.row)
    if err:
        return refuse(err)
    if not args.item_id or not args.item_id.strip():
        return refuse("--item-id is empty; the state records which card was posted")
    path = write_state(args.topic, args.item_id, args.row, args.now)
    print("wrote %s" % path)
    return 0


def cmd_read(args):
    """Print the recorded state — complete by default.

    ⚠️ `row_set` is printed, not withheld behind `--json`. A summary that shows a key
    derived from a row set while omitting the row set is a summary that will be read
    incomplete; the sibling script learned that the hard way and this one does not
    repeat it. The key is labelled as derived so it is not read as a second record.
    """
    err = validate_topic(args.topic)
    if err:
        return refuse(err)
    path = state_path(args.topic)
    if not os.path.exists(path):
        print("no state recorded for %s" % args.topic, file=sys.stderr)
        return 3
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    if args.json:
        print(json.dumps(data, indent=2, ensure_ascii=False))
        return 0
    rows = data.get("row_set", [])
    print("card_item_id: %s" % data.get("card_item_id", ""))
    print("posted_at:    %s" % data.get("posted_at", ""))
    print("row_set:      %s" % (" | ".join(rows) if rows else ""))
    print("key (derived): %s" % canonical_key(rows))
    return 0


def cmd_clear(args):
    err = validate_topic(args.topic)
    if err:
        return refuse(err)
    path = state_path(args.topic)
    if os.path.exists(path):
        os.remove(path)
    print("cleared %s" % args.topic)
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="The waiting-approval card's dedup key and its state file")
    sub = parser.add_subparsers(dest="cmd", required=True)

    def rows(p):
        # Never argparse-required: an empty row set is a *refusal* with a reason, not a
        # usage error. `validate_rows()` owns it so the message is the script's, not
        # argparse's. `--state-dir` overrides the store (tests; defaults to the live one).
        p.add_argument("--row", action="append", default=[])
        p.add_argument("--state-dir", default=None)

    def topic(p):
        rows(p)
        p.add_argument("--topic", required=True)

    k = sub.add_parser("key", help="print the card's canonical dedup key")
    rows(k)

    v = sub.add_parser("verify", help="confirm a key is this row set's, or refuse and say so")
    rows(v)
    v.add_argument("--dedup-key", required=True)

    w = sub.add_parser("write", help="record the posted card (atomic, refuses malformed)")
    topic(w)
    w.add_argument("--item-id", required=True)
    w.add_argument("--now", default=None, help="ISO-8601 override, for tests")

    r = sub.add_parser("read", help="print the recorded state")
    topic(r)
    r.add_argument("--json", action="store_true")

    c = sub.add_parser("clear", help="remove the recorded state")
    topic(c)

    args = parser.parse_args(argv)
    if getattr(args, "state_dir", None):
        global STATE_DIR
        STATE_DIR = args.state_dir
    return {
        "key": cmd_key, "verify": cmd_verify, "write": cmd_write,
        "read": cmd_read, "clear": cmd_clear,
    }[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
