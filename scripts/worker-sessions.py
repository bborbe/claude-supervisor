#!/usr/bin/env python3
"""Count live **worker sessions** — the fleet-wide worker target's unit.

The target bounds the laptop's load, and that load is every worker the supervisor opened:
interactive tabs, headless in-process workers, and cluster workers running as pods. The
instrument that shipped for it read the headless heartbeat store, which is stamped only
for in-process workers (`server/supervisor.mjs` writes `mode: 'headless'`, from
`server/agent-loop.mjs`) — so it answered **0 while 11 interactive workers were live**
(measured 2026-10-01). A target that reads 0 on a busy fleet is inert: managers always see
"below target" and always propose, and the cap never binds.

  --list    one line per live worker session: `<session-id>  <status>  <label>`
  --count   just the number of live worker sessions, and nothing else on stdout

**The definition takes one identity store and one liveness source.** A worker session is one
that is

  (a) present in the **supervisor spawn ledger** (`~/.local/state/claude-supervisor/sessions/`)
      — which is what makes it a session the supervisor opened, rather than a manager or one
      of the operator's own; and
  (b) **live**, which the **session-heartbeat endpoint** answers.

⚠️ **The liveness source is the endpoint, and the join survived the move.** Until 2026-10-09
this read two channels — the registry (`~/.claude/sessions/<pid>.json`, pid-keyed and
local-only, its entry deleted on exit) and the heartbeat store — as a UNION, because neither
answered alone: the registry saw every session holding a socket and was structurally blind to a
worker with no pid of its own (a headless worker is an in-process SDK `query()` inside the
server; a cluster worker is a process on another machine), while the store covered exactly
those. The endpoint holds BOTH populations in one store — it serves `source: cluster` and
`source: mcp-timer` rows beside local ones — so two channels collapse into one read and the
composition rule goes with them. **Operator ruling 2026-10-09: keep the ledger join, move only
the liveness source.**

⚠️ **The count legitimately goes UP, and that is the point of the move.** The registry was
blind to headless workers, so a fleet that was silently under-counting now counts what it was
rendering dead. Measured 2026-10-09 on this host: **11** under the registry join, **22** under
the endpoint. `docs/fleet-surface.md` § Spawn a worker item 5 already carries the warning —
re-read `spawn.maxConcurrent` before the first sweep after upgrading, because the first tick
can read **at or over** the target with no error on either side.

  * The **ledger's own `status` is not a liveness source.** Measured 2026-10-01: 824 of its
    1075 entries read `running` against 26 live registry sessions. It is the durable half —
    who started this, in what mode, from which manager — never the live one.
  * **`status` in the output is the store row's `source`** — `mcp-timer` for a session posting
    its own heartbeat, `cluster` for a mirrored cluster session. It is no longer the registry's
    own status nor a stamp's `mode`, because the endpoint is the only channel that answers.

⚠️ **A manager ever opened through `spawn_agent` would be counted**, because it would have a
ledger record like any worker. The one live example found, "Attention Routing", is a
2026-09-23 ledger entry whose session is long gone, so the liveness join filters it — but the
limit is real and is stated rather than hidden. Managers are normally started by hand, which
is why all four measured here had zero ledger entries.

⚠️ **An unreadable store is UNKNOWN, never 0.** The ledger and the endpoint are read before any
verdict, and either failing exits 2 with its message on **stderr** — so `$(… --count)` captures
nothing and a caller must consult the exit code. Folding an I/O error into "no workers" is how
a broken instrument reads as an idle fleet, which is the failure this file exists to correct.
"""
import argparse
import importlib.util
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))

_SPAWN_LEDGER = None


