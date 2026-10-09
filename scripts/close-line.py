#!/usr/bin/env python3
"""Emit the sweep's `✅ DONE — CLOSE:` action line — stamped, and never bare.

The line is correct at the moment it is emitted and can decay to false before the
operator reads it. The session can end in between, and a printed line cannot be
withdrawn: measured 2026-09-27, a Manager Layer sweep printed a close line for
`Extract fleet-loop's Per-Row Pane Read Into fleet-sweep-reader [b7d4c0fe]`, the
caller had verified the session live at emission, and ~10 minutes later the
registry entry was gone, `ps` reported nothing and no window pane matched — leaving
a close instruction for a session that no longer existed.

So the line carries the two things a reader needs in order to settle that without
trusting the render: **its verification moment** and **the exact command that
re-checks it**. The rule is the vault's own — a verification claim carries its
command and its moment — and the precedent is the sweep table's
`⏸️ HELD — <reason> · <age>`, where the age is part of the cell, not decoration.

⚠️ **This is the line's one producer.** `sweep-gate.py`'s `action_lines()` and the
two manager print steps (`commands/manager-loop.md`, `commands/manager-status.md`)
all render through here, so the three sites cannot drift and a bare close
instruction has no door left. A caller that hand-assembles the line is the defect
this script exists to remove, not an optimisation of it.

Usage:
  close-line.py --task "<task name>" --session "<full session id>" --liveness <verdict>

`--liveness` is the caller's own verdict, taken this run — the caller owns it and
this script never re-derives it (`session-liveness.py` is the instrument). The
vocabulary is that script's:

  live    | parked  → the runnable line, exit 0
  absent  | unknown → the candidate line, no command, exit 1

⚠️ `absent` and `unknown` are NOT the same state and the caller must not collapse
them: an unreadable registry cannot prove a session dead. They share an exit code
here because they share a rendered shape — the caller holds the distinction and is
the one that acts on it.

⚠️ **A LIVE VERDICT IS NOT SUFFICIENT, and `--closer` is the second half.** The
registry answers *is the process alive*; it cannot answer *has the turn finished*,
and a session that has run its own close is still live for as long as its process
holds the socket. So the runnable line needs a second input, and the manager
already takes it — `agents/manager-drive.md`'s fourth read, `scripts/reap-closer.py`:

  --closer closed     → the candidate line, no command, exit 1
  --closer open       → the liveness verdict decides, exactly as before
  --closer unreadable → the liveness verdict decides, exactly as before
  (omitted)           → the liveness verdict decides, exactly as before

⚠️ `unreadable` is deliberately NOT a suppression: `agents/manager-drive.md` states
the rule — this read "may only *remove* a notice, and only on a positive match. It
never adds a reap, and a failed read never suppresses one."

⚠️ **Omitting `--closer` is legal and behaves exactly as this script did before the
flag existed**, so a caller that has not adopted the second read is unaffected. It
is not a silent default to `open`: the flag is absent-or-given, never guessed.

⚠️ **Why the closed case still renders the CANDIDATE rather than nothing.** The row
is terminal with a live session, so the frame must still carry it — the defect is
the *command*, not the row, and a row that silently vanished would be a worse one.
Measured 2026-10-09: a completed row whose session had closed clean at 07:39:39Z was
handed a runnable close line at 07:43:21Z — four minutes AFTER the close it was
asking for. The line was void at birth, which is one step further than the decay
this script's opening note records: there the verdict aged out, here the work was
already done when the line was printed.

⚠️ **The stamp is the emission moment, taken here, not the caller's verdict time.**
That is deliberate: a caller whose verdict is already minutes old emits a line that
says so honestly, and the embedded re-check is what settles it for the reader.

Output is one line on stdout. Exit 0 for the runnable line, 1 for the candidate,
2 for a usage error.

⚠️ **A `check=True` caller must not read exit 1 as failure.** The candidate is a
normal outcome — a done task whose session is not confirmed live — and the exit
code exists so a CLI consumer can branch on it, not so a caller can abort. This
matters because the wiring this script exists for is `sweep-gate.py`'s
`action_lines()`, which shells out to its sibling `BOX_TABLE` with
`subprocess.run(..., check=True)`; copying that pattern verbatim would raise
`CalledProcessError` on every candidate and take the whole sweep frame down.
Capture stdout and read the exit code, or call without `check`.

Run: python3 scripts/close-line.py --help
"""

import argparse
import os
import sys
from datetime import datetime, timezone

LIVENESS_LIVE = "live"
LIVENESS_PARKED = "parked"
LIVENESS_ABSENT = "absent"
LIVENESS_UNKNOWN = "unknown"

LIVENESS_WORDS = (LIVENESS_LIVE, LIVENESS_PARKED, LIVENESS_ABSENT, LIVENESS_UNKNOWN)
RUNNABLE = (LIVENESS_LIVE, LIVENESS_PARKED)

