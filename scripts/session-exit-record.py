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
   process group, so `kill -9 -<pane pgid>` cannot reach it. It arms a `kqueue`
   `EVFILT_PROC` watch with `NOTE_EXITSTATUS` on the pane's pid, writes a `heartbeat` line
   every interval, and when the pid exits writes the kernel's own wait status — `exited` or
   `signalled kill` with the named signal. That status is the same fact the parent's
   `waitpid` reads, delivered by the kernel to a same-user non-parent with no privilege, so
   it covers the group kill the trap cannot. Only where no watch can be armed (no `kqueue`,
   or the pid already gone at start) does it fall back to polling and the `unknown-end`
   inference.

⚠️ **The heartbeat lives in the observer, not in a third pane-side loop.** An earlier
design put a 30-second heartbeat in the pane shell as a separate writer. It was dropped:
a pane-side loop dies in exactly the case the heartbeat exists for (a group kill), so it
would have bought nothing the observer does not already do, at the cost of a second
process and a second timer. The heartbeat's purpose is unchanged — under a group kill the
record's last line is stamped at most one interval before the death, which attributes the
death to a window — but one process now produces both the liveness and the end line.

⚠️ **Why `exec` is dropped.** With `exec "<launcher>"` the pane's shell *becomes* Claude,
so no parent is left to `waitpid` and the trap cannot run. The spawn recipe therefore runs
the launcher as a child and traps `EXIT`. The observer watches the shell's pid (`$$`), and
reads its wait status through `kqueue` rather than `waitpid` — which is why it may now
claim `signalled kill`, but only from a status the kernel delivered, never from the pid's
mere absence (2026-11-06: pane 243's group kill left `signal=unavailable` under polling).

**Keyed on the pane, not the session.** The pane is the process container that dies, and a
resumed session gets a new pane (it is a new process in a new container), so two panes are
two ends and must not overwrite each other. The session id rides as a *field* because for
a fresh manager spawn it is not known at spawn time — the spawn line says `session=-`. The
observer fills it in: it reads the id from the pane's `claude` registry record on every
heartbeat (`session_of`) and carries it onto any end line it writes. The trap's end line is
written after `claude` exited, so the trap passes `--session ""`; `append` then reads the id
back from this pid's heartbeats (`last_session`). Both end writers therefore name the session,
provided at least one heartbeat resolved it. That is load-bearing, because the registry
(`~/.claude/sessions/<pid>.json`) is deleted when the process dies, so a join deferred to read
time has nothing left to read.

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
import datetime
import json
import os
import re
import select
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


def has_end(path, pid):
    """Whether the record already carries an end line for THIS run of the pane — `pid`'s.

    Scoped to the pid, never to the file: a pane id is reused across sessions, so one log
    holds many runs, and an end line from an earlier run must not silence this run's
    observer. Measured 2026-11-05 on pane 115: the file carried a 2026-10-28 `kind=exited`
    for pid 56446, so when pid 72157's pane was killed the observer saw "already ended",
    wrote nothing and exited, and the kill left no record at all.

    A `spawn` line for `pid` resets the scan, so a reused pid in the same file cannot borrow
    an end from its earlier life. Every trap writer passes `--pid $$`, the same pid the
    observer watches, so the parent's own report still wins.
    """
    pid = str(pid)
    found = False
    try:
        # `errors="replace"`: one bad byte in a long-lived log must not raise here, which
        # would kill the detached observer and lose this run's end line the same way.
        with open(path, encoding="utf-8", errors="replace") as handle:
            for line in handle:
                if parse_field(line, "pid") != pid:
                    continue
                kind = parse_field(line, "kind")
                if kind == KIND_SPAWN:
                    found = False
                elif kind in END_KINDS:
                    found = True
    except FileNotFoundError:
        return False
    return found


def alive(pid):
    """Whether `pid` still exists. `EPERM` means it exists and belongs to someone else."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


#: `NOTE_EXITSTATUS` from `<sys/event.h>`. Python's `select` module does not export it.
#: With it, a `NOTE_EXIT` event's `data` carries the target's wait status.
_NOTE_EXITSTATUS = 0x04000000


def _exit_watch(pid):
    """A kqueue armed on `pid`'s exit, or `None` where the platform or the pid refuses one.

    `EVFILT_PROC` with `NOTE_EXITSTATUS` hands the wait status to *any* process run by the
    same user, not only the parent, and needs no privilege. This is how the observer, a
    non-parent in its own session, can name the signal from a process-group kill that took
    the trap's shell down with the child. Measured 2026-11-06: a `killpg(SIGTERM)` on a target
    reparented to launchd reported `signaled True sig 15` to a non-parent watcher.
    """
    kqueue = getattr(select, "kqueue", None)
    if kqueue is None:
        return None
    try:
        kq = kqueue()
        kq.control(
            [
                select.kevent(
                    pid,
                    filter=select.KQ_FILTER_PROC,
                    flags=select.KQ_EV_ADD | select.KQ_EV_ONESHOT,
                    fflags=select.KQ_NOTE_EXIT | _NOTE_EXITSTATUS,
                )
            ],
            0,
        )
    except OSError:
        return None
    return kq


def _wait_exit(kq, timeout):
    """The pid's wait status if it exited within `timeout` seconds, else `None`."""
    events = kq.control(None, 1, timeout)
    if not events:
        return None
    if events[0].flags & select.KQ_EV_ERROR:
        raise OSError(events[0].data, "kqueue exit watch failed")
    return events[0].data


def _rc_of(status):
    """A wait status in the shell's `$?` form: `128+n` for a signal, else the exit code."""
    if os.WIFSIGNALED(status):
        return 128 + os.WTERMSIG(status)
    return os.WEXITSTATUS(status)


#: Where Claude Code writes `<pid>.json` for each live session. The same override the rest of
#: the plugin's registry readers honour, so one fixture tree drives them all.
SESSIONS_DIR_ENV = "SUPERVISOR_SESSIONS_DIR"

#: How far below the watched pid to look. The Fleet Manager's `claude` is a direct child of
#: its pane shell; a supervisor-spawned pane's is a grandchild (shell → bash → claude, pane
#: 344 on 2026-11-06). Four levels covers both with room for a launcher wrapper.
_DESCENT_DEPTH = 4

#: A session id is written into a tab-separated record, so only an id that cannot forge a
#: field is accepted — every real one is a UUID. Checked with `fullmatch`: `$` also matches
#: before a trailing newline, and an id ending in one would split a record line in two.
_SESSION_ID = re.compile(r"[0-9A-Za-z-]{1,64}")

#: The registry `entrypoint` of a session that owns a pane. Headless workers a manager's
#: supervisor server starts below its own `claude` write `sdk-cli`, and `kind` cannot tell
#: them apart — measured 2026-11-06, every one of 29 live records said `kind: interactive`.
_PANE_ENTRYPOINT = "cli"

#: The process name that ends the descent. Everything below the pane's own `claude` belongs
#: to that session — its tools, its MCP servers, the workers its supervisor server spawns.
_CLAUDE_COMM = "claude"


def _process_table():
    """`{pid: (ppid, command basename)}` from one `ps` snapshot.

    One snapshot per heartbeat rather than one `pgrep` per level: the walk then sees a single
    consistent tree, and a process that exits mid-walk cannot make two levels disagree.

    ⚠️ **`None` when the query failed, `{}` only when `ps` answered with no rows** — the
    three-state contract `scripts/transport-read-check.py` enforces on every transport read.
    A failed `ps` collapsed into `{}` would read as "this pane has no processes", which is the
    broken-as-empty defect that check exists to catch.
    """
    try:
        proc = subprocess.run(
            ["ps", "-Ao", "pid=,ppid=,comm="],
            capture_output=True, text=True, timeout=5, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    out = proc.stdout or ""
    table = {}
    for line in out.splitlines():
        parts = line.split(None, 2)
        if len(parts) == 3 and parts[0].isascii() and parts[0].isdigit() \
                and parts[1].isascii() and parts[1].isdigit():
            table[int(parts[0])] = (int(parts[1]), os.path.basename(parts[2].strip()))
    return table


def _registry_session(directory, pid):
    """The `sessionId` of a pane-owning record `<directory>/<pid>.json`, or `""`.

    `""` for an absent, unreadable or half-written file, a non-pane `entrypoint`, or an id
    that fails `_SESSION_ID`.
    """
    try:
        with open(os.path.join(directory, f"{pid}.json"), encoding="utf-8") as handle:
            rec = json.load(handle)
    except (OSError, ValueError, RecursionError):
        return ""
    if not isinstance(rec, dict) or rec.get("entrypoint") != _PANE_ENTRYPOINT:
        return ""
    sid = rec.get("sessionId")
    return sid if isinstance(sid, str) and _SESSION_ID.fullmatch(sid) else ""


def session_of(pid):
    """The session id of the pane's own `claude` at or below `pid`, or `""`.

    A fresh spawn does not know its own session id, so the spawn line carries `session=-`;
    Claude Code writes it to `~/.claude/sessions/<claude pid>.json` moments later and deletes
    that file when the process ends. The observer is the one writer alive for the pane's
    whole life, so it reads the id while it exists and stamps it into every line it writes —
    after which the append-only record keeps the identity the registry discards. Measured
    2026-11-06: the Fleet Manager died three times that day and every record says
    `session=-`, so nothing on disk could name it afterwards.

    Breadth-first, bounded by `_DESCENT_DEPTH`, and **the descent stops at the first
    `claude`**. Without that stop, a pane `claude` whose record is momentarily unreadable
    (missing, or caught mid-rewrite) would let the walk continue into the headless workers
    its supervisor server spawns, and stamp a worker's id onto the manager's pane. A
    `claude` with no usable record therefore answers `""`, and the caller keeps the id it
    already holds. Records are also filtered on `entrypoint == "cli"`, so a worker can never
    be chosen even if it were reached.

    Re-read on every call, so a `/clear` — a new session id inside the same process — is
    followed rather than frozen at the first value.
    """
    directory = os.path.expanduser(os.environ.get(SESSIONS_DIR_ENV) or "~/.claude/sessions")
    found = _registry_session(directory, pid)
    if found:
        return found
    table = _process_table()
    if table is None:
        return ""  # the tree could not be read; the caller keeps the id it already holds
    children = {}
    for child, (parent, _) in table.items():
        children.setdefault(parent, []).append(child)
    frontier, seen = [pid], {pid}
    for _ in range(_DESCENT_DEPTH):
        nxt = []
        for parent in frontier:
            for child in sorted(children.get(parent, ())):
                if child in seen:
                    continue
                seen.add(child)
                found = _registry_session(directory, child)
                if found:
                    return found
                if table[child][1] == _CLAUDE_COMM:
                    return ""  # the pane's claude, unreadable right now: never look below it
                nxt.append(child)
        if not nxt:
            return ""
        frontier = nxt
    return ""


def last_session(path, pid):
    """The newest session id the record holds for `pid` in its current run, or `""`.

    The trap writes the end line after `claude` has exited, when its registry record is
    already gone — so `append` reads the id back from the observer's heartbeats instead.
    Only lines after this pid's own `spawn` count, so a reused pane id never lends an
    earlier run's session.
    """
    found = ""
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            for line in handle:
                if parse_field(line, "pid") != str(pid):
                    continue
                if parse_field(line, "kind") == KIND_SPAWN:
                    found = ""
                    continue
                sid = parse_field(line, "session")
                if sid and sid != "-" and _SESSION_ID.fullmatch(sid):
                    found = sid
    except OSError:
        return ""
    return found


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
    session = args.session
    if not session and args.pid and kind != KIND_SPAWN:
        # The trap's end line is written after `claude` exited and its registry record was
        # deleted, so the trap passes `--session ""`. The observer's heartbeats for this pid
        # still hold the id; without this, every trap-written end reads `session=-`.
        try:
            session = last_session(path, args.pid)
        except Exception:  # noqa: BLE001 — an extra, never worth losing the end line over
            session = ""
    line = format_line(
        now,
        kind,
        pane,
        session,
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
    """Watch a pane's pid and record its end. The out-of-group writer.

    Arms a `kqueue` exit watch first (see `_exit_watch`): the end line then carries the
    kernel's wait status — `rc`, `signal` and kind — exactly as the trap would. Without a
    watch it falls back to polling, and the disappearance is an `unknown-end` inference.

    Writes a `heartbeat` line every `--interval` seconds and one end line when the pid is gone
    — the kernel's status where a watch is armed, `unknown-end` otherwise. Every line carries
    the session id `session_of` last resolved, falling back to `--session`, so the end line
    names the session even though its registry record is already deleted by then. Stays
    silent when the record already carries an end line, so the parent's
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
    kq = _exit_watch(args.pid)
    heartbeats = 0
    status = None
    # The last id seen, carried past the registry's deletion: the end line is written after
    # the process is gone, when `session_of` can no longer answer.
    session = args.session
    while True:
        if status is not None:
            if not has_end(path, args.pid):
                rc = _rc_of(status)
                kind, signal = classify(rc)
                append_line(
                    path,
                    format_line(
                        now_utc(),
                        kind,
                        pane,
                        session,
                        args.pid,
                        rc,
                        signal,
                        sender_for(kind),
                        WRITER_OBSERVER,
                    ),
                )
            return 0
        if kq is None and not alive(args.pid):
            if not has_end(path, args.pid):
                append_line(
                    path,
                    format_line(
                        now_utc(),
                        KIND_UNKNOWN,
                        pane,
                        session,
                        args.pid,
                        "unavailable",
                        "unavailable",
                        sender_for(KIND_UNKNOWN),
                        WRITER_OBSERVER,
                    ),
                )
            return 0
        try:
            session = session_of(args.pid) or session
        except Exception:  # noqa: BLE001 — this process is the only writer left after a
            pass  # group kill; an unexpected lookup error must not cost the end line
        append_line(
            path,
            format_line(
                now_utc(),
                KIND_HEARTBEAT,
                pane,
                session,
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
        if kq is None:
            time.sleep(args.interval)
        else:
            try:
                status = _wait_exit(kq, args.interval)
            except OSError:
                # An error event is no wait status: fall back to polling's inference
                # rather than record an `rc` the kernel never reported.
                kq = None


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


def record_day(line):
    """The record's calendar day (UTC), or `None` when the line carries no leading timestamp.

    The timestamp is positional — the store's line format is fixed-field-order and `ts` is
    not a `name=value` field, so `parse_field` cannot reach it. A torn or hand-edited line
    whose first field is not a date is skipped rather than bucketed under a junk key such as
    `kind=unkno`, which would surface as a phantom row in the metric.
    """
    stamp = line.split("\t", 1)[0][:10]
    try:
        datetime.date.fromisoformat(stamp)
    except ValueError:
        return None
    return stamp


def _day_rows(counts, days):
    """One row per day: the days with records, or a zero-filled window of `days` days.

    `days > 0` anchors on today (UTC) and walks back `days - 1` days, so the window is
    contiguous and a quiet day prints as `ends=0`. `days == 0` returns only the days the
    store actually holds, which is the right shape for a one-off look at the whole history.
    """
    def build(day):
        bucket = counts.get(day, {})
        row = {"day": day, "total": 0}
        for kind in END_KINDS:
            row[kind] = bucket.get(kind, 0)
            row["total"] += row[kind]
        row["unknown_end_pct"] = (
            round(100.0 * row[KIND_UNKNOWN] / row["total"], 1) if row["total"] else 0.0
        )
        return row

    if days > 0:
        end = datetime.datetime.now(datetime.timezone.utc).date()
        if counts:
            latest = max(datetime.date.fromisoformat(day) for day in counts)
            end = max(end, latest)
        start = end - datetime.timedelta(days=days - 1)
        out, day = [], start
        while day <= end:
            out.append(build(day.isoformat()))
            day += datetime.timedelta(days=1)
        return out
    return [build(day) for day in sorted(counts)]


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

    ⚠️ **`--days N` zero-fills the window**, and that is the difference between a metric and
    a report. Emitting only the days that carry records makes a day with no deaths and a day
    the writer did not run look identical — both simply absent — which is precisely the
    confusion this store exists to remove. A window therefore always prints N consecutive
    days, a quiet one as `ends=0`.

    ⚠️ **Days are UTC**, because every record's `ts` is UTC (`now_utc`) and the day is the
    first ten characters of it. A caller firing at local midnight sees a *provisional*
    latest row until the UTC day rolls over; the plist states its own offset.
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
            # `errors="replace"` rather than a strict decode: one non-UTF-8 byte in any of
            # the several hundred pane logs would otherwise abort the whole unattended run
            # and lose that day's reading, since `UnicodeDecodeError` is raised during
            # iteration and is not an `OSError`.
            handle = open(path, encoding="utf-8", errors="replace")
        except OSError:
            continue
        with handle:
            for line in handle:
                if not line.strip():
                    continue
                kind = parse_field(line, "kind")
                if kind not in END_KINDS:
                    continue
                day = record_day(line)
                if day is None:
                    continue
                bucket = counts.setdefault(day, {})
                bucket[kind] = bucket.get(kind, 0) + 1

    rows = _day_rows(counts, args.days)

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
    # Every end kind gets a column, including `closed-tab`, which no writer emits today.
    # Dropping it would leave the columns not summing to `total` the day one appears —
    # a table that quietly under-reports is worse than one carrying a zero column.
    print(
        f"{'day':<12}{'exited':>8}{'signalled':>11}{'closed-tab':>12}"
        f"{'unknown-end':>13}{'total':>8}{'unknown%':>10}"
    )
    for row in rows:
        print(
            f"{row['day']:<12}{row[KIND_EXITED]:>8}{row[KIND_SIGNALLED]:>11}"
            f"{row[KIND_CLOSED_TAB]:>12}{row[KIND_UNKNOWN]:>13}"
            f"{row['total']:>8}{row['unknown_end_pct']:>10}"
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
