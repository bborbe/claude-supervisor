#!/usr/bin/env python3
"""Persist a fleet snapshot to ~/.claude/state/fleet-snapshot.json (fleet-loop loop).
stdin: JSON — the sessions dict {"<session id>": {...}}, or {"sessions": {...}}.
Key on the session id (`sessionId` from ~/.claude/sessions/<pid>.json), never on `[ref]`:
`[ref]` is computed per roster read, so an unchanged session would read as vanished-and-new
between sweeps — and diffing one sweep against the next is this file's whole job.
swept_at is stamped at write time.

An **empty** session list is refused, with a non-zero exit and the existing snapshot
left byte-identical. Writing it would replace a real snapshot with an empty one and
still exit 0, so the next round diffs against nothing: every session reads first-seen,
`stall_count` resets, and the loss is attributed to the fleet rather than to the read
that failed. Measured 2026-09-23 (v0.39.1): a sweep whose roster read came back empty
overwrote the snapshot and reported `snapshot written: … - 0 sessions` as success.
A failed read and a genuinely empty fleet must not produce the same file — the sweep
can recover from a refused write, never from a clobbered snapshot.

A `scope` field was recorded here until 2026-09-19, so a sweep could be compared only against
a previous sweep taken at the same scope. The roster is now always machine-wide, so there is
no scope to record and none to mismatch.
"""
import sys, json, os, datetime

PATH = os.path.expanduser("~/.claude/state/fleet-snapshot.json")


def extract_sessions(payload):
    """The sessions dict, from either {"sessions": {...}} or the bare dict/list."""
    if isinstance(payload, dict) and "sessions" in payload:
        return payload["sessions"]
    return payload


def entries_are_objects(sessions):
    """Every entry must be an object, per the documented schema `{"<id>": {...}}`.

    An empty guard alone is not enough: a failed read that returns an error
    envelope rather than nothing — `{"error": "roster read failed"}` — is
    non-empty, so it passed the guard, was written as a one-session snapshot,
    and exited 0. That is the same collapse the empty guard exists to prevent,
    reached by a payload that merely *looks* like data.

    The value shape is the discriminator, not the key shape. Real keys are
    UUIDs, but live snapshots also carry a `…dup` suffix (measured 2026-09-23:
    1 of 45 keys), so key-format validation would reject production data. Every
    real entry is an object; an error string, a scalar, or a bare `null` is not.
    """
    if isinstance(sessions, dict):
        return all(isinstance(v, dict) for v in sessions.values())
    if isinstance(sessions, list):
        return all(isinstance(v, dict) for v in sessions)
    return False


def write_snapshot(sessions, path=None, swept_at=None):
    """Write the snapshot atomically (tmp + os.replace) and return the document.

    The replace is the same discipline `open-items.py` already documents this
    script as following, and until now this script did not: a plain `open(path,
    "w")` truncates first, so a crash or a serialisation error part-way through
    leaves a truncated or zero-byte snapshot where a valid one stood. That is
    the same loss as writing an empty payload — the next round diffs against
    nothing — reached by a different route, which is why the guard alone is not
    enough. `os.replace` is atomic within a filesystem: a reader sees either the
    old snapshot or the new one, never a half-written file.
    """
    path = path or PATH
    swept_at = swept_at or datetime.datetime.now(datetime.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    data = {"swept_at": swept_at, "sessions": sessions}
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    tmp = path + ".tmp"
    try:
        with open(tmp, "w") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
    return data


def main(stdin=None, path=None):
    payload = json.load(stdin if stdin is not None else sys.stdin)
    sessions = extract_sessions(payload)
    if not sessions:
        print(
            "refusing to write an empty snapshot: the payload carries no sessions. "
            "A failed read and an empty fleet must not look alike; the existing "
            "snapshot is left unchanged.",
            file=sys.stderr,
        )
        return 1
    if not entries_are_objects(sessions):
        print(
            "refusing to write a malformed snapshot: an entry is not an object, so "
            'this payload did not come from a roster read (schema: {"<session id>": '
            '{"...": ...}}). The existing snapshot is left unchanged.',
            file=sys.stderr,
        )
        return 1
    data = write_snapshot(sessions, path=path)
    print("snapshot written:", data["swept_at"], "-", len(sessions), "sessions")
    return 0


if __name__ == "__main__":
    sys.exit(main())
