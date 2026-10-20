#!/usr/bin/env python3
"""Goals that live sessions anchor on but that no manager was ever armed for.

Why this exists
---------------
Nothing arms a goal manager when a session anchors on a goal, and nothing reported the
resulting state. `manager-liveness.py --state` answered `INVALID — unreadable cadence
file` for such a goal, which is a *probe failure*; a never-armed goal and an unreadable
one were the same answer, and both were indistinguishable from "no manager". Measured
2026-10-20: every goal named by the six idle sessions of the 2026-10-19 census read
`INVALID`, so a correct `revive` nudge from the drive leg had no manager loop to land on.

The reverse index
-----------------
The sweep classifies **live sessions**, so it can see what a live session is doing but not
what it is *missing*. This walks the other direction, from the task files, in the shape
`approved-not-started.py` already uses for its own gap: live sessions → non-terminal tasks
→ the tasks' `goals:` → each goal's manager state.

A goal is reported only when it has **both** a live session and no manager record under
either the prefixed or the pre-split name. That conjunction is what keeps the report
honest in both directions:

  * A goal with no live session is **never** reported. There are 318 goals in the primary
    vault and only a handful carry live sessions; reporting the rest would bury the signal.
  * A goal with a record is never reported, even a lapsed or stood-down one. Those are
    `manager-liveness.py`'s `STALE` / `STOPPED` classes, owned by `docs/sweep-output.md`;
    conflating them with "never armed" is the defect this script exists to end.

`--registry-dir` / `--heartbeat-dir` exist so the population can be *perturbed* in a test
or a probe: a report that ignores the registry would print the same rows against any input,
and the probe is what tells a real derivation from a hardcoded list.

A failed read is `unknown`, never `0`
-------------------------------------
Per `docs/pane-reads.md`, a failed transport read must never be representable as an empty
result. An unreadable registry prints `unarmed goals: unknown` and exits non-zero, because
"no unarmed goals" and "I could not ask" must not render the same.

Frontmatter only
----------------
Task bodies quote these keys in prose, so a whole-file scan reads a task as goal-bearing on
the strength of a sentence. Same rule, same reason as `approved-not-started.py`.
"""
import argparse
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
SESSION_LIVENESS = os.path.join(HERE, "session-liveness.py")
MANAGER_LIVENESS = os.path.join(HERE, "manager-liveness.py")

FRONTMATTER = re.compile(r"\A---\n(.*?)\n---\n", re.DOTALL)

# Non-terminal scheduling states. A completed, aborted, on-hold or backlogged task is not
# a live session's work whatever its recorded session id says.
LIVE_STATUSES = {"next", "todo", "in_progress", ""}

GOALS_DIR = "24 Goals"


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


def list_field(block, key):
    """The items of a YAML block list under `key`, or []."""
    out = []
    collecting = False
    for line in block.splitlines():
        if re.match(rf"^{re.escape(key)}:\s*$", line):
            collecting = True
            continue
        if collecting:
            item = re.match(r"^\s+-\s+(.*)$", line)
            if item:
                out.append(item.group(1).strip().strip('"').strip("'"))
            elif line.strip() and not line.startswith(" "):
                break
    return out


def links(items):
    """`[[Name]]` → `Name` for each item, leaving bare names alone."""
    out = []
    for item in items:
        match = re.match(r"^\[\[(.+?)\]\]$", item.strip())
        out.append(match.group(1) if match else item.strip())
    return out


def session_ids(block):
    """Every session id the task records: `claude_session_id` plus `metrics_sessions`."""
    ids = set()
    primary = field(block, "claude_session_id")
    if primary:
        ids.add(primary.strip().lower())
    collecting = False
    for line in block.splitlines():
        if re.match(r"^metrics_sessions:\s*$", line):
            collecting = True
            continue
        if collecting:
            if line.strip() and not line.startswith(" "):
                break
            match = re.search(r"\bsession_id:\s*(\S+)", line)
            if match:
                ids.add(match.group(1).strip().strip('"').strip("'").lower())
    ids.discard("")
    return ids


def parse_iso(value):
    """Parse an ISO-8601 frontmatter date, or None when it is absent/unparseable."""
    if not value:
        return None
    text = value.strip().strip('"').strip("'")
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def read_live(registry_dir=None, heartbeat_dir=None):
    """The live session records, or None when the liveness read failed.

    Delegates to `session-liveness.py --list --json`; `None` preserves a failed read
    rather than collapsing it to an empty fleet.
    """
    cmd = [sys.executable, SESSION_LIVENESS, "--list", "--json"]
    if registry_dir:
        cmd += ["--dir", registry_dir]
    if heartbeat_dir:
        cmd += ["--heartbeat-dir", heartbeat_dir]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True)
    except OSError:
        return None
    if proc.returncode != 0:
        return None
    try:
        records = json.loads(proc.stdout)
    except ValueError:
        return None
    return records if isinstance(records, list) else None


