#!/usr/bin/env python3
"""Is this session id live? One instrument, one answer, for the whole plugin.

Liveness is answered by the attention store's **session-heartbeat endpoint** and by nothing
else. Every session that can be asked about stamps that store — an interactive tab, a headless
in-process worker, a cluster pod — so one channel answers for all of them, and the verdict is
the store's own `live` boolean (its age against its window), never a recomputation here.

  --check <session-id>   LIVE (exit 0) / ABSENT (exit 1) / UNKNOWN (exit 2) / AMBIGUOUS (exit 3)
  --list                 one line per live session: `<session-id>  <where>  <name>  [<source>]`
  --coverage             endpoint-live vs registry-live counts; exit 1 when a registry-live
                         session has no live endpoint row

⚠️ **An id argument is a PREFIX, and a prefix is legal.** Callers pass prefixes: the sweep
digest carries 8-char prefixes, `[ref]` in a roster is 6 chars, `sid8` is the display
convention. A contract that rejects the input its own callers produce is the defect, not the
fix. A unique match resolves; an ambiguous one refuses rather than guesses — the semantics
`who-needs-me.py:pane_for()` already settled. The per-id route cannot resolve a prefix
(measured 2026-10-08: `/session-heartbeat/30fae0ae` → 404 for a live session), so a prefix is
resolved against the list route — and the list route is fetched FIRST, so a prefix costs ONE
round trip rather than the old 404-then-list pair; see `_check()`.

⚠️ **UNKNOWN is a third answer, not a flavour of ABSENT.** An unreachable endpoint, or a
response that is not 200 and not the store's 404, means the probe could not run — and a caller
that folds that into "not live" turns an I/O error into permission to resume, which is exactly
the double-writer this file exists to prevent. Exit 2 is deliberately distinct from exit 1.
Same rule as `live-workers.py`.

⚠️ **ABSENT is licensed only by a READABLE endpoint that positively reports the id as not
live** — a 200 carrying `live: false`, or a 404 from the store, or a readable list with no
matching id. Nothing else may reach it.

⚠️ **ABSENT is licensed only while the endpoint's coverage of live sessions is COMPLETE, and
that is a precondition this file cannot establish for itself.** Every verdict rests on the
store's `session-heartbeat` endpoint holding a row for EVERY live session — an interactive tab,
a headless worker, a cluster pod. While the fleet is fully stamped that holds; during a partial
rollout it does not, and then a session that is live but not yet stamping the store reads as
ABSENT. ⚠️ **That is the dangerous direction: ABSENT is the one verdict that authorises a
resume, so a partial rollout turns a live conversation into a second writer.** So do not deploy
this endpoint ahead of the sessions that must stamp it, and do not trust ABSENT until the
coverage has been MEASURED. `--coverage` is that measurement: it counts the endpoint's live
rows against the identity registry's live sessions and exits non-zero while a registry-live
session has no live endpoint row. See `coverage()`.

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
_LIVE_WORKERS = None


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


def _live_workers():
    """Import `live-workers.py` (hyphenated filename -> importlib) — the reachability marker.

    Lazy and cached, and here the laziness is the point: the marker is consulted ONLY for a
    not-live row whose `source` is `cluster` (see `_verdict_for`), so a probe that never meets
    one must not pay to load the sibling. Same importlib idiom as `_identity()` above — a
    hyphenated filename is not an importable module name.
    """
    global _LIVE_WORKERS
    if _LIVE_WORKERS is None:
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "live-workers.py")
        spec = importlib.util.spec_from_file_location("live_workers", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _LIVE_WORKERS = mod
    return _LIVE_WORKERS


def _cluster_reachable():
    """True when the mirror refreshed the cluster reachability marker inside its TTL.

    ⚠️ **This is the signal the endpoint does NOT carry, and losing it was a safety
    regression.** On the wire a heartbeat row's `state` is the session's ACTIVITY
    (`busy` / `idle` / `waiting-on-operator`), never whether the cluster was reachable — so the
    reachability fact lives only in the LOCAL heartbeat store, in `_cluster-reachability.json`,
    which `live-workers.py` reads. A cluster worker's stamp goes stale the moment the mirror
    stops reading the cluster, and a stale stamp is indistinguishable from a dead worker in the
    store; without the marker a network fault would read as ABSENT and authorise a resume onto a
    worker that is alive behind it.

    ⚠️ **Reading the marker is not reading the registry.** The heartbeat store is a different
    directory from the session registry, so SC9's "no registry read" holds; this file still
    never opens the registry on the verdict path.

    Resolution (which directory, how old is old) is DELEGATED to `live-workers.py` rather than
    re-derived here, so the two readers cannot drift: the file that writes the marker's meaning
    is the file that reads it. A missing marker is `False` (no mirror has run) — which errs
    toward UNKNOWN for a cluster row, the safe direction.

    ⚠️ **Three states, not two: `True`, `False`, and `None`.** `False` is the definite "no mirror
    has run" (or the marker aged out); `None` is "the marker's meaning could not be resolved at
    all" — `live-workers.py` could not be imported or executed, or its API moved. `None` is
    returned rather than raised, because this runs ON the ABSENT-licensing path (a not-live
    `source: cluster` row) and an exception here would otherwise escape `_check` as exit 1 =
    ABSENT — turning "cluster reachability unknown" into "the worker is dead". `_verdict_for`
    folds both `False` and `None` to UNKNOWN.
    """
    try:
        lw = _live_workers()
        return lw.cluster_reachable(lw.heartbeat_dir())
    except Exception:  # noqa: BLE001 — an unresolvable marker is UNKNOWN, never a confident ABSENT
        return None


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
    """`(verdict, message, row)` for one id argument — `_check` guarded so it never crashes.

    ⚠️ **An exception escaping here would exit 1, which is this file's ABSENT code, so no
    exception may escape.** `main()` returns `_check`'s verdict to `sys.exit()`, and that call is
    reached only if `main()` returns at all — an uncaught exception propagates past it and Python
    exits 1, the very code every caller maps to "not live, resume is authorised". That is the
    defect class this file's history already records for `_load_start_cache`'s `OverflowError`:
    an unexpected failure answered as permission to start a second writer. So the WHOLE probe is
    wrapped: a `check()` that cannot complete answers UNKNOWN (exit 2), never a crash that reads
    as ABSENT. `_check` also returns UNKNOWN on its own terms for the two failures it can name —
    a non-string `session_id`, and an unresolvable cluster-reachability marker.
    """
    try:
        return _check(session_id, endpoint, timeout)
    except Exception as exc:  # noqa: BLE001 — any escape here is ABSENT-shaped to a caller
        return UNKNOWN, "session-liveness could not complete the check for %r: %s: %s" % (
            (session_id or "").strip(),
            type(exc).__name__,
            exc,
        ), None


def _check(session_id, endpoint=None, timeout=_HTTP_TIMEOUT):
    """`(verdict, message, row)` for one id argument, from the endpoint alone.

    ⚠️ **The list route is fetched FIRST, and that is a cost fix, not a taste.** The per-id
    route is an EXACT lookup and 404s on a prefix (measured 2026-10-08:
    `/session-heartbeat/30fae0ae` → 404 for a live session), so resolving a prefix through it
    cost TWO round trips — the 404, then the whole-store list. Prefixes are what the fleet
    actually passes (`agents/fleet-sweep-reader.md` documents 8-char prefixes), so every probe
    doubled into a full-store fetch. The list route resolves BOTH a full id and a prefix in ONE
    fetch, so it is the primary route and the argument is matched against it:

      1. the **list** route. HTTP 200 carries every row the store holds — live and stale. An
         EXACT match is a dict lookup (the fast path, no prefix scan); otherwise the argument is
         a PREFIX and is matched by `startswith`. ⚠️ **Any status other than 200, and any
         unreachable endpoint, is UNKNOWN — never ABSENT.**
      2. only when step 1 was readable and matched NOTHING, the **per-id** route confirms the
         would-be ABSENT — because ABSENT is the one verdict that authorises a resume, it is the
         one verdict that pays for a second look. A 404 → ABSENT; a row → LIVE/ABSENT by its
         `live`; anything else → UNKNOWN.

    ⚠️ **The id is percent-encoded into the per-id URL (`quote(sid, safe="")`), and that is the
    only thing between a hostile argument and a path traversal or a query injection.** The id
    comes from a roster digest rather than a user, but it is still an argument — the suite drives
    `../`, `?` and `%2f` through it and asserts the wire never carries them raw.

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
        status, rows = _get_json(base + HEARTBEAT_PATH, timeout)
    except (OSError, ValueError):
        return UNKNOWN, "session-heartbeat endpoint unreachable at %s" % base, None
    if status != 200 or not isinstance(rows, list):
        # ⚠️ **Not ABSENT.** A 5xx, a 403, a body that is not JSON — none of them is the store
        # saying "this session is gone", and reading one as ABSENT is the resume-onto-a-live-
        # conversation this file exists to prevent.
        return UNKNOWN, "session-heartbeat endpoint returned HTTP %s" % status, None

    # The list carries every session the store has ever seen, live or stale — a stale row is
    # matched too, exactly as the registry's own keys were: the argument identifies a session,
    # and whether that session is live is the verdict that follows, not part of the match.
    # ⚠️ **A `session_id` that is not a string is UNKNOWN, never matched and never ABSENT** — the
    # same rule `_verdict_for` applies to a non-boolean `live`: the argument is a string, so a row
    # whose id is not a string cannot be compared to it, and "I cannot rule this row out" is not
    # "this session is gone". Checking the type here (rather than trusting the `.lower()` below)
    # is also what keeps an `AttributeError` from escaping `_check` as exit 1 = ABSENT.
    malformed = [
        r for r in rows
        if isinstance(r, dict) and r.get("session_id") and not isinstance(r.get("session_id"), str)
    ]
    if malformed:
        return UNKNOWN, (
            "the session-heartbeat endpoint served %d row(s) whose `session_id` is not a string "
            "— cannot match the argument against them" % len(malformed)
        ), None
    by_id = {
        r["session_id"]: r
        for r in rows
        if isinstance(r, dict) and r.get("session_id")
    }
    # ⚠️ **`by_id` spans STALE rows too, so a prefix shared with a long-dead session now reads
    # AMBIGUOUS where it used to resolve.** The old per-id route was an exact lookup that only
    # ever saw a live row; matching against the whole list means a prefix can collide with a row
    # the store has kept since a session died. That is a trade-off, not a bug: it fails SAFE —
    # AMBIGUOUS is a refusal, never a wrong LIVE or ABSENT — and the caller resolves it by
    # passing a longer id, which the AMBIGUOUS message names. See the module docstring on why a
    # prefix is legal input in the first place.
    #
    # ⚠️ **The full-id case now transfers the WHOLE store, not one row.** The list route is
    # fetched first so a prefix costs one round trip instead of two, and that makes a full-id
    # probe pay for every row the store holds — the price of resolving both input shapes on one
    # route. The exact-match fast path below is still a single dict lookup, so the added cost is
    # bandwidth, not CPU.
    # The exact-match fast path: a full id is one dict lookup, and the prefix scan is skipped.
    exact = next((k for k in by_id if k.lower() == sid), None)
    if exact is not None:
        return _verdict_for(by_id[exact], sid)
    matches = sorted(sid_ for sid_ in by_id if sid_.lower().startswith(sid))
    if len(matches) > 1:
        # 12 chars, not 8: the caller already passed a prefix long enough to collide, so
        # echoing 8 back hands them two identical strings and no way to tell them apart.
        return AMBIGUOUS, "%d session ids match %s — pass a longer id: %s" % (
            len(matches),
            sid,
            ", ".join(m[:12] for m in matches),
        ), None
    if len(matches) == 1:
        return _verdict_for(by_id[matches[0]], sid)

    # The list was readable and matched nothing. Confirm against the per-id route before
    # licensing ABSENT — the one verdict a caller acts on by starting a second writer.
    try:
        status, row = _get_json(
            "%s%s/%s" % (base, HEARTBEAT_PATH, urllib.parse.quote(sid, safe="")), timeout
        )
    except (OSError, ValueError):
        return UNKNOWN, "session-heartbeat endpoint unreachable at %s" % base, None
    if status == 200 and isinstance(row, dict):
        return _verdict_for(row, sid)
    if status == 404:
        return ABSENT, "no session id matches %s" % sid, None
    return UNKNOWN, "session-heartbeat endpoint returned HTTP %s for %s" % (status, sid), None


