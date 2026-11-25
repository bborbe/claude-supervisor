#!/usr/bin/env python3
"""One day's SC4 reading over the closer-gate-lint records.

The goal criterion reads *"no worker closer's `⏰ Ends:` observable is a
session-bound mechanism, over a 7-day window."* Until now the daily series
recorded a bare `closer-gate-lint.py` run: with no payload on stdin the hook
lints nothing and exits 0, so every recorded `exit 0` certified a clean day
without looking at a single closer. This reads the records the hook actually
wrote and prints one line.

    <date> closers=<n> session-bound=<n> sessions=<n> coverage=<n>/<m>

Definitions, pinned:

  * `closers=` — records carrying a non-null `closer`. A record with
    `reason: not-a-gate` or `no-closer` has `closer: null` and is not a closer
    the hook judged.
  * `session-bound=` — **distinct sessions** among those records whose `class`
    is non-null. Distinct, not records: a re-blocked closer re-emits its
    original `class` under `reason: blocked-retry`, so a record count would
    report one session twice.
  * `sessions=` — distinct `session_id` values among the `closers=` records.
  * `coverage=<n>/<m>` — `n` is `sessions=`; `m` is the same day's census
    `sessions` value, supplied with `--census-sessions` (or read from the goal
    page's readings row with `--goal-page`). `unknown` when neither is given.

⚠️ The two populations are not the same thing and the comparison is a floor,
never an equality: the census counts sessions **live at a moment**, while the
log accumulates every session that **stopped** during the day, so the log
normally runs far above the census (measured 2026-11-24: 115 distinct sessions
with a closer against `sessions=30`). A reading below 0.9 × `m` means the hook
went silent for most of the population — the failure this floor exists to
catch.

Exit codes (a non-zero exit is the reading refusing to certify):

  0  records present, no session-bound observable, coverage at or above floor
  2  at least one session-bound observable — the day is NOT clean
  3  no records for the day — an empty read cannot certify a clean day
  4  coverage below the floor, or no denominator to check it against
  1  usage or IO error

⚠️ 2 outranks 4: when a day is both unclean and thinly covered, the substantive
finding is the session-bound observable, not the population size.

`--root` points at a directory of `<session>.jsonl` files. It exists so two
copies of this script can be compared on ONE frozen snapshot of a day: the live
directory keeps accruing (measured 2026-11-24: 2 321 closers → 2 353 in fifteen
minutes), so comparing two live runs makes a real mismatch indistinguishable
from new records.

Read-only: nothing here writes, and the hook's own files are opened for reading
only.
"""

import argparse
import datetime
import json
import os
import sys

DEFAULT_ROOT = "~/.claude/state/closer-gate-lint"
#: The census population floor. Below this fraction the hook's own coverage is
#: unproven and the day cannot be certified, whatever it read.
COVERAGE_FLOOR = 0.9

EXIT_CLEAN = 0
EXIT_USAGE = 1
EXIT_SESSION_BOUND = 2
EXIT_EMPTY = 3
EXIT_COVERAGE = 4


def _local_date(raw):
    """The local calendar date a record's UTC `ts` falls on, or `None`."""
    if not raw or not isinstance(raw, str):
        return None
    text = raw.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        stamp = datetime.datetime.fromisoformat(text)
    except ValueError:
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=datetime.timezone.utc)
    return stamp.astimezone().date()


def record_paths(root):
    """Every `<session>.jsonl` under `root`, sorted for a deterministic read."""
    if not os.path.isdir(root):
        return []
    return sorted(
        os.path.join(root, name) for name in os.listdir(root) if name.endswith(".jsonl")
    )


def records_for(root, day):
    """Every record whose local date is `day`, plus the counts that were skipped."""
    kept, unreadable, unparsed, undated = [], 0, 0, 0
    for path in record_paths(root):
        try:
            handle = open(path, encoding="utf-8", errors="replace")
        except OSError:
            unreadable += 1
            continue
        with handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except ValueError:
                    unparsed += 1
                    continue
                if not isinstance(record, dict):
                    unparsed += 1
                    continue
                stamp = _local_date(record.get("ts"))
                if stamp is None:
                    undated += 1
                    continue
                if stamp == day:
                    kept.append(record)
    return kept, unreadable, unparsed, undated


