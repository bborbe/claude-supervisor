#!/usr/bin/env python3
"""The fleet tier's keep-alive: is the fleet ROUND still running?

The other tiers already have one — `worker-keepalive.py` for workers, `topic-keepalive.py`
for goal and topic managers. The fleet tier had **nothing**, so a dead fleet round was
restarted by no one and noticed only when a human looked.

⚠️ **The signal is the digest, NOT the heartbeat.** `manager-liveness.py` answers
`OK age=71s` for a fleet manager whose model has not run in two hours, because the heartbeat
is written every 60s by the model-free gate loop — a launchd job that keeps ticking whether
or not a round ever happens. Measured 2026-10-30: `manager-liveness` read `OK` throughout a
2h30m outage, while the digest sat at `2026-10-30T11:38:21Z` with `age=9030s limit=1080s`.
A heartbeat proves the timer works; only the digest proves the round did.

So this reads the digest ledger — the artifact a completed round writes — and never the
heartbeat. It is the same number the notifier already computes, read here so a keeper can
act on it instead of only announcing it.

Verdicts (first line of stdout):

  ok        the round is inside its limit — nothing to do
  stale     the round has not completed within its limit — resume it
  unknown   the ledger is missing or unreadable — cannot tell, never "fine"

Exit 0 for `ok`, 10 for `stale` (the code `scripts/fleet-digest.py --check` already uses for
a lapsed round), 2 for `unknown`.
"""

from __future__ import annotations

import argparse
import os
import re
import sys

DEFAULT_LEDGER = os.path.expanduser("~/.claude/state/fleet-digest/ledger.txt")

AGE = re.compile(r"\bage=(\d+)s\b")
LIMIT = re.compile(r"\blimit=(\d+)s\b")
DIGEST = re.compile(r"\bdigest=(\S+)")

TAIL_BYTES = 256 * 1024


def _tail_lines(path: str, window: int = TAIL_BYTES) -> list[str]:
    """The file's last `window` bytes as lines, with the first (partial) line dropped.

    ⚠️ **Bounded on purpose.** The ledger is append-only and grows one line a minute
    forever, so a whole-file read costs more every day for a reading that only ever needs
    the tail. `manager-menu-guard.py` and `who-needs-me.py` use the same 256 KB seek for
    the same shape. 256 KB is ~1700 lines, roughly 28 hours at one line a minute.
    """
    try:
        size = os.path.getsize(path)
        with open(path, encoding="utf-8") as fh:
            if size > window:
                fh.seek(size - window)
                fh.readline()      # discard the partial line the seek landed inside
            return fh.read().splitlines()
    except OSError:
        return []


def last_reading(ledger: str) -> tuple[int, int, str] | None:
    """`(age, limit, digest)` from the ledger's last line, or None when it carries neither.

    ⚠️ The LAST line, never the newest file: the ledger is append-only and the tick writes
    one line a minute, so the tail is the current reading even when the digest itself is
    hours old — that staleness is exactly what this reads.
    """
    lines = [ln for ln in _tail_lines(ledger) if ln.strip()]
    if not lines:
        return None
    last = lines[-1]
    age = AGE.search(last)
    limit = LIMIT.search(last)
    if not age or not limit:
        return None
    digest = DIGEST.search(last)
    return int(age.group(1)), int(limit.group(1)), (digest.group(1) if digest else "-")


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--ledger", default=DEFAULT_LEDGER,
                    help="the fleet digest ledger (default: ~/.claude/state/fleet-digest/ledger.txt)")
    a = ap.parse_args(argv)

    reading = last_reading(a.ledger)
    if reading is None:
        print("unknown")
        print(f"no readable digest reading in {a.ledger} — cannot tell, never 'fine'")
        return 2

    age, limit, digest = reading
    if age <= limit:
        print("ok")
        print(f"digest {digest} is {age}s old, inside its {limit}s limit")
        return 0

    print("stale")
    print(f"digest {digest} is {age}s old against a {limit}s limit — the round has not "
          f"completed. Resume the fleet manager; a heartbeat will read OK throughout, so do "
          f"not probe liveness instead")
    return 10


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
