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

**Two writers, layered by what each survives.** A record is only as good as the writer
that survives the death it is describing, and no single writer covers every end:

1. **The pane shell's `EXIT` trap** (`append`) — survives a normal exit, a non-zero exit,
   and a signal delivered to the *child* (the shell's `waitpid` reports `128+n`, which is
   how `137` becomes visible at all). Does NOT survive a signal delivered to the whole
   process group, because the shell dies with the child.
2. **A detached observer** (`observe`) — `os.setsid()` puts it in its own session and
   process group, so `kill -9 -<pane pgid>` cannot reach it. It polls the pane's pid,
   writes a `heartbeat` line every interval, and on the pid's disappearance writes
   `unknown-end`. Its report is an *inference*, so it never claims `signalled kill` — a
   signal is a fact only the parent's `waitpid` can report.

⚠️ **The heartbeat lives in the observer, not in a third pane-side loop.** An earlier
design put a 30-second heartbeat in the pane shell as a separate writer. It was dropped:
a pane-side loop dies in exactly the case the heartbeat exists for (a group kill), so it
would have bought nothing the observer does not already do, at the cost of a second
process and a second timer. The heartbeat's purpose is unchanged — under a group kill the
record's last line is stamped at most one interval before the death, which attributes the
death to a window — but one process now produces both the liveness and the end line.

⚠️ **Exit status is parent-only, and that is the whole point of dropping `exec`.** With
`exec "<launcher>"` the pane's shell *becomes* Claude, so no process is left to `waitpid`
and `137` is unobservable by construction. The spawn recipe therefore runs the launcher as
a child and traps `EXIT`. A record naming `137` is necessarily written *after* the kill,
by that parent — which is why the observer can never claim `signalled kill`, only
`unknown-end`.

**Keyed on the pane, not the session.** The pane is the process container that dies, and a
resumed session gets a new pane (it is a new process in a new container), so two panes are
two ends and must not overwrite each other. The session id rides as a *field* because for
a fresh manager spawn it is not known at spawn time — the pane id is the join key, and the
session registry maps pane → session for the rest.

Storage: `~/.claude/state/session-exit/pane-<pane_id>.log`, one tab-separated line per
event. Override the directory with `SUPERVISOR_SESSION_EXIT_DIR`.

Line shape (fixed field order, so a reader never guesses):

    ts  kind  pane  session  pid  rc  signal  sender  writer

e.g.

    2026-10-27T14:03:11Z	kind=signalled kill	pane=2156	session=-	pid=41234	rc=137	signal=SIGKILL	sender=unavailable(eslogger rc=1 ES_NEW_CLIENT_RESULT_ERR_NOT_PRIVILEGED)	writer=trap

`kind` is one of the four end kinds — `exited`, `signalled kill`, `unknown-end`,
`closed-tab` — plus two markers that are not ends: `spawn`, written when the trap is
installed, and `heartbeat`, written by the observer. ⚠️ The spawn marker is not
decoration: without it an empty record cannot be told apart from a record whose writer was
never wired, which is exactly how a previous attempt at this looked successful while
proving nothing. A record that begins with `kind=spawn` and has no end line is a session
that was still running (or whose writer died with it); a record that does not exist at all
is a spawn that never installed the writer. ⚠️ `closed-tab` is never emitted by this
terminal: WezTerm's own event surface has no pane/tab-close event (15 events, 13 window +
2 mux, enumerated 2026-10-27), and `SIGHUP` is not specific to a tab close — a terminal
quit, a wezterm crash and an SSH drop deliver the same signal — so a trap that stamped
`closed-tab` on `HUP` would assert a cause it cannot distinguish. The kind is kept in the
vocabulary because a different surface might one day report it.

**`sender` is about who ended the process, and it is deliberately three-valued.**
`sender=-` on a marker (nothing ended anything), `sender=none` on a normal exit (the
process ended itself), and `sender=unavailable(<reason>)` where a signal was delivered but
the machine cannot name the sender. The reason is the *probe's own output*, never a claim
from documentation: the only mechanism here that could name a sender is `eslogger`, and it
refuses a non-root caller. See `_sender_unavailable`.

**`writer` records which writer produced the line** — `trap` or `observer`. The two are
not equivalent evidence: the trap reports what a parent reaped, the observer reports what
a bystander noticed, and a reader that cannot tell them apart cannot tell a fact from an
inference.

