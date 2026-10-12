#!/usr/bin/env python3
"""Enumerate orphan candidates: open work whose owning session is gone.

The reverse index for the fleet sweep. Steps 0-2 run forward (live session ->
task) and structurally cannot see abandoned work: a task whose session died is
invisible because there is no live session to sweep from. This walks the other
direction.

A candidate is an `in_progress` task carrying a `claude_session_id` whose id is
not in the live set, and which is neither parked nor stale backlog.

Two filters, applied in order:

  1. PARK -- a parked routine and an orphan are BOTH genuinely dead, so no
     liveness evidence separates them. The discriminator has to be
     task-semantic, and it takes two signals in union:
       (a) a future `defer_date`   -- scheduled, not abandoned
       (b) `created_by: recurring-task-creator` -- the routine class that
           carries no `defer_date` (`Start Day`, `Plan Week`, ...)
     Neither alone suffices: some routines carry no `defer_date`, and some
     parked tasks carry no `created_by`. Both fail LOUD if the vault stops
     writing them -- tasks read unparked, the set floods, and the check
     over-reports. That is the safe direction.

  2. RECENCY -- upper bound only. Older than `--max-age-days` is backlog, not a
     worker that died mid-flight. There is deliberately NO lower bound: one used
     to exist (>=4h) and it excluded the recently-died orphans this check exists
     to find, because a session that dies mid-work leaves a task file that is
     only minutes stale.

Frontmatter only. Task bodies quote these keys in prose, so a whole-file scan
reads a task as parked on the strength of a sentence *about* parking.
"""

import argparse
import glob
import json
import os
import re
import subprocess
import sys
import time
from datetime import date

FRONTMATTER = re.compile(r"\A---\n(.*?)\n---\n", re.DOTALL)


def frontmatter(text):
    """Return the frontmatter block, or '' when the file has none."""
    match = FRONTMATTER.match(text)
    return match.group(1) if match else ""


def field(block, key):
    """First value of a scalar frontmatter key, or None."""
    match = re.search(rf"^{re.escape(key)}:\s*(.+)$", block, re.MULTILINE)
    if not match:
        return None
    return match.group(1).strip().strip('"').strip("'")


def is_parked(block, today):
    """True when the task is scheduled rather than abandoned."""
    defer_date = field(block, "defer_date")
    if defer_date and defer_date >= today:
        return "defer_date"
    if field(block, "created_by") == "recurring-task-creator":
        return "created_by"
    return None


def resolve_fleet_sessions(explicit=None):
    """Locate fleet-sessions.py.

    Prefers the sibling copy -- this script ships in the same `scripts/` directory,
    so the two move together and no environment variable is needed. The
    `~/.claude/scripts/` fallback is legacy: that directory was emptied on
    2026-09-18 when the manager scripts moved into the plugin, and a caller
    holding the old path silently gets zero candidates.
    """
    if explicit:
        return explicit
    here = os.path.dirname(os.path.abspath(__file__))
    candidates = [
        os.path.join(here, "fleet-sessions.py"),
        os.path.join(
            os.environ.get("CLAUDE_PLUGIN_ROOT", ""), "scripts", "fleet-sessions.py"
        ),
        os.path.expanduser(
            "~/.claude/plugins/marketplaces/claude-supervisor/scripts/fleet-sessions.py"
        ),
        os.path.expanduser("~/.claude/scripts/fleet-sessions.py"),
    ]
    for path in candidates:
        if path and os.path.exists(path):
            return path
    return None


def live_ids(script):
    """8-char session ids with a transcript seen recently.

    Delegates to fleet-sessions.py rather than reimplementing the probe. No
    scope flag is passed: the sweep is machine-wide by construction, so there
    is no scope to widen.
    """
    if not script:
        return None
    proc = subprocess.run(
        ["python3", script], capture_output=True, text=True, check=False
    )
    if proc.returncode != 0:
        print(f"error: {script} exited {proc.returncode}", file=sys.stderr)
        return None
    ids = set()
    for line in proc.stdout.splitlines():
        if not re.search(r"(^|\s)\d+(s|m) ago", line) and not re.search(
            r"(^|\s)[1-3]h ago", line
        ):
            continue
        ids.update(re.findall(r"\b[0-9a-f]{8}\b", line))
    return ids


