#!/usr/bin/env python3
"""Report a manager that has been pane-less across consecutive rounds.

`docs/role-mode-matrix.md` Rule 2 makes `manager` -> `interactive` with no exceptions, but a
manager started headless (or resumed after its tab died) runs pane-less and says so in its
own round output — `↪️ no branch — no pane · id non-dead` — indefinitely. Nothing turns that
self-report into an operator-visible condition, so it repeats for tens of rounds while the
session stays unreachable by every registry-keyed verb.

This is the code home for that fact. It is called once per round by the round render step:

    pane-less-report.py --transcript <round.jsonl> --state-dir <dir>

A round HAS a pane when any transcript line carries a non-empty `pane` field; that resets the
counter. Otherwise the counter for the session (the transcript's filename stem) increments,
and at `--threshold` consecutive pane-less rounds the literal condition is printed.

⚠️ **The counter must live on disk, not in the process.** The round render invokes this as a
fresh process each time, so an in-memory count would reset every round and the condition
could never fire.
"""
import argparse
import json
import os
import sys

DEFAULT_STATE_DIR = os.path.join(
    os.path.expanduser("~"), ".claude", "state", "pane-less"
)
DEFAULT_THRESHOLD = 3


def round_has_pane(path):
    """`True` when any line of the round transcript carries a non-empty `pane` field."""
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if isinstance(rec, dict) and rec.get("pane"):
                    return True
    except OSError:
        return False
    return False


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
    parser.add_argument("--transcript", required=True,
                        help="this round's transcript; its filename stem is the session id")
    parser.add_argument("--state-dir", default=DEFAULT_STATE_DIR)
    parser.add_argument("--threshold", type=int, default=DEFAULT_THRESHOLD)
    args = parser.parse_args(argv)

    sid = os.path.basename(args.transcript)
    if sid.endswith(".jsonl"):
        sid = sid[: -len(".jsonl")]
    if not sid:
        print("❌ pane-less-report: --transcript must name a file", file=sys.stderr)
        return 2

    if round_has_pane(args.transcript):
        # A pane resets the run: the manager is reachable again.
        write_count(args.state_dir, sid, 0, _now())
        return 0

    count = read_count(args.state_dir, sid) + 1
    write_count(args.state_dir, sid, count, _now())
    if count >= args.threshold:
        print("pane-less manager %s — remedy: /supervisor:restart %s" % (sid, sid))
    return 0


def _now():
    import datetime
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


if __name__ == "__main__":
    sys.exit(main())
