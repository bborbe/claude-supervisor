#!/usr/bin/env python3
"""Is this session id live? One instrument, one answer, for the whole plugin.

Liveness is answered by the attention store's **session-heartbeat endpoint** and by nothing
else. Every session that can be asked about stamps that store — an interactive tab, a headless
in-process worker, a cluster pod — so one channel answers for all of them, and the verdict is
the store's own `live` boolean (its age against its window), never a recomputation here.

  --check <session-id>   LIVE (exit 0) / ABSENT (exit 1) / UNKNOWN (exit 2) / AMBIGUOUS (exit 3)
  --list                 one line per live session: `<session-id>  <where>  <name>  [<source>]`

⚠️ **An id argument is a PREFIX, and a prefix is legal.** Callers pass prefixes: the sweep
digest carries 8-char prefixes, `[ref]` in a roster is 6 chars, `sid8` is the display
convention. A contract that rejects the input its own callers produce is the defect, not the
fix. A unique match resolves; an ambiguous one refuses rather than guesses — the semantics
`who-needs-me.py:pane_for()` already settled. The per-id route cannot resolve a prefix
(measured 2026-10-08: `/session-heartbeat/30fae0ae` → 404 for a live session), so a prefix is
resolved against the list route; see `check()`.

⚠️ **UNKNOWN is a third answer, not a flavour of ABSENT.** An unreachable endpoint, or a
response that is not 200 and not the store's 404, means the probe could not run — and a caller
that folds that into "not live" turns an I/O error into permission to resume, which is exactly
the double-writer this file exists to prevent. Exit 2 is deliberately distinct from exit 1.
Same rule as `live-workers.py`.

⚠️ **ABSENT is licensed only by a READABLE endpoint that positively reports the id as not
live** — a 200 carrying `live: false`, or a 404 from the store, or a readable list with no
matching id. Nothing else may reach it.

⚠️ **AMBIGUOUS is a fourth answer, and it is not a liveness verdict.** Two sessions sharing an
8-char prefix means the ARGUMENT did not identify one session; answering LIVE or ABSENT either
way would be a guess. Exit 3 names it and prints the candidates, so the caller can pass the
full id.

⚠️ **Why the endpoint and not the registry.** Until 2026-10-08 this file composed two sources
— the session registry and the heartbeat store — with an asymmetric rule between them (a
positive from either was an answer; a negative needed both readable). The reason was
structural: the registry is pid-keyed and local-only, so it sees every session holding a
socket and is blind to a session with no process of its own, while the heartbeat store covers
those. The session-heartbeat endpoint already holds BOTH populations in one store — it serves
`source: cluster` and `source: mcp-timer` rows beside local ones — so the composition
disappears into the store and this file reads one channel.

⚠️ **The registry read did not disappear; it moved.** The registry answers a different
question — identity, not liveness: the name a session holds, the names it used to hold, the
`cwd` to resume it into. That machinery now lives in `session-identity.py`, which this file
imports to join those fields onto `--list --json` (the shape `/supervisor:open` and
`agents/fleet-sweep-reader.md` resolve a roster row by). One reader over that store, still —
the 2026-09-26 near-miss that this file's history records was two readers over one directory
disagreeing about what an id argument means.
"""
import argparse
import importlib.util
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

LIVE, ABSENT, UNKNOWN, AMBIGUOUS = 0, 1, 2, 3

# The attention store, resolved exactly as the rest of the plugin resolves it: the same
# variable and default `who-needs-me.py`, `attention-board.py` and `attention-ask.py` carry,
# so a store moved once moves everywhere and this file gains no second convention to drift.
ENDPOINT_DEFAULT = "http://localhost:18080"
HEARTBEAT_PATH = "/api/1.0/session-heartbeat"

# Seconds any one HTTP read may take. The store is local and answers in milliseconds; this is
# a backstop against a wedged server, never a routine path. A slow store is a store this file
# should report as UNKNOWN, not one it should hang on.
_HTTP_TIMEOUT = 5

_IDENTITY = None


def endpoint_base(override=None):
    """The store's base URL: `--endpoint`, then `$ATTENTION_STORE_URL`, then localhost.

    A trailing slash is stripped so a path appended here cannot double it.
    """
    return (override or os.environ.get("ATTENTION_STORE_URL") or ENDPOINT_DEFAULT).rstrip("/")


