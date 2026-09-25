#!/usr/bin/env python3
"""Drop gates a manager does not own, so its model wakes only for its own.

The attention watcher a manager arms at startup is fleet-wide: `who-needs-me.py`
reports every open gate on the machine, and every emitted line costs the manager
a full turn — a re-sent session context per wake. Measured 2026-09-25 on Fleet
Manager session `64b4a415`: ~12 wakes in one session for panes owned by other
live managers, at a measured median of 345,344 cache-read tokens per turn
([[A Manager Sweep Costs Nothing When Nothing Changed]]) — roughly 4.1M tokens
in a single session, recurring every loop.

Ownership is resolvable without guessing, through four hops:

  1. PANE -> SESSION  the open item's `pane` and `session_id` in
     `~/.claude/state/attention/*.events.jsonl`. NOT the `*.needs.json` store:
     that is the retired pattern and carries nothing for the live feed
     (measured 2026-09-25: 0 of 9 feed panes had a record). NOT the HTTP
     attention store either -- it is authoritative for WHICH items are open and
     resolves producer liveness server-side, but carries no `pane`; the reader
     joins it to this log on `dedup_key` for the event-time fields.

  2. SESSION -> SPAWNER  `parent_session` in the spawn ledger
     (`~/.local/state/claude-supervisor/sessions/<sid>.json`).

  3. SPAWNER IS A MANAGER  when it has NO ledger record of its own. Managers
     are operator-started, so unlike a worker-spawner they were never themselves
     spawned. Measured 2026-09-25: 600 records, 15 distinct parents, 11 with no
     own record (managers) against 4 with one (worker-spawned workers). This is
     a property of the spawn edge, not a session-name convention -- and it is
     NOT `manager-liveness.py`, which tracks a loop's cadence by topic slug and
     never answers whether a session is a manager.

  4. SPAWNER IS LIVE  the session registry (`~/.claude/sessions/<pid>.json`,
     `sessionId` + `status`). A dead manager's panes are nobody's to route, so
     they are kept rather than dropped.

The keep-set is the complement and is deliberately wide: a gate is dropped ONLY
when its spawner is a live manager other than this watcher's own. Panes this
session spawned, panes whose spawner is a worker, panes whose spawner is dead,
and unowned panes all emit.

⚠️ A filter that silently matches nothing is indistinguishable from a quiet
fleet ([[A Broken Watcher Looks Exactly Like a Quiet One]]). So `--explain`
prints the reason for every verdict, and every run prints a
`gates: N  emit: N  dropped(peer-manager): N` line on stderr — which is what
makes an empty result legible as "nothing to filter" rather than "the filter is
broken".
"""

import argparse
import glob
import json
import os
import sys

STATE = os.path.expanduser("~/.claude/state/attention")
LEDGER = os.path.expanduser("~/.local/state/claude-supervisor/sessions")
REGISTRY = os.path.expanduser("~/.claude/sessions")

EMIT = "emit"
DROP = "drop"


def load_ledger(ledger_dir=LEDGER):
    """Spawn records keyed by session id, or {} when the directory is unreadable.

    An unreadable ledger is NOT an empty one -- it means every spawner resolves
    as a manager, which would drop the whole fleet. The caller must treat {}
    as unknown and fail open (keep everything) rather than as "no spawns".
    """
    out = {}
    for path in glob.glob(os.path.join(ledger_dir, "*.json")):
        try:
            with open(path, encoding="utf-8") as handle:
                rec = json.load(handle)
        except (OSError, ValueError):
            continue
        sid = rec.get("session_id") or os.path.basename(path)[: -len(".json")]
        out[sid] = rec
    return out


def is_manager(session_id, ledger):
    """A spawner is a manager when it was never itself spawned."""
    if not session_id:
        return False
    return session_id not in ledger


def live_ids(registry_dir=REGISTRY):
    """Session ids the registry lists, or None when the registry is unreadable.

    None is not "nothing is live": it is unknown, and the caller fails open.
    """
    if not os.path.isdir(registry_dir):
        return None
    out = set()
    for path in glob.glob(os.path.join(registry_dir, "*.json")):
        try:
            with open(path, encoding="utf-8") as handle:
                rec = json.load(handle)
        except (OSError, ValueError):
            continue
        sid = rec.get("sessionId") or rec.get("session_id")
        if sid:
            out.add(sid)
    return out