HOLDS_PATH = os.path.expanduser(
    os.environ.get("SUPERVISOR_SESSION_HOLDS", "~/.claude/state/session-holds.json")
)

# The session registry, read only by the Hold-window pane join. Same dir and env
# var as the other consumers, so a test can point it at a fixture tree.
REGISTRY_DIR = os.path.expanduser(
    os.environ.get("SUPERVISOR_SESSIONS_DIR") or "~/.claude/sessions"
)


def read_holds() -> dict:
    """The operator's session holds, keyed on session id. `{}` on any read failure.

    Inlined rather than shelling out to session-holds.py: the plugin's scripts do
    not import each other and a subprocess dependency on a plugin path is a
    fail-open. Reads are lock-free because the writer lands every change through
    `os.replace`, so a reader sees the whole old file or the whole new one.

    A missing or corrupt store reads as *nothing held*, and that direction is
    chosen: inventing a hold would silently drop a real orphan candidate, while
    reading a real store as empty merely fails to honour a hold.
    """
    try:
        with open(HOLDS_PATH, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return {}
    holds = data.get("holds") if isinstance(data, dict) else None
    return holds if isinstance(holds, dict) else {}


# --------------------------------------------------------------------------- #
# the Hold window -- the second source of heldness
# --------------------------------------------------------------------------- #
#
# A pane parked in the WezTerm role map's Hold window is held with no store entry
# at all: the operator's statement is the pane's POSITION, not a `hold` call. It is
# resolved at read time from the map's `Hold.window_id` plus `wezterm cli list`, and
# never persisted -- a stored entry would outlive the pane leaving the window.
# Inlined, like `read_holds` above: the plugin's scripts do not import each other.

# The WezTerm role map. Same variable the server reads (`server/config.mjs`) and
# `session-holds.py` reads.
ROLE_MAP = os.environ.get("SUPERVISOR_ROLE_MAP") or os.path.expanduser(
    "~/.cache/wezterm-role-map.json"
)


def panes():
    """Every WezTerm pane, or `None` when the query FAILED.

    ⚠️ `None`, never `[]`: a failed `wezterm cli list` cannot prove a pane is gone
    any more than it can prove one is live. `[]` is reserved for a transport that
    answered with zero panes, and for a machine with no `wezterm` BINARY -- an
    absent transport has no panes, and the Hold window is a WezTerm concept. Only
    `FileNotFoundError` reads that way; any other `OSError` is a genuine failure and
    stays `None`. See `docs/pane-reads.md`.
    """
    try:
        result = subprocess.run(
            ["wezterm", "cli", "list", "--format", "json"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except FileNotFoundError:
        return []
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    try:
        listing = json.loads(result.stdout)
    except ValueError:
        return None
    return listing if isinstance(listing, list) else None


# The activity glyph WezTerm puts in front of a pane title (`✳` active, `◑`/`◐`
# otherwise). Decoration on the title, never part of the name.
_GLYPH = re.compile(r"^[^\w\s]{1,2}\s+")


def registry_names():
    """name -> session id, for the pane join. Later entries win on a clash."""
    names = {}
    for path in glob.glob(os.path.join(REGISTRY_DIR, "*.json")):
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


def session_pane(session_id, listing):
    """The pane currently showing this session, or None when the listing has none.

    The registry carries no pane id, so a session id joins to its pane through the
    registry `name`, which is what the tab TITLE carries.
    """
    names = registry_names()
    for pane in listing:
        title = pane.get("title") or ""
        name = title if title in names else _GLYPH.sub("", title)
        if names.get(name) == session_id:
            return pane.get("pane_id")
    return None


def pane_window(pane_id, listing):
    """The WezTerm window id owning this pane, or None when the listing has none.

    Read straight off the pane record, with no name join, so it answers for a
    Hold-window pane whose title the registry does not know.
    """
    for pane in listing:
        if str(pane.get("pane_id")) == str(pane_id):
            window = pane.get("window_id")
            if isinstance(window, bool) or not isinstance(window, int):
                return None
            return window
    return None


def hold_window_id():
    """The role map's Hold window id, or None when it cannot be read.

    Read directly from the map's `Hold.window_id`, never through the server's role
    map: its `ROLES` deliberately excludes Hold, because the Hold window is a
    parking lot rather than a role and is never spawned into.
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
    """Whether session `target` sits in the role map's Hold window.

    Three states, per `docs/pane-reads.md`: `True` in the window, `False` not in
    it, `None` when the pane listing could NOT be read, so the question is
    unanswered. A missing role map, an unreadable one, or one carrying no `Hold`
    key is NOT that third state -- it is the documented fallback to explicit holds
    only, and it answers `False`.
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


def is_held(session_id: str) -> bool:
    """True when this session carries a hold -- store or Hold window. Never raises.

    The second source is the WezTerm Hold window: a pane parked there is held with
    no store entry, resolved at read time.

    ⚠️ A failed pane read answers *not held* here, and that direction is chosen for
    THIS caller exactly as it is for the store read above: this predicate DROPS a
    candidate, so inventing a hold would silently drop a real orphan -- the false
    clean this script exists to prevent -- while reading a broken transport as
    "nothing held" merely fails to honour a Hold-window hold and leaves the row in
    the candidate list, where a later sweep re-checks it.
    """
    if not session_id:
        return False
    if isinstance(read_holds().get(session_id), dict):
        return True
    return in_hold_window(session_id) is True


def candidates(tasks_dir, today, max_age_days, live):
    """Yield (name, session_id, age_hours) for each orphan candidate."""
    now = time.time()
    for name in sorted(os.listdir(tasks_dir)):
        if not name.endswith(".md"):
            continue
        path = os.path.join(tasks_dir, name)
        try:
            with open(path, encoding="utf-8", errors="replace") as handle:
                block = frontmatter(handle.read())
        except OSError as exc:
            print(f"warn: {path}: {exc}", file=sys.stderr)
            continue
        if not block or field(block, "status") != "in_progress":
            continue
        if is_parked(block, today):
            continue
        age_hours = (now - os.path.getmtime(path)) / 3600
        if age_hours > max_age_days * 24:
            continue
        session_id = field(block, "claude_session_id")
        if not session_id or session_id[:8] in live:
            continue
        # A held session is never an orphan candidate. The hold suppresses the
        # ACT only -- the row still renders in the sweep, which is what keeps a
        # hold distinguishable from a session that got fixed.
        if is_held(session_id):
            continue
        yield name[: -len(".md")], session_id, age_hours


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tasks-dir", required=True, help="directory of task .md files")
    parser.add_argument(
        "--fleet-sessions",
        default=None,
        help="path to fleet-sessions.py (default: the sibling copy)",
    )
    parser.add_argument(
        "--max-age-days",
        type=int,
        default=7,
        help="drop candidates older than this (default 7; 0 disables)",
    )
    parser.add_argument("--json", action="store_true", help="emit JSON")
    args = parser.parse_args()

    if not os.path.isdir(args.tasks_dir):
        print(f"error: not a directory: {args.tasks_dir}", file=sys.stderr)
        return 2

    script = resolve_fleet_sessions(args.fleet_sessions)
    live = live_ids(script)
    if live is None:
        # LOUD on stdout as well as stderr. Returning zero candidates with only a
        # stderr line is the false-clean this script exists to prevent: a caller
        # that ignores the exit code reads silence as "no orphans", which is the
        # exact failure mode the orphan check itself was rebuilt to fix.
        print("⚠️ ORPHAN CHECK FAILED — the result is not clean, it is UNKNOWN.")
        print(f"   liveness probe unavailable: {script or 'fleet-sessions.py not found'}")
        return 2

    today = date.today().isoformat()
    found = list(candidates(args.tasks_dir, today, args.max_age_days, live))

    if args.json:
        json.dump(
            [
                {"task": n, "session_id": s, "age_hours": round(a, 1)}
                for n, s, a in found
            ],
            sys.stdout,
            indent=2,
        )
        print()
    else:
        for name, session_id, age_hours in found:
            print(f"orphan: {name}  ({session_id[:8]}, {age_hours:.0f}h)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
