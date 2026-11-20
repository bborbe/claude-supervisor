#!/usr/bin/env python3
"""Report a manager that has been pane-less across consecutive rounds.

`docs/role-mode-matrix.md` Rule 2 makes `manager` -> `interactive` with no exceptions, but a
manager started headless (or resumed after its tab died) runs pane-less and says so in its
own round output — `↪️ no branch — no pane · id non-dead` — indefinitely. Nothing turns that
self-report into an operator-visible condition, so it repeats for tens of rounds while the
session stays unreachable by every registry-keyed verb.

The round render step calls this once per round, passing the pane it already resolved:

    pane-less-report.py --session <sid> --pane <pane>

⚠️ **The pane is an argument, not a transcript field.** A first cut parsed a `pane` key out
of the round transcript; no real transcript carries one, so every round looked pane-less and
the condition would have fired for EVERY manager at round 3. A detector that cries wolf on
the whole fleet is worse than none. The caller already knows the pane — it is the value the
render step resolved for the row — so it passes it, and an absent or empty `--pane` is the
only thing that counts as pane-less.

⚠️ **The counter must live on disk, not in the process.** The render invokes this as a fresh
process each round, so an in-memory count would reset every round and never reach the
threshold.
"""
import argparse
import datetime
import json
import os
import sys

DEFAULT_STATE_DIR = os.path.join(
    os.path.expanduser("~"), ".claude", "state", "pane-less"
)
DEFAULT_THRESHOLD = 3


def state_path(state_dir, sid):
    return os.path.join(state_dir, "%s.json" % sid)


def read_count(state_dir, sid):
    try:
        with open(state_path(state_dir, sid), encoding="utf-8") as fh:
            rec = json.load(fh)
    except (OSError, ValueError):
        return 0
    count = rec.get("count") if isinstance(rec, dict) else None
    return count if isinstance(count, int) and count > 0 else 0


def write_count(state_dir, sid, count, round_at):
    os.makedirs(state_dir, exist_ok=True)
    payload = {"count": count, "last_round_at": round_at}
    tmp = state_path(state_dir, sid) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(payload, fh)
    os.replace(tmp, state_path(state_dir, sid))


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Report a manager that has been pane-less across consecutive rounds.",
    )
    parser.add_argument("--session", required=True,
                        help="the manager's session id")
    parser.add_argument("--pane", default="",
                        help="the pane the round resolved for this row; empty = pane-less")
    parser.add_argument("--state-dir", default=DEFAULT_STATE_DIR)
    parser.add_argument("--threshold", type=int, default=DEFAULT_THRESHOLD)
    args = parser.parse_args(argv)

    sid = (args.session or "").strip()
    if not sid:
        print("❌ pane-less-report: --session must name a session id", file=sys.stderr)
        return 2

    round_at = datetime.datetime.now().astimezone().isoformat(timespec="seconds")

    if (args.pane or "").strip():
        # A pane resets the run: the manager is reachable again.
        write_count(args.state_dir, sid, 0, round_at)
        return 0

    count = read_count(args.state_dir, sid) + 1
    write_count(args.state_dir, sid, count, round_at)
    if count >= args.threshold:
        print("pane-less manager %s — remedy: /supervisor:restart %s" % (sid, sid))
    return 0


if __name__ == "__main__":
    sys.exit(main())
