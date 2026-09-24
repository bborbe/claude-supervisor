#!/usr/bin/env python3
"""Shared asked-ledger: which blocked session has already been asked, by whom.

Storage: ~/.claude/state/asked-ledger.json -- ONE file, shared by every layer
(override SUPERVISOR_ASKED_LEDGER, which is also what lets the tests exercise
claim/resolve/prune without touching the operator's real ledger). Keyed on the
BLOCKED session's id -- the subject -- never on the asker, and never on the
question text.

Why this exists (repair C, [[The Operator's Blocked Sessions Arrive as One
Grouped Decision]]): `commands/fleet-loop.md` drops an entry whose owning manager
is live, and `commands/manager-loop.md` batches its own topic's set. Both are
correct alone, and together they still let the operator receive the SAME question
twice -- the fleet drops what a manager covers, but a manager and the fleet can
both hold the same underlying blocked session and neither sees the other's batch.
Measured twice on 2026-09-20 (`notify-gate.py` records the same two duplicates
from the gate side). This ledger is the mark they were missing: a session claimed
here is asked exactly once, and `list` renders every open claim across all layers
as ONE consolidated list.

Two marks, deliberately not one -- do not merge them:

  * THIS ledger marks the ASK (has this blocked session been batched to the
    operator yet, and by whom). It is SHARED, because the property it protects
    -- asked exactly once -- is a cross-layer property, and a per-layer mark
    cannot express it.
  * `scripts/notify-gate.py` marks the GATE NOTIFICATION, and that one is PER
    LAYER on purpose: a shared cadence ledger would let one layer prune the
    other's gates as "cleared", after which they re-raise forever. See its
    docstring. The two marks answer different questions -- "already asked?"
    versus "how often delivered?" -- and one file serving both would break
    whichever answer it did not keep.

Pruning is by AGE and LIVENESS, never by sweep-absence. That is the rule keeping
a shared file from re-introducing the per-layer bug it replaced: "absent from my
sweep" is a claim about the sweep's own partial view, so a shared ledger pruned
that way would let the fleet's sweep delete a manager's marks. `prune` drops a
resolved entry past `--max-age-hours`, and an open entry past that age only once
its subject session is gone from ~/.claude/sessions/.

Locking: fcntl.flock on a sidecar lock, then atomic tmp+rename -- two layers
write this file concurrently, and a read-modify-write without the lock loses one
of them silently.

Subcommands: claim | resolve | list | prune

`claim` is the gate the callers act on: exit 0 means this asker owns the ask and
includes the session in its batch; exit 3 means another asker already holds an
open claim, so the caller DROPS it from the batch (it still appears in `list`).
"""
import argparse
import datetime
import fcntl
import glob
import json
import os
import sys

LEDGER = os.environ.get("SUPERVISOR_ASKED_LEDGER") or os.path.expanduser(
    "~/.claude/state/asked-ledger.json"
)
LOCK = LEDGER + ".lock"
REGISTRY = os.path.expanduser("~/.claude/sessions")
LAYERS = ("fleet", "worker")
HELD = 3  # exit code: another asker holds an open claim on this subject


def now():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_ts(value):
    try:
        return datetime.datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=datetime.timezone.utc
        )
    except (TypeError, ValueError):
        return None