def _identity():
    """Import `session-identity.py` (hyphenated filename -> importlib) — the registry reader.

    Lazy and cached: the verdict path (`--check`) never touches the registry, so a caller that
    only asks "is this id live?" must not pay to load the `ps` identity probe. `--list` pays
    for it, because a names-less list is the shape `/supervisor:open` cannot join.
    """
    global _IDENTITY
    if _IDENTITY is None:
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "session-identity.py")
        spec = importlib.util.spec_from_file_location("session_identity", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _IDENTITY = mod
    return _IDENTITY


def _get_json(url, timeout=_HTTP_TIMEOUT):
    """`(status, payload)` for a GET. Raises `OSError`/`ValueError` when the read could not run.

    ⚠️ **A structured HTTP error is returned as its status, and a transport failure raises.**
    They are different facts and must stay so: a 404 or a 500 is the store speaking, and the
    caller decides what it means; an unreachable socket, a timeout, or a body that is not JSON
    is the probe failing to run, and folding either into a status is the collapse this file's
    UNKNOWN exists to prevent.
    """
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
            return resp.status, (json.loads(raw) if raw.strip() else None)
    except urllib.error.HTTPError as exc:
        try:
            raw = exc.read().decode("utf-8")
        except OSError:
            raw = ""
        try:
            return exc.code, (json.loads(raw) if raw.strip() else None)
        except ValueError:
            return exc.code, None


def read_endpoint(endpoint=None, timeout=_HTTP_TIMEOUT):
    """Every row the heartbeat store serves, or `None` when the endpoint could not be read.

    `None` and `[]` are different answers and must stay so: `[]` is "read it, the store holds
    nothing", `None` is "could not read it". Collapsing them turns an unreachable store into a
    confident empty fleet — the failure this file exists to correct. A non-200 is not a
    liveness answer either, so it too is `None`.
    """
    url = endpoint_base(endpoint) + HEARTBEAT_PATH
    try:
        status, payload = _get_json(url, timeout)
    except (OSError, ValueError):
        return None
    if status != 200 or not isinstance(payload, list):
        return None
    return payload


def check(session_id, endpoint=None, timeout=_HTTP_TIMEOUT):
    """`(verdict, message, row)` for one id argument, from the endpoint alone.

    Two requests, and the split is the whole contract:

      1. the **per-id** route. HTTP 200 carries the row — LIVE when the store says `live`,
         ABSENT when it says otherwise (the store positively reporting the id as not live).
         HTTP 404 is the store positively reporting it holds no such session. ⚠️ **Any other
         status, and any unreachable endpoint, is UNKNOWN — never ABSENT.**
      2. only when step 1 answered 404, the **list** route — because the argument may be a
         PREFIX, which the per-id route cannot resolve (measured 2026-10-08:
         `/session-heartbeat/30fae0ae` → 404 for a live session). Zero prefix matches → ABSENT;
         exactly one → LIVE/ABSENT by its `live`; more than one → AMBIGUOUS, and the message
         names candidates a caller can actually tell apart.

    ⚠️ **A row whose `live` is not a boolean is UNKNOWN, never ABSENT.** The store computes
    `live` from age against its window and always sends it; a row that carries something else
    is a store this reader does not understand, and "I do not understand this row" must not
    become permission to resume onto a live conversation.
    """
    sid = (session_id or "").strip().lower()
    if not sid:
        return UNKNOWN, "no session id given", None
    base = endpoint_base(endpoint)

    try:
        status, row = _get_json(
            "%s%s/%s" % (base, HEARTBEAT_PATH, urllib.parse.quote(sid, safe="")), timeout
        )
    except (OSError, ValueError):
        return UNKNOWN, "session-heartbeat endpoint unreachable at %s" % base, None
    if status == 200 and isinstance(row, dict):
        return _verdict_for(row, sid)
    if status != 404:
        # ⚠️ **Not ABSENT.** A 5xx, a 403, a body that is not JSON — none of them is the store
        # saying "this session is gone", and reading one as ABSENT is the resume-onto-a-live-
        # conversation this file exists to prevent.
        return UNKNOWN, "session-heartbeat endpoint returned HTTP %s for %s" % (status, sid), None

    try:
        status, rows = _get_json(base + HEARTBEAT_PATH, timeout)
    except (OSError, ValueError):
        return UNKNOWN, "session-heartbeat endpoint unreachable at %s" % base, None
    if status != 200 or not isinstance(rows, list):
        return UNKNOWN, "session-heartbeat list endpoint returned HTTP %s" % status, None

    # The list carries every session the store has ever seen, live or stale — a stale row is
    # matched too, exactly as the registry's own keys were: the argument identifies a session,
    # and whether that session is live is the verdict that follows, not part of the match.
    by_id = {
        r["session_id"]: r
        for r in rows
        if isinstance(r, dict) and r.get("session_id")
    }
    matches = sorted(sid_ for sid_ in by_id if sid_.lower().startswith(sid))
    if not matches:
        return ABSENT, "no session id matches %s" % sid, None
    if len(matches) > 1:
        # 12 chars, not 8: the caller already passed a prefix long enough to collide, so
        # echoing 8 back hands them two identical strings and no way to tell them apart.
        return AMBIGUOUS, "%d session ids match %s — pass a longer id: %s" % (
            len(matches),
            sid,
            ", ".join(m[:12] for m in matches),
        ), None
    return _verdict_for(by_id[matches[0]], sid)


def _verdict_for(row, sid):
    """`(verdict, message, row)` for a single heartbeat row. UNKNOWN when `live` is unusable."""
    full = row.get("session_id") or sid
    live = row.get("live")
    if live is True:
        return LIVE, full, row
    if live is False:
        return ABSENT, full, row
    return UNKNOWN, (
        "%s has a heartbeat row with no usable `live` flag — cannot decide liveness" % full
    ), row


def main(argv=None):
    parser = argparse.ArgumentParser(description="Is this session id live? (session-heartbeat endpoint)")
    parser.add_argument("--check", metavar="SESSION_ID", help="full id or an 8-char prefix")
    parser.add_argument("--list", action="store_true", help="print every live session")
    parser.add_argument(
        "--endpoint",
        default=None,
        help="override the attention store base URL (default $ATTENTION_STORE_URL, else "
        "http://localhost:18080) — a test points this at a fixture server",
    )
    parser.add_argument(
        "--dir",
        default=None,
        help="override the identity registry path — the source of the `--list` name, "
        "formerNames and cwd fields",
    )
    parser.add_argument(
        "--heartbeat-dir",
        default=None,
        help="accepted for backward compatibility; the heartbeat store is no longer read here "
        "(the session-heartbeat endpoint replaces it)",
    )
    parser.add_argument(
        "--ttl",
        type=int,
        default=None,
        help="accepted for backward compatibility; the endpoint computes freshness itself",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="with --list, emit the records as JSON — the shape `/supervisor:open` reads to "
        "resolve a topic's manager, so it stops opening the registry itself",
    )
    args = parser.parse_args(argv)

    # ⚠️ Two flags are accepted and IGNORED, and saying so out loud is not optional. Both existed
    # because the old design read two LOCAL sources with a caller-chosen window; the endpoint
    # replaces both and computes freshness itself, so a caller's `--ttl` can no longer be
    # honoured here. Removing the flags would break the callers that still pass them
    # (`approved-not-started.py` passes `--heartbeat-dir`), and accepting them in silence would
    # let a caller believe a window is in force when it is not — a flag that resolves to nothing
    # is the failure shape this repo treats as worse than a refusal. So: accept, warn, continue.
    if args.heartbeat_dir is not None or args.ttl is not None:
        print(
            "WARNING — --heartbeat-dir and --ttl are ignored: the session-heartbeat endpoint "
            "computes freshness itself",
            file=sys.stderr,
        )

    if args.check:
        verdict, message, row = check(args.check, args.endpoint)
        if verdict == LIVE:
            print(
                "LIVE — %s  heartbeat age %ss  source %s"
                % (message, row.get("age_seconds"), row.get("source") or "heartbeat")
            )
        elif verdict == ABSENT:
            print("ABSENT — %s" % message, file=sys.stderr)
        elif verdict == AMBIGUOUS:
            print("AMBIGUOUS — %s" % message, file=sys.stderr)
        else:
            print("UNKNOWN — %s" % message, file=sys.stderr)
        return verdict

    # A list needs BOTH halves readable, and for the same reason it always did: a partial list
    # presented as complete reads as "this session is not live" for every session in the half
    # that failed. The halves changed with the source — the endpoint is the live set, the
    # identity registry is the names — and the names are load-bearing: a names-less list makes
    # `/supervisor:open`'s topic→manager join match nothing and spawn a SECOND manager onto a
    # live topic (observed 2026-09-18), which is the dangerous direction.
    rows = read_endpoint(args.endpoint)
    if rows is None:
        print(
            "UNKNOWN — cannot read the session-heartbeat endpoint at %s"
            % endpoint_base(args.endpoint),
            file=sys.stderr,
        )
        return UNKNOWN

    identity = _identity().read_registry(args.dir)
    if identity is None:
        print("UNKNOWN — cannot read the identity registry (%s)" % (args.dir or "default path"), file=sys.stderr)
        return UNKNOWN

    payload = []
    for row in rows:
        sid = row.get("session_id")
        # Only a live row is listed: the store keeps a row for a session long after it dies,
        # and a `--list` that echoed them would count a dead fleet as a live one. The old
        # registry-backed list had the same rule — presence in a pruned store meant live.
        if not sid or row.get("live") is not True:
            continue
        ident = identity.get(sid) or {}
        payload.append(
            {
                "sessionId": sid,
                "pid": ident.get("pid"),
                "status": ident.get("status", ""),
                "name": ident.get("name", ""),
                "formerNames": ident.get("formerNames", []),
                "cwd": ident.get("cwd", ""),
                "nameSource": ident.get("nameSource", ""),
                "alive": True,
                "source": row.get("source") or "heartbeat",
                "state": row.get("state") or "live",
                "age_seconds": row.get("age_seconds"),
            }
        )

    if args.json:
        json.dump(payload, sys.stdout, indent=2)
        sys.stdout.write("\n")
        return LIVE

    if not payload:
        print("no live sessions on the session-heartbeat endpoint")
        return LIVE
    for rec in sorted(payload, key=lambda r: r["sessionId"]):
        pid = rec["pid"]
        where = "pid %s" % pid if pid is not None else "age %ss" % rec["age_seconds"]
        print("%s  %s  %s  [%s]" % (rec["sessionId"], where, rec["name"] or "(no name)", rec["source"]))
    return LIVE


if __name__ == "__main__":
    sys.exit(main())
