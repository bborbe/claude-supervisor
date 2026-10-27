#!/usr/bin/env python3
"""Record how a Claude Code pane ended, so a dead session can be attributed after the fact.

**Why this exists.** Until now a Claude session that died left no trace anywhere: the
task file kept `claude_session_id`, the session registry kept a pid, and both still read
as "running" long after the process was gone. Nothing recorded *that* it ended, *when*,
or *how* — so a killed session and a session that was never started looked identical from
every surface the fleet reads.

**Why a new store, and not one of the three ledgers.** `asked-ledger.py` records who was
ASKED and who resolved it, `drive-ledger.py` records what a drive round nudged, and
`incident-ledger.py` records a *blocker's* lifecycle keyed on a dedup key. All three
answer questions about sessions that are alive or about the work in front of them; none
records a process ending. This store's population is different — every pane end, not
every blocker — so folding it into `incident-ledger.py` would either pollute that store's
permanent dedup-key namespace with non-blockers or make every ended session look like an
incident. Checked 2026-10-27; recorded here because the check is the reason.

**The writers, and what each survives.** A record is only as good as the writer that
survives the death it is describing:

1. **The pane shell's `EXIT` trap** — this script's `append` verb. Survives a normal
   exit, a non-zero exit, and a signal delivered to the *child* (the shell's `waitpid`
   reports `128+n`, which is how `137` becomes visible at all). Does NOT survive a signal
   delivered to the whole process group, because the shell dies with the child.
2. **A flushed heartbeat line** — a periodic `heartbeat` write, so a record that stops
   mid-sequence still says when the session was last known alive.
3. **A detached observer** — a `setsid` process outside the pane's process group that
   polls the pid and writes `unknown-end` when it disappears.

⚠️ **Exit status is parent-only, and that is the whole point of dropping `exec`.** With
`exec "<launcher>"` the pane's shell *becomes* Claude, so no process is left to `waitpid`
and `137` is unobservable by construction. The spawn recipe therefore runs the launcher as
a child and traps `EXIT`. A record naming `137` is necessarily written *after* the kill,
by that parent — which is why writer 3 can never claim `signalled kill`, only `unknown-end`.

**Keyed on the pane, not the session.** The pane is the process container that dies, and a
resumed session gets a new pane (it is a new process in a new container), so two panes are
two ends and must not overwrite each other. The session id rides as a *field* because for
a fresh manager spawn it is not known at spawn time — the pane id is the join key, and the
session registry maps pane → session for the rest.

Storage: `~/.claude/state/session-exit/pane-<pane_id>.log`, one tab-separated line per
event. Override the directory with `SUPERVISOR_SESSION_EXIT_DIR`.

Line shape (fixed field order, so a reader never guesses):

    2026-10-27T14:03:11Z\tkind=exited\tpane=2156\tsession=-\trc=0\tsignal=-

`kind` is one of `exited`, `signalled kill`, `unknown-end`, `closed-tab` — the four end
kinds — plus `spawn`, a **start** marker written when the trap is installed. ⚠️ The spawn
marker is not decoration: without it an empty record cannot be told apart from a record
whose writer was never wired, which is exactly how a previous attempt at this looked
successful while proving nothing. A record that begins with `kind=spawn` and has no end
line is a session that was still running (or whose writer died with it); a record that does
not exist at all is a spawn that never installed the writer. ⚠️ `closed-tab` is never
emitted by this terminal: WezTerm's own event surface has no pane/tab-close event (15
events, 13 window + 2 mux, enumerated 2026-10-27), and `SIGHUP` is not specific to a tab
close — a terminal quit, a wezterm crash and an SSH drop deliver the same signal — so a
trap that stamped `closed-tab` on `HUP` would assert a cause it cannot distinguish. The
kind is kept in the vocabulary because a different surface might one day report it.
"""

import argparse
import datetime
import json
import os
import sys

DEFAULT_DIR = "~/.claude/state/session-exit"

#: `128 + n` is how a shell reports a child killed by signal `n`. Named for the four that
#: actually occur on this fleet; anything else prints its number rather than a guess.
SIGNALS = {
    1: "SIGHUP",
    2: "SIGINT",
    3: "SIGQUIT",
    9: "SIGKILL",
    15: "SIGTERM",
}

KIND_EXITED = "exited"
KIND_SIGNALLED = "signalled kill"
KIND_UNKNOWN = "unknown-end"
KIND_CLOSED_TAB = "closed-tab"
#: A START marker, not an end kind. See the module docstring for why it is written at all.
KIND_SPAWN = "spawn"

KINDS = (KIND_EXITED, KIND_SIGNALLED, KIND_UNKNOWN, KIND_CLOSED_TAB, KIND_SPAWN)


def state_dir():
    """The store's directory, honouring the documented override."""
    return os.path.expanduser(os.environ.get("SUPERVISOR_SESSION_EXIT_DIR") or DEFAULT_DIR)


