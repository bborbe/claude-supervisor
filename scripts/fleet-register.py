#!/usr/bin/env python3
"""Write the Fleet Manager's `worker-manager` registration.

⚠️ **The registration, not the cadence, is what `scripts/answer-path-check.py` reads.** That
check resolves a session's `subject` and `branch` from `~/.claude/state/worker-manager/
<session-id>.json` and refuses at that gate before it ever asks `manager-liveness.py` anything.
Arming a `fleet--fleet-manager` cadence alone therefore changes nothing: the probe still says
`no manager registration`, and every headless spawn the Fleet Manager makes is still refused
(measured 2026-10-23 — this file exists because the cadence-only version of that fix did not
work, and the reviewer was right to block it).

⚠️ **Why the fleet tier writes its own record rather than borrowing the subject-resolution
block.** That block (`docs/subject-resolution.md` § Recording) resolves a GOAL or TOPIC page
and writes its name. The Fleet Manager manages no subject page, so there is nothing to resolve
— its subject is the fixed string `Fleet Manager` and its branch is `fleet`. Borrowing the
block would either fail to resolve or file a non-subject into the goal/topic namespace.

Idempotent: rewriting an existing registration with the same content is a no-op in effect, and
re-running it every round is harmless — the file carries `resolved_at`, which is meant to move.

Usage:
  fleet-register.py [--session-id <id>] [--vault <vault>]
"""
import argparse
import datetime
import json
import os
import sys

SUBJECT = "Fleet Manager"
BRANCH = "fleet"

MANAGER_DIR = os.path.expanduser(
    os.environ.get("SUPERVISOR_WORKER_MANAGER_DIR")
    or os.environ.get("WORKER_MANAGER_STATE_DIR")
    or "~/.claude/state/worker-manager"
)


def bare_filename(session_id):
    """True when the id can be joined into a path without escaping MANAGER_DIR."""
    return bool(session_id) and session_id not in (".", "..") and not any(
        ch in session_id for ch in ("/", "\\", "\x00")
    )


def write_registration(session_id, vault, directory=None, now=None):
    """Write the registration; returns the path. Refuses a non-bare session id."""
    if not bare_filename(session_id):
        raise ValueError(f"session id {session_id!r} is not a bare filename")
    target_dir = directory or MANAGER_DIR
    os.makedirs(target_dir, exist_ok=True)
    payload = {
        "subject": SUBJECT,
        "branch": BRANCH,
        "vault": vault,
        "resolved_at": (now or datetime.datetime.now(datetime.timezone.utc)).isoformat(),
    }
    path = os.path.join(target_dir, f"{session_id}.json")
    # ⚠️ Temp file + `os.replace`, matching `manager-liveness.py`'s `write_atomic` and every
    # sibling state writer here. This is the one file whose whole purpose is to be read by
    # another process (`answer-path-check.py`, once per headless spawn), and a plain
    # `open(path, "w")` truncates before the dump fills it. It cannot fail OPEN — the probe
    # refuses unparseable JSON — but a spurious refusal once per round is a defect worth three
    # lines to remove.
    tmp = f"{path}.tmp.{os.getpid()}"
    # ⚠️ `O_EXCL` + `O_NOFOLLOW` + `0o600`, which is this repo's convention for a state file
    # under `~/.claude` (`scripts/session-identity.py:222-238` names the bare `open()` as the
    # defect, `scripts/manager-predispatch.py` does the same). The name is pid-derived, so it
    # must not be followable, and this registration is read as an identity assertion by
    # `answer-path-check.py` — it must not be world-readable.
    try:
        os.unlink(tmp)
    except OSError:
        pass
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(tmp, flags, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
        os.replace(tmp, path)
    except BaseException:
        # A failed dump must not leave a stale temp file for the next round to trip over.
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return path


def main():
    parser = argparse.ArgumentParser(add_help=True)
    parser.add_argument(
        "--session-id",
        default=os.environ.get("CLAUDE_CODE_SESSION_ID", ""),
        help="defaults to $CLAUDE_CODE_SESSION_ID",
    )
    parser.add_argument(
        "--vault",
        default=os.path.basename(os.getcwd()),
        help="the vault this session runs in; defaults to the cwd's basename",
    )
    args = parser.parse_args()

    if not bare_filename(args.session_id):
        print(f"REFUSED\tno usable session id ({args.session_id!r}) — cannot name the registration file")
        return 1
    try:
        path = write_registration(args.session_id, args.vault)
    except OSError as err:
        print(f"REFUSED\tcannot write the registration: {err}")
        return 1
    print(f"registered {SUBJECT} ({BRANCH}) -> {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
