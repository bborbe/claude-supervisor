#!/usr/bin/env python3
"""session-holds — the operator's durable "do not work on this" mark.

A hold is a statement of OPERATOR POLICY about a SESSION: *"I do not currently
want to work on this, push it forward, or have any manager handle it."* It is
not a work disposition — `status: hold` on a task already means "blocked/paused,
no resume date", and the two are orthogonal. A hold is therefore keyed on the
SESSION ID, never on a task: the operator's gesture is on a session, a held
session may have no task at all (a side quest), and task rows join to a hold
through their own `claude_session_id`.

Storage is on disk, never in context — `~/.claude/state/session-holds.json`,
written ONLY through this script (flock on a sidecar, then atomic tmp+rename).
Never hand-edit the file.

THE ONE RULE THAT MATTERS — the row stays visible; only the message stops.

A hold does NOT hide a row, and it does NOT pause the session. Every consumer
must still RENDER the held row (`⏸️ HELD — <reason> · <age>`) and must stop
every ACT on it: no nudge, no reap, no auto-resume, no open, no escalation, no
board publish. Suppressing the ROW would remove the evidence, and a row that
disappears from a sweep is indistinguishable from a row that got fixed. Do not
conflate the two.

Pruning is by AGE and LIVENESS, never by sweep-absence. "Absent from my sweep"
is a claim about the sweep's own partial view, not about the hold — a consumer
that pruned on it would re-introduce, one layer down, exactly the per-consumer
invisibility this store exists to remove. A hold whose session has died is
SURFACED as a release candidate by `list`; only `release` removes it.

Reads are lock-free on purpose. Every write lands through `os.replace`, so a
reader sees either the whole old file or the whole new one and never a partial
one; taking the lock for a read would buy nothing and would make every consumer
depend on this script being present. Consumers therefore inline their own
reader — deliberately, and for the same reason `sweep-gate.py` inlines readers
for the registry, the attention feed and the heartbeat store: a subprocess
dependency on a plugin path is a fail-open, and a silent fail-open in a gate is
its worst failure mode.

Usage:
    session-holds.py hold <session|pane> --reason "..." [--by <who>]
    session-holds.py release <session|pane>
    session-holds.py list [--json]
    session-holds.py is-held <session-id>          # exit 0 held, 1 not held
"""
import argparse
import datetime
import fcntl
import glob
import json
import os
import re
import subprocess
import sys

HOLDS = os.environ.get("SUPERVISOR_SESSION_HOLDS") or os.path.expanduser(
    "~/.claude/state/session-holds.json"
)
LOCK = HOLDS + ".lock"
REGISTRY = os.path.expanduser("~/.claude/sessions")

OK = 0
NOT_HELD = 1
BAD_TARGET = 2


def now():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_ts(value):
    try:
        return datetime.datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=datetime.timezone.utc
        )
    except (TypeError, ValueError):
        return None