def _verdict_for(row, sid):
    """`(verdict, message, row)` for a single heartbeat row. UNKNOWN when `live` is unusable.

    ⚠️ **A not-live CLUSTER row is UNKNOWN while the local reachability marker is stale.** The
    endpoint's `state` field is the session's ACTIVITY and says nothing about reachability, so
    the fact that the cluster could not be read survives only in the local heartbeat store — see
    `_cluster_reachable`. A cluster worker whose stamp has gone stale because the mirror lost the
    cluster is a worker that may be ALIVE behind a network fault; folding that into ABSENT would
    authorise a resume onto it. `UNKNOWN` never authorises a resume, so it is the safe direction.
    A cluster row with a FRESH marker, and any non-cluster row, keep their ABSENT: there the
    staleness is the ordinary death case.
    """
    full = row.get("session_id") or sid
    live = row.get("live")
    if live is True:
        return LIVE, full, row
    if live is False:
        if row.get("source") == "cluster":
            reachable = _cluster_reachable()
            # `True` is the only value that licenses ABSENT here. `False` is a stale marker (no
            # mirror has run, or it aged out); `None` is a marker whose meaning could not be
            # resolved (the `live-workers.py` sibling unavailable). Both mean the cluster could
            # not be read, so the row's staleness proves nothing — see `_cluster_reachable`.
            if reachable is not True:
                why = (
                    "the local reachability marker could not be resolved (`live-workers.py` is "
                    "unavailable or unreadable)"
                    if reachable is None
                    else "the local reachability marker is stale"
                )
                return UNKNOWN, (
                    "%s is a cluster session and %s — the cluster could not be read, so this "
                    "row's staleness proves nothing" % (full, why)
                ), row
        return ABSENT, full, row
    return UNKNOWN, (
        "%s has a heartbeat row with no usable `live` flag — cannot decide liveness" % full
    ), row


