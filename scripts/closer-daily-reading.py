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

  * `closers=` — records carrying a non-null `closer`: every line the hook
    found a `👤 You:` closer on. ⚠️ `no-closer` is the only reason that leaves
    it null. `not-a-gate` DOES carry it — the hook found a closer line and then
    ruled it is not an ask — and those records are counted, because the
    session-bound check runs *before* the gate checks (line 821 precedes 895),
    so a `⏰ Ends:` on a non-gate closer was judged too. On 2026-11-24 they were
    2 672 of 3 324 `closers=` records; excluding them would hide most of the
    population the criterion is about.
  * `session-bound=` — **distinct sessions** among those records whose session-bound
    class is non-null. Distinct, not records: a re-blocked closer re-emits its
    original `class` under `reason: blocked-retry`, so a record count would report
    one session twice. ⚠️ The class is **re-derived** from each record's `ends` with
    `closer-gate-lint.py`'s own `session_bound_class()`, not read from the stored
    `class`: that field is a judgement made by whichever classifier the hook shipped
    when the record was written, so a detector fix would otherwise apply only to new
    records. A record with no `ends` (every record before 2026-11-19, and every
    `blocked-retry`) cannot be re-judged and falls back to its stored `class` — the
    fail-closed direction, so history is never silently cleared.
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
import importlib.util
import json
import os
import sys

DEFAULT_ROOT = "~/.claude/state/closer-gate-lint"
#: ⚠️ The hook's own override, honoured here too. `closer-gate-lint.py` resolves its
#: record directory from this variable first (line 587), so a host that sets it
#: writes somewhere else — and a reading that ignored it would look in the default
#: directory, find nothing, and exit 3 on every day. Reading the wrong directory
#: fails loudly, but it fails on the wrong question.
DEFAULT_ROOT_ENV = "SUPERVISOR_CLOSER_GATE_LINT_STATE_DIR"
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


def default_root(environ=None):
    """The hook's record directory: its own override if set, else the default."""
    environ = os.environ if environ is None else environ
    return environ.get(DEFAULT_ROOT_ENV) or DEFAULT_ROOT


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


def _load_classifier():
    """The lint's own `session_bound_class`, or `None` when the sibling script is absent.

    ⚠️ One source of truth, not a copy: the reading re-judges history with exactly the
    function the live hook uses, so a detector fix reaches records already on disk. A
    missing or broken sibling must not take the reading down — `None` falls back to each
    record's stored `class`, which is the pre-fix behaviour.
    """
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "closer-gate-lint.py")
    try:
        spec = importlib.util.spec_from_file_location("closer_gate_lint", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module.session_bound_class
    except Exception:
        return None


def _class_of(record, classify):
    """The record's session-bound class, re-derived from its `ends` when that is possible.

    ⚠️ The stored `class` is a judgement made at write time by whichever classifier the
    hook shipped then, and a detector fix would otherwise apply only to records written
    after it. Re-deriving is the whole point of this reading: the observable is in `ends`,
    so history can be re-judged without rewriting the log.

    ⚠️ `ends` was added to the record on 2026-11-19, and `blocked-retry` records carry no
    `ends` at all — 288 of 335 class-bearing records measured 2026-11-25. A record with no
    `ends` cannot be re-judged, and its stored `class` is the only evidence there is, so
    it is trusted. That is the fail-closed direction: an un-rejudgeable record stays red
    rather than being silently cleared.
    """
    ends = record.get("ends")
    if ends and classify is not None:
        try:
            return classify(ends)
        except Exception:
            pass
    return record.get("class")


def read(records, classify=None):
    """`(closers, session_bound_sessions, closer_sessions)` for one day's records.

    ⚠️ A null `session_id` counts as its own distinct session, never as absent.
    `verdict()` takes the id from the payload, so a record can carry a `class` with
    no id — and dropping it would UNDERCOUNT the session-bound population, which is
    the one direction that certifies a day clean. One shared sentinel keeps such
    records in the count without pretending they are one session each.
    """
    closers = [r for r in records if r.get("closer")]
    bound = {r.get("session_id") for r in closers if _class_of(r, classify)}
    # The coverage numerator keeps real ids only: a null id is not a session the
    # census counted, so counting it would inflate coverage — the opposite error.
    sessions = {r.get("session_id") for r in closers if r.get("session_id")}
    return len(closers), len(bound), len(sessions)


def census_from_goal_page(path, day):
    """The `sessions` cell of `day`'s row in a goal page's readings table, or `None`.

    The column is found by its header name, not by position: the table gains a
    column once and every positional read of it silently returns the wrong number.
    Falls back to index 3 only when no header row is found at all.
    """
    try:
        # `errors="replace"`: a goal page with one bad byte must not crash the
        # reading. The row is parsed by ASCII anchors (the date, the header name),
        # so a replacement character in prose costs nothing.
        handle = open(path, encoding="utf-8", errors="replace")
    except OSError:
        return None
    index = 3
    with handle:
        for line in handle:
            if not line.lstrip().startswith("|"):
                continue
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            if not cells:
                continue
            if "sessions" in [c.lower() for c in cells]:
                index = [c.lower() for c in cells].index("sessions")
                continue
            if cells[0] != day.isoformat():
                continue
            if len(cells) <= index:
                return None
            try:
                return int(cells[index])
            except ValueError:
                return None
    return None


class _Parser(argparse.ArgumentParser):
    """`argparse` exits 2 on a usage error, which is this script's session-bound code.

    A typo'd flag would then read as "the day is NOT clean" — a wrong answer in the
    direction that gets investigated as a real finding. Usage errors are 1 here, so
    the two can never be confused.
    """

    def error(self, message):
        self.print_usage(sys.stderr)
        self.exit(EXIT_USAGE, f"{self.prog}: error: {message}\n")


def parse_args(argv):
    parser = _Parser(
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
        default=None,
        help=f"directory of <session>.jsonl records "
        f"(default: ${DEFAULT_ROOT_ENV} or {DEFAULT_ROOT})",
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

    root = os.path.expanduser(args.root or default_root())
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

    closers, session_bound, sessions = read(records, _load_classifier())

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
    if census is not None and census <= 0:
        # ⚠️ A non-positive denominator makes the floor vacuous: `sessions < 0` is
        # never true, so a day with zero coverage would certify clean — exactly the
        # vacuous pass this reading replaces. No population, no clean day.
        print(
            f"closer-daily-reading: census sessions={census} is not a population — "
            "coverage cannot be proven against it",
            file=sys.stderr,
        )
        return EXIT_COVERAGE
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
