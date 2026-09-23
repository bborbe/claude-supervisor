#!/usr/bin/env python3
"""Persist a fleet snapshot to ~/.claude/state/fleet-snapshot.json (fleet-loop loop).
stdin: JSON — the sessions dict {"<session id>": {...}}, or {"sessions": {...}}.
Key on the session id (`sessionId` from ~/.claude/sessions/<pid>.json), never on `[ref]`:
`[ref]` is computed per roster read, so an unchanged session would read as vanished-and-new
between sweeps — and diffing one sweep against the next is this file's whole job.
swept_at is stamped at write time.

A `scope` field was recorded here until 2026-09-19, so a sweep could be compared only against
a previous sweep taken at the same scope. The roster is now always machine-wide, so there is
no scope to record and none to mismatch.
"""
import sys, json, os, datetime

payload = json.load(sys.stdin)
sessions = payload["sessions"] if isinstance(payload, dict) and "sessions" in payload else payload

data = {
    "swept_at": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    "sessions": sessions,
}
path = os.path.expanduser("~/.claude/state/fleet-snapshot.json")
os.makedirs(os.path.dirname(path), exist_ok=True)
with open(path, "w") as f:
    json.dump(data, f, indent=2)
print("snapshot written:", data["swept_at"], "-", len(sessions), "sessions")
