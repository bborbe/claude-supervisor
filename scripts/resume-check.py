#!/usr/bin/env python3
"""Did a resume actually run a turn? -- answered from the transcript, never from a claim.

WHY. `spawn_agent(resume=<id>, interactive=false)` returns `running` with a wrapper id the
moment the child starts, and that response is identical whether the resumed conversation
goes on to do work or never runs a turn at all. Measured 2026-11-05: three resumes issued in
one batch, two of them (agent_12 `bcefec80`, agent_13 `f43196c4`) came back with
`num_turns: 0` and an empty result while agent_14 (`5cceb1ea`) worked -- and every call had
looked the same at the time. A manager that reads only the spawn response records an action
it did not take.

The cause then was the JS supervisor's idle end-of-life: on resume the CLI queues its own
`<task-notification>` for orphaned background agents ahead of the caller's prompt, that
synthetic input settles as a zero-turn `result`, and the session was stopped before the
prompt ran. That supervisor is retired (the daemon is Go inside claude-wrapper since
2026-11-08) and has no end-of-turn reaper, so the specific stop is gone -- but the
observability gap is not: the live daemon reports no turn count at all. This script is the
answer that survives both implementations, because the transcript is written by the CLI.

WHAT IT READS. `<projects>/*/<session-id>.jsonl`, by session id (never by cwd), deduplicated
by inode -- the same reader rule `progress-window.py` follows. A record counts as evidence of
a turn when it is an `assistant` record carrying content, or a `result` record with
`num_turns > 0`. `<task-notification>` user records and zero-turn results are the synthetic
input that produced the false green, so they are counted separately and never as a turn.

VERDICTS
  productive     at least one turn in the window -- the resume did work
  zero-turn      the window holds synthetic input and no turn -- the resume ran nothing
  no-evidence    no transcript, or nothing recorded in the window at all
  running        the transcript's last record is not a turn boundary and no turn has landed
                 yet -- too early to call, ask again

Usage:
  resume-check.py --session <id> [--since <ISO8601>] [--until <ISO8601>] [--json]
                  [--fail-on-zero-turn]

`--since` defaults to the session's first record in the transcript, so a whole conversation
is measured; pass the resume time to measure only what came after it, and `--until` to
bound it above -- a transcript keeps growing on later resumes, so an unbounded window
attributes that later work to the resume being judged. Exit status is 0 for
every verdict unless `--fail-on-zero-turn` is given, which exits 1 on `zero-turn` -- a
manager loop wants that, a human reading a table does not.

Root: `SUPERVISOR_PROJECTS_DIR`, defaulting under `~/.claude/projects`.
"""

import argparse
import glob
import json
import os
import sys
from datetime import datetime, timezone


def parse_time(value):
    if not value:
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def record_time(record):
    stamp = record.get("timestamp")
    if not stamp:
        return None
    try:
        return parse_time(stamp)
    except ValueError:
        return None


def transcript_paths(session_id, projects_root):
    """Every file for this session id, deduplicated by inode (a symlinked project dir matches twice)."""
    seen = set()
    paths = []
    for path in glob.glob(os.path.join(projects_root, "*", f"{session_id}.jsonl")):
        try:
            stat = os.stat(path)
        except OSError:
            continue
        key = (stat.st_dev, stat.st_ino)
        if key in seen:
            continue
        seen.add(key)
        paths.append(path)
    return sorted(paths)


def classify(record):
    """One record's contribution: a turn, synthetic input, or neither."""
    kind = record.get("type")
    if kind == "assistant":
        message = record.get("message") or {}
        content = message.get("content")
        if isinstance(content, str) and content.strip():
            return "turn"
        if isinstance(content, list) and content:
            return "turn"
        return "other"
    if kind == "result":
        try:
            turns = int(record.get("num_turns") or 0)
        except (TypeError, ValueError):
            turns = 0
        return "turn" if turns > 0 else "synthetic"
    if kind == "user":
        message = record.get("message") or {}
        content = message.get("content")
        text = content if isinstance(content, str) else json.dumps(content)
        if "<task-notification>" in text:
            return "synthetic"
    return "other"


def read_records(path):
    records = []
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return records


def measure(session_id, since, projects_root, until=None):
    paths = transcript_paths(session_id, projects_root)
    if not paths:
        return {"session_id": session_id, "verdict": "no-evidence", "reason": "no transcript", "transcript": None}
    path = paths[0]
    records = read_records(path)
    if not records:
        return {"session_id": session_id, "verdict": "no-evidence", "reason": "empty transcript", "transcript": path}

    stamped = [(record_time(r), r) for r in records]
    stamped = [(when, r) for when, r in stamped if when is not None]
    window_start = since or (stamped[0][0] if stamped else None)
    in_window = [(when, r) for when, r in stamped
                if (window_start is None or when >= window_start) and (until is None or when <= until)]

    turns = 0
    synthetic = 0
    last_turn_at = None
    last_at = None
    for when, record in in_window:
        last_at = when
        kind = classify(record)
        if kind == "turn":
            turns += 1
            last_turn_at = when
        elif kind == "synthetic":
            synthetic += 1

    if turns:
        verdict, reason = "productive", f"{turns} turn record(s) in the window"
    elif synthetic:
        verdict, reason = "zero-turn", f"{synthetic} synthetic record(s) and no turn in the window"
    elif in_window:
        verdict, reason = "running", "records in the window but no turn yet"
    else:
        verdict, reason = "no-evidence", "nothing recorded in the window"

    return {
        "session_id": session_id,
        "transcript": path,
        "since": window_start.isoformat() if window_start else None,
        "until": until.isoformat() if until else None,
        "last_record_at": last_at.isoformat() if last_at else None,
        "last_turn_at": last_turn_at.isoformat() if last_turn_at else None,
        "turns": turns,
        "synthetic": synthetic,
        "verdict": verdict,
        "reason": reason,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description="Did a resume actually run a turn?")
    parser.add_argument("--session", required=True, help="full session id (UUID)")
    parser.add_argument("--since", default=None, help="ISO-8601; defaults to the transcript's first record")
    parser.add_argument("--until", default=None, help="ISO-8601; defaults to no upper bound")
    parser.add_argument("--json", action="store_true", help="print the reading as JSON")
    parser.add_argument("--fail-on-zero-turn", action="store_true", help="exit 1 when the verdict is zero-turn")
    parser.add_argument("--projects-root", default=os.environ.get("SUPERVISOR_PROJECTS_DIR") or os.path.expanduser("~/.claude/projects"))
    args = parser.parse_args(argv)

    result = measure(args.session, parse_time(args.since), args.projects_root, parse_time(args.until))
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print(f"{result['session_id']}: {result['verdict']} — {result['reason']}")
    if args.fail_on_zero_turn and result["verdict"] == "zero-turn":
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
