#!/usr/bin/env python3
"""Deliver one message or one gate answer to a live session, addressed by SESSION ID.

The defect this exists for: a live session with no row in `~/.claude/sessions/*.json`
cannot be reached. `SendMessage` addresses sessions by that registry, so it answers
`No agent named … is reachable` — and a manager holding only the session id then has no
channel at all to release the gate the worker is parked on. Measured 2026-11-15/16 on
three sessions (`694afb06`, `914849f9`, `33646d27`), each confirmed alive by its own
transcript while `SendMessage` refused it.

⚠️ **This reads two instruments and deliberately not a third.** `session-liveness.py
--check` answers liveness from the heartbeat store; `GET /api/1.0/sessions` answers which
controls the wrapper offers for that id. `~/.claude/sessions/*.json` is never read here —
reading it would reintroduce the exact dependency this script removes, and the whole point
is a channel that works when that file is absent.

⚠️ **A delivery is reported only when the wrapper accepted it, and acceptance is not
effect.** `POST …/message` answers 202 `{"status":"sent"}`, which means the turn was
queued, not that the worker acted on it. The caller reads the transcript for the effect.
This script never claims more than the accept, because "the send returned success" is
precisely the claim that was wrong in the pane transport before it was measured.

⚠️ **`--answer` and `--text` are mutually exclusive, and that split is the park.** A worker
parked on a permission gate or an `AskUserQuestion` is released through the answer route
with a `request_id`; a worker that is running and simply needs to be told something takes
the message route. Sending text at a parked worker leaves it parked, and answering a
worker that is not parked answers 409 — both are reported, neither is smoothed over.

Usage:
    deliver-message.py --session-id <uuid> --text "the gate is released; park and exit"
    deliver-message.py --session-id <uuid> --answer <request-id> --behavior deny \\
        --message "Manager answer, via supervisor: <option>"
    deliver-message.py --session-id <uuid> --text "…" --dry-run

Exit codes:
    0  delivered — the wrapper accepted it (see the caveat above)
    1  refused — the session is not live, or offers no usable control; the reason is printed
    2  an instrument could not be read; nothing was sent
"""
import argparse
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request

# The wrapper daemon's base, the same one every other supervisor script talks to.
WRAPPER_DEFAULT = "http://127.0.0.1:18090"

# The liveness reader, resolved from this file's own location rather than a hardcoded home
# path — a checkout anywhere works, and a missing sibling fails loudly instead of silently
# reading a different machine's copy.
HERE = os.path.dirname(os.path.abspath(__file__))
LIVENESS = os.path.join(HERE, "session-liveness.py")

# The only statuses that mean the wrapper took it. 202 is the documented accept; 409 means
# no open question (or one already answered) and 404 means the wrapper does not hold that
# session — both are refusals to report, never a quiet success.
ACCEPTED = {200, 202}


def _base_url():
    return os.environ.get("SUPERVISOR_WRAPPER_URL", WRAPPER_DEFAULT).rstrip("/")


def liveness(session_id):
    """The liveness verdict as a single word: LIVE, ABSENT, UNKNOWN — or None on error.

    Delegated to `session-liveness.py` rather than reimplemented. That file owns the
    definition of liveness (heartbeat first, registry second); a second copy here would
    drift the moment either source changed, and two definitions of "live" is how a manager
    ends up acting on one while the sweep reports the other.
    """
    try:
        out = subprocess.run(
            [sys.executable, LIVENESS, "--check", session_id],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        print(f"liveness check failed: {exc}", file=sys.stderr)
        return None
    # ⚠️ The reader writes `LIVE — …` to STDOUT but its non-LIVE verdicts (`ABSENT`,
    # `UNKNOWN`) to STDERR, and exits non-zero for them. Reading stdout alone therefore
    # turns a genuine ABSENT into "instrument unreadable" — a wrong verdict that looks
    # like a broken instrument and sends the reader looking in the wrong place. Measured
    # 2026-11-16 on the first run of this script. Both streams are read, and only a known
    # verdict token is accepted, so a traceback on stderr still reports None.
    known = {"LIVE", "ABSENT", "UNKNOWN"}
    for stream in (out.stdout or "", out.stderr or ""):
        for line in stream.splitlines():
            tokens = line.strip().split()
            if tokens and tokens[0] in known:
                return tokens[0]
    return None


def roster_row(session_id):
    """The wrapper's own row for this session, or None when it does not hold it."""
    url = f"{_base_url()}/api/1.0/sessions"
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:
            rows = json.load(resp)
    except (urllib.error.URLError, OSError, ValueError) as exc:
        print(f"roster read failed: {exc}", file=sys.stderr)
        return None
    for row in rows:
        if row.get("id") == session_id:
            return row
    return None


def post(path, payload):
    """POST one body and return (status, decoded-body-or-raw-text).

    A non-2xx is returned rather than raised: the caller needs the status and the wrapper's
    own message to report *why* nothing was delivered.
    """
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        _base_url() + path, data=data, headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, _decode(resp.read())
    except urllib.error.HTTPError as exc:
        return exc.code, _decode(exc.read())
    except (urllib.error.URLError, OSError) as exc:
        return 0, str(exc)


def _decode(raw):
    try:
        return json.loads(raw)
    except ValueError:
        return raw.decode(errors="replace") if isinstance(raw, bytes) else raw


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--session-id", required=True, help="the session uuid to deliver to")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--text", help="a message to hand the running worker as one turn")
    mode.add_argument("--answer", metavar="REQUEST_ID", help="release the gate on this request id")
    parser.add_argument("--behavior", choices=("allow", "deny"), help="required with --answer")
    parser.add_argument("--message", help="the text carried with an answer")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="resolve the channel and print the verdict, delivering nothing",
    )
    args = parser.parse_args()

    if args.answer and not args.behavior:
        print("REFUSED: --answer requires --behavior allow|deny", file=sys.stderr)
        return 1

    verdict = liveness(args.session_id)
    if verdict is None:
        print("REFUSED: the liveness instrument could not be read; nothing sent", file=sys.stderr)
        return 2
    if verdict != "LIVE":
        # ABSENT and UNKNOWN are different facts and both refuse here. UNKNOWN means the
        # instruments disagree, which is a reason to look — not a reason to guess.
        print(f"REFUSED: {args.session_id} reads {verdict}, not LIVE; nothing sent")
        return 1

    row = roster_row(args.session_id)
    if row is None:
        print(f"REFUSED: the wrapper holds no row for {args.session_id}; nothing sent")
        return 1

    controls = row.get("controls") or []
    if args.text and "message" not in controls:
        print(
            f"REFUSED: {args.session_id} is live but offers no message control "
            f"(controls={controls}); nothing sent"
        )
        return 1

    if args.text:
        path, payload = f"/api/1.0/sessions/{args.session_id}/message", {"text": args.text}
    else:
        path = f"/api/1.0/sessions/{args.session_id}/answer"
        payload = {"request_id": args.answer, "behavior": args.behavior}
        if args.message:
            payload["message"] = args.message

    if args.dry_run:
        print(json.dumps({"session_id": args.session_id, "liveness": verdict,
                          "controls": controls, "path": path, "delivered": False,
                          "dry_run": True}))
        return 0

    status, body = post(path, payload)
    delivered = status in ACCEPTED
    print(json.dumps({"session_id": args.session_id, "liveness": verdict,
                      "controls": controls, "path": path, "http_status": status,
                      "delivered": delivered, "body": body}))
    if not delivered:
        print(f"REFUSED: the wrapper answered {status}; nothing delivered", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
