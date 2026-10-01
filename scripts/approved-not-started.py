#!/usr/bin/env python3
"""Approved-but-unstarted tasks — the count, the oldest age, and the rows over one tick.

Why this exists
---------------
Measured 2026-10-01 21:00-21:40: approved rows sat unopened (`Build claude-interactive`
since 14:06, `Stop the Interactive Agent Pod Dying` since 21:25, Manager Layer's four
answered approvals) and no fleet round showed it. The operator noticed only by asking
"we have still not more task open".

The sweep classifies **live sessions**, so it is structurally blind to a row nobody ever
opened — there is no session to sweep from. This is the reverse index for that gap, in
the shape `orphan-candidates.py` already uses for dead-session work: it walks the other
direction, from the task files.

What counts as "approved, not started"
--------------------------------------
A row is **approved** when its frontmatter carries `approved_at`. That field is written
by `vault-cli task approve` — and only by it — in every vault, so it is the vault's own
record of the operator's approval rather than a proxy inferred from `status`/`phase`.

⚠️ **This is deliberately NOT the runbook's "approved and unowned" clause.**
`agents/manager-sweep-reader.md` forbids hard-coding *that* predicate, because the two
vaults' runbooks disagree about it (the primary vault reads `status: next`, the other is
phase-based) — it decides which rows a manager may **spawn** on. This script answers a
different question, and answers it from the approval's own record, so that disagreement
does not reach it. Do not "fix" this to read the runbook clause; doing so would make the
reporting line vault-dependent for no gain.

A row is **not started** when no LIVE session owns it: its `claude_session_id` is absent,
or names a session the liveness authority does not report live. A recorded id is not an
owner — measured 2026-09-30, 127 of the primary vault's 557 `phase: todo` rows carried a
`claude_session_id` whose session had already ended.

Liveness is delegated, never re-derived
---------------------------------------
`session-liveness.py` is the plugin's single liveness instrument (registry + heartbeat,
one answer). This script calls it once with `--list --json` and does a set membership
test. It never globs `~/.claude/sessions` itself: two readers over one directory is the
defect `session-liveness.py`'s own header records, and it does not become acceptable by
being written twice in one repo instead of twice in one week.

⚠️ **A failed read is `unknown`, never `0`.** Per `docs/pane-reads.md`, a failed transport
read must never be representable as an empty result. If the registry cannot be read this
script prints `unknown` and exits non-zero, because "0 approved rows" and "I could not
ask" must not render the same. The probe is `--registry-dir /nonexistent`.

Frontmatter only
----------------
Task bodies quote these keys in prose, so a whole-file scan reads a task as approved on
the strength of a sentence *about* approval. Same rule, same reason as
`orphan-candidates.py`.
"""
import argparse
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone

# One tick. A row waiting longer than this is listed with its owning manager; the tick is
# a constant rather than a flag because both callers (fleet-loop, manager-loop) must agree
# on it, and a per-call override is how two rounds start disagreeing about the same row.
TICK_MINUTES = 30

# Non-terminal scheduling states. A row that is completed, aborted, on hold or parked in
# backlog is not "waiting to start" whatever its approval record says.
LIVE_STATUSES = {"next", "todo", "in_progress", ""}

FRONTMATTER = re.compile(r"\A---\n(.*?)\n---\n", re.DOTALL)

HERE = os.path.dirname(os.path.abspath(__file__))
SESSION_LIVENESS = os.path.join(HERE, "session-liveness.py")


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


def format_age(seconds):
    """`45m` / `7h35m` / `2d3h` — the coarse shape an operator scans."""
    minutes = max(0, int(seconds)) // 60
    if minutes < 60:
        return "%dm" % minutes
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return "%dh%02dm" % (hours, minutes)
    days, hours = divmod(hours, 24)
    return "%dd%dh" % (days, hours)


