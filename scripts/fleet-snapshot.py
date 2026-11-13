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

# ⚠️ `FLEET_SNAPSHOT_PATH` overrides, matching the env knobs the rest of the store
# family already honours (`ATTENTION_STATE_DIR` in `who-needs-me.py`, `declared-wait.py`,
# `fleet-board.py`, `manager-predispatch.py`). Without it a replay cannot seed a prior
# snapshot without writing the LIVE one, so the `stalled` rule — which needs
# `stall_count >= 2` from a previous sweep — is unexercisable outside a real fleet.
PATH = os.environ.get("FLEET_SNAPSHOT_PATH") or os.path.expanduser(
    "~/.claude/state/fleet-snapshot.json"
)


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


LABEL = "snapshot written:"


def compose_header(session_count, swept_at):
    """Compose a round's `.digest` first line — the one place the label is written.

    Measured 2026-11-05: a round's first line read
    `snapshot written: snapshot written: 2026-11-05T17:34:30Z - 43 sessions`.
    The sweep prompt told the reader to quote `fleet-snapshot.py`'s own
    already-labelled stdout line into a template slot that carried the label
    itself, so two instructions in one prompt disagreed and the agent followed
    the wrong one. Composing the line here removes the disagreement: the prompt
    pastes this line verbatim and composes only from line 2 onward.

    `swept_at` is unwrapped before it is used. A caller that hands back a whole
    labelled line must still yield one label — the historical doubling is
    exactly that input, and it is pinned by a test.

    The three ASCII markers `DIGEST `, `sessions` and `snapshot written:` are the
    shape `scripts/manager-receipt.py` reads a digest by, so none is optional.
    """
    value = swept_at.strip()
    while value.lower().startswith(LABEL):
        value = value[len(LABEL):].strip()
    return "DIGEST %s · %d sessions · %s %s" % (value, session_count, LABEL, value)


def write_digest_marker(swept_at, digest_path, session_count):
    """Name `swept_at` in the round's `.digest` at the instant the snapshot is taken.

    The watcher judges freshness from the newest `.digest` on disk, so a round whose
    file lands after its own `snapshot written:` line reads as a lapse even when the
    round ran on time. Measured 2026-10-22: `snapshot written: 15:43:58Z`, the file
    on disk at `15:46:28Z`, and the `15:44:30Z` tick reported `STALE … age=1083s` on
    a round whose successor gap was 1051 s — inside the 1080 s limit.

    The sweep reader writes the full render at the end of its own turn, so the gap
    between the stamp and the file is that turn's remaining work, not the drive leg.
    Writing the marker here — inside the same call that persists the snapshot, and
    atomically, so a reader never sees a partial file — makes the file's arrival and
    the baseline it carries the same instant. The round's full render later replaces
    this path and carries the identical `snapshot written:` value.

    When `session_count` is given the marker is the round's complete first line
    (`compose_header`), so the file lands with the same shape the render later
    replaces it with.

    Returns the path written, or None when no digest path was given.
    """
    if not digest_path:
        return None
    parent = os.path.dirname(digest_path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    line = compose_header(session_count, swept_at)
    tmp = digest_path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(line + "\n")
        os.replace(tmp, digest_path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
    return digest_path


def main(stdin=None, path=None, digest_path=None):
    digest_path = digest_path or os.environ.get("FLEET_SNAPSHOT_DIGEST_PATH")
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
    write_digest_marker(data["swept_at"], digest_path, len(sessions))
    print("snapshot written:", data["swept_at"], "-", len(sessions), "sessions")
    return 0


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--path", default=None, help="snapshot path (default: FLEET_SNAPSHOT_PATH)")
    parser.add_argument(
        "--digest-path",
        default=None,
        help="this round's .digest; its complete first line is written there at "
        "stamp time (default: FLEET_SNAPSHOT_DIGEST_PATH)",
    )
    args = parser.parse_args()
    sys.exit(main(path=args.path, digest_path=args.digest_path))