def coverage(endpoint=None, registry_dir=None, timeout=_HTTP_TIMEOUT):
    """`(verdict, line)` — the countable stamp-coverage state the ABSENT precondition needs.

    ⚠️ **It reads the endpoint and `session-identity.py` and NOTHING else** — deliberately not
    the heartbeat store's stamps, so it measures the same two sources `--check` decides from. It
    answers one question: does every session the identity registry calls live have a live row at
    the endpoint? `endpoint_live` counts the endpoint's `live: true` rows; `registry_live`
    counts the registry's NON-DEAD sessions (`alive is not False` — a session whose pid is
    occupied but whose identity cannot be proven is still one that must be stamped, so counting
    only `alive is True` would read a partial rollout as complete, the exact direction that makes
    ABSENT unsafe); `missing` is the difference.

    Exit codes: 0 complete, 1 incomplete (a registry-live session has no live endpoint row), 2
    the measurement could not be taken (an unreadable endpoint or registry) — never a fabricated
    `0`, because "could not measure" and "measured complete" are different facts.
    """
    rows = read_endpoint(endpoint, timeout)
    if rows is None:
        return UNKNOWN, "UNKNOWN — cannot read the session-heartbeat endpoint at %s" % endpoint_base(endpoint)
    identity = _identity().read_registry(registry_dir)
    if identity is None:
        return UNKNOWN, "UNKNOWN — cannot read the identity registry (%s)" % (registry_dir or "default path")
    # ⚠️ **The `isinstance(..., str)` guard is what keeps a malformed row from crashing the
    # measurement.** A row carrying `live: true` and a non-string `session_id` (a list or a
    # dict) is unhashable, so the set build would raise `TypeError` out of `main()` and Python
    # would exit 1 — this file's ABSENT code. `_check` guards the identical case at the verdict
    # path; the diagnostic path is not a place to be looser.
    endpoint_live = {
        r["session_id"]
        for r in rows
        if isinstance(r, dict)
        and isinstance(r.get("session_id"), str)
        and r["session_id"]
        and r.get("live") is True
    }
    registry_live = {sid for sid, rec in identity.items() if rec.get("alive") is not False}
    missing = registry_live - endpoint_live
    line = "endpoint_live=%d registry_live=%d missing=%d" % (
        len(endpoint_live),
        len(registry_live),
        len(missing),
    )
    return (ABSENT if missing else LIVE), line


