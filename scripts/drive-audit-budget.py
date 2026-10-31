#!/usr/bin/env python3
"""The manager drive leg's audit bound -- a wall clock, never a row count.

`agents/goal-drive.md` clause (1) scores every non-terminal row with no live worker
by dispatching one `vault-cli:task-auditor` subagent PER ROW, sequentially. Nothing
bounded that loop, so its duration was a function of the tracked set's size and of the
audit cache's warmth -- and a cold tick ran to completion however long that took.

Measured 2026-10-16 on a round with ZERO actionable rows, which is the case where the
audit is the whole of the remaining work. Recurring Operator Gates: `ready-to-start` 0
and `stuck` 0 in the tick's own store record, and the tick's own artifacts ran
`.tracked.txt` 11:05:04 -> `.verdicts.json` 11:39:01, about 34 minutes, the cold audit
being all of it -- the cache that tick wrote is that same tick's output. Corroborated on
`attention-controller-is-the-fleet-s-single-source-of-session-state`, 09:24 -> 09:43,
about 19 minutes. `commands/steps/goal-drive.md` had already recorded the sibling
measurement: 27 dispatches, 0 cache hits, about 246k subagent tokens, 29 minutes.

WHY A WALL CLOCK AND NOT A ROW CAP. Clause (1) forbids a per-tick row cap in as many
words -- *"Do not invent a budget, a batch size or a sampling rule: there is no per-tick
cap in this clause, and a leg that introduces one silently narrows the set the caller
asked it to cover."* That prohibition is about SILENCE: a row cap truncates coverage and
reports nothing, so a narrowed sweep is indistinguishable from a complete one. A wall
clock that ends the loop loudly is the opposite shape, and it is the one clause (1)
already blesses -- the `Cap failure` block, *"a FAILURE, not a `Held`"*: the bound is
named, the rows audited are counted, and every row left is reported as unaudited. A
caller reading the report can tell a bounded stop from a complete sweep, which is the
whole of what the prohibition protects.

So the caller checks this BEFORE each dispatch and stops on `TERMINAL`. It never sizes
the audit set, never samples it, and never drops a row quietly.

⚠️ **This bound can only ever SHORTEN a walk that would otherwise run to completion.**
It never admits a row the audit would have skipped, and it never licenses acting on a
row that was not scored -- an unaudited row is reported, never opened.

Usage:
  drive-audit-budget.py --started-at <ISO8601> [--budget-seconds N]
                        [--audited N] [--total M] [--now <ISO8601>]

`--started-at` is the LEG's own start, read once with `date -u +%Y-%m-%dT%H:%M:%SZ` and
passed unchanged on every call. A naive stamp is read as local time. `--now` exists so a
caller that already read the clock can pass it, and so the boundary is testable without
a sleep; it defaults to the real clock.

Output is one line, and the leading token is the contract:

  CONTINUE elapsed=<n>s remaining=<n>s audited=<n>/<total>
  TERMINAL: drive audit budget exhausted -- ...

Exit: 0 CONTINUE (dispatch the next row) / 3 TERMINAL (stop and report the remainder)
      / 2 REFUSED (the arguments are unusable -- never read as a verdict).

⚠️ **REFUSED is not CONTINUE and not TERMINAL.** An unusable `--started-at` -- absent,
unparseable, or in the future -- is a read that failed, and the three-state discipline
this repo applies everywhere else applies here: a failed read is not a measurement. A
caller that treats it as CONTINUE has no bound at all, and one that treats it as
TERMINAL stops a sweep on a typo. Report it and take neither branch.
"""
import argparse
import datetime
import sys

#: The default wall-clock bound for one leg's audit loop, in seconds.
#:
#: Sized from the measurements in the module docstring: a cold audit runs roughly 60s
#: per row (27 dispatches / 29 min, and ~20 rows / ~34 min both land near a minute), so
#: 900s covers a cold set of about fifteen rows and still bounds a pathological one. It
#: is a backstop for the cold tick, NOT a per-tick allowance -- a warm tick dispatches
#: nothing and never approaches it.
DEFAULT_BUDGET_SECONDS = 900

