#!/usr/bin/env python3
"""Persist a fleet snapshot to ~/.claude/state/fleet-snapshot.json (fleet-manager loop).
stdin: JSON — either the sessions dict {"<ref>": {...}} or {"scope": "...", "sessions": {...}}.
swept_at is stamped at write time.
"""
import sys, json, os, datetime

payload = json.load(sys.stdin)
if isinstance(payload, dict) and "sessions" in payload:
    scope = payload.get("scope", "all")
    sessions = payload["sessions"]
else:
    scope = "all"
    sessions = payload

data = {
    "swept_at": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    "scope": scope,
    "sessions": sessions,
}
path = os.path.expanduser("~/.claude/state/fleet-snapshot.json")
os.makedirs(os.path.dirname(path), exist_ok=True)
with open(path, "w") as f:
    json.dump(data, f, indent=2)
print("snapshot written:", data["swept_at"], "-", len(sessions), "sessions")
