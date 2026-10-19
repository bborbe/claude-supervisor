#!/usr/bin/env python3
"""The supervisor's spawn ledger — who it opened, in what mode, from which manager.

One reader, so the two consumers that need it cannot drift: `worker-sessions.py` (the
fleet-wide worker counter, whose *identity* half this is) and `session-liveness.py`
(whose `unstamped()` guard needs the set of sessions that **should** be stamping).

⚠️ **Why this is its own module rather than a function in either consumer.** The guard
in `session-liveness.py` needs the ledger, and `worker-sessions.py` — which held the
only copy of this reader — imports `session-liveness.py` through `_load` to reach
`read_endpoint()`. Loading `worker-sessions.py` back from inside `unstamped()` closes a
cycle the `_load` idiom does not expect. Extracting the reader is the same move this
codebase already made for the session registry (`session-identity.py`), and it leaves
the dependency direction one-way: both consumers point here, this file points nowhere.

⚠️ **This reader reads the attention store's ENDPOINT, not the file ledger.** Until
2026-10-14 it walked a directory of `<session_id>.json` files under the supervisor's own
state root; the supervisor now
publishes every row to `POST /api/1.0/session-ledger` beside the file write, and the
one-time import of the historical file ledger has run (1915 rows), so the endpoint is
the WHOLE ledger and the file is a transitional duplicate. The switch is what makes a
worker spawned by another manager visible here — the file was only ever this machine's
own spawns plus whatever had been imported, while the endpoint is the store every
manager writes to.

⚠️ **A 404 is `None`, and that is the deploy-order hazard made visible rather than
silent.** A controller older than the session-ledger route answers 404, and `None`
REFUSES a spawn (`worker-sessions.py` returns exit 2, which the cap reads as "count
could not be taken") where `{}` would permit one. That asymmetry is deliberate: an
empty store is a machine that has never spawned, an absent route is an instrument that
cannot answer, and only the second is a reason to stop.

⚠️ **The wire row is not the file record, and one field was renamed.** The row carries
every field these consumers read — `resumed_from`, `role`, `label`, `session_id` — but
the file's `parent_session` is spelled `spawner_session_id` on the wire. A reader that
still asks for `parent_session` gets `None` rather than an error, so the rename has to
be made per consumer rather than discovered.

⚠️ **The ledger's own `status` is not a liveness source.** Measured 2026-10-01: 824 of
its 1075 entries read `running` against 26 live registry sessions. It is the durable
half — who started this, in what mode, from which manager — never the live one.
"""
import errno
import json
import os
import socket
import sys
import time
import urllib.error
import urllib.request

#: The endpoint's path for the ledger. Named once so the reader and `session-liveness.py`
#: cannot drift on a path that, resolved differently, reports an empty fleet — and the
#: empty answer is the dangerous direction.
LEDGER_PATH = "/api/1.0/session-ledger"

#: The attention store's default base, the same one `session-liveness.py`,
#: `attention-ask.py` and `attention-board.py` use.
ENDPOINT_DEFAULT = "http://localhost:18080"

# Kept EQUAL to `session-liveness.py`'s `_HTTP_TIMEOUT` — the two halves of one count must give up
# at the same point, or the refusal names a channel for a reason that is only about timing.
_HTTP_TIMEOUT = 15

# One retry, ~250 ms apart, before a read declares failure — the same rule and the same reason as
# `session-liveness.py`'s pair: the failure this covers is intermittent, and two failed attempts
# still answer `None`.
_READ_ATTEMPTS = 2
_READ_RETRY_DELAY_S = 0.25


def _describe_read_failure(error):
    """The cause of a failed ledger read, in the operator's terms.

    ⚠️ **Duplicated from `session-liveness.py` for the same reason `_get_json` is** — this module
    is loaded BY that one, so importing its helpers back would close the cycle the extraction
    exists to break. Kept in the same vocabulary (`timeout`, `ECONNREFUSED`, `unparseable body`)
    so one failure reads the same whichever reader reported it.
    """
    if isinstance(error, (TimeoutError, socket.timeout)):
        return "timeout"
    if isinstance(error, urllib.error.URLError):
        reason = error.reason
        if isinstance(reason, (TimeoutError, socket.timeout)):
            return "timeout"
        code = getattr(reason, "errno", None)
        name = errno.errorcode.get(code) if code is not None else None
        return "%s (%s)" % (name, reason) if name else "%s: %s" % (type(reason).__name__, reason)
    if isinstance(error, ValueError):
        return "unparseable body: %s" % error
    code = getattr(error, "errno", None)
    name = errno.errorcode.get(code) if code is not None else None
    return "%s (%s)" % (name, error) if name else "%s: %s" % (type(error).__name__, error)


