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

A pane in the role map's HOLD WINDOW is held with no store entry at all. The Hold
window is a parking lot, not a role -- the operator moves a tab there to say
"leave this alone" -- so the statement is made by the pane's POSITION rather than
by a `hold` call, and a consumer that reads only the store sees it as an ordinary
session. It is resolved at read time from the role map's `Hold.window_id` plus
`wezterm cli list`, and never persisted: a stored entry would outlive the pane
leaving the window, so the hold would then be unreleasable by the gesture that
set it.

Usage:
    session-holds.py hold <session|pane> --reason "..." [--by <who>]
    session-holds.py release <session|pane>
    session-holds.py list [--json]
    session-holds.py is-held <session|pane>        # exit 0 held, 1 not held
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
# The WezTerm role map, which names each window's role. Same variable the server
# reads (`server/config.mjs`), so a test can point at a tmp map exactly as it
# already points SUPERVISOR_SESSION_HOLDS at a tmp store.
ROLE_MAP = os.environ.get("SUPERVISOR_ROLE_MAP") or os.path.expanduser(
    "~/.cache/wezterm-role-map.json"
)

OK = 0
NOT_HELD = 1
BAD_TARGET = 2

# What `is-held` prints for a pane held only by the WINDOW it sits in. Never
# written to the store: a window-derived hold is decided at READ time, because a
# persisted entry would outlive the pane leaving the window.
HOLD_WINDOW_REASON = "hold-window"


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


def panes():
    """Every WezTerm pane, or `None` when the query FAILED.

    ⚠️ `None`, never `[]`: a failed `wezterm cli list` cannot prove a pane is gone
    any more than it can prove one is live, so no caller may read it as "no panes
    exist". `[]` is reserved for a WezTerm that answered with zero panes — a real
    empty answer that must keep working. Same convention as `jump.py`'s
    `wezterm_panes()`, and the reason `transport-read-check.py` enumerates this
    function as a transport read site.
    """
    try:
        result = subprocess.run(
            ["wezterm", "cli", "list", "--format", "json"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode != 0:
            return None
        listing = json.loads(result.stdout)
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    return listing if isinstance(listing, list) else None


# The activity glyph WezTerm puts in front of a pane title (`✳` active, `◑`/`◐`
# otherwise). Decoration on the title, never part of the name.
_GLYPH = re.compile(r"^[^\w\s]{1,2}\s+")


def pane_title(pane_id):
    """The registry name this pane is showing, or None.

    ⚠️ Read from `title`, NOT `tab_title`. Measured 2026-10-12: `tab_title` is
    EMPTY for every pane in this setup, so the old join through it resolved
    nothing while looking exactly like a pane nobody had named. `window_title` is
    no better -- it is one value shared by every pane in the window, so it cannot
    identify a pane either.

    The registry carries no pane id, so a pane joins to a session by the name the
    tab shows -- the same join the server's `findRegisteredByName` uses. A
    WezTerm restart renumbers panes, so a pane id is only ever valid for the run
    that produced it.

    ⚠️ `None` covers BOTH "no such pane" and "the listing could not be read", and
    that is a deliberate, narrow conflation: the only caller is `resolve()`, whose
    own caller `cmd_hold` refuses non-zero on `None` -- the *refuse non-zero*
    response `docs/pane-reads.md` prescribes for a caller that already exits on
    failure. A caller that must tell the two apart reads `panes()` itself.
    """
    listing = panes()
    if listing is None:
        return None
    title = None
    for pane in listing:
        if str(pane.get("pane_id")) == str(pane_id):
            title = pane.get("title")
            break
    if not title:
        return None
    names = registry_names()
    if title in names:
        return title
    stripped = _GLYPH.sub("", title)
    return stripped if stripped in names else None


def pane_window(pane_id, listing):
    """The WezTerm window id owning this pane, or None when the listing has none.

    Read straight off the pane record, with no name join -- so it answers for a
    Hold-window pane whose title the registry does not know. `listing` is passed
    in rather than re-read, so one decision asks the transport exactly once.
    """
    for pane in listing:
        if str(pane.get("pane_id")) == str(pane_id):
            window = pane.get("window_id")
            if isinstance(window, bool) or not isinstance(window, int):
                return None
            return window
    return None


def session_pane(session_id, listing):
    """The pane currently showing this session, or None when the listing has none.

    The reverse of `pane_title`: a session id joins to its pane through the
    registry `name`, which is what the tab title carries.
    """
    names = registry_names()
    for pane in listing:
        title = pane.get("title") or ""
        name = title if title in names else _GLYPH.sub("", title)
        if names.get(name) == session_id:
            return pane.get("pane_id")
    return None


def hold_window_id():
    """The role map's Hold window id, or None when it cannot be read.

    Read directly from the map's `Hold.window_id`, never through the server's
    role map: its `ROLES` deliberately excludes Hold, because the Hold window is
    a parking lot rather than a role and is never spawned into.
    """
    try:
        with open(ROLE_MAP, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return None
    entry = data.get("Hold") if isinstance(data, dict) else None
    window = entry.get("window_id") if isinstance(entry, dict) else None
    if isinstance(window, bool) or not isinstance(window, int):
        return None
    return window


def in_hold_window(target):
    """Whether `target` -- a pane id or a session id -- sits in the Hold window.

    Three states, per `docs/pane-reads.md`:

    * `True`  -- it is in the Hold window;
    * `False` -- it is not (the map and the pane listing were both read);
    * `None`  -- the pane listing could NOT be read, so the question is
      unanswered. A caller must refuse rather than read this as "not held": a hold
      suppresses ACTS, so collapsing a broken read into `False` is the direction
      that lets a manager nudge a session the operator parked.

    A missing role map, an unreadable one, or one carrying no `Hold` key is NOT
    that third state -- it is the documented fallback to explicit holds only, and
    it answers `False`.
    """
    window = hold_window_id()
    if window is None:
        return False
    listing = panes()
    if listing is None:
        return None
    pane = target if str(target).isdigit() else session_pane(target, listing)
    if pane is None:
        return False
    return pane_window(pane, listing) == window


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
    entry = _read()["holds"].get(args.target)
    if entry is not None:
        print(entry.get("reason", ""))
        return OK
    verdict = in_hold_window(args.target)
    if verdict is None:
        sys.stderr.write(
            "error: the WezTerm pane list could not be read, so whether %r sits in\n"
            "       the Hold window is unknown. Refusing to report it as not held.\n"
            % args.target
        )
        return BAD_TARGET
    if verdict:
        print(HOLD_WINDOW_REASON)
        return OK
    return NOT_HELD


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

    isheld = sub.add_parser(
        "is-held", help="exit 0 when held (store or Hold window), 1 when not"
    )
    isheld.add_argument("target", help="session id (uuid) or WezTerm pane id")
    isheld.set_defaults(func=cmd_is_held)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