def manager_state(subject):
    """`manager-liveness.py --state`'s verdict for one goal, or None when unreadable."""
    try:
        proc = subprocess.run(
            [sys.executable, MANAGER_LIVENESS, "--state", "--branch", "goal",
             "--subject", subject],
            capture_output=True, text=True,
        )
    except OSError:
        return None
    first = (proc.stdout.splitlines() or [""])[0]
    return first.split("\t")[0].strip() or None


def collect(tasks_dir, live):
    """The rows: unarmed goals and no-subject tasks, each with its live sessions.

    `live` is never None here — the caller refuses before this runs, so an unreadable
    registry can never reach the point where it looks like an empty fleet.
    """
    live_ids = {(r.get("sessionId") or "").lower() for r in live if r.get("alive", True)}
    goals = {}
    no_subject = []
    for entry in sorted(os.listdir(tasks_dir)):
        if not entry.endswith(".md"):
            continue
        try:
            with open(os.path.join(tasks_dir, entry), encoding="utf-8") as handle:
                block = frontmatter(handle.read())
        except OSError:
            continue
        if not block:
            continue
        if (field(block, "status") or "") not in LIVE_STATUSES:
            continue
        owning = sorted(session_ids(block) & live_ids)
        if not owning:
            continue  # no live session on this task
        name = entry[:-3]
        named = links(list_field(block, "goals"))
        topics = links(list_field(block, "topics"))
        if named:
            for goal in named:
                goals.setdefault(goal, {"goal": goal, "tasks": [], "sessions": set()})
                goals[goal]["tasks"].append(name)
                goals[goal]["sessions"].update(owning)
        elif not topics:
            # Neither `goals:` nor `topics:` — no manager subject is derivable, and the
            # subject-resolution rule forbids inventing one from `themes:`. Report it
            # rather than dropping it, which is what a silent `continue` would do.
            no_subject.append({"task": name, "sessions": owning})

    unarmed = []
    for goal in sorted(goals):
        if manager_state(goal) == "UNARMED":
            row = goals[goal]
            unarmed.append({
                "goal": row["goal"],
                "tasks": sorted(row["tasks"]),
                "sessions": sorted(row["sessions"]),
            })
    no_subject.sort(key=lambda r: r["task"])
    return unarmed, no_subject


def render(unarmed, no_subject):
    """The round's line, plus the rows beneath it."""
    total = len(unarmed) + len(no_subject)
    if not total:
        return "unarmed goals: 0"
    parts = []
    if unarmed:
        head = unarmed[0]
        parts.append(
            "%d unarmed goal%s · %s (%d session%s)" % (
                len(unarmed), "" if len(unarmed) == 1 else "s",
                head["goal"], len(head["sessions"]),
                "" if len(head["sessions"]) == 1 else "s",
            )
        )
    if no_subject:
        parts.append("%d no-subject task%s" % (
            len(no_subject), "" if len(no_subject) == 1 else "s"))
    lines = ["unarmed goals: " + " · ".join(parts)]
    for row in unarmed:
        lines.append("    · %s — %d live session%s — no manager record under either name"
                     % (row["goal"], len(row["sessions"]),
                        "" if len(row["sessions"]) == 1 else "s"))
    for row in no_subject:
        lines.append("    · NO-SUBJECT — %s — %d live session%s — carries neither goals: nor topics:"
                     % (row["task"], len(row["sessions"]),
                        "" if len(row["sessions"]) == 1 else "s"))
    return "\n".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Goals with live sessions and no armed manager (fleet/manager loop)")
    parser.add_argument("--vault", required=True, help="the vault ROOT path")
    parser.add_argument("--tasks-dir", required=True, help="the vault's task directory")
    parser.add_argument("--registry-dir", default=None, help="override the session registry path")
    parser.add_argument("--heartbeat-dir", default=None, help="override the heartbeat store")
    parser.add_argument("--now", default=None, help="ISO-8601 'now' for tests")
    parser.add_argument("--json", action="store_true", help="emit the rows as JSON")
    args = parser.parse_args(argv)

    if not os.path.isdir(args.tasks_dir):
        print("❌ tasks dir not found: %s" % args.tasks_dir, file=sys.stderr)
        return 1
    if args.now and parse_iso(args.now) is None:
        print("❌ --now is not an ISO-8601 timestamp: %s" % args.now, file=sys.stderr)
        return 1

    live = read_live(args.registry_dir, args.heartbeat_dir)
    if live is None:
        # The third state. Never `0` — an unreadable registry and an empty fleet must not
        # render the same, because only one of them is a fact about the fleet.
        print("unarmed goals: unknown")
        print(
            "⚠️ session registry unreadable — cannot tell which goals carry live sessions; "
            "this is not a count of 0",
            file=sys.stderr,
        )
        return 2

    unarmed, no_subject = collect(args.tasks_dir, live)

    if args.json:
        json.dump(
            {"unarmed_goals": unarmed, "no_subject": no_subject,
             "count": len(unarmed) + len(no_subject)},
            sys.stdout, indent=2,
        )
        sys.stdout.write("\n")
        return 0

    print(render(unarmed, no_subject))
    return 0


if __name__ == "__main__":
    sys.exit(main())
