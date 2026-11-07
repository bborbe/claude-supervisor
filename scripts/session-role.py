#!/usr/bin/env python3
"""Resolve a session's DECLARED role into an attention item's `producer_kind`.

Why this exists
---------------
Every attention producer hardcoded `producer_kind: "session"`, or defaulted a
`--producer-kind` flag to it. A manager session's card therefore reached the
store labelled `session` unless the caller remembered to pass the flag — and the
board's default view filters on `manager`, so the population the operator triages
was near-empty by construction. A flag that must be remembered is the defect.

`manager` is **declared, never derived**
----------------------------------------
`docs/role-mode-matrix.md` Rule 1: `manager` is declared by whoever files the
row, and only that. So this module reads exactly one source — the supervisor
session ledger's explicit `role` field — and never infers a manager from
absence-from-ledger (that is provenance, not role: see the KB page *A Provenance
Marker Read as a Role Signal*), from a session's name, or from its terminal
colour.

The vault task's `role:` frontmatter is the other declared source, but it is not
read here: measured 2026-11-07 it has no live population (the 34 `role: manager`
task files are all in a test-fixture vault), and a full vault walk costs 132 s
over 50,530 files — unusable on a per-post path.

The card's `producer_id`, never the caller
------------------------------------------
Resolution keys on the id the item is *about*, not on the process that invoked
the producer. `manager-attention-watch.py` posts on behalf of a parked worker:
the invoking process is a manager, the `producer_id` is the worker, and the card
must read `session`. Callers pass the same id they put in the payload's
`producer_id`; passing `$CLAUDE_CODE_SESSION_ID` instead would relabel exactly
the cards this module exists to get right.

Fail-open direction, and why failures are NOT cached
----------------------------------------------------
An unreachable ledger, a 404, a malformed row, or an empty session id all resolve
`session` — the value every producer emitted before this module existed. A
ledger outage must never relabel a card as a manager's.

A failed read is deliberately **not** memoized. The watcher is a weeks-long
process, so caching a transient blip would pin that session to `session` for the
rest of the process's life — silently, and exactly for the manager it should have
resolved. Only a successful read is cached, and the role is written once at
spawn, so the cache is still one HTTP call per session rather than per card.

Usage
-----
    # hyphenated filename -> load via importlib, as the sibling scripts do
    spec = importlib.util.spec_from_file_location("session_role", path)
    mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
    kind = mod.resolve_producer_kind(producer_id, declared)

`declared` is the producer's own `--producer-kind` value. Any non-`session` value
(`cron`, `agent`, `dark-factory`) is returned untouched: an explicit declaration
always wins, and only the `session` default is eligible for resolution.

`ssl_context` and `headers` exist because a producer that already supports an
HTTPS or authenticated store must not silently lose that here — `attention-ask.py`
passes its own `_ssl_context()` / `_headers()`. The default (None) is the plain
local HTTP store the watcher already talks to.
"""

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

#: The attention store the supervisor ledger lives in. Same resolution
#: `spawn-ledger.py` uses, so a reader pointed elsewhere than the writer is a
#: configuration error rather than a silent divergence.
STORE = (os.environ.get("ATTENTION_STORE_URL") or "http://localhost:18080").rstrip("/")

#: One row per session, keyed by the session's own id.
LEDGER_PATH = "/api/1.0/session-ledger/"


def _timeout():
    """The ledger read bound, validated rather than assumed.

    A malformed or non-positive `ATTENTION_SESSION_ROLE_TIMEOUT` falls back to
    the default: `urlopen(timeout=0)` raises on every call, which would disable
    resolution entirely while looking configured.
    """
    raw = os.environ.get("ATTENTION_SESSION_ROLE_TIMEOUT", "3")
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return 3.0
    return value if value > 0 else 3.0


#: A ledger read is a local HTTP call. Bounded so a wedged store cannot stall a
#: producer that is mid-turn; a timeout resolves `session` like any other failure.
TIMEOUT = _timeout()

#: Role by session id, successful reads only. The role is written once at spawn
#: and never changes, so a long-lived consumer pays one HTTP call per session, not
#: per card. Capped rather than unbounded: the fleet has run thousands of
#: sessions, and a process that lives for weeks must not accumulate a row per
#: session forever.
_CACHE = {}
_CACHE_LIMIT = 4096


def reset_cache():
    """Drop the memoized roles. Tests use this; production never needs it."""
    _CACHE.clear()


def _ledger_role(session_id, endpoint=None, ssl_context=None, headers=None):
    """The ledger row's `role` for one session.

    Returns the role string (possibly "") on a successful read, and **None** when
    the read failed — the caller must not cache the difference away.

    Never raises: the contract is that a producer mid-turn can always ask.
    """
    base = (endpoint or STORE).rstrip("/")
    # ⚠️ `session_id` is caller-supplied (`--producer-id`), so it is quoted: a
    # value carrying `/`, `?` or `#` would otherwise rewrite the request path or
    # query against the store API rather than addressing one row.
    url = f"{base}{LEDGER_PATH}{urllib.parse.quote(session_id, safe='')}"
    request = urllib.request.Request(url, headers=dict(headers or {}))
    try:
        with urllib.request.urlopen(
            request, timeout=TIMEOUT, context=ssl_context
        ) as resp:
            row = json.loads(resp.read().decode("utf-8") or "{}")
    except (urllib.error.URLError, OSError, ValueError):
        # HTTPError is a URLError subclass, so a 404 (no ledger row) lands here
        # with the same answer as a dead store — both mean "no declared role".
        # Neither is worth a line: an unspawned session is the common case.
        return None
    except Exception as err:  # noqa: BLE001 — the contract is "never raises"
        # Anything else is unexpected and would otherwise be invisible: a rule
        # that dies silently reads exactly like a rule that is working.
        print(f"session-role: ledger read failed ({err!r})", file=sys.stderr)
        return None
    if not isinstance(row, dict):
        return None
    # `role` is typed string in the ledger, but a malformed row must not raise
    # out of a helper documented as never raising.
    return str(row.get("role") or "").strip()


def resolve_producer_kind(
    session_id,
    declared="session",
    endpoint=None,
    ssl_context=None,
    headers=None,
):
    """The `producer_kind` an item for `session_id` should carry.

    `declared` wins whenever it names anything but `session` — the producer said
    what it is, and only the default is the defect. Otherwise the session's
    ledger `role` decides: `manager` -> `manager`, everything else -> `session`.
    """
    declared = str(declared or "").strip()
    if declared and declared != "session":
        return declared

    session_id = str(session_id or "").strip()
    if not session_id:
        return "session"

    key = (session_id, (endpoint or STORE).rstrip("/"))
    if key in _CACHE:
        return _CACHE[key]

    role = _ledger_role(session_id, endpoint, ssl_context, headers)
    if role is None:
        # Unreadable — answer the pre-change value and do NOT memoize it, or one
        # transient blip would pin this session to `session` for the life of a
        # weeks-long consumer.
        return "session"

    kind = "manager" if role == "manager" else "session"
    if len(_CACHE) >= _CACHE_LIMIT:
        _CACHE.clear()
    _CACHE[key] = kind
    return kind