def age(held_at):
    started = parse_ts(held_at)
    if started is None:
        return "?"
    seconds = int(
        (datetime.datetime.now(datetime.timezone.utc) - started).total_seconds()
    )
    if seconds < 0:
        seconds = 0
    if seconds < 3600:
        return "%dm" % (seconds // 60)
    if seconds < 86400:
        return "%dh" % (seconds // 3600)
    return "%dd" % (seconds // 86400)


def live_sessions():
    """Session ids the registry currently holds. Read by prune and by list."""
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


def registry_names():
    """name -> session id, for the pane join. Later entries win on a clash."""
    names = {}
    for path in glob.glob(os.path.join(REGISTRY, "*.json")):
        try:
            with open(path, encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, ValueError):
            continue
        sid = data.get("sessionId") or data.get("session_id")
        name = data.get("name")
        if sid and name:
            names[name] = sid
    return names


def pane_title(pane_id):
    """The WezTerm tab title owning this pane, or None.

    The registry carries no pane id, so a pane joins to a session by the title
    the supervisor named the tab after -- the same join the server's
    `findRegisteredByName` uses. A WezTerm restart renumbers panes, so a pane id
    is only ever valid for the run that produced it.
    """
    try:
        out = subprocess.run(
            ["wezterm", "cli", "list", "--format", "json"],
            capture_output=True,
            text=True,
            timeout=10,
        ).stdout
        panes = json.loads(out)
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    for pane in panes:
        if str(pane.get("pane_id")) == str(pane_id):
            return pane.get("tab_title")
    return None


def resolve(target):
    """Resolve a session id or a pane id to a session id. None when it cannot."""
    if re.match(r"^[0-9a-fA-F-]{36}$", target or ""):
        return target
    title = pane_title(target)
    if not title:
        return None
    return registry_names().get(title)


def _read():
    try:
        with open(HOLDS, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return {"version": 1, "holds": {}}
    if not isinstance(data, dict) or not isinstance(data.get("holds"), dict):
        return {"version": 1, "holds": {}}
    return data


def _write(data):
    parent = os.path.dirname(HOLDS)
    if parent:
        os.makedirs(parent, exist_ok=True)
    tmp = HOLDS + ".tmp.%d" % os.getpid()
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=2, sort_keys=True)
        handle.write("\n")
    os.replace(tmp, HOLDS)


class Locked(object):
    """Hold the sidecar lock across a read-modify-write. Every consumer races here."""

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


def held_by():
    return (
        os.environ.get("CLAUDE_CODE_SESSION_ID")
        or os.environ.get("CLAUDE_SESSION_ID")
        or "operator"
    )


def cmd_hold(args):
    sid = resolve(args.target)
    if not sid:
        sys.stderr.write(
            "error: cannot resolve %r to a session id.\n"
            "note: a session id is a uuid; a pane id resolves through its WezTerm\n"
            "      tab title to the registry `name`. Re-resolve after any WezTerm\n"
            "      restart -- pane ids do not survive one.\n" % args.target
        )
        return BAD_TARGET
    with Locked():
        data = _read()
        existing = data["holds"].get(sid)
        data["holds"][sid] = {
            "reason": args.reason,
            # A re-hold keeps the ORIGINAL held_at: the age of a hold is how long
            # the operator's policy has stood, not how long ago they last edited
            # the reason. Overwriting it would make an old hold read as fresh.
            "held_at": existing["held_at"] if existing else now(),
            "held_by": args.by or held_by(),
        }
        _write(data)
    print(
        "⏸️ HELD %s — %s%s"
        % (
            sid,
            args.reason,
            " (reason updated; held_at kept at %s)" % existing["held_at"]
            if existing
            else "",
        )
    )
    return OK


def cmd_release(args):
    sid = resolve(args.target)
    if not sid:
        sys.stderr.write("error: cannot resolve %r to a session id.\n" % args.target)
        return BAD_TARGET
    with Locked():
        data = _read()
        entry = data["holds"].pop(sid, None)
        if entry is None:
            sys.stderr.write("error: %s is not held.\n" % sid)
            return NOT_HELD
        _write(data)
    print(
        "▶️ RELEASED %s — was held %s: %s" % (sid, age(entry["held_at"]), entry["reason"])
    )
    return OK


def cmd_list(args):
    data = _read()
    holds = data["holds"]
    live = live_sessions()
    if args.json:
        print(json.dumps(data, indent=2, sort_keys=True))
        return OK
    if not holds:
        print("⏸️ No session holds.")
        return OK
    stale = []
    for sid in sorted(holds, key=lambda s: holds[s].get("held_at") or ""):
        entry = holds[sid]
        dead = sid not in live
        if dead:
            stale.append(sid)
        print(
            "%s %s — %s · %s · by %s"
            % (
                "⚠️ RELEASE CANDIDATE" if dead else "⏸️ HELD",
                sid,
                entry.get("reason", ""),
                age(entry.get("held_at")),
                entry.get("held_by", "?"),
            )
        )
        if dead:
            print(
                "   └─ session is gone (not in %s) — release with: "
                "session-holds.py release %s" % (REGISTRY, sid)
            )
    print("\n%d hold(s); %d release candidate(s)." % (len(holds), len(stale)))
    return OK


def cmd_is_held(args):
    entry = _read()["holds"].get(args.session)
    if entry is None:
        return NOT_HELD
    print(entry.get("reason", ""))
    return OK


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    # The skill's documented shape is flag-first -- `--release <id>` and `--list`
    # are the verbs, and a bare `<session|pane>` means "hold". Translate that to
    # the subcommands the parser defines so the skill passes $ARGUMENTS through
    # verbatim and the shape has exactly one home: here.
    if "--list" in argv:
        argv = ["list"] + [a for a in argv if a != "--list"]
    elif "--release" in argv:
        argv = ["release"] + [a for a in argv if a != "--release"]
    elif argv and argv[0] not in ("hold", "release", "list", "is-held"):
        argv = ["hold"] + argv

    parser = argparse.ArgumentParser(
        description="Session holds — the operator's do-not-work mark."
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    hold = sub.add_parser("hold", help="mark a session held")
    hold.add_argument("target", help="session id (uuid) or WezTerm pane id")
    hold.add_argument(
        "--reason", required=True, help="why it is held (shown on every row)"
    )
    hold.add_argument("--by", default=None, help="who set it (default: this session)")
    hold.set_defaults(func=cmd_hold)

    release = sub.add_parser("release", help="release a hold")
    release.add_argument("target", help="session id (uuid) or WezTerm pane id")
    release.set_defaults(func=cmd_release)

    listing = sub.add_parser(
        "list", help="render every hold, flagging release candidates"
    )
    listing.add_argument("--json", action="store_true", help="raw store, for consumers")
    listing.set_defaults(func=cmd_list)

    isheld = sub.add_parser("is-held", help="exit 0 when held, 1 when not")
    isheld.add_argument("session")
    isheld.set_defaults(func=cmd_is_held)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