def record_path(pane):
    """`<state dir>/pane-<pane>.log`. The pane id must be a bare token.

    It is interpolated into a filename, so a value carrying `/` or `..` would write outside
    the store — the same guard every sibling writer applies to an id it joins into a path.
    """
    if not pane or "/" in pane or pane in (".", ".."):
        raise ValueError(f"pane id must be a bare token, got {pane!r}")
    return os.path.join(state_dir(), f"pane-{pane}.log")


def classify(rc):
    """`(kind, signal)` for a wait status.

    `rc` past 128 is a signal death and is named as one; every other value is an exit, and
    the status itself is the evidence — `rc=1` is an error exit, not a normal one, and
    collapsing the two would lose the only distinction the field can carry.
    """
    if rc > 128:
        return KIND_SIGNALLED, SIGNALS.get(rc - 128, str(rc - 128))
    return KIND_EXITED, "-"


def format_line(now, kind, pane, session, rc, signal):
    """One record line. Field order is fixed and load-bearing for every reader."""
    return "\t".join(
        (
            now,
            f"kind={kind}",
            f"pane={pane}",
            f"session={session or '-'}",
            f"rc={rc}",
            f"signal={signal}",
        )
    )


def append_line(path, line):
    """Append one line, creating the store first. A single `write` of a whole line.

    Appended with `O_APPEND` rather than rewritten: concurrent writers (the trap and the
    observer can both fire for one pane) must not lose each other's line, and the record is
    a log, not a document.
    """
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    try:
        os.write(fd, (line + "\n").encode("utf-8"))
    finally:
        os.close(fd)


def cmd_append(args):
    """Write one end line. The trap's entry point — never raises past its own exit code.

    Returns 0 on success and 2 on a malformed pane id. The trap wraps the call in `|| true`,
    so a failure here can never change the session's own exit status; it is reported on
    stderr so the failure is visible rather than silently absent.
    """
    pane = args.pane or os.environ.get("WEZTERM_PANE") or ""
    try:
        path = record_path(pane)
    except ValueError as exc:
        print(f"session-exit-record: {exc}", file=sys.stderr)
        return 2
    now = args.now or datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    if args.kind:
        kind, signal = args.kind, (args.signal or "-")
    else:
        kind, signal = classify(args.rc)
    append_line(path, format_line(now, kind, pane, args.session, args.rc, signal))
    return 0


def cmd_path(args):
    """Print the record path for a pane, creating nothing."""
    pane = args.pane or os.environ.get("WEZTERM_PANE") or ""
    try:
        print(record_path(pane))
    except ValueError as exc:
        print(f"session-exit-record: {exc}", file=sys.stderr)
        return 2
    return 0


def cmd_read(args):
    """Print a pane's record lines verbatim. Absent store prints nothing and exits 0.

    Absence is not an error here: it is the state this tool exists to make *visible*, and a
    non-zero exit would be indistinguishable from a broken read.
    """
    pane = args.pane or os.environ.get("WEZTERM_PANE") or ""
    try:
        path = record_path(pane)
    except ValueError as exc:
        print(f"session-exit-record: {exc}", file=sys.stderr)
        return 2
    try:
        with open(path, encoding="utf-8") as handle:
            sys.stdout.write(handle.read())
    except FileNotFoundError:
        return 0
    return 0


def cmd_list(args):
    """`--json` listing of every record in the store, newest line last per pane."""
    directory = state_dir()
    rows = []
    try:
        names = sorted(os.listdir(directory))
    except FileNotFoundError:
        names = []
    for name in names:
        if not (name.startswith("pane-") and name.endswith(".log")):
            continue
        path = os.path.join(directory, name)
        try:
            with open(path, encoding="utf-8") as handle:
                lines = [line.rstrip("\n") for line in handle if line.strip()]
        except OSError:
            continue
        rows.append({"pane": name[len("pane-") : -len(".log")], "path": path, "lines": lines})
    if args.json:
        print(json.dumps({"records": rows}, indent=2))
    else:
        for row in rows:
            print(f"{row['pane']}\t{row['path']}\t{len(row['lines'])} line(s)")
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description="Record how a Claude Code pane ended.")
    sub = parser.add_subparsers(dest="command", required=True)

    p_append = sub.add_parser("append", help="append one end line (the EXIT trap's writer)")
    p_append.add_argument("--pane", default="", help="pane id (default: $WEZTERM_PANE)")
    p_append.add_argument("--session", default="", help="session id, or empty when unknown")
    p_append.add_argument("--rc", type=int, default=0, help="the child's wait status")
    p_append.add_argument("--kind", choices=KINDS, default="", help="override the derived kind")
    p_append.add_argument("--signal", default="", help="signal name, with --kind")
    p_append.add_argument("--now", default="", help="timestamp override, for tests")
    p_append.set_defaults(func=cmd_append)

    p_path = sub.add_parser("path", help="print the record path for a pane")
    p_path.add_argument("--pane", default="")
    p_path.set_defaults(func=cmd_path)

    p_read = sub.add_parser("read", help="print a pane's record lines")
    p_read.add_argument("--pane", default="")
    p_read.set_defaults(func=cmd_read)

    p_list = sub.add_parser("list", help="list every record in the store")
    p_list.add_argument("--json", action="store_true")
    p_list.set_defaults(func=cmd_list)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
