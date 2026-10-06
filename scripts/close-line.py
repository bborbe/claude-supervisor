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


def close_line(task, session, liveness, now=None, root=None):
    """The runnable line, or the candidate that carries no command."""
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

    print(close_line(args.task, args.session, args.liveness, now, args.plugin_root))
    return 0 if args.liveness in RUNNABLE else 1


if __name__ == "__main__":
    sys.exit(main())