CONTINUE = 0
REFUSED = 2
TERMINAL = 3


class Refused(Exception):
    """The arguments are unusable. Never a verdict about the budget."""


def parse_stamp(text, flag):
    """An ISO8601 stamp as an aware datetime. A naive one is read as local time."""
    raw = text.strip()
    if raw.endswith(("Z", "z")):
        raw = raw[:-1] + "+00:00"
    try:
        stamp = datetime.datetime.fromisoformat(raw)
    except ValueError:
        raise Refused("--%s is not an ISO8601 timestamp: %r" % (flag, text))
    if stamp.tzinfo is None:
        stamp = stamp.astimezone()
    return stamp


def non_negative(text, flag):
    """A count as a non-negative int. A bad one refuses rather than defaulting."""
    try:
        value = int(text)
    except ValueError:
        raise Refused("--%s is not an integer: %r" % (flag, text))
    if value < 0:
        raise Refused("--%s is negative: %d" % (flag, value))
    return value


def progress(audited, total):
    """`<n>/<total>`, or `<n>/?` when the caller did not pass a total."""
    return "%d/%s" % (audited, total if total else "?")


def render_terminal(elapsed, budget, audited, total):
    """The TERMINAL line. One line, leading token fixed, nothing after it.

    It names the bound, the progress, and the remainder, because the whole point of
    choosing a wall clock over a row cap is that the stop is legible: a reader must be
    able to tell a bounded sweep from a complete one without re-deriving anything.
    """
    left = max(total - audited, 0) if total else 0
    return (
        "TERMINAL: drive audit budget exhausted -- elapsed %ds >= budget %ds; "
        "audited %s rows, %d left unaudited -- reported as a failure, never a hold"
        % (int(elapsed), budget, progress(audited, total), left)
    )


def main():
    parser = argparse.ArgumentParser(
        description="Bound the manager drive leg's audit loop by wall clock."
    )
    parser.add_argument(
        "--started-at",
        required=True,
        help="the leg's own start, ISO8601 (read once with `date -u +%%Y-%%m-%%dT%%H:%%M:%%SZ`)",
    )
    parser.add_argument(
        "--budget-seconds",
        type=int,
        default=DEFAULT_BUDGET_SECONDS,
        help="the wall-clock bound for this leg's audit loop (default %d)" % DEFAULT_BUDGET_SECONDS,
    )
    parser.add_argument("--audited", default="0", help="rows already scored this leg")
    parser.add_argument("--total", default="0", help="rows in this leg's audit set")
    parser.add_argument(
        "--now",
        default=None,
        help="the current time, ISO8601 (defaults to the real clock; a test seam)",
    )
    args = parser.parse_args()

    try:
        started = parse_stamp(args.started_at, "started-at")
        now = parse_stamp(args.now, "now") if args.now else datetime.datetime.now().astimezone()
        audited = non_negative(args.audited, "audited")
        total = non_negative(args.total, "total")
        if args.budget_seconds <= 0:
            raise Refused("--budget-seconds must be positive: %d" % args.budget_seconds)
    except Refused as exc:
        print("drive-audit-budget: REFUSED -- %s" % exc, file=sys.stderr)
        print(
            "  a refused read is not a measurement: take neither the CONTINUE nor the "
            "TERMINAL branch on it",
            file=sys.stderr,
        )
        return REFUSED

    elapsed = (now - started).total_seconds()
    if elapsed < 0:
        print(
            "drive-audit-budget: REFUSED -- --started-at is %ds in the future (clock skew, "
            "or the wrong stamp); an unreadable elapsed time is not a budget verdict"
            % int(-elapsed),
            file=sys.stderr,
        )
        return REFUSED

    if elapsed >= args.budget_seconds:
        print(render_terminal(elapsed, args.budget_seconds, audited, total))
        return TERMINAL

    print(
        "CONTINUE elapsed=%ds remaining=%ds audited=%s"
        % (int(elapsed), int(args.budget_seconds - elapsed), progress(audited, total))
    )
    return CONTINUE


if __name__ == "__main__":
    sys.exit(main())