⚠️ **Two fields from the original design are not here, deliberately.** The design listed
`tty` and a separate `event` field. `tty` existed only to resolve pane → pid through
`ps -t`, and the spawn passes the pid directly, so it would be a derivable duplicate
(`ps -o tty= -p <pid>`). `event` duplicated `kind`, which already distinguishes the two
markers from the four end kinds. Recorded here because the design in the task file still
lists them; the code is the truth.
"""

import argparse
import datetime
import json
import os
import re
import subprocess
import sys
import time

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
#: Written by the observer. Also not an end kind — it says the pane was alive at `ts`.
KIND_HEARTBEAT = "heartbeat"

KINDS = (KIND_EXITED, KIND_SIGNALLED, KIND_UNKNOWN, KIND_CLOSED_TAB, KIND_SPAWN, KIND_HEARTBEAT)

#: The kinds that mean the pane has ended. A record carrying one of these is closed, and
#: the observer stays silent rather than adding an inference beside a parent's report.
END_KINDS = (KIND_EXITED, KIND_SIGNALLED, KIND_UNKNOWN, KIND_CLOSED_TAB)

WRITER_TRAP = "trap"
WRITER_OBSERVER = "observer"

DEFAULT_INTERVAL = 30


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


def now_utc():
    """The record's timestamp: ISO 8601, UTC, second resolution, `Z`-suffixed."""
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def format_line(now, kind, pane, session="", pid="", rc="-", signal="-", sender="-", writer="-"):
    """One record line. Field order is fixed and load-bearing for every reader."""
    return "\t".join(
        (
            now,
            f"kind={kind}",
            f"pane={pane}",
            f"session={session or '-'}",
            f"pid={pid or '-'}",
            f"rc={rc}",
            f"signal={signal}",
            f"sender={sender or '-'}",
            f"writer={writer or '-'}",
        )
    )


def parse_field(line, name):
    """The value of `name=` in one record line, or `""`. Never raises on a malformed line."""
    prefix = name + "="
    for field in line.rstrip("\n").split("\t"):
        if field.startswith(prefix):
            return field[len(prefix) :]
    return ""


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


