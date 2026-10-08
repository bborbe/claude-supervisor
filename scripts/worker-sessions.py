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

**The definition takes one identity store and two liveness channels.** A worker session is
one that is

  (a) present in the **supervisor spawn ledger** (`~/.local/state/claude-supervisor/sessions/`)
      — which is what makes it a session the supervisor opened, rather than a manager or one
      of the operator's own; and
  (b) **live**, which the registry OR the heartbeat store answers.

Neither liveness channel answers alone, and the reason is structural rather than historical:

  * The **registry** (`~/.claude/sessions/<pid>.json`) is pid-keyed and local-only. It sees
    every session holding a socket — every interactive tab — and its entry is deleted on
    exit, which is what makes absence meaningful. It cannot see a worker with no pid of its
    own: a headless worker is an in-process SDK `query()` inside the server, and a cluster
    worker is a process on another machine.
  * The **heartbeat store** is what covers those. `server/supervisor.mjs` stamps a headless
    worker from its agent loop, and `server/cluster-heartbeat.mjs`'s `pollCluster()` mirrors
    cluster sessions into the same store on the server's own timer.

⚠️ **The union is a correction, and the shape of the defect is worth keeping.** Until
2026-10-05 the count was *registry ∩ ledger* alone — itself a fix, because the instrument
before it read the heartbeat store only, which is stamped for headless workers, so it
answered **0 while 11 interactive workers were live**. That fix traded one blindness for
another: it restored the tabs and lost every worker with no registry entry, which is both
headless and cluster. Two populations cannot be recovered by fixing one channel.

  * The **ledger's own `status` is not a liveness source.** Measured 2026-10-01: 824 of its
    1075 entries read `running` against 26 live registry sessions. It is the durable half —
    who started this, in what mode, from which manager — never the live one.
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
    """Every session id the ledger knows, mapped to its full record — or None when unreadable.

    None and `{}` are different answers and must stay so: `{}` is "read it, nobody is
    recorded", None is "could not read it". Collapsing them turns a permissions error into
    a confident empty fleet.

    ⚠️ **The whole record, not just its label.** The filter below needs `resumed_from`, and a
    map that keeps only the label cannot express it — the caller would have to reopen every
    file to answer a question the read already had in hand. The mjs twin returns the record
    for the same reason, and the two must stay in step or the pair stops being one definition.
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
            known[session_id] = record
    return known


def live_workers(registry_dir=None, ledger=None, heartbeat_dir=None, now=None):
    """Live worker sessions as `[{session_id, status, label}]`, or None when a store failed.

    The ledger supplies the worker identity; the registry and the heartbeat store supply
    liveness, as a UNION. A session in either liveness channel but not the ledger is a manager
    or one of the operator's own; a session in the ledger and in neither channel has exited.

    `status` names whichever channel answered: the registry's own `status` for a session
    holding a socket, the stamp's `mode` for a headless or cluster worker. Descriptive only —
    nothing decides on it.
    """
    identity = _load("session_identity", "session-identity.py")
    registry = identity.read_registry(registry_dir) if registry_dir else identity.read_registry()
    if registry is None:
        return None
    # The heartbeat store is read through its own reader, `live-workers.py` — the file
    # `session-liveness.py` used to wrap. Liveness for the plugin moved to the
    # session-heartbeat endpoint, but this counter needs the store's ROWS (a stamp's `mode`
    # is the column it reports), and the store is where they live.
    lw = _load("live_workers", "live-workers.py")
    store = heartbeat_dir if heartbeat_dir is not None else lw.heartbeat_dir()
    heartbeats = lw.read_live(store, ttl=lw.TTL_SECONDS, now=now)
    if heartbeats is None:
        return None
    if ledger is None:
        return None

    # The union, built once rather than per ledger entry — `resolve()` answers for a single id
    # and re-reads its sources on every call, which over a ~1000-record ledger is a directory
    # listing per record.
    live = {}
    for session_id, record in registry.items():
        # Presence is the claim, and this channel's rule is deliberately UNCHANGED by the
        # union: the registry's entry is deleted on exit, so its existence is what the counter
        # has always read. `read_registry`'s three-state `alive` is a stronger check the resume
        # paths use, and tightening this to `alive is True` would be a second, silent change to
        # the population this fix is not about.
        live[session_id] = record.get("status")
    for stamp in heartbeats:
        session_id = stamp.get("session_id")
        if not session_id or session_id in live:
            # The registry wins where both speak: a session holding a socket is the case the
            # readers already understood, and its `status` is the richer descriptor.
            continue
        # `state` defaults to `live` for a stamp written before the field existed; `unknown`
        # is a cluster stamp whose own store could not be read, which is "cannot tell" and
        # never liveness.
        if stamp.get("state", "live") != "live":
            continue
        live[session_id] = stamp.get("mode")

    workers = []
    for session_id, record in ledger.items():
        if session_id not in live:
            continue
        # Auto-resumes are excluded — they answer to the auto-resume gate's own 30-min
        # crash-loop cap, and counting them here would leave a sweep that revived two dead
        # workers unable to start any new one (docs/fleet-surface.md § Spawn a worker item 5).
        # The marker is the ledger record's `resumed_from`, the same field
        # `check-spawn-ledger.py` reads. See the mjs twin for why this filter is written down
        # for the first time rather than inherited: the exclusion used to hold by accident,
        # because every auto-resume is headless and a headless worker held no registry entry.
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
    parser.add_argument("--dir", default=None, help="override the registry path")
    parser.add_argument("--ledger-dir", default=None, help="override the spawn-ledger path")
    parser.add_argument("--heartbeat-dir", default=None, help="override the heartbeat-store path")
    args = parser.parse_args(argv)

    directory = args.ledger_dir or ledger_dir()
    ledger = read_ledger(directory)
    if ledger is None:
        print(f"UNKNOWN — cannot read the spawn ledger at {directory}", file=sys.stderr)
        return 2

    workers = live_workers(args.dir, ledger, args.heartbeat_dir)
    if workers is None:
        print("UNKNOWN — cannot read the session registry or the heartbeat store", file=sys.stderr)
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