def _load(module_name, filename):
    """Import a hyphenated sibling script by path — the same idiom `session-liveness.py`
    uses for `live-workers.py`, so the endpoint gains no second reader."""
    path = os.path.join(HERE, filename)
    spec = importlib.util.spec_from_file_location(module_name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _ledger():
    """Import `spawn-ledger.py` (hyphenated filename -> importlib) — the spawn-ledger reader.

    ⚠️ **Extracted, and the extraction is load-bearing rather than tidiness.** This file
    used to hold the only copy of the reader, while `session-liveness.py`'s `unstamped()`
    guard needs the same ledger and this file reaches `session-liveness.py` through `_load`
    — so the guard could not import the ledger from here without closing a cycle. One
    module both consumers point at leaves the dependency direction one-way.
    """
    global _SPAWN_LEDGER
    if _SPAWN_LEDGER is None:
        _SPAWN_LEDGER = _load("spawn_ledger", "spawn-ledger.py")
    return _SPAWN_LEDGER


def live_workers(endpoint=None, ledger=None):
    """Live worker sessions as `[{session_id, status, label}]`, or None when a store failed.

    The ledger supplies the worker identity; the **session-heartbeat endpoint** supplies
    liveness. A session live at the endpoint but not in the ledger is a manager or one of the
    operator's own; a session in the ledger and not live at the endpoint has exited.

    ⚠️ **The endpoint replaced the registry AND the heartbeat store, and the join survived
    both.** Until 2026-10-09 this read the registry ∪ the store directly; the endpoint holds
    both populations in one store (it serves `source: cluster` and `source: mcp-timer` rows
    beside local ones), so two channels collapse into one read. The *unit* is deliberately
    unchanged — still ledger ∩ live — because that is what makes this a count of **workers**
    rather than of every session on the machine. Operator ruling 2026-10-09: keep the join,
    move only the liveness source.

    `status` names the store row's `source` — `mcp-timer` for a session posting its own
    heartbeat, `cluster` for a mirrored cluster session. Descriptive only — nothing decides
    on it.
    """
    liveness = _load("session_liveness", "session-liveness.py")
    rows = liveness.read_endpoint(endpoint)
    if rows is None:
        return None
    if ledger is None:
        return None

    # The endpoint's live set, built once rather than per ledger entry. A row is live only on
    # an explicit `live: true` — a renamed or missing key must never be read as liveness, and
    # a malformed row is skipped rather than allowed to crash the count.
    live = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        session_id = row.get("session_id")
        if not isinstance(session_id, str) or not session_id:
            continue
        if row.get("live") is not True:
            continue
        live[session_id] = row.get("source")

    workers = []
    for session_id, record in ledger.items():
        if session_id not in live:
            continue
        # Auto-resumes are excluded — they answer to the auto-resume gate's own 30-min
        # crash-loop cap, and counting them here would leave a sweep that revived two dead
        # workers unable to start any new one (docs/fleet-surface.md § Spawn a worker item 5).
        # The marker is the ledger record's `resumed_from`, the same field
        # `check-spawn-ledger.py` reads.
        if record.get("resumed_from"):
            continue
        workers.append(
            {
                "session_id": session_id,
                "status": live[session_id],
                "label": record.get("label"),
            }
        )
    return workers


def main(argv=None):
    parser = argparse.ArgumentParser(description="Count live worker sessions.")
    parser.add_argument("--list", action="store_true", help="print every live worker session")
    parser.add_argument("--count", action="store_true", help="print only the number of live worker sessions")
    parser.add_argument("--endpoint", default=None, help="override the attention store base URL")
    parser.add_argument("--ledger-dir", default=None, help="override the spawn-ledger path")
    parser.add_argument("--dir", default=None, help="ACCEPTED AND IGNORED — the registry left the liveness path")
    parser.add_argument("--heartbeat-dir", default=None, help="ACCEPTED AND IGNORED — the endpoint computes freshness")
    args = parser.parse_args(argv)

    # ⚠️ Both flags are kept and ignored rather than removed, the same call `session-liveness.py`
    # made: callers pass them (`approved-not-started.py` passes one), so removing them breaks
    # those callers, while accepting them in silence would let a reader believe a path or a
    # window is in force when neither is.
    for flag, value in (("--dir", args.dir), ("--heartbeat-dir", args.heartbeat_dir)):
        if value:
            print(
                f"warning: {flag} is accepted and IGNORED — liveness comes from the "
                "session-heartbeat endpoint, which resolves the store and its own window",
                file=sys.stderr,
            )

    directory = args.ledger_dir or _ledger().ledger_dir()
    ledger = _ledger().read_ledger(directory)
    if ledger is None:
        print(f"UNKNOWN — cannot read the spawn ledger at {directory}", file=sys.stderr)
        return 2

    workers = live_workers(args.endpoint, ledger)
    if workers is None:
        print("UNKNOWN — cannot read the session-heartbeat endpoint", file=sys.stderr)
        return 2

    if args.count:
        # The integer and nothing else — `$(… --count)` goes straight into a comparison.
        # The unreadable cases above report on stderr and exit 2, the only channel a
        # counter may use.
        print(len(workers))
        return 0

    if not workers:
        print("no live worker sessions")
        return 0
    for worker in sorted(workers, key=lambda w: str(w["label"])):
        print(f"{worker['session_id']}  {str(worker['status']):8}  {worker['label']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