def age(asked_at):
    started = parse_ts(asked_at)
    if started is None:
        return "?"
    seconds = int((datetime.datetime.now(datetime.timezone.utc) - started).total_seconds())
    if seconds < 0:
        seconds = 0
    if seconds < 3600:
        return "%dm" % (seconds // 60)
    if seconds < 86400:
        return "%dh" % (seconds // 3600)
    return "%dd" % (seconds // 86400)


def asker_id(args):
    sid = (
        args.asker
        or os.environ.get("CLAUDE_CODE_SESSION_ID")
        or os.environ.get("CLAUDE_SESSION_ID")
    )
    if not sid:
        sys.exit(
            "error: no asker session id. Pass --asker <your-session-id>.\n"
            "note: Claude Code exports it as CLAUDE_CODE_SESSION_ID, so this default fires\n"
            "      for any Bash a session runs. Both vars are empty in a spawned child, which\n"
            "      is deliberately stripped of them -- there, pass --asker explicitly from the\n"
            "      session's own transcript path ~/.claude/projects/<project>/<session-id>.jsonl."
        )
    return sid


def live_sessions():
    """Session ids the registry currently holds. Read by prune, never by claim."""
    ids = set()
    for path in glob.glob(os.path.join(REGISTRY, "*.json")):
        try:
            with open(path, encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, ValueError):
            continue
        sid = data.get("sessionId") or data.get("session_id")
        if sid:
            ids.add(sid)
    return ids


def _read():
    try:
        with open(LEDGER, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return {"version": 1, "entries": {}}
    if not isinstance(data, dict) or not isinstance(data.get("entries"), dict):
        return {"version": 1, "entries": {}}
    return data


def _write(data):
    parent = os.path.dirname(LEDGER)
    if parent:
        os.makedirs(parent, exist_ok=True)
    tmp = LEDGER + ".tmp.%d" % os.getpid()
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=2, sort_keys=True)
        handle.write("\n")
    os.replace(tmp, LEDGER)


class Locked(object):
    """Hold the sidecar lock across a read-modify-write. Two layers race here."""

    def __enter__(self):
        parent = os.path.dirname(LOCK)
        if parent:
            os.makedirs(parent, exist_ok=True)
        self.handle = open(LOCK, "a+")
        fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX)
        return self

    def __exit__(self, *exc):
        fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
        self.handle.close()
        return False


def normalise(text):
    return " ".join((text or "").split())


def cmd_claim(args):
    asker = asker_id(args)
    with Locked():
        data = _read()
        entry = data["entries"].get(args.session)
        if entry and entry.get("state") == "open":
            if entry.get("asker") == asker:
                # The same asker re-claiming is its own cadence, not a cross-layer
                # duplicate. Refresh the text so a changed question is recorded.
                entry["text"] = normalise(args.text) or entry.get("text", "")
                entry["asked_at"] = now()
                _write(data)
                print("reclaimed %s (layer %s)" % (args.session, args.layer))
                return 0
            print(
                "held by %s (layer %s) since %s"
                % (entry.get("asker", "?"), entry.get("layer", "?"), entry.get("asked_at", "?"))
            )
            return HELD
        # Absent, or resolved and blocked again -- either way this asker takes it.
        reopened = bool(entry)
        data["entries"][args.session] = {
            "session": args.session,
            "asker": asker,
            "layer": args.layer,
            "text": normalise(args.text),
            "asked_at": now(),
            "state": "open",
            "resolved_at": None,
        }
        _write(data)
        print(
            "%s %s (layer %s)"
            % ("reopened" if reopened else "claimed", args.session, args.layer)
        )
        return 0


def cmd_resolve(args):
    asker = args.asker or os.environ.get("CLAUDE_CODE_SESSION_ID")
    with Locked():
        data = _read()
        entry = data["entries"].get(args.session)
        if not entry:
            print("no entry for %s" % args.session)
            return 0
        if entry.get("state") != "open":
            print("already resolved %s" % args.session)
            return 0
        # A resolver that is not the asker is recorded, not refused: the operator
        # may answer a session the fleet raised, and the mark is about the subject.
        entry["state"] = "resolved"
        entry["resolved_at"] = now()
        if asker and asker != entry.get("asker"):
            entry["resolved_by"] = asker
        _write(data)
        print("resolved %s" % args.session)
        return 0


def cmd_list(args):
    data = _read()
    entries = [e for e in data["entries"].values() if e.get("state") == "open"]
    entries.sort(key=lambda e: (e.get("layer", ""), e.get("asked_at", "")))
    if not entries:
        print("asked-ledger: no open claims")
        return 0
    print("asked-ledger: %d open claim(s), one consolidated list" % len(entries))
    for entry in entries:
        print(
            "  %s · %s · asked by %s · %s ago · %s"
            % (
                entry.get("session", "?"),
                entry.get("layer", "?"),
                entry.get("asker", "?"),
                age(entry.get("asked_at")),
                entry.get("text") or "(no text recorded)",
            )
        )
    return 0


def cmd_prune(args):
    cutoff = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(
        hours=args.max_age_hours
    )
    live = None if args.no_liveness else live_sessions()
    with Locked():
        data = _read()
        kept, dropped = {}, []
        for sid, entry in data["entries"].items():
            started = parse_ts(entry.get("asked_at"))
            too_old = started is None or started < cutoff
            if not too_old:
                kept[sid] = entry
                continue
            # Age alone never drops an OPEN claim on a session that is still live:
            # a long-blocked session is exactly the one that must not be re-asked.
            if entry.get("state") == "open" and live is not None and sid in live:
                kept[sid] = entry
                continue
            dropped.append(sid)
        data["entries"] = kept
        _write(data)
    print("pruned %d entry(ies), kept %d" % (len(dropped), len(kept)))
    for sid in dropped:
        print("  - %s" % sid)
    return 0


def main():
    parser = argparse.ArgumentParser(description="Shared asked-ledger for blocked sessions.")
    sub = parser.add_subparsers(dest="command", required=True)

    p_claim = sub.add_parser("claim", help="claim a blocked session for this asker")
    p_claim.add_argument("--session", required=True, help="the BLOCKED session's id")
    p_claim.add_argument("--layer", required=True, choices=LAYERS)
    p_claim.add_argument("--asker", default=None, help="defaults to $CLAUDE_CODE_SESSION_ID")
    p_claim.add_argument("--text", default="", help="the question being put to the operator")
    p_claim.set_defaults(func=cmd_claim)

    p_resolve = sub.add_parser("resolve", help="mark a subject resolved")
    p_resolve.add_argument("--session", required=True)
    p_resolve.add_argument("--asker", default=None)
    p_resolve.set_defaults(func=cmd_resolve)

    p_list = sub.add_parser("list", help="render the consolidated open-claim list")
    p_list.set_defaults(func=cmd_list)

    p_prune = sub.add_parser("prune", help="drop stale entries by age and liveness")
    p_prune.add_argument("--max-age-hours", type=float, default=24.0)
    p_prune.add_argument(
        "--no-liveness",
        action="store_true",
        help="age-only prune; ignores ~/.claude/sessions (tests, and hosts with no registry)",
    )
    p_prune.set_defaults(func=cmd_prune)

    args = parser.parse_args()
    sys.exit(args.func(args))


if __name__ == "__main__":
    main()
