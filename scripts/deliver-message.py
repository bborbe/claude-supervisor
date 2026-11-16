#!/usr/bin/env python3
"""Deliver one message or one gate answer to a live session, addressed by SESSION ID.

The defect this exists for: a live session with no row in `~/.claude/sessions/*.json`
cannot be reached. `SendMessage` addresses sessions by that registry, so it answers
`No agent named … is reachable` — and a manager holding only the session id then has no
channel at all to release the gate the worker is parked on. Measured 2026-11-15/16 on
three sessions (`694afb06`, `914849f9`, `33646d27`), each confirmed alive by its own
transcript while `SendMessage` refused it.

⚠️ **The registry is never read to GRANT a verdict.** Liveness comes from
`session-liveness.py --check` (heartbeat first); the message control comes from
`GET /api/1.0/sessions`. That sibling may consult `~/.claude/sessions/*.json` on its own
ABSENT-licensing path, so the accurate claim is: nothing here reads the registry to decide
a session is live, and a session missing from it is still deliverable.

⚠️ **A delivery is reported only when the wrapper accepted it, and acceptance is not
effect.** `POST …/message` answers 202 `{"status":"sent"}`, which means the turn was
queued, not that the worker acted on it. The caller reads the transcript for the effect.
A transport failure is reported as **delivery unknown**, never as "nothing delivered" —
a request that was written and then timed out may well have landed.

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
    0  delivered — the wrapper accepted it, or `--dry-run` resolved the channel
    1  refused — the session is not live, or the wrapper would not take it
    2  an instrument could not be read, or a transport failed; delivery is unknown
"""
import argparse
import json
import os
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request

# The wrapper daemon's base, the same one every other supervisor script talks to.
WRAPPER_DEFAULT = "http://127.0.0.1:18090"

# The liveness reader, resolved from this file's own location rather than a hardcoded home
# path — a checkout anywhere works, and a missing sibling fails loudly instead of silently
# reading a different machine's copy.
HERE = os.path.dirname(os.path.abspath(__file__))
LIVENESS = os.path.join(HERE, "session-liveness.py")

# Every verdict `session-liveness.py --check` can print. AMBIGUOUS and MALFORMED are
# refusals about the ID, not about the instrument, and reading only the first three turned
# them into "instrument could not be read" — the wrong direction, since it sends the reader
# to look at the heartbeat store instead of at the id they typed.
VERDICTS = frozenset({"LIVE", "ABSENT", "UNKNOWN", "AMBIGUOUS", "MALFORMED"})


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse a redirect instead of following it.

    `urlopen` follows a 3xx by default, so a proxy or a misrouted port could turn this
    POST into a request to somewhere else and still report success. A redirect here means
    the address is not what the caller thinks it is, which is a refusal.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


def _base_url():
    return os.environ.get("SUPERVISOR_WRAPPER_URL", WRAPPER_DEFAULT).rstrip("/")


def liveness(session_id):
    """(verdict, sibling's own line) or (None, reason) when the instrument could not be read.

    Delegated to `session-liveness.py` rather than reimplemented: that file owns the
    definition of liveness, and a second copy here would drift the moment either source
    changed.

    ⚠️ **The token and the exit code must agree.** The sibling's contract is exit 0 iff
    LIVE. Scanning text alone would let a stray `LIVE` line in a diagnostic decide "may I
    send", so a token is accepted only when the code corroborates it.
    """
    try:
        out = subprocess.run(
            [sys.executable, LIVENESS, "--check", session_id],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return None, f"liveness check failed: {exc}"

    # ⚠️ The sibling writes `LIVE — …` to STDOUT but its non-LIVE verdicts to STDERR, and
    # exits non-zero for them. Reading stdout alone turns a genuine ABSENT into "instrument
    # unreadable" — measured 2026-11-16 on this script's first run. Both streams are read.
    for stream in (out.stdout or "", out.stderr or ""):
        for line in stream.splitlines():
            tokens = line.strip().split()
            if tokens and tokens[0] in VERDICTS:
                verdict = tokens[0]
                agrees = (out.returncode == 0) == (verdict == "LIVE")
                if not agrees:
                    return None, (
                        f"liveness verdict {verdict} disagrees with exit {out.returncode}; "
                        f"refusing to act on either"
                    )
                return verdict, line.strip()
    return None, "the liveness reader printed no recognisable verdict"


def roster_row(session_id):
    """(row, None) for the wrapper's row, (None, None) when it holds none, (None, why) on
    a read failure.

    The three outcomes are distinct on purpose: a wrapper that cannot be read is an
    instrument failure (exit 2), while a wrapper that answers and does not hold the session
    is a refusal (exit 1). Collapsing them makes a dead wrapper look like a missing session.
    """
    url = f"{_base_url()}/api/1.0/sessions"
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:
            rows = json.load(resp)
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return None, f"roster read failed: {exc}"
    if not isinstance(rows, list):
        return None, f"roster answered {type(rows).__name__}, not a list of sessions"
    for row in rows:
        if isinstance(row, dict) and row.get("id") == session_id:
            return row, None
    return None, None


def post(path, payload):
    """POST one body and return (status, decoded-body-or-raw-text, transport-failed).

    A non-2xx is returned rather than raised — the caller needs the status and the
    wrapper's own message to report why nothing was delivered. A transport failure is
    flagged separately because the request may already have been written.
    """
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        _base_url() + path, data=data, headers={"Content-Type": "application/json"}
    )
    try:
        with _OPENER.open(req, timeout=30) as resp:
            return resp.status, _decode(resp.read()), False
    except urllib.error.HTTPError as exc:
        return exc.code, _decode(exc.read()), False
    except (urllib.error.URLError, OSError) as exc:
        return 0, str(exc), True


