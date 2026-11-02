#!/usr/bin/env python3
"""One home for the park threshold, and one watch per event key.

    park-event.py threshold [--seconds | --human]
    park-event.py register --task <name> --event <key> --recheck <ISO8601>
                           [--session <id>] [--vault <name>]
    park-event.py list  [--json]
    park-event.py due   [--json]
    park-event.py release --event <key> --session <id>

WHY THIS IS A SCRIPT AND NOT PROSE. Three facts decide whether a worker parks
instead of holding a fleet slot for hours, and each of them was, until now,
either duplicated across files or held only in the round's memory:

  * THE THRESHOLD. It lived in `commands/worker-drive.md` alone, at 2 h, while
    the always-loaded closer rule legalises a resident watcher with no duration
    cap at all — so the rule a worker always reads and the rule it reads only
    under `/supervisor:worker-drive` disagreed, and the permissive one won.
    A threshold that is a constant here is a threshold a doc, a step and a test
    can all read, and a follow-up wording change is one line, not a refactor.

  * THE EVENT KEY. Two sessions waiting on the same metric, the same image
    bump, the same review, each armed their own watcher. Nothing recorded that
    they were the same event, so nothing could serve them once. The key is
    written onto the TASK, not only into this script's state, so a restarted
    round re-derives the watch from disk instead of posting a second one.

  * THE MEMBERSHIP. A watch is not a process here: the fleet round asks this
    store what is due. So "one watch" means one entry carrying every session
    parked on that key — not one process, and never a second entry.

THE THRESHOLD IS A DURATION, NOT A DATE. A wait whose expected end is within
`threshold` seconds stays resident under its watcher; anything longer parks. The
re-check is therefore a TIME, and `--recheck` takes a full ISO-8601 timestamp
with its offset — a date-only value parks until midnight and resumes hours early.

STATE. `$PARK_EVENT_STATE_DIR`, default `~/.claude/state/park-events/`, one JSON
file per event key. `PARK_EVENT_NOW` overrides the clock so tests can drive
`due` without sleeping.

EXIT CODES. 0 ok · 2 usage or validation error · 3 no such watch.
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

# The single home of the park threshold. A worker whose only open items wait on
# a machine event expected to take longer than this parks and frees its slot.
PARK_THRESHOLD_SECONDS = 15 * 60

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_MISSING = 3

DEFAULT_STATE_DIR = Path.home() / ".claude" / "state" / "park-events"


class ParkError(ValueError):
    """A park record that cannot be written or read honestly."""


class WatchMissing(ParkError):
    """No watch file exists for this key. Distinct from an unreadable one:
    absence is the normal first-register case, while an unreadable record holds
    live memberships that must never be silently replaced."""


# --- the threshold -----------------------------------------------------------

def humanize(seconds: int) -> str:
    if seconds % 3600 == 0:
        return f"{seconds // 3600}h"
    if seconds % 60 == 0:
        return f"{seconds // 60}m"
    return f"{seconds}s"


# --- keys and state paths ----------------------------------------------------

def slugify(key: str) -> str:
    """A stable filename for an event key, without losing key distinctness.

    A plain `lower().replace(' ', '-')` collides two different keys onto one
    file ("metric a/b" and "metric a-b"), and a collision here silently merges
    two watches — the exact defect the store exists to prevent. The digest is
    therefore part of the name, not a fallback for long keys only.
    """
    norm = " ".join(key.strip().lower().split())
    if not norm:
        raise ParkError("event key is empty — name the event, not 'waiting'")
    readable = re.sub(r"[^a-z0-9]+", "-", norm).strip("-")[:48] or "event"
    digest = hashlib.sha256(norm.encode("utf-8")).hexdigest()[:8]
    return f"{readable}-{digest}"


def state_dir() -> Path:
    raw = os.environ.get("PARK_EVENT_STATE_DIR")
    return Path(raw) if raw else DEFAULT_STATE_DIR


def watch_path(key: str) -> Path:
    return state_dir() / f"{slugify(key)}.json"


def now() -> datetime:
    raw = os.environ.get("PARK_EVENT_NOW")
    if raw:
        return parse_instant(raw, "PARK_EVENT_NOW")
    return datetime.now(timezone.utc)


# --- instants ----------------------------------------------------------------

def parse_instant(raw: str, what: str) -> datetime:
    """A full ISO-8601 instant with an offset, or a refusal.

    Date-only input is REFUSED rather than defaulted to midnight: the whole
    point of the park re-check is its time of day, and a silent midnight default
    is the truncation this task exists to remove — reintroduced at the write
    side, where it would be invisible.
    """
    text = raw.strip()
    if not text:
        raise ParkError(f"{what} is empty")
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        raise ParkError(
            f"{what} is date-only ({text!r}) — a park re-check needs a time and "
            f"an offset, e.g. {text}T02:00:00+01:00"
        )
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ParkError(f"{what} is not ISO-8601 ({text!r}): {exc}") from exc
    if parsed.tzinfo is None:
        raise ParkError(f"{what} carries no offset ({text!r}) — add one, e.g. +01:00")
    return parsed


def iso(instant: datetime) -> str:
    return instant.isoformat(timespec="seconds")


# --- the watch record --------------------------------------------------------

def read_watch(key: str) -> dict:
    path = watch_path(key)
    if not path.exists():
        raise WatchMissing(f"no watch for event key {key!r}")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ParkError(f"watch {path} is unreadable: {exc}") from exc


@contextlib.contextmanager
def locked(key: str):
    """Serialise read-modify-write on one event key.

    `register` and `release` are read-modify-write cycles over one JSON file. Two
    sessions parking on the same event at the same moment is the *expected* case
    here, not an exotic one, so an unlocked cycle loses a membership and the
    second session is silently dropped from the watch it just joined.
    """
    directory = state_dir()
    directory.mkdir(parents=True, exist_ok=True)
    lock_path = directory / f"{slugify(key)}.lock"
    with open(lock_path, "w", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def write_watch(key: str, record: dict) -> Path:
    path = watch_path(key)
    path.parent.mkdir(parents=True, exist_ok=True)
    # A unique temp name per writer: a fixed `.json.tmp` lets two concurrent
    # writers interleave into one file and rename a corrupt record into place.
    handle, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=f"{slugify(key)}.", suffix=".tmp")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            stream.write(json.dumps(record, indent=2, sort_keys=True) + "\n")
        os.replace(tmp_name, path)
    except BaseException:
        Path(tmp_name).unlink(missing_ok=True)
        raise
    return path


def all_watches() -> tuple[list[dict], list[str]]:
    """Every readable watch, plus the names of the ones that would not parse.

    A corrupt watch is RETURNED AS A DEFECT, never skipped. `read_watch` refuses
    one, so swallowing it here would make `due` report "nothing due" while a
    record holding live memberships sat unreadable — an absence manufactured by
    a read failure, which is the one answer this store must never give.
    """
    directory = state_dir()
    if not directory.exists():
        return [], []
    records, corrupt = [], []
    for path in sorted(directory.glob("*.json")):
        try:
            records.append(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError):
            corrupt.append(path.name)
    return records, corrupt


# --- task write --------------------------------------------------------------

def write_task_key(task: str, key: str, recheck: str, vault: str | None) -> None:
    """Record the event key and the re-check instant on the task.

    `defer_date` carries the full instant, not a bare date: the readers honour
    the time of day (`scripts/defer-date.py`), so this is the field that
    actually resurfaces the row at the right moment. The event key rides
    alongside so a restarted round re-derives the watch from disk instead of
    arming a second one.

    Written through `vault-cli task set`, never by editing the file: the vault
    owns its own frontmatter and an unattended writer that bypasses the CLI is
    how a page ends up with a key no reader expects.
    """
    for field, value in (("park_event", key), ("defer_date", recheck)):
        cmd = ["vault-cli", "task", "set", task, field, value]
        if vault:
            cmd += ["--vault", vault]
        done = subprocess.run(cmd, capture_output=True, text=True, check=False)
        if done.returncode != 0:
            raise ParkError(
                f"could not write {field} on {task!r}: "
                f"{(done.stderr or done.stdout).strip() or 'vault-cli failed'}"
            )


# --- subcommands -------------------------------------------------------------

def cmd_threshold(args: argparse.Namespace) -> int:
    if args.seconds:
        print(PARK_THRESHOLD_SECONDS)
    else:
        print(humanize(PARK_THRESHOLD_SECONDS))
    return EXIT_OK


def cmd_register(args: argparse.Namespace) -> int:
    key = " ".join(args.event.strip().split())
    recheck = parse_instant(args.recheck, "--recheck")
    session = args.session or os.environ.get("CLAUDE_CODE_SESSION_ID") or ""

    if not session:
        raise ParkError(
            "no session id — pass --session or set $CLAUDE_CODE_SESSION_ID; a watch "
            "with no member can never be released and names no resume owner"
        )

    with locked(key):
        record = _register_locked(args, key, recheck, session)

    members = record.get("sessions") or []
    print(f"{'created' if record['_created'] else 'joined'} {record['slug']}")
    print(f"watch: {watch_path(key)}")
    print(f"recheck: {record['recheck_at']}")
    print(f"sessions: {len(members)} ({', '.join(m[:8] for m in members) or 'none'})")
    return EXIT_OK


def _register_locked(args: argparse.Namespace, key: str, recheck, session: str) -> dict:
    try:
        record = read_watch(key)
    except WatchMissing:
        record = None
    # An UNREADABLE record is not an absent one: it may hold live memberships,
    # and replacing it would silently unpark every session it carried.

    created = record is None
    if record is None:
        record = {
            "event": key,
            "slug": slugify(key),
            "recheck_at": iso(recheck),
            "created_at": iso(now()),
            "sessions": [],
            # session id -> task name. `due` must name the TASK, because
            # `/supervisor:open --task "<name>"` is what resumes it; a bare
            # session id leaves the round with nothing to open.
            "tasks": {},
            # Persisted, because the parked session has exited by the time the
            # round re-checks: nothing else survives to count re-parks.
            "repark_count": 0,
        }
    else:
        # A later re-check never shortens a watch another session is already
        # parked on: the earliest wake wins, so joining cannot delay a peer.
        # A record missing its instant (hand-edited, or written by an older
        # shape) is treated as having none rather than raising a KeyError: the
        # join is still correct, and crashing here would strand the caller at
        # the moment it is trying to park.
        try:
            held = parse_instant(record["recheck_at"], "recheck_at")
        except (KeyError, ParkError):
            held = None
        # A RE-PARK sets the instant unconditionally. The earliest-wake rule
        # below exists so a JOINING peer cannot delay one already parked — but a
        # re-park is the opposite act: the event has not fired, the old instant
        # is in the past, and keeping it would leave the watch due so `due`
        # re-fired it every round and the three-re-park escalation tripped in
        # consecutive ticks instead of after the intended waits.
        if args.repark or held is None or recheck < held:
            record["recheck_at"] = iso(recheck)

    members = record.setdefault("sessions", [])
    if session not in members:
        members.append(session)
    if args.task:
        record.setdefault("tasks", {})[session] = args.task
    if args.repark:
        record["repark_count"] = int(record.get("repark_count", 0)) + 1

    write_watch(key, record)

    if args.task:
        write_task_key(args.task, key, record["recheck_at"], args.vault)

    record["_created"] = created
    return record


def _report_corrupt(corrupt: list[str]) -> None:
    for name in corrupt:
        print(f"CORRUPT {name} — unreadable watch; memberships inside are invisible until it is repaired")


def cmd_list(args: argparse.Namespace) -> int:
    watches, corrupt = all_watches()
    _report_corrupt(corrupt)
    if args.json:
        print(json.dumps(watches, indent=2, sort_keys=True))
        return EXIT_OK
    if not watches:
        print("no park watches")
        return EXIT_OK
    for record in watches:
        members = record.get("sessions") or []
        print(
            f"{record.get('slug', '?')} · event: {record.get('event', '?')} · "
            f"recheck: {record.get('recheck_at', '?')} · "
            f"sessions: {len(members)} ({', '.join(s[:8] for s in members) or 'none'})"
        )
    return EXIT_OK


def cmd_due(args: argparse.Namespace) -> int:
    clock = now()
    watches, corrupt = all_watches()
    _report_corrupt(corrupt)
    due = []
    for record in watches:
        try:
            when = parse_instant(record["recheck_at"], "recheck_at")
        except (KeyError, ParkError):
            continue
        if when <= clock:
            due.append(record)
    if args.json:
        print(json.dumps(due, indent=2, sort_keys=True))
        return EXIT_OK
    if not due:
        print(f"no park watches due at {iso(clock)}")
        return EXIT_OK
    for record in due:
        members = record.get("sessions") or []
        tasks = record.get("tasks") or {}
        named = [f"{tasks[m]} [{m}]" if tasks.get(m) else m for m in members]
        print(
            f"{record.get('slug', '?')} · event: {record.get('event', '?')} · "
            f"due since {record.get('recheck_at', '?')} · "
            f"re-parks: {int(record.get('repark_count', 0))} · "
            f"resume: {', '.join(named) or 'none recorded'}"
        )
    return EXIT_OK


def cmd_release(args: argparse.Namespace) -> int:
    key = " ".join(args.event.strip().split())
    session = args.session.strip()
    with locked(key):
        record = read_watch(key)
        members = record.get("sessions") or []
        # A session that was never a member is reported, never "released": the
        # old branch printed a success line and rewrote the record unchanged,
        # so a typo in the session id read as a completed release.
        if session not in members:
            raise ParkError(
                f"{session[:8]} is not a member of watch {record['slug']!r} "
                f"(members: {', '.join(m[:8] for m in members) or 'none'})"
            )
        members.remove(session)
        record["sessions"] = members
        (record.get("tasks") or {}).pop(session, None)
        if not members:
            watch_path(key).unlink(missing_ok=True)
            print(f"released {session[:8]} — watch {record['slug']} removed (no sessions left)")
            return EXIT_OK
        write_watch(key, record)
    print(f"released {session[:8]} — watch {record['slug']} keeps {len(members)} session(s)")
    return EXIT_OK


# --- wiring ------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("threshold", help="print the park threshold")
    p.add_argument("--seconds", action="store_true", help="raw seconds instead of 15m")
    p.set_defaults(func=cmd_threshold)

    p = sub.add_parser("register", help="record an event key and join its watch")
    p.add_argument("--task", help="task name; its park_event frontmatter is written")
    p.add_argument("--event", required=True, help="what is being waited on, in words")
    p.add_argument("--recheck", required=True, help="ISO-8601 instant with offset")
    p.add_argument("--session", help="session id (default: $CLAUDE_CODE_SESSION_ID)")
    p.add_argument("--vault", help="vault name for the task write")
    p.add_argument("--repark", action="store_true",
                   help="count this as a re-park of the same event (escalate at 3)")
    p.set_defaults(func=cmd_register)

    p = sub.add_parser("list", help="one line per watch")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("due", help="watches whose re-check time has passed")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_due)

    p = sub.add_parser("release", help="drop one session from a watch")
    p.add_argument("--event", required=True)
    p.add_argument("--session", required=True)
    p.set_defaults(func=cmd_release)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except WatchMissing as exc:
        print(f"REFUSED\t{exc}", file=sys.stderr)
        return EXIT_MISSING
    except ParkError as exc:
        print(f"REFUSED\t{exc}", file=sys.stderr)
        return EXIT_USAGE


if __name__ == "__main__":
    sys.exit(main())
