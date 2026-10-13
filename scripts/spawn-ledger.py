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

⚠️ **Not yet the only ledger reader, and the others are named rather than implied.**
`fleet-sessions.py`, `gate-owner-filter.py`, `stuck-heal.py`, `shutdown-drill.py` and
`check-spawn-ledger.py` each carry their own reader, with their own contract —
`fleet-sessions.py`'s returns `{}` where this returns `None`, and it degrades rather
than refusing because a roster must still render. Consolidating them is a separate
change: each caller would have to adopt this file's unreadable-is-`None` rule, and that
is a behaviour change per caller, not a move. This file exists to break the cycle and to
give the guard one definition; it does not claim to be the only reader yet.

⚠️ **The ledger's own `status` is not a liveness source.** Measured 2026-10-01: 824 of
its 1075 entries read `running` against 26 live registry sessions. It is the durable
half — who started this, in what mode, from which manager — never the live one.
"""
import json
import os

#: The ledger's own directory name under the state root. Named once so the reader and
#: `server/config.mjs` cannot drift on a path that, resolved differently, reports an
#: empty fleet — and the empty answer is the dangerous direction.
_LEDGER_SUBDIR = ("claude-supervisor", "sessions")


def ledger_dir():
    """The spawn ledger's path, resolved as `fleet-sessions.py` and `config.mjs` do.

    `SUPERVISOR_LEDGER_DIR` wins, else `$XDG_STATE_HOME|~/.local/state` +
    `/claude-supervisor/sessions`. Resolved rather than hardcoded so a relocated ledger
    (or an isolated test run) is read without editing this file.

    ⚠️ Deliberately NOT `SUPERVISOR_SESSIONS_DIR`: that name already means the live
    registry (`~/.claude/sessions`), a different store with a different lifetime.
    """
    override = os.environ.get("SUPERVISOR_LEDGER_DIR")
    if override:
        return override
    state = os.environ.get("XDG_STATE_HOME") or os.path.join(os.path.expanduser("~"), ".local", "state")
    return os.path.join(state, *_LEDGER_SUBDIR)


def read_ledger(directory=None):
    """Every session id the ledger knows, mapped to its full record — or None when unreadable.

    ⚠️ **None and `{}` are different answers and must stay so.** `{}` is "read it, nobody
    is recorded" — a machine that has never spawned a worker — while None is "could not
    read it", a permissions or I/O failure. Collapsing them turns a failure into a
    confident empty fleet, and for `session-liveness.py`'s guard it would turn a failed
    read into a **passing** precondition, which is the resume-authorising direction.

    ⚠️ **A missing directory is `{}`, not None.** The writer creates it on the first
    spawn, so its absence genuinely means no worker was ever opened here; a guard that
    refused on it would refuse on every fresh machine for no reason.

    ⚠️ **The whole record, not just its label.** `worker-sessions.py`'s filter needs
    `resumed_from`, and a map that kept only the label cannot express it — the caller
    would have to reopen every file to answer a question the read already had in hand.
    """
    directory = directory or ledger_dir()
    try:
        names = os.listdir(directory)
    except FileNotFoundError:
        return {}
    except OSError:
        return None

    known = {}
    for name in names:
        if not name.endswith(".json"):
            continue
        try:
            with open(os.path.join(directory, name), encoding="utf-8") as handle:
                record = json.load(handle)
        except (OSError, ValueError):
            continue
        if not isinstance(record, dict):
            continue
        session_id = record.get("session_id")
        if session_id:
            known[session_id] = record
    return known
