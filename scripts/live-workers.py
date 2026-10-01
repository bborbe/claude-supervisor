#!/usr/bin/env python3
"""Read the headless-worker heartbeat store — the read half of `server/heartbeat.mjs`.

A headless worker is an in-process SDK `query()` owned by the supervisor server that spawned
it: no process of its own, no registry entry (it holds no socket), no argv to match. So every
probe a THIRD PARTY can run finds nothing, and a manager that did not spawn a worker read the
same decisive "not live" for one genuinely mid-turn and for one that finished an hour ago.
That is the defect: the guard acts on "not live" by ALLOWING a resume, so two writers land on
one conversation.

The owning server now re-stamps a small file per live worker while the turn runs, and this
reads it. The question it answers is the one a non-spawning process could not answer before.

  --list                 one line per live worker: `<session-id>  age <n>s  pid <n>`
  --count                just the number of live workers, and nothing else on stdout
  --check <session-id>   LIVE (exit 0) / STALE (exit 1) / UNKNOWN (exit 2)

⚠️ **`--count` exists because `--list | wc -l` is wrong, and wrong in the direction that looks
right.** An empty store prints `no live headless workers in <dir>` — to *stdout*, like every
other line this script emits — so `--list | wc -l` answers **1** for an idle fleet. A caller
comparing that against a target reads "one worker live" on a machine with none, and the error
is invisible precisely because 1 is a plausible count. Measured 2026-10-01 against the real
store. A counter must therefore be its own mode: `--count` writes the integer and nothing
else, and the unreadable case keeps its message on stderr where `$(...)` cannot capture it.

⚠️ The verdict is the stamp's AGE against the TTL, never the file's existence. A server
killed with `kill -9` never clears its stamps, so a file that exists but has stopped being
refreshed is a DEAD worker — reading existence reports exactly the wrong answer for the case
this store exists to catch.

⚠️ UNKNOWN is a third answer, not a flavour of STALE. An unreadable store means the probe
could not run, and a caller that folds that into "not live" turns an I/O error into
permission to start a second writer. Exit 2 is deliberately distinct from exit 1.
"""
import argparse
import json
import os
import sys
import time

# Mirrors HEARTBEAT_TTL_MS in server/heartbeat.mjs. Two copies of one number is a real cost,
# paid knowingly: this script must run without a Node toolchain (launchd, the sweep gate), so
# it cannot import the constant. The pair is pinned by a test on the Node side asserting
# TTL >= 2 x interval, and by `--ttl` here for an operator who changes one and not the other.
TTL_SECONDS = 60

# The mirror's reachability marker, refreshed on every poll that successfully read the cluster.
# It lives in the store beside the stamps so it cannot drift from them, and it is named with a
# leading underscore so it can never collide with a session id.
#
# ⚠️ **Why this file needs it at all.** The mirror stops refreshing a cluster worker's stamp
# when the cluster cannot be read, so that stamp goes stale — and a stale stamp and a dead
# worker are indistinguishable in the store. Without this marker the reader would report
# `STALE` for a worker that is alive behind a network fault, which is the answer that permits a
# second writer onto it. The marker is what tells the two apart, and it is the only thing that
# can.
REACHABILITY_FILE = "_cluster-reachability.json"


def heartbeat_dir():
    """The store's path, resolved the same way `config.mjs` resolves it.

    `SUPERVISOR_HEARTBEAT_DIR` wins, then XDG_STATE_HOME, then `~/.local/state` — the same
    precedence the server uses. A reader that resolved a different path than the writer would
    report a healthy fleet as empty, and the empty answer is the dangerous direction.
    """
    override = os.environ.get("SUPERVISOR_HEARTBEAT_DIR")
    if override:
        return override
    state = os.environ.get("XDG_STATE_HOME") or os.path.join(os.path.expanduser("~"), ".local", "state")
    return os.path.join(state, "claude-supervisor", "live")


def cluster_reachable(directory, ttl=TTL_SECONDS, now=None):
    """True when the mirror refreshed the cluster marker inside the TTL.

    A missing marker is `False`: no mirror has ever run, so no cluster stamp in the store can
    be trusted as fresh. That errs toward `UNKNOWN` for cluster stamps, which is the safe
    direction — `UNKNOWN` never authorises a resume.
    """
    now = time.time() if now is None else now
    try:
        age = now - os.stat(os.path.join(directory, REACHABILITY_FILE)).st_mtime
    except OSError:
        return False
    return age < ttl