def _log_read_failure(what, cause, started, on_failure=None):
    """One stderr line naming what failed, why, and how long it waited — always `None`."""
    elapsed_ms = int(round((time.monotonic() - started) * 1000))
    message = "%s read failed after %d ms: %s" % (what, elapsed_ms, cause)
    print(message, file=sys.stderr)
    if on_failure is not None:
        on_failure(message)
    return None


def _read_once(url, timeout):
    """`(payload, cause)` — the rows when the read succeeded, the failure cause otherwise."""
    try:
        status, payload = _get_json(url, timeout)
    except (OSError, ValueError) as exc:
        return None, _describe_read_failure(exc)
    if status != 200:
        return None, "HTTP %s" % status
    if not isinstance(payload, list):
        return None, "unparseable body: expected a JSON array, got %s" % type(payload).__name__
    return payload, None


def endpoint_base(override=None):
    """The store's base URL: the argument, then `$ATTENTION_STORE_URL`, then localhost.

    ⚠️ **`$ATTENTION_STORE_URL`, never the retired ledger-directory override.** The ledger
    now lives in the attention store beside the heartbeat, so the variable every other
    store reader already honours is the one that points at it. Honouring the old
    directory variable would be worse than dropping it: a caller that set it would believe
    it had redirected the read while the endpoint was still what answered.

    ⚠️ **The retired variable is named in the CHANGELOG rather than here, on purpose.** It
    is not spelled in this file because the migration probe is a line-based grep over the
    named accessors, and it cannot tell a note explaining the retirement from a live read
    of it — so the name lives where a migration note belongs and this docstring keeps the
    reason. A reader who greps for the variable should look there.
    """
    return (override or os.environ.get("ATTENTION_STORE_URL") or ENDPOINT_DEFAULT).rstrip("/")


def _get_json(url, timeout=_HTTP_TIMEOUT):
    """`(status, payload)` for one GET — the HTTP status whatever it is, `None` on no answer.

    ⚠️ **Duplicated from `session-liveness.py` on purpose, and the duplication is the
    cheaper of the two costs.** That file's `_spawn_ledger()` loads THIS module, so
    importing its helpers back would close the very cycle this module was extracted to
    break, and the cycle is what makes the guard refuse. A shared `attention-endpoint.py`
    is the right home once the remaining readers switch; until then a second copy of
    fifteen lines beats a cycle. ⚠️ **The copy is kept BYTE-FOR-BYTE identical to the
    original, signature included** — a divergence that reads as harmless is what makes a
    later extraction unsafe, and the first draft of this function did diverge on the
    `timeout` default. A non-JSON body is a `None` payload rather than an exception, as
    there.
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


def read_ledger(endpoint=None, timeout=_HTTP_TIMEOUT, on_failure=None):
    """Every session id the endpoint's ledger serves, mapped to its wire row — or None.

    ⚠️ **None and `{}` are different answers and must stay so.** `{}` is "read it, nobody
    is recorded" — a machine that has never spawned a worker — while None is "could not
    read it": an unreachable socket, a timeout, a non-200, or a body that is not a JSON
    array. Collapsing them turns a failure into a confident empty fleet, and for
    `session-liveness.py`'s guard it would turn a failed read into a **passing**
    precondition, which is the resume-authorising direction.

    ⚠️ **An empty store is `{}`, not None.** The endpoint answers `200 []` on a machine
    that has never spawned, and that genuinely means no worker was ever opened here; a
    guard that refused on it would refuse on every fresh machine for no reason. A 404 —
    the route absent from a controller older than this change — is None, and the
    distinction is the whole point: `[]` permits a spawn, 404 must not.

    ⚠️ **The whole row, not just its label.** `worker-sessions.py`'s filter needs
    `resumed_from` and `role`, and a map that kept only the label cannot express either —
    the caller would have to re-read every row to answer a question the read already had
    in hand. The endpoint serves the whole row, so the map keeps it verbatim.
    """
    url = endpoint_base(endpoint) + LEDGER_PATH
    started = time.monotonic()
    cause = "the read was never attempted"
    for attempt in range(1, _READ_ATTEMPTS + 1):
        if attempt > 1:
            time.sleep(_READ_RETRY_DELAY_S)
        payload, cause = _read_once(url, timeout)
        if payload is not None:
            break
        if attempt < _READ_ATTEMPTS:
            _log_read_failure(
                "session-ledger",
                "%s (attempt %d of %d, retrying)" % (cause, attempt, _READ_ATTEMPTS),
                started,
                on_failure,
            )
    else:
        return _log_read_failure("session-ledger", cause, started, on_failure)

    known = {}
    for row in payload:
        if not isinstance(row, dict):
            continue
        session_id = row.get("session_id")
        if session_id:
            known[session_id] = row
    return known
