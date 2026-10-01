#!/usr/bin/env python3
"""Count live **worker sessions** — the fleet-wide worker target's unit.

The target bounds the laptop's load, and the load is the interactive worker tabs. The
instrument that shipped for it read the headless heartbeat store, which is stamped only
for in-process workers (`server/supervisor.mjs` writes `mode: 'headless'`, from
`server/agent-loop.mjs`) — so it answered **0 while 11 interactive workers were live**
(measured 2026-10-01). A target that reads 0 on a busy fleet is inert: managers always see
"below target" and always propose, and the cap never binds.

  --list    one line per live worker session: `<session-id>  <status>  <label>`
  --count   just the number of live worker sessions, and nothing else on stdout

**The definition, and it takes two stores to answer.** A worker session is one that is

  (a) present in the **live registry** (`~/.claude/sessions/<pid>.json`) — the liveness
      authority, since its entry is deleted on exit; and
  (b) present in the **supervisor spawn ledger** (`~/.local/state/claude-supervisor/sessions/`)
      — which is what makes it a session the supervisor opened, rather than a manager or one
      of the operator's own.

Neither store answers alone, and the join is the instrument:

  * The **ledger's own `status` is not a liveness source.** Measured 2026-10-01: 824 of its
    1075 entries read `running` against 26 live registry sessions. It is the durable half —
    who started this, in what mode, from which manager — and the registry is the live half.
  * The **registry has no name field**, so "is this a worker" is not derivable from it. Its
    entries carry `pid`, `sessionId`, `cwd`, `kind`, `entrypoint`, `status` and nothing that
    identifies a role.

⚠️ **A manager ever opened through `spawn_agent` would be counted**, because it would have a
ledger record like any worker. The one live example found, "Attention Routing", is a
2026-09-23 ledger entry whose session is long gone, so the liveness join filters it — but the
limit is real and is stated rather than hidden. Managers are normally started by hand, which
is why all four measured here had zero ledger entries.

⚠️ **An unreadable store is UNKNOWN, never 0.** Both stores are read before any verdict, and
either failing exits 2 with its message on **stderr** — so `$(… --count)` captures nothing and
a caller must consult the exit code. Folding an I/O error into "no workers" is how a broken
instrument reads as an idle fleet, which is the failure this file exists to correct.
"""
import argparse
import importlib.util
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))


def _load(module_name, filename):
    """Import a hyphenated sibling script by path — the same idiom `session-liveness.py`
    uses for `live-workers.py`, so neither store gains a second reader."""
    path = os.path.join(HERE, filename)
    spec = importlib.util.spec_from_file_location(module_name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def ledger_dir():
    """The spawn ledger's path, resolved as `fleet-sessions.py` and `config.mjs` do.

    A reader that resolved a different path than the writer would report an empty fleet,
    and the empty answer is the dangerous direction.
    """
    override = os.environ.get("SUPERVISOR_LEDGER_DIR")
    if override:
        return override
    state = os.environ.get("XDG_STATE_HOME") or os.path.join(os.path.expanduser("~"), ".local", "state")
    return os.path.join(state, "claude-supervisor", "sessions")


def read_ledger(directory):
    """Every session id the ledger knows, mapped to its label — or None when unreadable.

    None and `{}` are different answers and must stay so: `{}` is "read it, nobody is
    recorded", None is "could not read it". Collapsing them turns a permissions error into
    a confident empty fleet.
    """
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
        session_id = record.get("session_id")
        if session_id:
            known[session_id] = record.get("label")
    return known


def live_workers(registry_dir=None, ledger=None):
    """Live worker sessions as `[{session_id, status, label}]`, or None when a store failed.

    The registry supplies liveness; the ledger supplies the worker identity. A session in
    the registry but not the ledger is a manager or one of the operator's own; a session in
    the ledger but not the registry has exited.
    """
    liveness = _load("session_liveness", "session-liveness.py")
    registry = liveness.read_registry(registry_dir) if registry_dir else liveness.read_registry()
    if registry is None:
        return None
    if ledger is None:
        return None

    workers = []
    for session_id, record in registry.items():
        if session_id in ledger:
            workers.append(
                {
                    "session_id": session_id,
                    "status": record.get("status"),
                    "label": ledger[session_id],
                }
            )
    return workers


def main(argv=None):
    parser = argparse.ArgumentParser(description="Count live worker sessions.")
    parser.add_argument("--list", action="store_true", help="print every live worker session")
    parser.add_argument("--count", action="store_true", help="print only the number of live worker sessions")
    parser.add_argument("--dir", default=None, help="override the registry path")
    parser.add_argument("--ledger-dir", default=None, help="override the spawn-ledger path")
    args = parser.parse_args(argv)

    directory = args.ledger_dir or ledger_dir()
    ledger = read_ledger(directory)
    if ledger is None:
        print(f"UNKNOWN — cannot read the spawn ledger at {directory}", file=sys.stderr)
        return 2

    workers = live_workers(args.dir, ledger)
    if workers is None:
        print("UNKNOWN — cannot read the session registry", file=sys.stderr)
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