def safe_coverage(endpoint=None, registry_dir=None, timeout=_HTTP_TIMEOUT):
    """`coverage()` guarded so no exception can escape `main()` as exit 1.

    ⚠️ **Same invariant `check()` carries, and for the same reason.** `coverage()` reaches the
    registry through `_identity()`, which `exec_module`s a sibling file with no guard — so a
    missing or unparseable `session-identity.py` raises straight out of it, and `main()`
    returning is the only thing between that and `sys.exit(1)`. One is this file's ABSENT code,
    which every caller reads as "not live, resume is authorised"; a diagnostic that cannot be
    taken is UNKNOWN (exit 2), never a crash wearing ABSENT's exit code. The suite already
    treats an unimportable sibling as a real case (`load_from()` isolates exactly that), so
    leaving this path unguarded while `check()` is guarded end-to-end was an oversight rather
    than a decision.
    """
    try:
        return coverage(endpoint, registry_dir, timeout)
    except Exception as exc:  # noqa: BLE001 — any escape here is ABSENT-shaped to a caller
        return UNKNOWN, "UNKNOWN — the stamp-coverage measurement could not be taken: %s: %s" % (
            type(exc).__name__,
            exc,
        )


def unstamped(endpoint=None, registry_dir=None, timeout=_HTTP_TIMEOUT):
    """`(verdict, line)` — registry-live sessions the endpoint holds NO row for at all.

    ⚠️ **This is NOT `coverage()`, and the difference is the whole point of it existing.**
    `coverage()`'s `missing` is `registry_live - endpoint_live`, which folds two unlike facts
    into one number: a session the endpoint has never heard of — a genuine ROLLOUT GAP, where
    a list would omit a session that is really live — and a session it holds a row for that
    reads `live: false`, which is a dead or hung session the endpoint can see and is answering
    about. Only the first is a reason to withhold a list.

    ⚠️ **Measured, not reasoned:** applying `coverage()` to the non-empty `--list` path refused
    an ordinary steady state — a registered session whose heartbeat had lapsed, planted by
    `test_list_shows_only_live_sessions`, which expects the live one to be listed. The
    regression surfaced as that suite's own failure, which is why this measures row PRESENCE
    rather than liveness.

    `--coverage` keeps its own definition: it is a fleet metric about stamping, not this
    precondition, and its `missing` column is deliberately the broader one. Exit codes match
    `coverage()`'s: 0 complete, 1 a registry-live session has no row, 2 the measurement could
    not be taken (never a fabricated `0`).
    """
    rows = read_endpoint(endpoint, timeout)
    if rows is None:
        return UNKNOWN, "UNKNOWN — cannot read the session-heartbeat endpoint at %s" % endpoint_base(endpoint)
    identity = _identity().read_registry(registry_dir)
    if identity is None:
        return UNKNOWN, "UNKNOWN — cannot read the identity registry (%s)" % (registry_dir or "default path")
    stamped = {
        r["session_id"]
        for r in rows
        if isinstance(r, dict) and isinstance(r.get("session_id"), str) and r["session_id"]
    }
    registry_live = {sid for sid, rec in identity.items() if rec.get("alive") is not False}
    unstamped_ids = registry_live - stamped
    return (ABSENT if unstamped_ids else LIVE), "unstamped=%d registry_live=%d" % (
        len(unstamped_ids),
        len(registry_live),
    )


