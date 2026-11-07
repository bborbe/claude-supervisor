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

Fail-open direction
-------------------
An unreachable ledger, a 404, a malformed row, or an empty session id all resolve
`session` — the value every producer emitted before this module existed. A
ledger outage must never relabel a card as a manager's; the failure direction is
"behave as before", not "guess manager".

Usage
-----
    # hyphenated filename -> load via importlib, as the sibling scripts do
    spec = importlib.util.spec_from_file_location("session_role", path)
    mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
    kind = mod.resolve_producer_kind(producer_id, declared)

`declared` is the producer's own `--producer-kind` value. Any non-`session` value
(`cron`, `agent`, `dark-factory`) is returned untouched: an explicit declaration
always wins, and only the `session` default is eligible for resolution.
"""

import json
import os
import urllib.request

#: The attention store the supervisor ledger lives in. Same resolution
#: `spawn-ledger.py` uses, so a reader pointed elsewhere than the writer is a
#: configuration error rather than a silent divergence.
STORE = (os.environ.get("ATTENTION_STORE_URL") or "http://localhost:18080").rstrip("/")

#: One row per session, keyed by the session's own id.
LEDGER_PATH = "/api/1.0/session-ledger/"

#: A ledger read is a local HTTP call. Bounded so a wedged store cannot stall a
#: producer that is mid-turn; a timeout resolves `session` like any other failure.
TIMEOUT = float(os.environ.get("ATTENTION_SESSION_ROLE_TIMEOUT", "3") or 3)

#: Role by session id. The role is written once at spawn and never changes, so a
#: long-lived consumer (the watcher) pays one HTTP call per session, not per card.
#: Capped rather than unbounded: the fleet has run thousands of sessions, and a
#: process that lives for weeks must not accumulate a row per session forever.
_CACHE = {}
_CACHE_LIMIT = 4096


def reset_cache():
    """Drop the memoized roles. Tests use this; production never needs it."""
    _CACHE.clear()


def _ledger_role(session_id, endpoint=None):
    """The ledger row's `role` for one session, or "" when it cannot be read.

    Never raises. An unreadable store is not an error a producer can act on: the
    card still has to be posted, and `session` is the pre-change answer.
    """
    base = (endpoint or STORE).rstrip("/")
    url = f"{base}{LEDGER_PATH}{session_id}"
    try:
        with urllib.request.urlopen(url, timeout=TIMEOUT) as resp:
            row = json.loads(resp.read().decode("utf-8") or "{}")
    except Exception:  # noqa: BLE001 — the contract is "never raises"
        # Deliberately broad. `HTTPError` is a `URLError` subclass, so a 404 (no
        # ledger row) lands here with the same answer as a dead store — both mean
        # "no declared role". The broader clause exists because this helper is
        # called from a producer that is mid-turn: a card must still post when
        # the ledger client itself is broken (a patched transport in a test, a
        # response object of an unexpected shape), and `session` is the safe
        # answer in every one of those cases.
        return ""
    if not isinstance(row, dict):
        return ""
    return (row.get("role") or "").strip()


def resolve_producer_kind(session_id, declared="session", endpoint=None):
    """The `producer_kind` an item for `session_id` should carry.

    `declared` wins whenever it names anything but `session` — the producer said
    what it is, and only the default is the defect. Otherwise the session's
    ledger `role` decides: `manager` -> `manager`, everything else -> `session`.
    """
    declared = (declared or "").strip()
    if declared and declared != "session":
        return declared

    session_id = (session_id or "").strip()
    if not session_id:
        return "session"

    key = (session_id, (endpoint or STORE).rstrip("/"))
    if key in _CACHE:
        return _CACHE[key]

    kind = "manager" if _ledger_role(session_id, endpoint) == "manager" else "session"
    if len(_CACHE) >= _CACHE_LIMIT:
        _CACHE.clear()
    _CACHE[key] = kind
    return kind