def read_live(directory, ttl=TTL_SECONDS, now=None):
    """Every stamp the store can speak for, or None when the store could not be read.

    Each entry carries `state`:

      * `"live"`   — a fresh stamp. The verdict rests on the AGE, never on the file existing.
      * `"unknown"` — a STALE stamp whose record says `source: cluster` while the mirror's
        reachability marker is also stale. The cluster could not be read, so this stamp's
        staleness proves nothing about the worker, and the honest answer is that we cannot
        tell. Reporting it stale would be the dangerous direction: `STALE` permits a resume
        onto a worker that may be alive behind a network fault.

    A stale stamp that is not cluster-sourced, or a stale cluster stamp while the mirror is
    demonstrably reachable (so the worker really did stop refreshing), is dropped — that is
    the ordinary death case and it is what the readers already expect.

    None and [] are different answers and must stay so: [] is "read it, nothing to report",
    None is "could not read it". Collapsing them is how a permissions error becomes a
    confident all-clear.
    """
    now = time.time() if now is None else now
    try:
        names = os.listdir(directory)
    except FileNotFoundError:
        return []
    except OSError:
        return None

    reachable = cluster_reachable(directory, ttl=ttl, now=now)

    live = []
    for name in names:
        if not name.endswith(".json") or name == REACHABILITY_FILE:
            continue
        path = os.path.join(directory, name)
        try:
            age = now - os.stat(path).st_mtime
        except OSError:
            continue  # swept between the listdir and the stat — already gone
        session_id = name[: -len(".json")]
        try:
            with open(path, encoding="utf-8") as handle:
                meta = json.load(handle)
        except (OSError, ValueError):
            # Unreadable as JSON. For a FRESH stamp the AGE is what the verdict rests on, so
            # the worker is live and only the descriptive fields are lost — reporting it dead
            # because its metadata is malformed would invert the failure into the dangerous
            # one. A stale one has no verdict to rescue, so it falls through to the drop.
            meta = {}
        if age >= ttl:
            if meta.get("source") == "cluster" and not reachable:
                live.append(
                    {
                        "session_id": session_id,
                        "age_seconds": round(age, 1),
                        "pid": meta.get("pid"),
                        "state": "unknown",
                    }
                )
            continue
        live.append(
            {"session_id": session_id, "age_seconds": round(age, 1), "pid": meta.get("pid"), "state": "live"}
        )
    return live


def main(argv=None):
    parser = argparse.ArgumentParser(description="Read the headless-worker heartbeat store.")
    parser.add_argument("--list", action="store_true", help="print every live headless worker")
    parser.add_argument("--count", action="store_true", help="print only the number of live workers")
    parser.add_argument("--check", metavar="SESSION_ID", help="LIVE / STALE / UNKNOWN for one session id")
    parser.add_argument("--dir", default=None, help="override the store path")
    parser.add_argument("--ttl", type=int, default=TTL_SECONDS, help=f"staleness bound in seconds (default {TTL_SECONDS})")
    args = parser.parse_args(argv)

    directory = args.dir or heartbeat_dir()
    workers = read_live(directory, ttl=args.ttl)

    if workers is None:
        print(f"UNKNOWN — cannot read {directory}", file=sys.stderr)
        return 2

    if args.count:
        # The integer and nothing else. `$(... --count)` is written straight into a
        # comparison, so any prose on stdout would be captured as the value; the
        # unreadable case above already reports on stderr and exits 2, which is the only
        # channel a counter may use. Counts what `--list` lists — including an
        # `unknown`-state cluster stamp, which is a worker we cannot prove is gone.
        print(len(workers))
        return 0

    if args.check:
        match = next((w for w in workers if w["session_id"] == args.check), None)
        if match and match["state"] == "unknown":
            print(
                f"UNKNOWN — {match['session_id']} is a cluster worker and the cluster store could not be read",
                file=sys.stderr,
            )
            return 2
        if match:
            print(f"LIVE — {match['session_id']} stamped {match['age_seconds']}s ago (pid {match['pid']})")
            return 0
        print(f"STALE — {args.check} has no fresh stamp in {directory}")
        return 1

    if not workers:
        print(f"no live headless workers in {directory}")
        return 0
    for worker in sorted(workers, key=lambda w: w["session_id"]):
        suffix = "  UNKNOWN (cluster unreachable)" if worker["state"] == "unknown" else f"  pid {worker['pid']}"
        print(f"{worker['session_id']}  age {worker['age_seconds']}s{suffix}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