# The second input — the caller's own read of the session's NEWEST CLOSER, taken
# this run by `scripts/reap-closer.py` and owned by the caller exactly as the
# liveness verdict is. Only `closed` changes the render; `open` and `unreadable`
# both leave the liveness verdict deciding, for the reason in the module docstring.
CLOSER_CLOSED = "closed"
CLOSER_OPEN = "open"
CLOSER_UNREADABLE = "unreadable"
CLOSER_WORDS = (CLOSER_CLOSED, CLOSER_OPEN, CLOSER_UNREADABLE)

# The plugin root, resolved the way `sweep-gate.py` resolves its `BOX_TABLE`: an
# absolute expanduser path rather than an inherited `${CLAUDE_PLUGIN_ROOT}`. The
# gate is vault-local and does not inherit that variable, so a path it must embed
# in a *runnable* command has to be resolved rather than deferred to the reader's
# shell. `--plugin-root` and `CLOSE_LINE_PLUGIN_ROOT` both override, so a fixture
# can exercise the line without reading the operator's real install.
DEFAULT_PLUGIN_ROOT = "~/.claude/plugins/marketplaces/claude-supervisor"

# The half the line exists to deliver. Kept verbatim from the shipped wording —
# `65 Runbooks/Manager Session.md` § Sweep output specifies it, and `sweep-gate.py`
# emitted it before this script existed.
CLOSE_ACTION = "run /vault-cli:sync-progress + /vault-cli:session-close"

CANDIDATE_SUFFIX = "terminal, liveness UNVERIFIED"

# The candidate's OTHER reason, and it must not be folded into the one above: the
# liveness here is CONFIRMED live, so "UNVERIFIED" would be a false statement about
# the probe. What is void is the instruction, because the session already ran it.
CLOSED_SUFFIX = "session already closed clean — the close it asks for has run"


def plugin_root(override=None):
    """Resolve the plugin root the re-check command is built from."""
    return os.path.expanduser(
        override or os.environ.get("CLOSE_LINE_PLUGIN_ROOT") or DEFAULT_PLUGIN_ROOT
    )


def recheck_command(session, root=None):
    """The exact command a reader runs to re-settle the line.

    The full session id, never a prefix: `session-liveness.py` accepts an 8-char
    prefix for its own convenience, but the id embedded here is the one the caller
    holds, and shortening it would hand the reader a weaker probe than the caller
    had. The prefix form is also the measured trap on the sibling read — a prefix
    returns UNREADABLE, which is the branch that says "act as before".
    """
    return f"python3 {plugin_root(root)}/scripts/session-liveness.py --check {session}"


def stamp(now=None):
    """The emission moment, ISO-8601 in UTC, second precision, `Z`-suffixed."""
    moment = now or datetime.now(timezone.utc)
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def sid8(session):
    """The display handle. Short here is correct — the full id rides the re-check."""
    return session[:8]


def close_line(task, session, liveness, now=None, root=None, closer=None):
    """The runnable line, or the candidate that carries no command.

    `closer` is the caller's own newest-closer read, and `closed` outranks a live
    verdict: a session that has already run `/vault-cli:session-close` is live for
    as long as its process holds the socket, so liveness alone cannot see that the
    instruction is void. See the module docstring for why this renders the
    candidate rather than nothing.
    """
    if closer == CLOSER_CLOSED:
        return f"🔧 CLOSE-ME CANDIDATE: {task} [{sid8(session)}] — {CLOSED_SUFFIX}"
    if liveness in RUNNABLE:
        return (
            f"✅ DONE — CLOSE: {task} [{sid8(session)}] — verified {stamp(now)} "
            f"· re-check: {recheck_command(session, root)} · {CLOSE_ACTION}"
        )
    return f"🔧 CLOSE-ME CANDIDATE: {task} [{sid8(session)}] — {CANDIDATE_SUFFIX}"


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Emit the sweep's stamped close line, or the no-command candidate."
    )
    parser.add_argument("--task", required=True, help="the task name, verbatim")
    parser.add_argument("--session", required=True, help="the FULL session id")
    parser.add_argument(
        "--liveness",
        required=True,
        choices=LIVENESS_WORDS,
        help="the caller's own verdict, taken this run",
    )
    parser.add_argument(
        "--closer",
        default=None,
        choices=CLOSER_WORDS,
        help="the caller's own newest-closer read (reap-closer.py) — omit if "
             "the caller did not take it; only `closed` changes the render",
    )
    parser.add_argument("--now", default=None, help="override the stamp (ISO-8601)")
    parser.add_argument("--plugin-root", default=None, help="override the plugin root")
    args = parser.parse_args(argv)

    now = None
    if args.now:
        try:
            now = datetime.fromisoformat(args.now.replace("Z", "+00:00"))
        except ValueError:
            print(f"close-line: --now is not ISO-8601: {args.now}", file=sys.stderr)
            return 2

    print(close_line(args.task, args.session, args.liveness, now, args.plugin_root,
                     args.closer))
    # The exit code tracks the RENDER, never the liveness alone: a closed-clean
    # session renders the candidate even on a `live` verdict, and a caller that
    # branches on 0/1 must see the same split the reader does.
    runnable = args.liveness in RUNNABLE and args.closer != CLOSER_CLOSED
    return 0 if runnable else 1


if __name__ == "__main__":
    sys.exit(main())
