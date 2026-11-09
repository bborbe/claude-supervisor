#!/usr/bin/env python3
"""Record that this session's WRAPPER answer command reached its route.

`answer_permission` covers the MCP route; a manager can also answer a wrapper-backed worker
with a Bash `curl` POST to `/api/1.0/sessions/<sid>/answer` (`commands/steps/worker-channels.md`).
That is the route the measured failure was on — the command itself is gated by the session's
permission classifier, so a denied command never reaches the wrapper and produces **no HTTP
status at all**.

⚠️ **This recorder writes only on `409` or `404`.** Those are the two answers that mean the
request arrived and was refused there harmlessly — 409 = no open question / already answered,
404 = unknown session. Every other input refuses to write and says why, because a recorder
that wrote unconditionally would certify a spawner whose command was denied before it ever
left the machine. That refusal is the whole point of the file; see SC1 of the task.

⚠️ **Called in a SEPARATE Bash invocation from the curl.** The curl must keep the exact
command shape the permission classifier judges — folding a second command onto the same line
changes the shape being judged, and then a green reach would prove the wrong thing.

Exit 0 = the reach was written. Exit 1 = refused, with the reason on stdout.
"""
import argparse
import json
import os
import sys

# ⚠️ Same default and the same env var as `server/config.mjs`'s `reachDir`. The two writers
# (this script and the server's foreign-park path) must agree on the directory or the probe
# reads one and the other fills a directory nobody looks at. The env var wins, and it is also
# the fixture knob the probe's own controls set.
REACH_DIR = os.path.expanduser(
    os.environ.get("SUPERVISOR_ANSWER_REACH_DIR")
    or os.path.join(
        os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state"),
        "claude-supervisor",
        "answer-reach",
    )
)

# The only statuses that mean "arrived and was refused there". 409 = no open question or
# already answered; 404 = unknown session. Both land nothing.
ACCEPTED_STATUSES = (404, 409)


def reach_path(session_id, leg):
    return os.path.join(REACH_DIR, f"{session_id}.{leg}.json")


def record(session_id, leg, status):
    """(ok, detail) — never raises."""
    if not session_id:
        return False, "no session id (pass --session-id or set $CLAUDE_CODE_SESSION_ID)"
    # The id is joined into a path, so it must be a bare filename — the same refusal
    # `answer-path-check.py` makes, for the same reason.
    if session_id in (".", "..") or "/" in session_id or "\\" in session_id or "\x00" in session_id:
        return False, f"session id {session_id!r} is not a bare filename — refusing to join it into a path"
    try:
        status = int(status)
    except (TypeError, ValueError):
        return False, f"--status {status!r} is not an integer — a reach needs the HTTP status the curl saw"
    if status not in ACCEPTED_STATUSES:
        # ⚠️ The origin case lands HERE, and writing nothing is the correct outcome: the
        # command was denied, refused, or the wrapper was down, so this session has not shown
        # that it can reach the route.
        return False, (
            f"status {status} does not mean 'arrived and was refused there' — only "
            f"{'/'.join(str(s) for s in ACCEPTED_STATUSES)} do. Nothing was written: a denied "
            f"command, a 5xx, or an unreachable wrapper leaves this session with no reach."
        )
    try:
        os.makedirs(REACH_DIR, exist_ok=True)
        record_body = {
            "session_id": session_id,
            "leg": leg,
            "ts": _now(),
            "outcome": f"http-{status}",
        }
        with open(reach_path(session_id, leg), "w", encoding="utf-8") as handle:
            handle.write(json.dumps(record_body) + "\n")
    except OSError as err:
        return False, f"could not write {reach_path(session_id, leg)} ({err})"
    return True, f"{leg} reach recorded at {reach_path(session_id, leg)} (http-{status})"


def _now():
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def main():
    parser = argparse.ArgumentParser(add_help=True)
    parser.add_argument(
        "--session-id",
        default=os.environ.get("CLAUDE_CODE_SESSION_ID", ""),
        help="defaults to $CLAUDE_CODE_SESSION_ID",
    )
    parser.add_argument(
        "--leg",
        default="wrapper",
        help="the answer route this reach is for; only `wrapper` is recorded by this script",
    )
    parser.add_argument(
        "--status",
        default="",
        help="the HTTP status the answer curl saw (`-w '%%{http_code}'`)",
    )
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    # ⚠️ The MCP leg is written by the server, not here. Accepting it would let a caller
    # manufacture the leg the server owns, which is the fail-open direction this file guards.
    if args.leg != "wrapper":
        ok, detail = False, (
            f"--leg {args.leg!r} is not this script's to write — the `mcp` leg is recorded by "
            f"the server on its foreign-park path, and a caller that could write it could "
            f"manufacture evidence for a route it never used"
        )
    else:
        ok, detail = record(args.session_id, args.leg, args.status)

    if args.json:
        print(json.dumps({"ok": ok, "detail": detail}, ensure_ascii=True))
    else:
        print(f"{'OK' if ok else 'REFUSED'}\t{detail}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