def read(records):
    """`(closers, session_bound_sessions, closer_sessions)` for one day's records."""
    closers = [r for r in records if r.get("closer")]
    bound = {r.get("session_id") for r in closers if r.get("class") and r.get("session_id")}
    sessions = {r.get("session_id") for r in closers if r.get("session_id")}
    return len(closers), len(bound), len(sessions)


def census_from_goal_page(path, day):
    """The `sessions` cell of `day`'s row in a goal page's readings table, or `None`."""
    try:
        handle = open(path, encoding="utf-8")
    except OSError:
        return None
    with handle:
        for line in handle:
            if not line.lstrip().startswith("|"):
                continue
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            if not cells or cells[0] != day.isoformat():
                continue
            # Date | census unkept | census double-kept | sessions | closer-lint
            if len(cells) < 4:
                return None
            try:
                return int(cells[3])
            except ValueError:
                return None
    return None


def parse_args(argv):
    parser = argparse.ArgumentParser(
        description="One day's SC4 reading over the closer-gate-lint records.",
        epilog="Exit 0 clean · 2 session-bound observable · 3 no records · "
        "4 coverage below floor or unknown · 1 usage/IO.",
    )
    parser.add_argument(
        "--date",
        default=None,
        help="calendar day, YYYY-MM-DD (default: today, local time)",
    )
    parser.add_argument(
        "--root",
        default=DEFAULT_ROOT,
        help=f"directory of <session>.jsonl records (default: {DEFAULT_ROOT})",
    )
    parser.add_argument(
        "--census-sessions",
        type=int,
        default=None,
        help="the same day's census `sessions` value (the coverage denominator)",
    )
    parser.add_argument(
        "--goal-page",
        default=None,
        help="goal page whose readings row carries the census `sessions` cell",
    )
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)

    if args.date:
        try:
            day = datetime.date.fromisoformat(args.date)
        except ValueError:
            print(
                f"closer-daily-reading: --date must be YYYY-MM-DD, got {args.date!r}",
                file=sys.stderr,
            )
            return EXIT_USAGE
    else:
        day = datetime.datetime.now().astimezone().date()

    root = os.path.expanduser(args.root)
    if not os.path.isdir(root):
        print(f"closer-daily-reading: no such directory: {root}", file=sys.stderr)
        return EXIT_USAGE

    records, unreadable, unparsed, undated = records_for(root, day)
    if unreadable or unparsed or undated:
        print(
            f"closer-daily-reading: skipped {unreadable} unreadable file(s), "
            f"{unparsed} unparsable and {undated} undated line(s)",
            file=sys.stderr,
        )

    closers, session_bound, sessions = read(records)

    census = args.census_sessions
    if census is None and args.goal_page:
        census = census_from_goal_page(os.path.expanduser(args.goal_page), day)

    coverage = "unknown" if census is None else str(census)
    print(
        f"{day.isoformat()} closers={closers} session-bound={session_bound} "
        f"sessions={sessions} coverage={sessions}/{coverage}"
    )

    if not records:
        print(
            f"closer-daily-reading: no records dated {day.isoformat()} — "
            "an empty read cannot certify a clean day",
            file=sys.stderr,
        )
        return EXIT_EMPTY
    if session_bound:
        print(
            f"closer-daily-reading: {session_bound} session(s) ended on a session-bound "
            "observable — the day is NOT clean",
            file=sys.stderr,
        )
        return EXIT_SESSION_BOUND
    if census is None:
        print(
            "closer-daily-reading: no census denominator — coverage unproven "
            "(pass --census-sessions or --goal-page)",
            file=sys.stderr,
        )
        return EXIT_COVERAGE
    if sessions < COVERAGE_FLOOR * census:
        print(
            f"closer-daily-reading: coverage {sessions}/{census} is below the "
            f"{COVERAGE_FLOOR:.0%} floor — the hook went silent for most of the "
            "population",
            file=sys.stderr,
        )
        return EXIT_COVERAGE
    return EXIT_CLEAN


if __name__ == "__main__":
    sys.exit(main())