def _decode(raw):
    try:
        return json.loads(raw)
    except ValueError:
        return raw.decode(errors="replace") if isinstance(raw, bytes) else raw


def _accepted(status, body, is_answer):
    """Whether the wrapper took it — status and body, not status alone.

    The message route documents 202 `{"status":"sent"}`. A bare 200 from something that is
    not the wrapper would otherwise read as a delivery, which is the "success without
    evidence" shape this file exists to avoid. The answer route's success status is not
    documented, so a 2xx is accepted there but the body is still reported.
    """
    if is_answer:
        return 200 <= status < 300
    if status != 202:
        return False
    return isinstance(body, dict) and body.get("status") == "sent"


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

    # `is not None`, never truthiness: `--text ""` is falsy, and a truthiness test would
    # fall through to the answer branch and POST a payload with `request_id: null`.
    is_answer = args.answer is not None
    if not is_answer and not args.text:
        print("REFUSED: --text must not be empty", file=sys.stderr)
        return 1
    if is_answer and not args.answer:
        print("REFUSED: --answer must not be empty", file=sys.stderr)
        return 1
    if is_answer and not args.behavior:
        print("REFUSED: --answer requires --behavior allow|deny", file=sys.stderr)
        return 1
    if not is_answer and (args.behavior or args.message):
        # Silently dropping these is how a caller believes a message was carried when it
        # was not — the flags only mean anything on the answer route.
        print("REFUSED: --behavior and --message only apply with --answer", file=sys.stderr)
        return 1

    verdict, detail = liveness(args.session_id)
    if verdict is None:
        print(f"REFUSED: {detail}; nothing sent", file=sys.stderr)
        return 2
    if verdict != "LIVE":
        # ABSENT, UNKNOWN, AMBIGUOUS and MALFORMED are different facts and all refuse here.
        # The sibling's own line is echoed so the caller gets its hint (a longer id, a
        # malformed id) rather than a verdict with the reason stripped off.
        print(f"REFUSED: {detail}", file=sys.stderr)
        return 1

    row, why = roster_row(args.session_id)
    if why is not None:
        print(f"REFUSED: {why}; nothing sent", file=sys.stderr)
        return 2
    if row is None:
        print(f"REFUSED: the wrapper holds no row for {args.session_id}; nothing sent",
              file=sys.stderr)
        return 1

    controls = row.get("controls")
    controls = controls if isinstance(controls, list) else []
    if not is_answer and "message" not in controls:
        print(
            f"REFUSED: {args.session_id} is live but offers no message control "
            f"(controls={controls}); nothing sent",
            file=sys.stderr,
        )
        return 1

    # The id is caller-supplied and goes into a URL path. Quoting it stops a `/`, `?` or
    # `#` from changing which route is called.
    quoted = urllib.parse.quote(args.session_id, safe="")
    if not is_answer:
        path, payload = f"/api/1.0/sessions/{quoted}/message", {"text": args.text}
    else:
        path = f"/api/1.0/sessions/{quoted}/answer"
        payload = {"request_id": args.answer, "behavior": args.behavior}
        if args.message:
            payload["message"] = args.message

    if args.dry_run:
        print(json.dumps({"session_id": args.session_id, "liveness": verdict,
                          "controls": controls, "path": path, "delivered": False,
                          "dry_run": True}))
        return 0

    status, body, transport_failed = post(path, payload)
    if transport_failed:
        # The request may have been written before the failure. Saying "nothing delivered"
        # here would assert an absence this side cannot observe.
        print(json.dumps({"session_id": args.session_id, "path": path,
                          "delivered": None, "delivery": "unknown", "error": body}))
        print("REFUSED: no response from the wrapper; delivery unknown", file=sys.stderr)
        return 2

    delivered = _accepted(status, body, is_answer)
    print(json.dumps({"session_id": args.session_id, "liveness": verdict,
                      "controls": controls, "path": path, "http_status": status,
                      "delivered": delivered, "body": body}))
    if not delivered:
        print(f"REFUSED: the wrapper answered {status}; nothing delivered", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
