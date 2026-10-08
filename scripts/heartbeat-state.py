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
    SessionEnd       -> clear                 (the session is over — unlink its record)

Usage (from hooks/hooks.json):

    heartbeat-state.py busy
    heartbeat-state.py clear

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
# this, a `../` component or a leading `/` writes outside the state directory. The guard is
# applied on the CLEAR path as well as the write path, because both build a filename from the
# id. ⚠️ **The read side of this contract is `readState` in `server/heartbeat.mjs`**, which is
# already on this tree: it reads `<heartbeatStateDir>/<id>.json` and drops a value outside
# `SESSION_STATES` rather than passing it on. Shape-only rather than a UUID pattern, matching
# `validateSessionID` in attention-controller: a stricter rule would reject ids the registry
# genuinely holds.
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

    ⚠️ The XDG resolution and the override name match what the supervisor server uses, so a
    hook and the stamp timer cannot disagree about the path — a disagreement would look
    exactly like a hook that never fired. ⚠️ **The server-side constant that mirrors this is
    `heartbeatStateDir` in `server/config.mjs`**, which is already on this tree. The resolution
    is identical either way — that file builds `STATE_HOME`/`STATE_DIR` the same way.
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

    ⚠️ Catches `Exception`, not `(ValueError, OSError)`. `json.load` raises `RecursionError`
    on deeply-nested input, which is not a `ValueError` — so a narrower handler would let it
    escape to the caller, print a traceback and exit 1, which is the one outcome this script's
    fail-open contract rules out. The docstring claims EVERY failure path returns 0; that claim
    has to hold for exception classes nobody enumerated, not just the two that were obvious.
    """
    if sys.stdin.isatty():
        return ""
    try:
        payload = json.load(sys.stdin) or {}
    except Exception:
        return ""
    if not isinstance(payload, dict):
        return ""
    return str(payload.get("session_id") or "")


def main(argv: list[str]) -> int:
    state = argv[1] if len(argv) > 1 else ""

    # ⚠️ The kill switch, mirroring `SUPERVISOR_PERMISSION_POLL=off` on the sibling
    # `PermissionRequest` hook in the same manifest. Without it the only way to stop four
    # `python3` spawns per turn — for EVERY session on the machine, `Notification` included —
    # is to edit a plugin manifest, which is not a lever an operator can reach in the moment.
    # Checked first, so "off" costs one `getenv` and nothing else.
    if os.environ.get("SUPERVISOR_HEARTBEAT_STATE") == "off":
        return 0

    # `clear` is a COMMAND, not a state: the SessionEnd hook calls it so a finished session
    # leaves nothing behind. ⚠️ Without it the directory grows monotonically — this hook fires
    # for every session on the machine, and a record that is never unlinked outlives the
    # session it describes. The sibling heartbeat store has a `sweepStale` for the same reason
    # ("so a dead server's stamps do not accumulate"); this is that remedy on the write side,
    # which is cheaper than a sweep because the session knows when it is ending.
    if state == "clear":
        session_id = safe_session_id(session_id_from_stdin())
        if not session_id:
            return 0
        try:
            (state_dir() / f"{session_id}.json").unlink(missing_ok=True)
        except Exception:
            # Fail-open, same as every other path: an unclearable record costs a stale file,
            # which is strictly better than failing the turn that is ending.
            return 0
        return 0

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
        #
        # ⚠️ The temp name carries the PID, and is built with `with_name` rather than
        # `with_suffix`. A per-session name alone is NOT enough for the atomicity claimed
        # above: two hooks for one session can overlap — `Stop` and `Notification` land
        # together, and `UserPromptSubmit` fires per prompt — and `write_text` truncates before
        # it writes, so the second writer would truncate the first's file, the first would
        # rename it into place, and the published record would be the second's partial
        # content. `with_suffix` is also the wrong operation for the job: it REPLACES the final
        # suffix rather than appending, so it happens to be correct here only because the
        # target is always `<id>.json`.
        target = directory / f"{session_id}.json"
        tmp = target.with_name(f".{target.name}.{os.getpid()}.tmp")
        record = {
            "session_id": session_id,
            "state": state,
            "at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        }
        try:
            tmp.write_text(json.dumps(record) + "\n", encoding="utf-8")
            tmp.replace(target)
        finally:
            # A failure between the write and the rename must not leave the temp behind. The
            # happy path is covered by `test_no_leftover_temp_file`; the crash path is exactly
            # the case this file exists to care about.
            tmp.unlink(missing_ok=True)
    except Exception:
        # An unwritable state directory must not fail the turn. See the fail-open note above.
        #
        # ⚠️ `Exception`, not `OSError` — and the reason is the CONTRACT, not a named case. The
        # module docstring claims EVERY failure path returns 0, and a claim of *every* has to
        # hold for exception classes nobody enumerated, which is the same reasoning that
        # widened `session_id_from_stdin`. An `OSError`-only handler leaves any other exception
        # escaping `main` to print a traceback and exit 1 — the one outcome this contract rules
        # out, on a hook that runs inside the operator's turn. An earlier revision justified
        # the widening with "a `ValueError` from a path containing a null byte"; that case is
        # unreachable, because `safe_session_id` runs first and its character class rejects a
        # null byte. It is named here as a rejected example rather than a motivating one.
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
