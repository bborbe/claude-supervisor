#!/usr/bin/env python3
"""Which manager has taken a gated session off the fleet.

`gate-owner-filter.py` resolves ownership from the spawn edge -- the gated
session's `parent_session` in the spawn ledger -- and, since 2026-10-04, from the
gated session being a live manager itself (its "hop 3b"). Neither answers "who
has taken this on". A pane whose session has no spawner (`spawner=None`) **and is
not itself a live manager** reads `unowned` and is emitted to EVERY manager's
watcher on every poll, permanently: nothing about the pane changes when a
manager adopts it, so the adoption is invisible and the wakes never stop.

Measured 2026-10-02, Attention Routing topic manager, one 60-minute tick: 5+
wakes for gates the manager could not act on, 1 of them actionable -- after the
Fleet Manager had taken ownership of panes 372, 90 and 594 by `SendMessage`, a
channel no record reads.

This file is that record. A manager claims the GATED SESSION's id; the filter
reads the claim and drops the pane for every watcher but the holder's.

⚠️ Keyed on the gated session id -- never on the pane, never on the claimer.
The filter already resolves pane -> session (`session_for_pane`) before it
decides anything, so a session-keyed claim joins with no new hop; a pane-keyed
one would need a second pane -> session read inside the filter, against a log
that may have moved on. A claimer-keyed store cannot answer "is this pane
already held" without scanning every claimer -- the same reasoning the
asked-ledger records for keying on its subject.

⚠️ This is NOT the asked-ledger, and the two must not be merged. That ledger
marks the ASK ("has this blocked session been batched to the operator", shared
across layers); this one marks OWNERSHIP ("which manager has adopted this
session"). Its `SKILL.md` section "Two marks -- do not merge them" is the rule:
one file serving both would break whichever answer it did not keep.

⚠️ A claim held by a manager that is no longer live does NOT drop anything --
the filter fails open on a dead holder exactly as it does on a dead spawner. A
dead manager's panes are nobody's to route.

Storage: `~/.claude/state/ownership-claims.json`, written only through this
script (flock, then atomic tmp+rename). Never hand-edit the file.

Subcommands: claim | release | list
"""

import argparse
import datetime
import fcntl
import glob
import json
import os
import sys

CLAIMS = os.environ.get("SUPERVISOR_OWNERSHIP_CLAIMS") or os.path.expanduser(
    "~/.claude/state/ownership-claims.json"
)
LOCK = CLAIMS + ".lock"
REGISTRY = os.path.expanduser("~/.claude/sessions")
HELD = 3  # exit code: another live manager already holds this session


def now():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def manager_id(args):
    """The claiming manager's session id.

    Claude Code exports CLAUDE_CODE_SESSION_ID into every Bash a session runs,
    so the default fires for an ordinary call and --manager exists for the cases
    where it does not (a spawned child is deliberately stripped of both vars).
    """
    sid = (
        args.manager
        or os.environ.get("CLAUDE_CODE_SESSION_ID")
        or os.environ.get("CLAUDE_SESSION_ID")
    )
    if not sid:
        sys.exit(
            "error: no manager session id. Pass --manager <your-session-id>.\n"
            "note: Claude Code exports it as CLAUDE_CODE_SESSION_ID, so this default\n"
            "      fires for any Bash a session runs."
        )
    return sid


def live_sessions():
    """Session ids the registry currently holds, or None when it is unreadable.

    None is NOT "nothing is live" -- it is unknown, and the two must not be
    conflated here. A missing registry directory would otherwise make every
    holder read dead, so any manager could steal a claim from a live one, and a
    wrongly-taken claim silently silences a pane for the manager that actually
    holds it. `cmd_claim` therefore refuses a takeover on an unknown registry:
    the safe direction is the one that keeps the existing holder, since a
    wrongly-refused claim is visible and retryable while a stolen one is not.

    This is deliberately the OPPOSITE of the filter's fail-open on the same
    unreadable registry. The filter must never silence a watcher; the writer
    must never move a claim it cannot prove is free. Different safe directions,
    same unknown.
    """
    if not os.path.isdir(REGISTRY):
        return None
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
        with open(CLAIMS, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return {"version": 1, "claims": {}}
    if not isinstance(data, dict) or not isinstance(data.get("claims"), dict):
        return {"version": 1, "claims": {}}
    return data


def _write(data):
    parent = os.path.dirname(CLAIMS)
    if parent:
        os.makedirs(parent, exist_ok=True)
    tmp = CLAIMS + ".tmp.%d" % os.getpid()
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=2, sort_keys=True)
        handle.write("\n")
    os.replace(tmp, CLAIMS)