def _sender_unavailable():
    """Why this machine cannot name the process that delivered a signal, quoted from a probe.

    `eslogger` is the only surface here that could attribute a signal to a sender, and it
    is root-only. The recorded reason is the probe's own refusal, not a claim from its
    documentation — run it once and see. Bounded to the symbolic code it prints so the
    reason stays a token rather than a sentence inside a line-oriented record.
    """
    eslogger = "/usr/bin/eslogger"
    if not os.path.exists(eslogger):
        return "unavailable(eslogger not installed)"
    try:
        proc = subprocess.run(
            [eslogger, "exec"], capture_output=True, text=True, timeout=5, check=False
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return f"unavailable(eslogger failed: {type(exc).__name__})"
    if proc.returncode == 0:
        # The probe was accepted, which only happens as root. Nothing here subscribes to
        # its stream, so the sender is still not carried — say that rather than imply it is.
        return "unavailable(eslogger accepted but no subscription is wired)"
    text = (proc.stderr or proc.stdout or "").strip().splitlines()
    first = text[0] if text else "no output"
    match = re.search(r"\(([A-Z][A-Z0-9_]*)\)\s*$", first)
    if match:
        return f"unavailable(eslogger rc={proc.returncode} {match.group(1)})"
    return f"unavailable(eslogger rc={proc.returncode} {first[:80]})"


def sender_for(kind):
    """The honest `sender` value for a kind. See the module docstring's three-valued note."""
    if kind == KIND_EXITED:
        return "none"
    if kind in (KIND_SIGNALLED, KIND_UNKNOWN, KIND_CLOSED_TAB):
        return _sender_unavailable()
    return "-"


def has_end(path):
    """Whether the record already carries an end line for this pane."""
    try:
        with open(path, encoding="utf-8") as handle:
            for line in handle:
                if parse_field(line, "kind") in END_KINDS:
                    return True
    except FileNotFoundError:
        return False
    return False


def alive(pid):
    """Whether `pid` still exists. `EPERM` means it exists and belongs to someone else."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _daemonize():
    """Detach into a new session, so a process-group kill cannot reach this process.

    Double fork: the intermediate child exits so the grandchild is reparented and can never
    be waited for, and `os.setsid()` gives it its own session and process group — which is
    the whole point, since `kill -9 -<pane pgid>` does not reach a process in another
    group.

    The parent blocks on one byte from a pipe before returning. Without that handshake the
    shell resumes while the observer may still be inside the shell's process group, and a
    group kill landing in that window takes the observer with it — the exact failure this
    process exists to survive.

    Returns `True` in the daemon (carry on) and `False` in the caller (return 0).
    """
    read_fd, write_fd = os.pipe()
    pid = os.fork()
    if pid > 0:
        os.close(write_fd)
        try:
            os.read(read_fd, 1)
        finally:
            os.close(read_fd)
        os.waitpid(pid, 0)
        return False
    os.close(read_fd)
    os.setsid()
    if os.fork() > 0:
        os._exit(0)
    devnull = os.open(os.devnull, os.O_RDWR)
    for fd in (0, 1, 2):
        os.dup2(devnull, fd)
    if devnull > 2:
        os.close(devnull)
    os.write(write_fd, b"1")
    os.close(write_fd)
    return True


def cmd_append(args):
    """Write one record line. The trap's entry point — never raises past its own exit code.

    Returns 0 on success and 2 on a malformed pane id. The trap wraps the call in `|| true`,
    so a failure here can never change the session's own exit status; it is reported on
    stderr so the failure is visible rather than silently absent.

    The line is echoed to stdout as well as appended. That is what makes the writer's own
    output a usable control: a test can capture a pre-kill line from here without reading
    the record path, which is the only way to tell "the writer was never wired" apart from
    "the writer wrote nothing" when the record path is still empty.
    """
    pane = args.pane or os.environ.get("WEZTERM_PANE") or ""
    try:
        path = record_path(pane)
    except ValueError as exc:
        print(f"session-exit-record: {exc}", file=sys.stderr)
        return 2
    now = args.now or now_utc()
    if args.kind:
        kind, signal = args.kind, (args.signal or "-")
    else:
        kind, signal = classify(args.rc)
    line = format_line(
        now,
        kind,
        pane,
        args.session,
        args.pid,
        args.rc,
        signal,
        args.sender or sender_for(kind),
        args.writer or WRITER_TRAP,
    )
    append_line(path, line)
    print(line)
    return 0


def cmd_observe(args):
    """Poll a pane's pid and record its disappearance. The out-of-group writer.

    Writes a `heartbeat` line every `--interval` seconds and one `unknown-end` line when the
    pid is gone. Stays silent when the record already carries an end line, so the parent's
    report always wins over this process's inference — the trap runs before the shell exits,
    so by the time this process sees the pid gone, any line the trap was going to write is
    already on disk.

    The pid is polled *before* the first sleep, so a pane that is already gone is recorded
    without waiting a full interval, and the first heartbeat lands immediately — which is
    also the evidence that this process started at all.

    `--ticks N` stops after N heartbeats and `--foreground` skips the detach. Both exist for
    the tests, which cannot drive an unbounded daemon.
    """
    pane = args.pane or os.environ.get("WEZTERM_PANE") or ""
    try:
        path = record_path(pane)
    except ValueError as exc:
        print(f"session-exit-record: {exc}", file=sys.stderr)
        return 2
    if args.pid <= 0:
        print("session-exit-record: --pid must be positive", file=sys.stderr)
        return 2
    if not args.foreground and not _daemonize():
        return 0
    heartbeats = 0
    while True:
        if not alive(args.pid):
            if not has_end(path):
                append_line(
                    path,
                    format_line(
                        now_utc(),
                        KIND_UNKNOWN,
                        pane,
                        args.session,
                        args.pid,
                        "unavailable",
                        "unavailable",
                        sender_for(KIND_UNKNOWN),
                        WRITER_OBSERVER,
                    ),
                )
            return 0
        append_line(
            path,
            format_line(
                now_utc(),
                KIND_HEARTBEAT,
                pane,
                args.session,
                args.pid,
                "-",
                "-",
                "-",
                WRITER_OBSERVER,
            ),
        )
        heartbeats += 1
        if args.ticks and heartbeats >= args.ticks:
            return 0
        time.sleep(args.interval)


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


def cmd_daily(args):
    """Per-calendar-day counts of every end kind, and the unknown-end share of them.

    A standing metric rather than a one-off query. The rate at which sessions die without a
    recorded cause is the thing this store exists to make visible, and it stayed invisible
    for four days after the first jump because seeing it meant remembering to run a shell
    pipeline. This subcommand is the pipeline; `--line` is the form a monitor or a `grep`
    reads without parsing a table.

    Only end kinds are counted. `spawn` and `heartbeat` are markers, and folding them in
    would make the denominator track the fleet's size instead of its deaths — the fraction
    is meant to answer "of the deaths, how many are unexplained", not "how busy was the
    machine". A pane with a `spawn` and no end line is a session still running, so it is
    absent from every count rather than counted as zero.
    """
    directory = state_dir()
    counts = {}
    try:
        names = sorted(os.listdir(directory))
    except FileNotFoundError:
        names = []
    for name in names:
        if not (name.startswith("pane-") and name.endswith(".log")):
            continue
        path = os.path.join(directory, name)
        try:
            handle = open(path, encoding="utf-8")
        except OSError:
            continue
        with handle:
            for line in handle:
                if not line.strip():
                    continue
                kind = parse_field(line, "kind")
                if kind not in END_KINDS:
                    continue
                day = line.split("\t", 1)[0][:10]
                bucket = counts.setdefault(day, {})
                bucket[kind] = bucket.get(kind, 0) + 1

    rows = []
    for day in sorted(counts):
        row = {"day": day, "total": 0}
        for kind in END_KINDS:
            row[kind] = counts[day].get(kind, 0)
            row["total"] += row[kind]
        row["unknown_end_pct"] = (
            round(100.0 * row[KIND_UNKNOWN] / row["total"], 1) if row["total"] else 0.0
        )
        rows.append(row)
    if args.days > 0:
        rows = rows[-args.days :]

    if args.json:
        print(json.dumps({"days": rows}, indent=2))
        return 0
    if args.line:
        for row in rows:
            print(
                f"{row['day']} ends={row['total']} unknown_end={row[KIND_UNKNOWN]} "
                f"unknown_end_pct={row['unknown_end_pct']}"
            )
        return 0
    if not rows:
        print("no end records in the store")
        return 0
    print(
        f"{'day':<12}{'exited':>8}{'signalled':>11}{'unknown-end':>13}"
        f"{'total':>8}{'unknown%':>10}"
    )
    for row in rows:
        print(
            f"{row['day']:<12}{row[KIND_EXITED]:>8}{row[KIND_SIGNALLED]:>11}"
            f"{row[KIND_UNKNOWN]:>13}{row['total']:>8}{row['unknown_end_pct']:>10}"
        )
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description="Record how a Claude Code pane ended.")
    sub = parser.add_subparsers(dest="command", required=True)

    p_append = sub.add_parser("append", help="append one record line (the EXIT trap's writer)")
    p_append.add_argument("--pane", default="", help="pane id (default: $WEZTERM_PANE)")
    p_append.add_argument("--session", default="", help="session id, or empty when unknown")
    p_append.add_argument("--pid", default="", help="the pid whose end this record describes")
    p_append.add_argument("--rc", type=int, default=0, help="the child's wait status")
    p_append.add_argument("--kind", choices=KINDS, default="", help="override the derived kind")
    p_append.add_argument("--signal", default="", help="signal name, with --kind")
    p_append.add_argument("--sender", default="", help="override the derived sender")
    p_append.add_argument("--writer", default="", help=f"writer name (default: {WRITER_TRAP})")
    p_append.add_argument("--now", default="", help="timestamp override, for tests")
    p_append.set_defaults(func=cmd_append)

    p_observe = sub.add_parser("observe", help="poll a pane's pid and record its disappearance")
    p_observe.add_argument("--pane", default="", help="pane id (default: $WEZTERM_PANE)")
    p_observe.add_argument("--pid", type=int, required=True, help="the pane's pid to watch")
    p_observe.add_argument("--session", default="", help="session id, or empty when unknown")
    p_observe.add_argument(
        "--interval", type=int, default=DEFAULT_INTERVAL, help="seconds between polls"
    )
    p_observe.add_argument(
        "--ticks", type=int, default=0, help="stop after N heartbeats (0 = never); for tests"
    )
    p_observe.add_argument(
        "--foreground", action="store_true", help="do not detach (tests, and debugging)"
    )
    p_observe.set_defaults(func=cmd_observe)

    p_path = sub.add_parser("path", help="print the record path for a pane")
    p_path.add_argument("--pane", default="")
    p_path.set_defaults(func=cmd_path)

    p_read = sub.add_parser("read", help="print a pane's record lines")
    p_read.add_argument("--pane", default="")
    p_read.set_defaults(func=cmd_read)

    p_list = sub.add_parser("list", help="list every record in the store")
    p_list.add_argument("--json", action="store_true")
    p_list.set_defaults(func=cmd_list)

    p_daily = sub.add_parser(
        "daily", help="per-day end-kind counts — the standing unknown-end metric"
    )
    p_daily.add_argument("--days", type=int, default=0, help="only the last N days (0 = all)")
    p_daily.add_argument("--json", action="store_true")
    p_daily.add_argument("--line", action="store_true", help="one key=value line per day")
    p_daily.set_defaults(func=cmd_daily)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
