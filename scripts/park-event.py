#!/usr/bin/env python3
"""One home for the park threshold, and one watch per event key.

    park-event.py threshold [--seconds | --human]
    park-event.py register --task <name> --event <key> --recheck <ISO8601>
                           [--session <id>] [--vault <name>] [--action <words>]
    park-event.py list  [--json]
    park-event.py due   [--json]
    park-event.py release --event <key> --session <id>
    park-event.py backfill --vault <name> [--tasks-dir <dir>] [--apply] [--json]

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

  * THE RECORD. A park is only closable when the task page carries a
    `later (on …)` line; the fleet round reads a deferred row without one as
    `unverifiable` and never frees its slot. That line used to be the parking
    session's own job, so every park made by a script or by a session that
    skipped the step held its slot for ever. `register` writes it now, and
    `backfill` repairs the rows parked before it did.

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
        # Name the keys that DO exist. `due` and `list` print the slug, so the
        # commonest miss is a caller holding the wrong identifier — and a bare
        # "no watch" reads as "nothing is parked" rather than "you passed the
        # wrong key", which is the one answer that sends them away.
        known = sorted({r.get("event") or r.get("slug") or "?" for r in all_watches()[0]})
        raise WatchMissing(
            f"no watch for event key {key!r} (known: {', '.join(known) or 'none'})"
        )
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


def resolve_event_key(key: str) -> str:
    """The stored event description for `key`, given as one or as its slug.

    `due` and `list` print the slug first, and a caller copies that token
    straight back into `--event`. The slug cannot be inverted — `slugify`
    appends a digest of its whole input — so the way back to the record is a
    lookup across the store, never a path computation.

    Resolve BEFORE any lock is taken: `lock_path` is `slugify(key).lock`, so a
    slug and its description would otherwise serialise on two different lock
    files and the read-modify-write cycles could interleave.

    An unknown key is returned unchanged, because `register` legitimately
    creates one; `release` refuses it at `read_watch`.
    """
    normalised = " ".join(key.strip().split())
    if watch_path(normalised).exists():
        return normalised
    records, _corrupt = all_watches()
    for record in records:
        if record.get("slug") == normalised:
            return record.get("event") or normalised
    return normalised


# --- task write --------------------------------------------------------------

def _vault_cli(args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(["vault-cli", *args], capture_output=True, text=True, check=False)


def _set_field(task: str, field: str, value: str, vault: str | None) -> None:
    cmd = ["task", "set", task, field, value]
    if vault:
        cmd += ["--vault", vault]
    done = _vault_cli(cmd)
    if done.returncode != 0:
        raise ParkError(
            f"could not write {field} on {task!r}: "
            f"{(done.stderr or done.stdout).strip() or 'vault-cli failed'}"
        )


def task_file(task: str, vault: str | None) -> Path:
    """The task's own page, as the CLI that owns the vault resolves it.

    `vault-cli task show --output json` reports `file_path` for exactly the
    vault the frontmatter write targets, so the record lands in the same page
    as the `defer_date`. Resolving it independently — walking a configured
    `tasks_dir` by hand — is how a record ends up in one vault while the defer
    it describes lands in another, which reads as a task with no record at all.
    """
    cmd = ["task", "show", task, "--output", "json"]
    if vault:
        cmd += ["--vault", vault]
    done = _vault_cli(cmd)
    if done.returncode != 0:
        raise ParkError(
            f"could not resolve the file for task {task!r}: "
            f"{(done.stderr or done.stdout).strip() or 'vault-cli failed'}"
        )
    try:
        payload = json.loads(done.stdout)
    except json.JSONDecodeError as exc:
        raise ParkError(f"vault-cli returned no readable task for {task!r}: {exc}") from exc
    path = str(payload.get("file_path") or "").strip()
    if not path:
        raise ParkError(
            f"vault-cli resolved no file for task {task!r} — the park record has "
            "nowhere to go, and a park with no record reads unverifiable for ever"
        )
    return Path(path)


# --- the park record ---------------------------------------------------------
#
# THE RECORD THE ROUND READS. `agents/fleet-drive.md` classifies a clock-bound
# park on three facts: `status: in_progress`, a `defer_date` after today, AND a
# `later (on ` line in the task file. The first two are frontmatter; the third
# is body prose, which `vault-cli task set` cannot write. Until this existed the
# line was the parking session's own job (`commands/worker-drive.md` step 2), so
# a park made by a script, a resumed session, or a worker that skipped the step
# left a correct defer and no record — measured 2026-11-13 at **29 of 37**
# future-dated `in_progress` rows. A row read `unverifiable` is never closed, so
# a live parked session's process holds its slot until it dies on its own.

PROGRESS_HEADING = "# Progress"
PROGRESS_HEADING_RE = re.compile(r"^(#{1,6})[ \t]+Progress[ \t]*$")


def _heading_level(row: str) -> int:
    """The ATX heading level of `row`, or 0 when it is not a heading."""
    match = re.match(r"^(#{1,6})[ \t]+\S", row)
    return len(match.group(1)) if match else 0


def record_line(key: str, action: str) -> str:
    return f"- later (on {key}): {action}"


def default_action(recheck: str) -> str:
    return f"the defer_date re-check at {recheck} — the fleet round re-opens this row"


def is_future(recheck: str) -> bool:
    """Whether this re-check actually defers the row.

    A past instant is not a park — the row is due, and a `later (on …)` line
    naming it would tell the round the wait is still running when it is not.
    """
    try:
        return parse_instant(recheck, "recheck") > now()
    except ParkError:
        return False


def _replace_or_insert(text: str, key: str, line: str) -> str:
    """Put `line` where the round's reader finds it, without stacking copies.

    One park per task is the normal case, so an existing line for this key is
    REPLACED: a re-park moves the instant, and stacking a second line leaves the
    page naming a re-check that has already passed.

    The section heading is matched at ANY level, because the vault is not
    uniform: `# Progress` is the template's, and a large minority of pages carry
    `## Progress` instead. Appending a new `# Progress` to one of those would
    leave the page with two Progress sections — the record still found by the
    reader's grep, and the page wrong to a human.
    """
    pattern = re.compile(r"^[ \t]*(?:[-*][ \t]+)?later \(on " + re.escape(key) + r"\):.*$", re.M)
    if pattern.search(text):
        return pattern.sub(lambda _match: line, text, count=1)

    lines = text.splitlines()
    start = level = None
    for index, row in enumerate(lines):
        match = PROGRESS_HEADING_RE.match(row.strip())
        if match:
            start, level = index, len(match.group(1))
            break
    if start is None:
        # No section yet: create one, rather than appending a bare line to
        # whatever the page happens to end with.
        return f"{text.rstrip()}\n\n{PROGRESS_HEADING}\n\n{line}\n"
    end = next(
        (i for i in range(start + 1, len(lines)) if 1 <= _heading_level(lines[i]) <= level),
        len(lines),
    )
    at = end
    # Stay inside the section: a blank separator before the next heading is the
    # page's own layout, not a slot for a new entry.
    while at > start + 1 and not lines[at - 1].strip():
        at -= 1
    lines.insert(at, line)
    return "\n".join(lines) + "\n"


def atomic_write(path: Path, text: str) -> None:
    """Replace `path` with `text`, never leaving a half-written page behind.

    The task page is rewritten while the vault autocommits, so a truncating
    write is a worse failure than the missing record it was fixing.
    """
    handle, tmp_name = tempfile.mkstemp(
        dir=str(path.parent), prefix=f".{path.stem}.", suffix=".tmp"
    )
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            stream.write(text)
        os.replace(tmp_name, path)
    except BaseException:
        Path(tmp_name).unlink(missing_ok=True)
        raise


def write_park_record(task: str, key: str, recheck: str, action: str, vault: str | None) -> Path:
    """Write the `later (on …)` line the fleet round reads as the park's record.

    Frontmatter goes through `vault-cli`; body prose has no CLI writer, so the
    page is rewritten in place.
    """
    path = task_file(task, vault)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ParkError(f"could not read the task page {path}: {exc}") from exc
    updated = _replace_or_insert(text, key, record_line(key, action))
    if updated != text:
        atomic_write(path, updated)
    return path


def write_task_key(
    task: str, key: str, recheck: str, vault: str | None, action: str | None = None
) -> None:
    """Record the event key, the re-check instant and the park's own record.

    `defer_date` carries the full instant, not a bare date: the readers honour
    the time of day (`scripts/defer-date.py`), so this is the field that
    actually resurfaces the row at the right moment. The event key rides
    alongside so a restarted round re-derives the watch from disk instead of
    arming a second one.

    Frontmatter is written through `vault-cli task set`, never by editing the
    file: the vault owns its own frontmatter and an unattended writer that
    bypasses the CLI is how a page ends up with a key no reader expects. The
    `later (on …)` line is body prose with no CLI writer, so it is written
    directly (above) — and it is written HERE, not left to the caller, because
    a defer without it reads `unverifiable` and the slot is never freed.
    """
    _set_field(task, "park_event", key, vault)
    _set_field(task, "defer_date", recheck, vault)
    if is_future(recheck):
        write_park_record(task, key, recheck, action or default_action(recheck), vault)


def advance_members(
    record: dict,
    recheck: str,
    vault: str | None,
    skip: str | None,
    action: str | None = None,
) -> list[str]:
    """Move EVERY member's `defer_date` forward on a re-park.

    A watch with several members is the case this store exists for. Advancing
    only the `--task` given to the re-register leaves the peers holding a past
    `defer_date`, and a past defer is not "deferred" to `worker-keepalive.py` —
    it falls through to `resume`, so those peers are resumed into a wait that
    has not fired. The watch is counted once; the clock moves for all of it.

    A peer's record moves with its clock: leaving the old line behind would name
    a re-check that has already passed on a row now deferred to a later one.
    """
    moved = []
    key = record.get("event") or ""
    for task in sorted({t for t in (record.get("tasks") or {}).values() if t}):
        if task == skip:
            continue
        _set_field(task, "defer_date", recheck, vault)
        if key and is_future(recheck):
            write_park_record(task, key, recheck, action or default_action(recheck), vault)
        moved.append(task)
    return moved


# --- subcommands -------------------------------------------------------------

def cmd_threshold(args: argparse.Namespace) -> int:
    if args.seconds:
        print(PARK_THRESHOLD_SECONDS)
    else:
        print(humanize(PARK_THRESHOLD_SECONDS))
    return EXIT_OK


def cmd_register(args: argparse.Namespace) -> int:
    key = resolve_event_key(args.event)
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
    if record.get("_moved"):
        print(f"advanced: {', '.join(record['_moved'])}")
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
        write_task_key(args.task, key, record["recheck_at"], args.vault, args.action)
    if args.repark:
        record["_moved"] = advance_members(
            record, record["recheck_at"], args.vault, args.task, args.action
        )

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
    key = resolve_event_key(args.event)
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


def read_defer(raw: str) -> datetime | None:
    """A stored `defer_date`, read leniently — `None` when it cannot be read.

    Unlike `--recheck`, a task's own `defer_date` may legitimately be date-only:
    the vault writes those, and `scripts/defer-date.py` resolves one to the start
    of its local day. The backfill must read the field as the readers do, not as
    the writer validates it.
    """
    text = raw.strip().strip('"').strip("'")
    try:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
            return datetime.strptime(text, "%Y-%m-%d").replace(
                tzinfo=datetime.now().astimezone().tzinfo
            )
        return parse_instant(text, "defer_date")
    except (ParkError, ValueError):
        return None


def vault_config(vault: str) -> dict:
    done = _vault_cli(["config", "list", "--output", "json"])
    if done.returncode != 0:
        raise ParkError(
            f"could not read the vault config: "
            f"{(done.stderr or done.stdout).strip() or 'vault-cli failed'}"
        )
    try:
        entries = json.loads(done.stdout)
    except json.JSONDecodeError as exc:
        raise ParkError(f"vault config is not readable JSON: {exc}") from exc
    for entry in entries:
        if entry.get("name") == vault:
            return entry
    raise ParkError(f"no configured vault named {vault!r}")


def cmd_backfill(args: argparse.Namespace) -> int:
    """Give every already-deferred row the record the fleet round reads.

    The forward fix is `register` writing the line itself; this is the one-off
    sweep over the rows parked before that existed. It re-derives the population
    with the SAME filter the measurement uses — `status: in_progress` plus a
    `defer_date` strictly after now — so the before/after counts describe one
    population rather than two.
    """
    entry = vault_config(args.vault)
    root = Path(os.path.expanduser(str(entry.get("path") or "")))
    tasks_dir = args.tasks_dir or entry.get("tasks_dir") or "25 Tasks"
    directory = root / tasks_dir
    if not directory.is_dir():
        raise ParkError(f"no tasks directory at {directory}")

    clock = now()
    rows = missing = written = 0
    for path in sorted(directory.glob("*.md")):
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise ParkError(f"could not read {path}: {exc}") from exc
        status = re.search(r"^status:\s*(\S+)", text, re.M)
        if not status or status.group(1).strip() != "in_progress":
            continue
        defer = re.search(r"^defer_date:\s*(\S+)", text, re.M)
        if not defer:
            continue
        when = read_defer(defer.group(1))
        if when is None or when <= clock:
            continue
        rows += 1
        if re.search(r"later \(on ", text):
            continue
        missing += 1
        event = re.search(r"^park_event:\s*(.+)$", text, re.M)
        key = (event.group(1).strip().strip("\"'") if event else "") or iso(when)
        line = record_line(key, default_action(iso(when)))
        if args.apply:
            updated = _replace_or_insert(text, key, line)
            if updated != text:
                atomic_write(path, updated)
                written += 1
        if not args.json:
            print(f"{'wrote' if args.apply else 'would write'}\t{path.name}\t{line}")

    summary = (
        f"{rows} future-dated in_progress | {missing} missing later(on) | "
        f"{written} written"
    )
    if args.json:
        print(json.dumps(
            {"rows": rows, "missing": missing, "written": written, "applied": bool(args.apply)},
            indent=2, sort_keys=True,
        ))
    else:
        print(summary)
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
    p.add_argument(
        "--action",
        help="what replaces the session's live watcher, for the `later (on …)` "
        "record (default: the defer_date re-check + the fleet round)",
    )
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

    p = sub.add_parser(
        "backfill", help="write the park record onto rows deferred before register wrote it"
    )
    p.add_argument("--vault", required=True, help="vault whose tasks are swept")
    p.add_argument("--tasks-dir", help="override the vault's configured tasks dir")
    p.add_argument("--apply", action="store_true", help="write; default is a dry run")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_backfill)

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
