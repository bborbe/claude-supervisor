#!/usr/bin/env python3
"""Which of these workers' parks is THIS session the owner of — and so may answer?

The fleet round's own-parks step (`commands/steps/own-parks.md`) answers routine permission
parks for the workers the Fleet Manager spawned. A park is this session's to answer only when
the spawn ledger names this session as the worker's spawner. A parked request reaching this
server is not proof of that: `server/registry-rehydrate.mjs` adopts other sessions' workers
into every server's roster (`rehydrated: true` on `list_agents`), so "the request is in my
`pending_permissions`" and "I spawned this worker" are different facts.

⚠️ **Fail closed, every branch.** Only a ledger row whose spawner field equals `--self` reads
`OWN`. An unreadable ledger, a missing row, an empty spawner, a different spawner, an empty
`--self` and an empty worker id all refuse. A check that failed open would let the Fleet
Manager answer a park it cannot prove is its own, which is the act the step exists to bound.

⚠️ **Two spellings, one field.** The file record called the spawn edge `parent_session`, the
endpoint row calls it `spawner_session_id`; both are read, matching `gate-owner-filter.py`
`evaluate` and `server/registry-rehydrate.mjs` `toAgentRecord`.

Usage:
  own-park-check.py --self <session-id> --session <worker-session-id> [--session …]

Output: one line per `--session`, `OWN <sid>` or `REFUSE <sid> <reason>`.
Exit codes: 0 the ledger was read (verdicts on stdout; only `OWN` lines may be answered) ·
2 nothing can be verified (unreadable ledger or empty `--self`) — every session refused.
"""

import argparse
import importlib.util
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))


def _spawn_ledger():
    """Import `spawn-ledger.py` — the ONE ledger reader; never a second endpoint client."""
    spec = importlib.util.spec_from_file_location("spawn_ledger", os.path.join(_HERE, "spawn-ledger.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def spawner_of(row):
    """The spawn edge of one ledger row, under either spelling — `None` when absent."""
    if not isinstance(row, dict):
        return None
    return row.get("spawner_session_id") or row.get("parent_session") or None


def verdict(self_id, worker_sid, ledger):
    """`(own, reason)` for one worker. `ledger` is the read map, or `None` when unreadable."""
    if not self_id:
        return False, "no --self session id"
    if not worker_sid:
        return False, "empty worker session id"
    if ledger is None:
        return False, "spawn ledger unreadable"
    row = ledger.get(worker_sid)
    if row is None:
        return False, "no spawn-ledger row for this worker"
    spawner = spawner_of(row)
    if not spawner:
        return False, "ledger row names no spawner"
    if spawner != self_id:
        return False, "spawned by %s, not this session" % spawner[:8]
    return True, "spawned by this session"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--self", dest="self_id", default="")
    parser.add_argument("--session", action="append", default=[])
    parser.add_argument("--endpoint", default=None)
    args = parser.parse_args(argv)

    ledger = _spawn_ledger().read_ledger(endpoint=args.endpoint) if args.self_id else None
    for sid in args.session:
        own, reason = verdict(args.self_id, sid, ledger)
        print(("OWN %s" % sid) if own else ("REFUSE %s %s" % (sid, reason)))
    return 0 if (args.self_id and ledger is not None) else 2


if __name__ == "__main__":
    sys.exit(main())
