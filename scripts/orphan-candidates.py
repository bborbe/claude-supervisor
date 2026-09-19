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