def read_live(registry_dir=None, heartbeat_dir=None):
    """The live session records, or None when the liveness read failed.

    Delegates to `session-liveness.py --list --json`, whose exit code is the three-state
    answer: 0 means both halves were read, non-zero means at least one could not be — and
    a partial list presented as complete reads as "this session is not live" for every
    session in the half that failed. `None` preserves that; it is never collapsed to `[]`.
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


def manager_for(goals, live_by_name):
    """The live manager session for a row, or None.

    Convention, not invention: a session whose registry name matches a goal title (a
    `<X> Manager` suffix allowed) is that subject's manager — the same rule
    `fleet-board.py` uses to place a session under its parent. A row whose goal has no
    live manager reads `unmanaged`, which is a fact about the fleet, not a failure.
    """
    for goal in goals:
        title = goal.strip().strip("[]").strip()
        for candidate in (title, title + " Manager"):
            if live_by_name.get(candidate.lower()):
                return candidate
    return None


def collect(tasks_dir, live, now):
    """Every approved-not-started row, oldest first.

    `live` is never None here — the caller refuses before this runs, so an unreadable
    registry can never reach the point where it would be indistinguishable from an empty
    fleet.
    """
    live_ids = {(r.get("sessionId") or "").lower() for r in live if r.get("alive", True)}
    live_by_name = {}
    for rec in live:
        name = (rec.get("name") or "").strip().lower()
        if name and rec.get("alive", True):
            live_by_name.setdefault(name, rec.get("sessionId"))

    rows = []
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
        approved = parse_iso(field(block, "approved_at"))
        if approved is None:
            continue
        if (field(block, "status") or "") not in LIVE_STATUSES:
            continue
        sid = (field(block, "claude_session_id") or "").strip().lower()
        if sid and sid in live_ids:
            continue  # a live session owns it — started
        rows.append(
            {
                "name": entry[:-3],
                "approved_at": approved.isoformat(),
                "age_seconds": (now - approved).total_seconds(),
                "manager": manager_for(list_field(block, "goals"), live_by_name),
            }
        )
    rows.sort(key=lambda r: r["age_seconds"], reverse=True)
    return rows


def render(rows, tick_minutes):
    """The round's line, plus the over-tick block."""
    if not rows:
        return "approved, not started: 0"
    oldest = rows[0]
    head = "approved, not started: %d · oldest %s (%s)" % (
        len(rows),
        format_age(oldest["age_seconds"]),
        oldest["name"],
    )
    over = [r for r in rows if r["age_seconds"] > tick_minutes * 60]
    if not over:
        return head
    lines = [head, "  over one tick (%dm):" % tick_minutes]
    for row in over:
        lines.append(
            "    · %s — %s — manager %s"
            % (row["name"], format_age(row["age_seconds"]), row["manager"] or "unmanaged")
        )
    return "\n".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Approved-but-unstarted tasks (fleet/manager loop)")
    parser.add_argument("--tasks-dir", required=True, help="the vault's task directory")
    parser.add_argument("--registry-dir", default=None, help="override the session registry path")
    parser.add_argument("--heartbeat-dir", default=None, help="override the heartbeat store")
    parser.add_argument(
        "--tick-minutes",
        type=int,
        default=TICK_MINUTES,
        help="rows older than this are listed (default %d)" % TICK_MINUTES,
    )
    parser.add_argument("--now", default=None, help="ISO-8601 'now' for tests")
    parser.add_argument("--json", action="store_true", help="emit the rows as JSON")
    args = parser.parse_args(argv)

    if not os.path.isdir(args.tasks_dir):
        print("❌ tasks dir not found: %s" % args.tasks_dir, file=sys.stderr)
        return 1

    now = parse_iso(args.now) if args.now else datetime.now(timezone.utc)
    if now is None:
        print("❌ --now is not an ISO-8601 timestamp: %s" % args.now, file=sys.stderr)
        return 1

    live = read_live(args.registry_dir, args.heartbeat_dir)
    if live is None:
        # The third state. Never `0` — an unreadable registry and an empty fleet must not
        # render the same, because only one of them is a fact about the fleet.
        #
        # The line goes to BOTH streams on purpose. `docs/pane-reads.md`'s third response
        # is "already exits non-zero on failure — refuse non-zero, naming the transport",
        # and the exit code is the signal; but a caller that captured only stdout would
        # otherwise render nothing at all, which reads as a clean round rather than a
        # failed one. The line is honest on either stream and is never a number.
        print("approved, not started: unknown")
        print(
            "⚠️ session registry unreadable — cannot tell started from unstarted; "
            "this is not a count of 0",
            file=sys.stderr,
        )
        return 2

    rows = collect(args.tasks_dir, live, now)

    if args.json:
        json.dump({"rows": rows, "tick_minutes": args.tick_minutes, "count": len(rows)}, sys.stdout, indent=2)
        sys.stdout.write("\n")
        return 0

    print(render(rows, args.tick_minutes))
    return 0


if __name__ == "__main__":
    sys.exit(main())
