#!/usr/bin/env python3
"""The under-target branch's state file, its dedup key, its per-row startability
snapshot, its re-post verdict and its suppression line — one owner.

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

Why the snapshot exists, and why `compare` owns the verdict
-----------------------------------------------------------
The re-post bound is *"a row that was UNSTARTABLE in the declined snapshot and can start
now"* — a **change**, never a state. A set whose rows were already `✅ Ready for approval`
and unheld when the operator declined them is the same ask, and re-posting it each tick is
the timer loop the bound exists to remove. So the bound needs the one thing a row set alone
cannot carry: **what each row's startability was when the operator declined**.

Without it, no row can be shown to have CHANGED, and the branch is fail-closed — which is
the silence the whole bound exists to remove, arrived at deliberately. So `write` records a
per-row bit and **refuses to write a state without one**: a snapshot missing its bits is
indistinguishable from the fail-closed state, and only one of those is a decision.

`compare` is the bound's single derivation, for the same reason the key and the line have
one. Left to each manager, "compare this tick's set against the declined set" is a
set-difference over JSON that every caller would spell differently — and the two arms that
must NOT drift are exactly the subtle ones: an **unknown** startability is never read as
unstartable (a degraded audit read must re-post, not suppress), and a row that was
unstartable then and can start now re-posts even though every row can start now.

Usage
-----
    under-target-state.py key      --topic T --row R [--row R …]
    under-target-state.py write    --topic T --item-id ID --row R [--row R …]
                                   (--startable R | --unstartable R | --unknown R) … [--now ISO]
    under-target-state.py read     --topic T [--json]
    under-target-state.py clear    --topic T
    under-target-state.py compare  --topic T --row R [--row R …]
                                   (--startable R | --unstartable R | --unknown R) …
    under-target-state.py suppress --topic T --item-id ID --live N --target N --row R …

Exit codes: 0 ok (suppress) · 2 refused (malformed input, nothing written) · 3 no state
recorded · 10 re-post (compare only: this tick's set is not the ask the operator declined).
`10` is this repo's existing "this run saw a change" code — `manager-predispatch.py --save`
prints it for the same reason.
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

STARTABLE = "startable"
UNSTARTABLE = "unstartable"
UNKNOWN = "unknown"
# Rendered, never accepted: a state written before the startability field existed has no bit
# for a row, and that absence is NOT the recorded `unknown` — `unknown` re-posts, absence
# fails closed. Collapsing the two in the render is what makes one file read two ways.
UNRECORDED = "(unrecorded)"


def canonical_key(topic, rows):
    """The one derivation. Membership only — order must not move the key."""
    digest = hashlib.sha256("\n".join(sorted(rows)).encode()).hexdigest()[:16]
    return "under-target:%s:%s" % (topic, digest)


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


def validate(topic, rows):
    """Returns an error string, or None. Every caller runs this before touching disk."""
    err = validate_topic(topic)
    if err:
        return err
    if not rows:
        return "no --row given; an empty row set has no membership to key on"
    for r in rows:
        if not r or not r.strip():
            return "a --row is empty; the row set must name rows"
    if len(set(rows)) != len(rows):
        return "the row set repeats a row; membership is a set"
    return None


def read_classification(args):
    """The caller's per-row classification, as ({row: token}, error).

    Three tokens, not two, because the middle one is the whole of one arm: a blank or
    degraded audit verdict is **unknown**, and the branch must read it as unknown rather
    than as unstartable. Collapsing it into `unstartable` is what lets a degraded read
    suppress — the defect class the re-post bound exists to remove.
    """
    seen = {}
    for token, rows in ((STARTABLE, args.startable), (UNSTARTABLE, args.unstartable),
                        (UNKNOWN, args.unknown)):
        for r in rows:
            if not r or not r.strip():
                return None, "a startability is empty; it must name the row it classifies"
            if r in seen:
                return None, ("row %r is classified twice (%s and %s); a row carries one "
                              "startability" % (r, seen[r], token))
            seen[r] = token
    return seen, None


def validate_startability(rows, seen):
    """The classification and the row set must be the SAME set, or the bit means nothing."""
    missing = sorted(set(rows) - set(seen))
    if missing:
        return ("no startability for %s; a snapshot without a bit for every row is the "
                "fail-closed state and cannot be told from a decision"
                % ", ".join(repr(m) for m in missing))
    extra = sorted(set(seen) - set(rows))
    if extra:
        return ("a startability for %s, which is not in the row set; the snapshot and the "
                "row set are the same set" % ", ".join(repr(e) for e in extra))
    return None


def write_state(topic, item_id, rows, startability, now=None):
    """Atomic: a refused or interrupted write leaves the previous state intact."""
    os.makedirs(STATE_DIR, exist_ok=True)
    stamp = now or datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
    payload = {
        "card_item_id": item_id,
        "row_set": rows,
        "startability": {r: startability[r] for r in rows},
        "posted_at": stamp,
    }
    path = state_path(topic)
    tmp = path + ".tmp.%d" % os.getpid()
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    os.replace(tmp, path)
    return path


def load_state(path):
    """The recorded state as a dict, or `(None, reason)`. The ONE reader, for both callers.

    A corrupt file is a REFUSAL with a reason, never a traceback. The write is atomic, so
    corruption is unlikely — but `compare`'s exit code IS the branch's decision input, and an
    uncaught `JSONDecodeError` exits **1**, a code `commands/manager-loop.md` does not
    enumerate. A caller reading only `0` / `3` / `10` would read that `1` as "not suppressed",
    i.e. post — the wrong direction for the one file whose job is to show that nothing CHANGED.
    """
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError) as exc:
        return None, ("the recorded state at %s is unreadable (%s); `… clear` it to start "
                      "over" % (path, exc))
    if not isinstance(data, dict):
        return None, "the recorded state at %s is not an object" % path
    return data, None


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
    seen, err = read_classification(args)
    if err:
        return refuse(err)
    err = validate_startability(args.row, seen)
    if err:
        return refuse(err)
    path = write_state(args.topic, args.item_id, args.row, seen, args.now)
    print("wrote %s" % path)
    return 0


def cmd_read(args):
    err = validate_topic(args.topic)
    if err:
        return refuse(err)
    path = state_path(args.topic)
    if not os.path.exists(path):
        print("no state recorded for %s" % args.topic, file=sys.stderr)
        return 3
    data, err = load_state(path)
    if err:
        return refuse(err)
    if args.json:
        print(json.dumps(data, indent=2, ensure_ascii=False))
    else:
        was = data.get("startability") or {}
        rows = data.get("row_set") or []
        print("card_item_id: %s" % data.get("card_item_id", ""))
        print("posted_at:    %s" % data.get("posted_at", ""))
        print("key:          %s" % canonical_key(args.topic, rows))
        # `(unrecorded)`, never `unknown`: a state written before the field existed has no bit
        # at all, and rendering that as a *recorded* `unknown` would contradict the verdict
        # `compare` returns for the same file — `unknown` re-posts, absence fails closed. Row
        # names are `%r` like every other message here; they are free text and a newline in one
        # would split the line a manager parses.
        print("startability: %s" % (" | ".join(
            "%r=%s" % (r, was[r] if r in was else UNRECORDED) for r in rows) or "(none)"))
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


def repost(reason):
    print("REPOST: %s" % reason)
    return 10


def suppress(reason):
    print("SUPPRESS: %s" % reason)
    return 0


def verdict(snapshot, rows, now):
    """Does this tick's candidate set re-post? The bound's ONE derivation.

    Four arms — the first is structural and short-circuits, the other three are collected
    into one line so a mixed tick names every reason it re-posts:

    1. **Subset?** A row not in the declined batch is a different ask, whatever the rest
       of the set looks like.
    2. **Unknown this tick?** A blank or unreadable audit verdict is neither startable nor
       unstartable. It is asked about, never suppressed: a degraded read is the state in
       which the operator is most likely to be needed, and reading it as unstartable is
       what let a degraded audit read withhold a card.
    3. **Unstartable this tick?** The operator declined *work they could start*, so a set
       whose rows can no longer start is not the same ask however identical its
       membership — measured 2026-10-08, five unstartable rows withheld a card for hours.
    4. **Unstartable in the snapshot?** The last arm, and the only one the row set alone
       could not answer: a row that could not start when the operator declined and can
       start now has CHANGED, and a change is what the bound re-posts on.

    Everything else suppresses. A row with no recorded bit — a state written before this
    field existed — is not unstartable, so it fails closed, which is the direction the
    bound requires.
    """
    declined = snapshot.get("row_set") or []
    new = sorted(set(rows) - set(declined))
    if new:
        return repost("the candidate set is not a subset of the declined set — %s was not "
                      "in the declined batch" % ", ".join(repr(r) for r in new))
    was = snapshot.get("startability") or {}
    # Arms 2-4 are collected rather than returned on first match: the rule tells the caller
    # to name what changed in the card's `--context`, and a mixed tick — one row unstartable,
    # another newly startable — is exactly when the second reason is worth the line. The exit
    # code is `10` either way; only the attribution would have been lost.
    reasons = []
    unknown = sorted(r for r in rows if now[r] == UNKNOWN)
    if unknown:
        reasons.append("%s carries no readable startability this tick" %
                       ", ".join(repr(r) for r in unknown))
    unstartable = sorted(r for r in rows if now[r] == UNSTARTABLE)
    if unstartable:
        reasons.append("%s cannot start this tick" %
                       ", ".join(repr(r) for r in unstartable))
    became = sorted(r for r in rows if now[r] == STARTABLE and was.get(r) == UNSTARTABLE)
    if became:
        reasons.append("%s was unstartable in the declined snapshot and can start now" %
                       ", ".join(repr(r) for r in became))
    if reasons:
        return repost("%s — a set that is not the ask the operator declined re-posts"
                      % "; ".join(reasons))
    unrecorded = sorted(r for r in rows if r not in was)
    if unrecorded:
        return suppress("declined ask %s withheld — no row changed, and %s carries no recorded "
                        "startability, so no change can be shown"
                        % (snapshot.get("card_item_id", ""),
                           ", ".join(repr(r) for r in unrecorded)))
    return suppress("declined ask %s withheld — every row is unchanged and can start"
                    % snapshot.get("card_item_id", ""))


def cmd_compare(args):
    """The re-post verdict: 0 suppress · 3 nothing declined · 10 re-post."""
    err = validate(args.topic, args.row)
    if err:
        return refuse(err)
    seen, err = read_classification(args)
    if err:
        return refuse(err)
    err = validate_startability(args.row, seen)
    if err:
        return refuse(err)
    path = state_path(args.topic)
    if not os.path.exists(path):
        print("no declined snapshot for %s" % args.topic, file=sys.stderr)
        return 3
    data, err = load_state(path)
    if err:
        return refuse(err)
    return verdict(data, args.row, seen)


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
        description="The under-target branch's state file, dedup key, startability "
                    "snapshot, re-post verdict and suppression line")
    sub = parser.add_subparsers(dest="cmd", required=True)

    def common(p):
        p.add_argument("--topic", required=True)
        # Never argparse-required: an empty row set is a *refusal* with a reason, not a
        # usage error. `validate()` owns it so the message is the script's, not argparse's.
        p.add_argument("--row", action="append", default=[])
        p.add_argument("--state-dir", default=None,
                       help="override the state directory (tests; defaults to the live store)")

    def classified(p):
        """The three startability tokens, declared once for the two verbs that take them."""
        p.add_argument("--startable", action="append", default=[],
                       help="a row that could start — its audit verdict is present and is "
                            "`✅ Ready for approval`, it is not held, and it is not deferred")
        p.add_argument("--unstartable", action="append", default=[],
                       help="a row that could not start this tick")
        p.add_argument("--unknown", action="append", default=[],
                       help="a row whose verdict could not be read — neither startable nor "
                            "unstartable, and never read as either")

    common(sub.add_parser("key", help="print the canonical dedup key"))
    w = sub.add_parser("write", help="record the posted card and its per-row startability "
                                     "(atomic, refuses malformed)")
    common(w)
    classified(w)
    w.add_argument("--item-id", required=True)
    w.add_argument("--now", default=None, help="ISO-8601 override, for tests")
    r = sub.add_parser("read", help="print the recorded state")
    common(r)
    r.add_argument("--json", action="store_true")
    c = sub.add_parser("clear", help="remove the recorded state")
    common(c)
    cmp = sub.add_parser("compare", help="the re-post verdict for this tick's candidate set "
                                         "(0 suppress · 3 nothing declined · 10 re-post)")
    common(cmp)
    classified(cmp)
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
        "key": cmd_key, "write": cmd_write, "read": cmd_read, "clear": cmd_clear,
        "compare": cmd_compare, "suppress": cmd_suppress,
    }[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