class Locked(object):
    """Hold the sidecar lock across a read-modify-write. Two managers race here."""

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
    manager = manager_id(args)
    with Locked():
        data = _read()
        entry = data["claims"].get(args.session)
        if not isinstance(entry, dict):
            # A non-dict entry is malformed, and the reader already skips this
            # shape (gate-owner-filter.py `load_claims`). Treating it as absent
            # here keeps a hand-edited file from killing the claim with a
            # traceback while every watcher reads straight past it.
            entry = None
        if entry:
            holder = entry.get("manager")
            if holder == manager:
                # Re-claiming your own session is your own cadence, not a
                # conflict. Refresh the note so a changed reason is recorded.
                entry["note"] = normalise(args.note) or entry.get("note", "")
                entry["claimed_at"] = now()
                _write(data)
                print("reclaimed %s" % args.session)
                return 0
            live = live_sessions()
            if live is None:
                print(
                    "held by %s since %s (registry unreadable -- liveness unknown, "
                    "not taking over)" % (holder or "?", entry.get("claimed_at", "?"))
                )
                return HELD
            if holder in live:
                print(
                    "held by %s since %s"
                    % (holder or "?", entry.get("claimed_at", "?"))
                )
                return HELD
            # The holder is gone, so the claim is stale and this manager takes
            # it. Unlike the asked-ledger's prune, no age gate is needed: the
            # holder's liveness is the control, exactly as it is for a spawner.
            print("took over %s (previous holder %s is gone)" % (args.session, holder))
        data["claims"][args.session] = {
            "session": args.session,
            "manager": manager,
            "note": normalise(args.note),
            "claimed_at": now(),
        }
        _write(data)
        if not entry:
            print("claimed %s" % args.session)
        return 0


def cmd_release(args):
    with Locked():
        data = _read()
        entry = data["claims"].pop(args.session, None)
        if entry is None:
            print("no claim for %s" % args.session)
            return 0
        _write(data)
        holder = entry.get("manager", "?") if isinstance(entry, dict) else "?"
        print("released %s (was held by %s)" % (args.session, holder))
        return 0


def cmd_list(args):
    data = _read()
    claims = data["claims"]
    if not claims:
        print("ownership-claims: no open claims")
        return 0
    live = live_sessions()
    print("ownership-claims: %d claim(s)" % len(claims))
    for sid in sorted(claims):
        entry = claims[sid]
        if not isinstance(entry, dict):
            # The same malformed shape `cmd_claim` normalises; `list` is the
            # operator's only read of the store, so it must not traceback on it.
            print("  %-36s  (malformed entry)" % sid)
            continue
        holder = entry.get("manager") or "?"
        state = "unknown" if live is None else ("live" if holder in live else "GONE")
        note = entry.get("note") or ""
        print(
            "  %-36s  holder=%s (%s)  since=%s  %s"
            % (sid, holder[:8], state, entry.get("claimed_at", "?"), note)
        )
    return 0


def main():
    parser = argparse.ArgumentParser(
        description="Which manager has taken a gated session off the fleet."
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_claim = sub.add_parser("claim", help="claim a gated session for this manager")
    p_claim.add_argument("--session", required=True, help="the GATED session's id")
    p_claim.add_argument("--manager", default=None, help="this manager's session id")
    p_claim.add_argument("--note", default="", help="why this manager owns it")
    p_claim.set_defaults(func=cmd_claim)

    p_release = sub.add_parser("release", help="drop a claim this manager no longer holds")
    p_release.add_argument("--session", required=True)
    p_release.set_defaults(func=cmd_release)

    p_list = sub.add_parser("list", help="render every open claim")
    p_list.set_defaults(func=cmd_list)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