def open_items(state_dir=STATE):
    """Currently-open items from the event logs, newest write per item.

    The log is append-only and never rewritten: an `open` line carries the item,
    a later `close` line carrying only its `item_id` clears it. Folded here
    rather than read from the record, so the `pane` and `session_id` always come
    from the same line.
    """
    items = []
    for path in glob.glob(os.path.join(state_dir, "*.events.jsonl")):
        opened, closed = {}, set()
        try:
            with open(path, encoding="utf-8", errors="replace") as handle:
                for line in handle:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except ValueError:
                        continue
                    item_id = rec.get("item_id")
                    if not item_id:
                        continue
                    if rec.get("type") == "open":
                        opened[item_id] = rec
                    else:
                        closed.add(item_id)
        except OSError:
            continue
        for item_id, rec in opened.items():
            if item_id not in closed:
                items.append(rec)
    return items


def session_for_pane(pane, items):
    """The session that raised the gate on `pane`, or None.

    Pane ids are reused, so a stale open item can wear a live pane's id. The
    caller pairs this with the feed's own liveness read; here the newest open
    item for the pane wins.
    """
    hits = [r for r in items if str(r.get("pane")) == str(pane)]
    if not hits:
        return None
    hits.sort(key=lambda r: r.get("ts") or 0)
    return str(hits[-1].get("session_id") or "") or None


def verdict(session_id, spawner, ledger, live, self_id):
    """(emit|drop, reason) for one gated session.

    The keep-set is wide on purpose: drop ONLY on a live peer manager.
    """
    if not session_id:
        return EMIT, "unowned"
    if self_id and session_id == self_id:
        return EMIT, "self"
    if not spawner:
        return EMIT, "unowned"
    if self_id and spawner == self_id:
        return EMIT, "own-worker"
    if not is_manager(spawner, ledger):
        return EMIT, "worker-spawner"
    if live is None:
        return EMIT, "liveness-unknown"
    if spawner not in live:
        return EMIT, "dead-manager"
    return DROP, "peer-manager"


def evaluate(pane, ledger, live, items, self_id):
    """(verdict, reason, session_id, spawner) for one pane."""
    session_id = session_for_pane(pane, items)
    spawner = (ledger.get(session_id) or {}).get("parent_session") if session_id else None
    call, reason = verdict(session_id, spawner, ledger, live, self_id)
    return call, reason, session_id, spawner


def panes_from_feed(stream):
    """Pane ids in a `who-needs-me.py` render, in order of first appearance.

    Reads the `[<pane>]` row marker rather than any `--pane-id` flag: the flag
    also appears on the activate command of a row whose pane may already be
    gone, so the marker is the row's own identity.
    """
    import re

    out, seen = [], set()
    for line in stream:
        match = re.match(r"\s*\[(\d+)\]", line)
        if not match:
            continue
        pane = match.group(1)
        if pane not in seen:
            seen.add(pane)
            out.append(pane)
    return out


def main():
    parser = argparse.ArgumentParser(description="Drop gates a manager does not own.")
    parser.add_argument("--self", dest="self_id", default=None, help="this watcher's session id")
    parser.add_argument("--pane", action="append", default=[], help="evaluate one pane (repeatable)")
    parser.add_argument("--feed", action="store_true", help="read who-needs-me.py output on stdin")
    parser.add_argument("--explain", action="store_true", help="print the reason for every verdict")
    parser.add_argument("--json", action="store_true", help="emit JSON")
    parser.add_argument("--ledger-dir", default=LEDGER)
    parser.add_argument("--registry-dir", default=REGISTRY)
    parser.add_argument("--state-dir", default=STATE)
    args = parser.parse_args()

    ledger = load_ledger(args.ledger_dir)
    live = live_ids(args.registry_dir)
    items = open_items(args.state_dir)

    panes = list(args.pane)
    if args.feed:
        panes += panes_from_feed(sys.stdin)
    if not panes:
        parser.error("pass --pane, --feed, or both")

    rows = []
    for pane in panes:
        call, reason, session_id, spawner = evaluate(
            pane, ledger, live, items, args.self_id
        )
        rows.append(
            {
                "pane": pane,
                "verdict": call,
                "reason": reason,
                "session_id": session_id,
                "spawner": spawner,
            }
        )

    kept = [r for r in rows if r["verdict"] == EMIT]
    dropped = [r for r in rows if r["verdict"] == DROP]

    if args.json:
        json.dump(rows, sys.stdout, indent=2)
        print()
        return 0

    if args.explain:
        for r in rows:
            print(
                f"{r['verdict']:4} {r['reason']:16} pane {r['pane']:>5}  "
                f"session={str(r['session_id'])[:8]:8} spawner={str(r['spawner'])[:8]:8}"
            )
    elif args.feed:
        # The watcher's own shape: the panes worth a turn, nothing else.
        for r in kept:
            print(r["pane"])
    else:
        for r in rows:
            print(r["verdict"])

    # Loud on the count, so an empty drop set reads as "nothing to filter"
    # rather than as a filter that never matched.
    print(
        f"gates: {len(rows)}  emit: {len(kept)}  dropped(peer-manager): {len(dropped)}",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
