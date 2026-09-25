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
  --check <session-id>   LIVE (exit 0) / STALE (exit 1) / UNKNOWN (exit 2)

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


def read_live(directory, ttl=TTL_SECONDS, now=None):
    """Every fresh stamp as a dict, or None when the store could not be read.

    None and [] are different answers and must stay so: [] is "read it, no worker is live",
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

    live = []
    for name in names:
        if not name.endswith(".json"):
            continue
        path = os.path.join(directory, name)
        try:
            age = now - os.stat(path).st_mtime
        except OSError:
            continue  # swept between the listdir and the stat — already gone
        if age >= ttl:
            continue
        session_id = name[: -len(".json")]
        try:
            with open(path, encoding="utf-8") as handle:
                meta = json.load(handle)
        except (OSError, ValueError):
            # Fresh but unreadable as JSON. The AGE is what the verdict rests on, so the
            # worker is live; only the descriptive fields are lost. Reporting it dead because
            # its metadata is malformed would invert the failure into the dangerous one.
            meta = {}
        live.append({"session_id": session_id, "age_seconds": round(age, 1), "pid": meta.get("pid")})
    return live


def main(argv=None):
    parser = argparse.ArgumentParser(description="Read the headless-worker heartbeat store.")
    parser.add_argument("--list", action="store_true", help="print every live headless worker")
    parser.add_argument("--check", metavar="SESSION_ID", help="LIVE / STALE / UNKNOWN for one session id")
    parser.add_argument("--dir", default=None, help="override the store path")
    parser.add_argument("--ttl", type=int, default=TTL_SECONDS, help=f"staleness bound in seconds (default {TTL_SECONDS})")
    args = parser.parse_args(argv)

    directory = args.dir or heartbeat_dir()
    workers = read_live(directory, ttl=args.ttl)

    if workers is None:
        print(f"UNKNOWN — cannot read {directory}", file=sys.stderr)
        return 2

    if args.check:
        match = next((w for w in workers if w["session_id"] == args.check), None)
        if match:
            print(f"LIVE — {match['session_id']} stamped {match['age_seconds']}s ago (pid {match['pid']})")
            return 0
        print(f"STALE — {args.check} has no fresh stamp in {directory}")
        return 1

    if not workers:
        print(f"no live headless workers in {directory}")
        return 0
    for worker in sorted(workers, key=lambda w: w["session_id"]):
        print(f"{worker['session_id']}  age {worker['age_seconds']}s  pid {worker['pid']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