def safe_unstamped(endpoint=None, registry_dir=None, timeout=_HTTP_TIMEOUT):
    """`unstamped()` guarded — see `safe_coverage` for why no exception may escape `main()`."""
    try:
        return unstamped(endpoint, registry_dir, timeout)
    except Exception as exc:  # noqa: BLE001 — any escape here is ABSENT-shaped to a caller
        return UNKNOWN, "UNKNOWN — the stamp-coverage measurement could not be taken: %s: %s" % (
            type(exc).__name__,
            exc,
        )


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
    parser.add_argument(
        "--coverage",
        action="store_true",
        help="print the countable stamp-coverage state — how many live sessions the endpoint "
        "reports, how many the identity registry holds, and the difference — and exit non-zero "
        "while a registry-live session has no live endpoint row (the state in which ABSENT is "
        "unsafe)",
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

    if args.coverage:
        # ⚠️ A countable line on stdout and nothing else, so `$(... --coverage)` reads a value;
        # the unmeasurable case keeps its message on stderr, where `$(...)` cannot capture it.
        # Exit 1 while the fleet is not fully stamped — see the module docstring on why a partial
        # rollout makes ABSENT unsafe.
        verdict, line = safe_coverage(args.endpoint, args.dir)
        if verdict == UNKNOWN:
            print(line, file=sys.stderr)
        else:
            print(line)
        return verdict

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

    # Only a live row is listed — the store keeps a row for a session long after it dies, and a
    # `--list` that echoed them would count a dead fleet as a live one.
    live_rows = [
        row for row in rows
        if isinstance(row, dict) and row.get("session_id") and row.get("live") is True
    ]
    if not live_rows:
        # ⚠️ **An empty list is a POSITIVE claim about the whole fleet, so it pays the coverage
        # precondition — the same one ABSENT pays, and for the same reason.** "No live rows"
        # reads as "no session is live anywhere", which is exactly the ABSENT shape: during a
        # partial rollout the endpoint holds no live row while the identity registry still lists
        # live sessions, and `commands/open.md` Step 2C reads THIS call to decide whether to open
        # a session — so a confident empty fleet there spawns a second session onto a live topic
        # (the 2026-09-18 failure the non-empty path's names rule already guards against).
        # `unstamped()` is the measurement: it is LIVE only when every registry-live session has
        # a ROW at the endpoint — not necessarily a live one, since a row reading `live: false`
        # is a dead session the endpoint is answering about rather than a rollout gap (see that
        # function's docstring). Anything else — an unreadable registry, or a registry-live
        # session the endpoint has never heard of — is UNKNOWN, never an empty list presented
        # as complete.
        #
        # ⚠️ **This is stricter than the earlier "decide empty before the registry" ordering**,
        # which returned exit 0 whenever the registry could not be read. That direction reads an
        # unmeasurable fleet as an empty one, and empty is the resume-authorising shape, so it is
        # wrong even though it kept a genuinely idle fleet from looking like a failed probe — and
        # a genuinely idle fleet still measures LIVE here (endpoint empty AND registry empty), so
        # it keeps its empty list.
        verdict, line = safe_unstamped(args.endpoint, args.dir)
        if verdict != LIVE:
            print(
                "UNKNOWN — no live row at the session-heartbeat endpoint, but it has no row at "
                "all for some registry-live session (%s), so an empty list cannot be presented "
                "as a complete fleet" % line,
                file=sys.stderr,
            )
            return UNKNOWN
        if args.json:
            json.dump([], sys.stdout, indent=2)
            sys.stdout.write("\n")
            return LIVE
        print("no live sessions on the session-heartbeat endpoint")
        return LIVE

    # ⚠️ **A NON-EMPTY list is a positive claim about the whole fleet too, so it pays the same
    # coverage precondition the empty one pays — and it is the MORE dangerous of the two to
    # skip.** The rows below are the endpoint's `live: true` rows joined to identity; during a
    # partial rollout the endpoint holds a row only for already-stamped sessions, so every
    # live-but-unstamped session is dropped while the command still exits 0. A caller reads
    # that as the whole live fleet. `commands/open.md` Step 2C matches `<topic> Manager`
    # against `name`/`formerNames` from exactly this output and spawns a manager on no match,
    # so the omission is a duplicate manager onto a live topic — the 2026-09-18 failure the
    # names rule above exists to prevent, reached by a different route. The base version could
    # not produce this answer: its registry half contributed rows regardless of heartbeat
    # state. Reading the registry is not enough to license the list — a readable registry is
    # precisely what makes the omission silent, since it proves the omitted sessions EXIST.
    #
    # ⚠️ **The measurement is `unstamped()`, not `coverage()`** — see that function's docstring.
    # `coverage()`'s `missing` also counts a registered session whose row reads `live: false`,
    # which is a dead or hung session the endpoint is answering about rather than a rollout
    # gap, and refusing on it broke `test_list_shows_only_live_sessions`.
    verdict, line = safe_unstamped(args.endpoint, args.dir)
    if verdict != LIVE:
        print(
            "UNKNOWN — the session-heartbeat endpoint holds live rows, but it has no row at "
            "all for some registry-live session (%s), so this list would omit live sessions "
            "that have not been stamped yet and cannot be presented as the whole live fleet"
            % line,
            file=sys.stderr,
        )
        return UNKNOWN

    identity = _identity().read_registry(args.dir)
    if identity is None:
        print("UNKNOWN — cannot read the identity registry (%s)" % (args.dir or "default path"), file=sys.stderr)
        return UNKNOWN

    payload = []
    for row in live_rows:
        sid = row.get("session_id")
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

    for rec in sorted(payload, key=lambda r: r["sessionId"]):
        pid = rec["pid"]
        where = "pid %s" % pid if pid is not None else "age %ss" % rec["age_seconds"]
        print("%s  %s  %s  [%s]" % (rec["sessionId"], where, rec["name"] or "(no name)", rec["source"]))
    return LIVE


if __name__ == "__main__":
    sys.exit(main())
