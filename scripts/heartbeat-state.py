#!/usr/bin/env python3
"""Record what a session is doing, for the heartbeat timer to read.

The supervisor's heartbeat timer stamps a session every 30 s so any process can decide
liveness from the stamp's age alone. Age answers *is it alive*; it cannot answer *what is it
doing*, because the timer fires on a clock rather than on an event. This script is the event
half: a Claude Code hook calls it, it records the state, and the next timer tick carries that
state onto the stamp.

    UserPromptSubmit -> busy                  (a turn started)
    Stop             -> idle                  (the turn ended, waiting for the next prompt)
    Notification     -> waiting-on-operator   (a permission prompt or question is open)

Usage (from hooks/hooks.json):

    heartbeat-state.py busy

⚠️ **Fail-open, and silent.** This runs inside the operator's turn on every prompt and every
stop. A hook that blocks, prompts, or exits non-zero degrades the session it is meant to
observe — so every failure path here returns 0 having done nothing. The cost of a missed
write is one stamp reporting a stale state for up to 30 s, which is strictly better than a
hook that stalls a turn.

⚠️ **The state is passed as an ARGUMENT, not read from the hook payload.** The manifest entry
already knows which event it is; making the script infer it from `hook_event_name` would add a
dependency on a field this repo has never verified is present. An unknown argument writes
nothing rather than guessing a state.

⚠️ **A file per session, in a SIBLING directory of the heartbeat store — never inside it.**
Everything in the store directory is a stamp, read for its age and listed as a session; a
state file living there would be listed as a session that does not exist.
"""

import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

# The vocabulary the heartbeat contract names. An argument outside it is a caller bug, not a
# value to repair — the same rule the attention store applies to a malformed session id.
ALLOWED_STATES = ("busy", "idle", "waiting-on-operator")

# What a session id may look like before it becomes a FILENAME.
#
# ⚠️ The id arrives from the hook payload, so it is untrusted input on a path-join. Without
# this, a `../` component or a leading `/` writes outside the state directory — and the read
# side (`readState` in `server/heartbeat.mjs`) has the same shape, so the guard is applied on
# both. Shape-only rather than a UUID pattern, matching `validateSessionID` in
# attention-controller: a stricter rule would reject ids the registry genuinely holds.
#
# Mirrors the precedent in `scripts/resolve-task-file.py`, which refuses absolute paths and
# `..` components for the same reason.
SESSION_ID_PATTERN = re.compile(r"^[A-Za-z0-9._-]+$")


def safe_session_id(raw: str) -> str:
    """The id if it can safely become a filename, else the empty string.

    A bare `.` or `..` passes the character class and must be refused explicitly — it is a
    valid path component that resolves to a directory rather than a file.
    """
    if not raw or raw in (".", ".."):
        return ""
    if not SESSION_ID_PATTERN.fullmatch(raw):
        return ""
    return raw


def state_dir() -> Path:
    """The directory the timer reads.

    Mirrors `heartbeatStateDir` in `server/config.mjs` — same XDG resolution, same override
    name — because a hook and the timer disagreeing about the path would look exactly like a
    hook that never fired.
    """
    override = os.environ.get("SUPERVISOR_HEARTBEAT_STATE_DIR")
    if override:
        return Path(override)
    state_home = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(state_home) / "claude-supervisor" / "heartbeat-state"


def session_id_from_stdin() -> str:
    """The calling session's id, from the hook JSON Claude Code writes to stdin.

    ⚠️ Read only when stdin is not a TTY. Run by hand — which is how this gets tested — stdin
    is the terminal, and `json.load` would block waiting for a line that never comes.
    """
    if sys.stdin.isatty():
        return ""
    try:
        payload = json.load(sys.stdin) or {}
    except (ValueError, OSError):
        return ""
    if not isinstance(payload, dict):
        return ""
    return str(payload.get("session_id") or "")


def main(argv: list[str]) -> int:
    state = argv[1] if len(argv) > 1 else ""
    if state not in ALLOWED_STATES:
        return 0

    session_id = safe_session_id(session_id_from_stdin())
    if not session_id:
        # No usable id means there is nothing to key the record on — and an id carrying a path
        # separator is the one input that could write outside this directory. Writing under a
        # guess would put a row in the store that no session can clear.
        return 0

    try:
        directory = state_dir()
        directory.mkdir(parents=True, exist_ok=True)
        # Written via a temporary file and renamed, matching `stampRecord`: the timer reads
        # this file on a clock, and a reader must never see a half-written record.
        target = directory / f"{session_id}.json"
        tmp = target.with_suffix(".json.tmp")
        record = {
            "session_id": session_id,
            "state": state,
            "at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        }
        tmp.write_text(json.dumps(record) + "\n", encoding="utf-8")
        tmp.replace(target)
    except OSError:
        # An unwritable state directory must not fail the turn. See the fail-open note above.
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
