#!/usr/bin/env python3
"""Nudge every session that has sat `watching` past the park threshold to park and exit.

    session-activity.py --json | park-nudge.py [--activity -|<file>] [--json]
                                               [--state <file>] [--dry-run]

WHY THIS EXISTS. The park rule (a wait on a machine event longer than the
threshold parks with a `defer_date` and exits) is always loaded, and it was
measured ignored: a session sat `watching` for 31 minutes after the rule landed,
while a 12-minute one beside it was legal. The reading that tells the two apart
already exists: the `watching` bucket and its state duration, published by the
session-activity instrument. No leg consumed it. `agents/fleet-drive.md` excludes
`parked-on-watcher` rows from nudge candidacy on purpose, because a declared
machine wait is not a stalled row, so the only nudge path skipped exactly the
population the park rule targets. This script is the enforcement point the
round's keep-alive leg runs beside the park-watch resume.

WHAT IT SELECTS. A row is a candidate only when its bucket is exactly
`watching` AND its `watching_min` — the span from the first `🟡 WAITING` closer
of its CURRENT watching episode, published by `session-activity.py` — exceeds
`park-event.py`'s threshold. The comparison is strict: 15 min is inside the
allowance, 16 is not. The bucket is the gate, not the duration. A `working`,
`light`, `gated`, `idle` or `unclassified` row is never a candidate however long
it has waited: `gated` waits on the operator, and the others are not declared
machine waits. A row with no `watching_min` (the instrument could not measure it)
is skipped, never treated as 0.

⚠️ THE DURATION IS NOT `quiet_min`. Transcript silence resets on every watcher
re-arm, so a session parked 3 h 34 m across 20+ re-arms read `quiet 12m` and was
never nudged — the defect this selection exists to remove. `watching_min` does
not reset on a re-arm. The emitted line keeps its `quiet <N>m` field name for
compatibility with the round's parser, but N is the watching-state duration.

SUPPRESSION: ONE NUDGE PER WATCHING EPISODE. Keyed on the session id AND the
episode's first-closer timestamp (`watching_since`), never on the duration —
`watching_min` grows every round, so a key carrying it never matches twice and
the session would be re-nudged every tick. An episode ends, and the session
becomes nudgeable again, when the session is seen outside the `watching` bucket,
is absent from the reading, or the reading carries a DIFFERENT `watching_since`
— a new park began. Keying on the timestamp rather than on the duration dropping
matters: the round cadence is 15 minutes and rounds are routinely skipped, so a
reading can land after one park ended and the next began, where the duration
never fell and a drop-based key would suppress the new park for its whole
length. A reading that carries no `watching_since` falls back to the duration
comparison. The ledger lives at `$PARK_NUDGE_STATE`, default
`~/.claude/state/park-nudge/ledger.json`; its `last_quiet` key holds the
watching-state duration and its `episode` key the episode timestamp.

WHAT IT DOES NOT DO. It never parks, kills, reaps or exits a session: the nudge
asks, the session performs it. It does not deliver the nudge either. It prints
one `NUDGE` line per candidate and the round sends the message.

OUTPUT. One `NUDGE <session-id> quiet <N>m (<name>) :: <message>` line per
emitted nudge, then exactly one `park-nudge:` summary line. `--json` prints the
same as one object; its `quiet_min` key carries the same watching-state duration
the text line labels `quiet`. An unreadable reading prints
`park-nudge: unknown — <why>` and exits 2: a reading that could not be taken is
never reported as zero.

EXIT CODES. 0 ok (with or without nudges) · 2 unreadable input or ledger.
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import importlib.util
import json
import os
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
PARK_EVENT = HERE / "park-event.py"
DEFAULT_STATE = Path.home() / ".claude" / "state" / "park-nudge" / "ledger.json"

EXIT_OK = 0
EXIT_UNKNOWN = 2


def threshold_seconds() -> int:
    """The park threshold, read from its one home and never restated here."""
    spec = importlib.util.spec_from_file_location("park_event", PARK_EVENT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return int(mod.PARK_THRESHOLD_SECONDS)


def _version_key(p: Path) -> list:
    return [(0, int(x)) if x.isdigit() else (1, x) for x in p.parent.parent.name.split(".")]


def self_exit_path() -> str:
    """Where `session-self-exit.py` lives. It ships in vault-cli, NOT in this
    plugin, so a bare `scripts/session-self-exit.py` resolves to nothing from
    the receiving session. Order: `$VAULT_CLI_SCRIPTS`, the newest installed
    vault-cli cache version, then the marketplace clone."""
    env = os.environ.get("VAULT_CLI_SCRIPTS")
    if env and (Path(env) / "session-self-exit.py").is_file():
        return str(Path(env) / "session-self-exit.py")
    home = Path(os.environ.get("HOME") or str(Path.home()))
    cache = home / ".claude" / "plugins" / "cache" / "vault-cli" / "vault-cli"
    found = sorted(cache.glob("*/scripts/session-self-exit.py"), key=_version_key) if cache.is_dir() else []
    if found:
        return str(found[-1])
    return str(home / ".claude" / "plugins" / "marketplaces" / "vault-cli" / "scripts" / "session-self-exit.py")


def nudge_text(row: dict, minutes: int, limit_min: int, exit_script: str) -> str:
    waits = row.get("waits_on") or "the machine event your panel names"
    task = '"<your anchored task>"'
    return (
        f"PARK NOW: you have sat `watching` {minutes} min (limit {limit_min} min) on: {waits}. "
        f"A machine wait over {limit_min} min parks and exits; it never holds a slot. "
        f"(a) Register the wait: `python3 {PARK_EVENT} register --task {task} "
        f"--event \"<the event you wait on>\" --recheck \"<re-check TIME, ISO-8601 with offset, "
        f"e.g. 2026-11-04T23:45:00+01:00>\"`. "
        f"(b) Set `defer_date` to that re-check TIME with its offset: register writes it; confirm with "
        f"`vault-cli task get {task} defer_date`, and if it is not that instant run "
        f"`vault-cli task set {task} defer_date \"<same ISO-8601 instant>\"`. Never a bare date. "
        f"(c) Stop your watcher, then free the slot with `python3 {exit_script}` as your last call."
    )


@contextlib.contextmanager
def locked(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(str(path) + ".lock", "w") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def read_ledger(path: Path) -> dict:
    if not path.exists():
        return {}
    data = json.loads(path.read_text())
    if not isinstance(data, dict):
        raise ValueError("ledger is not an object")
    return data


def write_ledger(path: Path, data: dict) -> None:
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".ledger-")
    with os.fdopen(fd, "w") as fh:
        json.dump(data, fh, indent=1, sort_keys=True)
    os.replace(tmp, path)


def same_episode(prev: dict, row: dict, quiet: int) -> bool:
    """True when this reading is the episode the ledger already nudged.

    Keyed on the episode's first-closer timestamp (`watching_since`) when the
    instrument publishes one: a round can MISS the boundary between two parks —
    the cadence is 15 minutes and rounds are routinely skipped — so a duration
    that merely failed to drop is not proof of the same episode. The duration
    comparison stays as the fallback for a reading that carries no
    `watching_since`."""
    if not prev.get("nudged"):
        return False
    episode = row.get("watching_since")
    if episode is not None:
        return prev.get("episode") == episode
    return quiet >= int(prev.get("last_quiet", 0))


def evaluate(sessions: list, ledger: dict, limit_s: int, exit_script: str):
    """Pure decision over one reading. Returns (nudges, next_ledger, stats).

    The next ledger carries ONLY sessions seen `watching` in this reading, so a
    session that left the bucket or the fleet loses its entry, and that loss is
    what lets a later re-entry be nudged again."""
    nudges, nxt = [], {}
    stats = dict(watching=0, over=0, suppressed=0, within=0)
    limit_min = limit_s // 60
    for row in sessions:
        if row.get("bucket") != "watching":
            continue
        # Counted before the sid and measurement guards: the reading's own
        # `counts.watching` counts every watching row, and a summary that
        # disagrees with it is a number the operator cannot reconcile.
        stats["watching"] += 1
        sid = row.get("sid") or ""
        if not sid:
            continue
        prev = ledger.get(sid) or {}
        quiet = row.get("watching_min")
        if not isinstance(quiet, int) or isinstance(quiet, bool):
            # Not measurable this round. Carry the episode's ledger entry rather
            # than dropping it: dropping would re-arm suppression and re-nudge
            # the same park on the next reading that can measure it.
            if prev:
                nxt[sid] = prev
            continue
        nudged = same_episode(prev, row, quiet)
        episode = row.get("watching_since")
        if quiet * 60 <= limit_s:
            stats["within"] += 1
            nxt[sid] = dict(nudged=nudged, last_quiet=quiet, episode=episode)
            continue
        stats["over"] += 1
        if nudged:
            stats["suppressed"] += 1
            nxt[sid] = dict(nudged=True, last_quiet=quiet, episode=episode)
            continue
        nudges.append(dict(sid=sid, name=row.get("name") or "-", quiet_min=quiet,
                           message=nudge_text(row, quiet, limit_min, exit_script)))
        nxt[sid] = dict(nudged=True, last_quiet=quiet, episode=episode)
    return nudges, nxt, stats


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--activity", default="-",
                    help="session-activity.py --json output: a file, or - for stdin (default)")
    ap.add_argument("--state", default=os.environ.get("PARK_NUDGE_STATE") or str(DEFAULT_STATE),
                    help="suppression ledger path (default $PARK_NUDGE_STATE or ~/.claude/state/park-nudge/ledger.json)")
    ap.add_argument("--dry-run", action="store_true", help="decide and print, never write the ledger")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)

    try:
        raw = sys.stdin.read() if a.activity == "-" else Path(a.activity).read_text()
        reading = json.loads(raw)
        sessions = reading["sessions"]
        if not isinstance(sessions, list):
            raise ValueError("`sessions` is not a list")
    except Exception as e:  # never report an unread fleet as a clean one
        print(f"park-nudge: unknown — activity reading unreadable ({type(e).__name__}: {e})")
        return EXIT_UNKNOWN

    state = Path(a.state).expanduser()
    try:
        # The threshold's one home. A missing or unreadable park-event.py is a
        # broken install, and a traceback would read as a crash rather than the
        # "could not decide" this line reports.
        limit_s = threshold_seconds()
    except Exception as e:
        print(f"park-nudge: unknown — threshold unreadable ({type(e).__name__}: {e})")
        return EXIT_UNKNOWN
    exit_script = self_exit_path()
    try:
        with locked(state):
            ledger = read_ledger(state)
            nudges, nxt, stats = evaluate(sessions, ledger, limit_s, exit_script)
            if not a.dry_run:
                write_ledger(state, nxt)
    except Exception as e:
        print(f"park-nudge: unknown — ledger unreadable ({type(e).__name__}: {e})")
        return EXIT_UNKNOWN

    summary = (f"park-nudge: {len(nudges)} nudged · {stats['suppressed']} suppressed · "
               f"{stats['within']} within {limit_s // 60}m · {stats['watching']} watching · "
               f"{len(sessions)} live")
    if a.json:
        print(json.dumps(dict(threshold_min=limit_s // 60, nudges=nudges, summary=summary, **stats), indent=1))
        return EXIT_OK
    for n in nudges:
        print(f"NUDGE {n['sid']} quiet {n['quiet_min']}m ({n['name']}) :: {n['message']}")
    print(summary)
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
