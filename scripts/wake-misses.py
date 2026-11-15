#!/usr/bin/env python3
"""List the subjects whose most recent manager wake did not reach a manager.

The sweep gate wakes a subject's manager on every actionable tick and records the
outcome in the subject's ledger as one tab-separated column (attention-controller
`appendLedger`):

    wake=<result> session=<sid8> live=<yes|no|unknown>

`wake=none` marks a tick that ran no wake (empty, failed, progress-only, invariant).
This script reads each subject's LAST line that ran a wake and reports it as a miss
unless the result is `WOKEN` or a guard refusal (`SKIP:<reason>`), which are the two
outcomes where the wake path worked: the prompt was typed, or the guard deliberately
held it back from a busy or waiting pane.

    wake-misses.py [--state-dir DIR] [--max-age-hours 48] [--now EPOCH]

Output, one line per finding, tab-separated, then one summary line:

    MISS   <vault> <subject> <ts> <result> <session> <live>
    STALE  <vault> <subject> <ts> <result> <session> <live>
    wake-misses=<n> stale=<n> measured=<n> unmeasured=<n>

- `MISS`   — the last wake missed and happened inside `--max-age-hours`.
- `STALE`  — the last wake missed, but longer ago than the window: the subject may
  have stopped producing actionable ticks. Listed, never counted as a miss.
- `measured`   — subjects whose ledger carries the wake column at all.
- `unmeasured` — subjects whose ledger predates the column. ⚠️ They are counted, never
  folded into "no misses": a ledger without the field cannot show a miss, so reading
  its silence as a clean wake is the false green this script exists to remove.

Why this exists: `/supervisor:fleet-triage` § 3.6 checks the manager of every live
manager session, so a subject with NO live manager — every registration dead, or the
manager running headless where no pane-wake reaches it — was never enumerated. Measured
2026-11-14/15: 58 of 126 wakes read `NO-PANE` and six subjects had no `WOKEN` in more
than 20 hours while every fleet check read green.

Exit 0 when the state directory was read (with or without misses); 2 when it could not
be read at all — an unreadable directory is never "0 misses".
"""
import argparse
import json
import os
import sys
import time
from datetime import datetime

DEFAULT_STATE_DIR = os.path.expanduser(
    os.environ.get("SWEEP_GATE_LOOP_STATE_DIR", "~/.claude/state/sweep-gate-loop")
)
LEDGER_SUFFIX = ".ledger.txt"
WAKE_NONE = "wake=none"
# The two results where the wake path worked; everything else is a missed wake.
REACHED = ("WOKEN",)
GUARD_PREFIX = "SKIP:"


def parse_wake(column):
    """{result, session, live} from a `wake=… session=… live=…` column."""
    out = {"result": "", "session": "-", "live": "unknown"}
    for field in column.split():
        key, sep, value = field.partition("=")
        if not sep:
            continue
        if key == "wake":
            out["result"] = value
        elif key in ("session", "live"):
            out[key] = value
    return out


def last_wake(lines):
    """(ts, wake dict) for the last line that ran a wake, plus whether any line carried the column.

    Returns ((None, None), has_field) when no line ran a wake.
    """
    has_field = False
    found = (None, None)
    for line in lines:
        columns = line.rstrip("\n").split("\t")
        wake = next((c for c in columns if c.startswith("wake=")), None)
        if wake is None:
            continue
        has_field = True
        if wake.strip() == WAKE_NONE:
            continue
        found = (columns[0], parse_wake(wake))
    return found, has_field


def is_miss(result):
    return not (result in REACHED or result.startswith(GUARD_PREFIX))


def epoch(ts):
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
    except (AttributeError, ValueError):
        return None


def subject_name(directory, slug):
    """The subject's own name from the gate's `<slug>.json` (`topic`), else the slug."""
    try:
        with open(os.path.join(directory, slug + ".json"), encoding="utf-8") as fh:
            data = json.load(fh)
        name = data.get("topic") if isinstance(data, dict) else None
        return name or slug
    except (OSError, ValueError):
        return slug


def scan(state_dir, max_age_s, now):
    """(rows, counts) over every `<vault>/<slug>.ledger.txt`; raises OSError when unreadable."""
    rows = []
    counts = {"wake-misses": 0, "stale": 0, "measured": 0, "unmeasured": 0}
    for vault in sorted(os.listdir(state_dir)):
        directory = os.path.join(state_dir, vault)
        if not os.path.isdir(directory):
            continue
        for name in sorted(os.listdir(directory)):
            if not name.endswith(LEDGER_SUFFIX):
                continue
            slug = name[: -len(LEDGER_SUFFIX)]
            try:
                with open(os.path.join(directory, name), encoding="utf-8") as fh:
                    (ts, wake), has_field = last_wake(fh)
            except (OSError, UnicodeDecodeError):
                counts["unmeasured"] += 1
                continue
            if not has_field:
                counts["unmeasured"] += 1
                continue
            counts["measured"] += 1
            if wake is None or not is_miss(wake["result"]):
                continue
            when = epoch(ts)
            kind = "MISS" if when is not None and now - when <= max_age_s else "STALE"
            counts["wake-misses" if kind == "MISS" else "stale"] += 1
            rows.append([kind, vault, subject_name(directory, slug), ts,
                         wake["result"], wake["session"], wake["live"]])
    return rows, counts


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--state-dir", default=DEFAULT_STATE_DIR)
    ap.add_argument("--max-age-hours", type=float, default=48.0)
    ap.add_argument("--now", type=float, default=None, help="epoch override (tests)")
    args = ap.parse_args(argv)
    now = args.now if args.now is not None else time.time()
    try:
        rows, counts = scan(args.state_dir, args.max_age_hours * 3600, now)
    except OSError as exc:
        print(f"wake-misses: cannot read {args.state_dir}: {exc}", file=sys.stderr)
        return 2
    for row in rows:
        print("\t".join(row))
    print(" ".join(f"{k}={v}" for k, v in counts.items()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
