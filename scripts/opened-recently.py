#!/usr/bin/env python3
"""Was this subject already opened inside the window? — the batch's per-run memory.

`/supervisor:open --flagged` has no memory between runs, so two commands minutes apart
re-open the same rows. The ledger already records every spawn, so the answer is a READ
of it — never a second store, which is the duplicate-writer shape this repo guards
against everywhere else (`docs/subject-resolution.md` § Recording).

⚠️ **The ledger is a candidate list, not a liveness answer.** A record reads
`result: null, ended_at: null, status: running` whether its worker is alive or four
hours dead. This script therefore answers *"was it opened"* and nothing else; liveness
stays with `scripts/session-liveness.py`.

⚠️ **An unreadable ledger is exit 2, never "not opened".** Collapsing the two would let
a failed read authorise a duplicate spawn, which is the exact fold `read_ledger()`'s own
docstring forbids — `{}` is a fact, `None` is the absence of an answer.

Usage:
  opened-recently.py --subject "<name>" [--minutes 60] [--endpoint URL] [--json]

Exit codes: 0 opened inside the window (the row is on stdout) · 1 not opened ·
2 the ledger could not be read · 64 usage error.
"""

import argparse
import importlib.util
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_WINDOW_MINUTES = 60


def _load(name):
    """Load a hyphenated sibling module by path — a plain import cannot."""
    path = os.path.join(HERE, name + ".py")
    spec = importlib.util.spec_from_file_location(name.replace("-", "_"), path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


spawn_ledger = _load("spawn-ledger")
flagged_rows = _load("flagged-rows")

#: `⚙ <subject>` is what a tab spawn writes, and `uniqueTabName` suffixes `(2)` when the
#: base name is taken — so neither a raw equality nor a prefix test matches a real row.
#: The glyph rule itself is `flagged-rows.py`'s; only the suffix strip is local.
_SUFFIX = re.compile(r"\s+\(\d+\)$")


def normalise(name):
    """The subject a session label names, with the glyph and any uniqueness suffix gone."""
    return _SUFFIX.sub("", flagged_rows.strip_status_glyph(name or ""))


def parse_stamp(value):
    """`spawned_at` as an aware datetime, or None when it is absent or unparseable."""
    if not value or not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def opened_within(ledger, subject, window, now=None):
    """The newest row whose label names `subject` and whose spawn falls inside `window`."""
    want = normalise(subject)
    if not want:
        return None
    now = now or datetime.now(timezone.utc)
    hits = []
    for session_id, row in ledger.items():
        if not isinstance(row, dict):
            continue
        if normalise(row.get("label")) != want:
            continue
        when = parse_stamp(row.get("spawned_at"))
        if when is None or now - when > window:
            continue
        hits.append((when, session_id, row))
    return max(hits, key=lambda hit: hit[0]) if hits else None


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--subject", required=True,
                        help="the row's subject — the label the spawn would carry")
    parser.add_argument("--minutes", type=int, default=DEFAULT_WINDOW_MINUTES,
                        help="window length in minutes (default %d)" % DEFAULT_WINDOW_MINUTES)
    parser.add_argument("--endpoint", help="attention-store base URL override")
    parser.add_argument("--now", help="ISO-8601 instant to measure the window from "
                                      "(default: the wall clock; fixtures pin it)")
    parser.add_argument("--json", action="store_true", help="emit the row as JSON")
    args = parser.parse_args(argv)

    if args.minutes <= 0:
        parser.error("--minutes must be positive")

    now = parse_stamp(args.now) if args.now else None
    if args.now and now is None:
        parser.error("--now is not an ISO-8601 instant")

    ledger = spawn_ledger.read_ledger(endpoint=args.endpoint)
    if ledger is None:
        print("opened-recently: the session ledger could not be read — "
              "an unreadable ledger is not an empty one", file=sys.stderr)
        return 2

    hit = opened_within(ledger, args.subject, timedelta(minutes=args.minutes), now=now)
    if hit is None:
        return 1

    when, session_id, row = hit
    if args.json:
        json.dump({"session_id": session_id, "spawned_at": when.isoformat(),
                   "label": row.get("label"), "mode": row.get("mode"),
                   "resumed_from": row.get("resumed_from")}, sys.stdout, indent=2)
        sys.stdout.write("\n")
    else:
        print("%s\t%s\t%s" % (when.isoformat(), session_id, row.get("label")))
    return 0


if __name__ == "__main__":
    sys.exit(main())
