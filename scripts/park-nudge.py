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

WHAT IT SELECTS. A row is a candidate when its `watching_min` — the span from
the first `🟡 WAITING` closer of its CURRENT watching episode, published by
`session-activity.py` — exceeds `park-event.py`'s threshold. The comparison is
strict: 15 min is inside the allowance, 16 is not.

⚠️ THE DURATION IS THE GATE, NOT THE BUCKET. `session-activity.py` awards the
`watching` bucket only when a background shell is running, so a session parked on
a clock or a `defer_date` with no shell reads `idle` — yet it still holds a spawn
slot and an open `🟡 WAITING` episode, and its `watching_min` keeps growing. Those
are exactly the rows that used to sit for hours while the bucket gate skipped
them, so an `idle` row is a candidate whenever its `watching_min` is measurable
(its episode is open) and its registry `status` reads `idle` (see below). A `watching` row is always a candidate, measured or not,
because the summary's `watching` count reconciles with the reading's own
`counts.watching`. A `working`, `light`, `gated` or `unclassified` row is never a
candidate however long it has waited: `gated` waits on the operator, `working` /
`light` have no open episode, and `unclassified` names no declared machine wait.
A row with no `watching_min` (the instrument could not measure it) is skipped,
never treated as 0.

⚠️ THE DURATION IS NOT `quiet_min`. Transcript silence resets on every watcher
re-arm, so a session parked 3 h 34 m across 20+ re-arms read `quiet 12m` and was
never nudged — the defect this selection exists to remove. `watching_min` does
not reset on a re-arm. The emitted line keeps its `quiet <N>m` field name for
compatibility with the round's parser, but N is the watching-state duration.

⚠️ THE MANAGER TIER IS NEVER A CANDIDATE, HOWEVER LONG IT HAS WAITED. The park this
script asks for is a WORKER mechanism: `park-event.py register` binds an event key
to an ANCHORED TASK and writes a `defer_date`, and a manager has no anchored task —
so the request is inapplicable to it rather than merely unwise. The harm is
specific and it is the pane: a goal manager's whole wake path is the detached
sweep gate typing into its pane, so following the nudge (register → `defer_date` →
`session-self-exit.py`) closes the only channel that can reach it, and the arm text
says so — *"a goal manager whose gate cannot reach it never runs again."* Recovery
is `topic-keepalive.py` restarting it off a `STALE` reading, which is a restart
loop, not a park.

⚠️ `OK` IS THE NORMAL STATE OF A HEALTHY ARMED MANAGER, so this is a TIER test and
not a `--state` test. Measured 2026-11-12: three managers were drafted in one round
at 28%, 52% and 55% of their own bounds, and a fourth read `OK age=0s` — armed,
re-armed at head and tail. A guard that skipped only `SLEEPING` / `STOPPED` would
still have nudged the common case. `manager_tier()` therefore tests tier FIRST and
reads `--state` SECOND, as a backstop only.

⚠️ AN `idle` BUCKET IS A CANDIDATE ONLY WHEN THE REGISTRY READS `idle`. The bucket
is not the registry status: `session-activity.py::bucket_for` lets a `status: busy`
row with no tool calls in its window fall through to `idle`. A pusher — idle
between rounds by design, its registry `busy` — was ordered to park six times
(2026-11-14, 2026-11-18), and its `watching_min` went 29 → 17 between two sends
because it restarts per `🟡 WAITING` episode. Each such row counts
`<n> status-skipped`. A `watching` row is `shell` by construction and is unaffected.

⚠️ AN UNANCHORED SESSION IS NEVER A CANDIDATE. The park binds the wait to an
ANCHORED TASK, so the order is inapplicable to a session no task claims — the
manager-tier reasoning again. The anchor comes from `close-worker.py`'s
`find_task_checked` (the `claude_session_id` join), and the text names that task
instead of a `"<your anchored task>"` placeholder. No anchor counts
`<n> unanchored-skipped`; a FAILED search counts `<n> anchor-unknown` and nudges
nothing, because a search that errored proves no absence.

⚠️ HANDED-OVER PATHS ARE THE MARKETPLACE CLONES. `park-event.py` and
`session-self-exit.py` are named by their unversioned marketplace paths, never by
this process's cache dir (the SENDER's loaded version) or the newest installed
cache version — both go stale for a recipient on another version.

SUPPRESSION: ONE NUDGE PER WATCHING EPISODE. Keyed on the session id AND the
episode's first-closer timestamp (`watching_since`), never on the duration —
`watching_min` grows every round, so a key carrying it never matches twice and
the session would be re-nudged every tick. An episode ends, and the session
becomes nudgeable again, when the session is seen outside the candidate
population (`watching` or `idle`), is absent from the reading, or the reading
carries a DIFFERENT `watching_since`
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
emitted nudge, then exactly one `park-nudge:` summary line. The summary carries
`<n> manager-skipped`: how many over-threshold rows the tier test withheld a
nudge from, followed by `<n> status-skipped · <n> unanchored-skipped ·
<n> anchor-unknown` for the status and anchor gates (`--json`: `status_skipped`,
`unanchored`, `anchor_unknown`). That count is what makes the gate observable — a zero from a round
that saw no manager cannot be told from a round where the gate fired, and the two
are different readings. `--json` prints the same as one object, plus a
`manager_rows` list naming each skipped session and WHY (which signal caught it);
its `quiet_min` key carries the same watching-state duration the text line labels
`quiet`. An unreadable reading prints `park-nudge: unknown — <why>` and exits 2: a
reading that could not be taken is never reported as zero.

⚠️ A `manager-liveness.py` that cannot be imported prints
`park-nudge: unknown — manager-tier gate unreadable (…)` and exits 2. It is the
same rule as the threshold's: the two scripts ship in one directory, so a broken
import is a broken install, and running WITHOUT the gate is the one outcome that
must not happen — it restores the defect this gate exists to remove, and it would
do so silently, because a nudged manager looks exactly like a nudged worker in
the output. Skipping the leg is visible; nudging a manager is not.

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
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
PARK_EVENT = HERE / "park-event.py"
MANAGER_LIVENESS = HERE / "manager-liveness.py"
DEFAULT_STATE = Path.home() / ".claude" / "state" / "park-nudge" / "ledger.json"

EXIT_OK = 0
EXIT_UNKNOWN = 2


def threshold_seconds() -> int:
    """The park threshold, read from its one home and never restated here."""
    spec = importlib.util.spec_from_file_location("park_event", PARK_EVENT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return int(mod.PARK_THRESHOLD_SECONDS)


def liveness_module():
    """Import `manager-liveness.py` — the ONE manager-tier reader, never a copy.

    Two of its facts are read here and neither may be reimplemented: which session
    ids are managers (`manager_sessions`) and what a manager subject's `--state`
    reads (`classify`). A second copy of either would drift from the writer, and the
    drift is silent in the worst direction — a manager drafted for a park whose only
    wake path is its pane.
    """
    spec = importlib.util.spec_from_file_location("manager_liveness", MANAGER_LIVENESS)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def candidate_slugs(liveness, name: str) -> list:
    """Every manager slug this session NAME could belong to.

    A goal manager's session name is `<subject> Manager` (`/supervisor:open` names it
    so), and its state slug is `<branch>--<base slug of subject>`. Both spellings are
    tried, against every branch, because the row carries no branch — the tier is a
    property of the subject, and a subject may be a goal, a topic or the fleet.
    """
    raw = (name or "").strip()
    if not raw:
        return []
    bases = [raw]
    for suffix in (" Manager", " manager"):
        if raw.endswith(suffix):
            bases.append(raw[: -len(suffix)].strip())
    out = []
    for base in bases:
        # A name that slugifies to nothing (`!!!`) names no subject; without this
        # guard it would produce `goal--`, a slug no record can carry.
        if not base or not liveness.base_slug(base):
            continue
        for branch in liveness.BRANCHES:
            s = liveness.slug(base, branch)
            if s not in out:
                out.append(s)
    return out


def manager_tier(liveness, row: dict, sessions: dict, slugs: set, now: float):
    """(is_manager, why) for one reading row. The tier test, then the `--state` guard.

    ⚠️ **The order is the fix, not a detail.** A manager-tier session has no anchored
    task, so `park-event.py register` and `defer_date` have nothing to bind to — the
    worker park mechanism is inapplicable to it at all, and no `--state` value can
    make it applicable. `OK` is the NORMAL state of a healthy armed manager, so a
    guard that skipped only `SLEEPING` / `STOPPED` would still have nudged the common
    case. The tier test therefore runs first and settles the question on its own.

    Two tier signals, in the order of their strength:

    1. **This session id armed a manager cadence.** `--arm` runs inside the manager's
       own session, so `<slug>.session` is a positive, self-declared statement of
       tier — the only one on disk.
    2. **This session's NAME is a manager subject.** Covers a manager that armed
       before the marker existed; its next arm writes one.

    Then the SECONDARY guard, for a row that still reaches the draft path: a name
    that resolves a candidate subject whose `--state` reads `OK` inside its bound is
    a manager even if both signals missed.

    ⚠️ **The `--state` backstop cannot over-suppress a worker** — an unarmed
    candidate subject reads `UNARMED`, never `OK`. Signal 2 CAN, in one narrow case:
    a worker whose own name is byte-identical to an armed goal subject's. That is a
    known and accepted trade — the cost is one worker that stays running (the state
    before the park rule existed), against a manager whose pane is closed and whose
    only recovery is a restart loop.
    """
    sid = row.get("sid") or ""
    slug = sessions.get(sid)
    if slug:
        return True, f"armed as {slug}"
    candidates = candidate_slugs(liveness, row.get("name"))
    for cand in candidates:
        if cand in slugs:
            return True, f"manager subject {cand}"
    for cand in candidates:
        try:
            verdict, detail = liveness.classify(cand, now)
        except Exception:  # an unreadable record is not an OK reading
            continue
        # `WORKING` and `IDLE` are managers too: the exit a park nudge demands would close
        # the pane that is their only wake path.
        if verdict in ("OK", "WORKING", "IDLE"):
            return True, f"--state {verdict} ({cand}: {detail})"
    return False, ""


def _home() -> Path:
    return Path(os.environ.get("HOME") or str(Path.home()))


def park_event_handover_path() -> str:
    """The `park-event.py` path handed to the RECIPIENT — the marketplace clone.

    ⚠️ **Never `PARK_EVENT`.** That is this process's own `HERE`, a cache dir pinned
    to the version the SENDER loaded, so it is stale for every recipient on another
    version — measured 2026-11-14, an order handed `…/0.308.1/…` while `0.309.2` was
    installed. The marketplace clone carries no version segment and survives every
    release, whatever version the recipient loads."""
    return str(_home() / ".claude" / "plugins" / "marketplaces" / "claude-supervisor"
               / "scripts" / "park-event.py")


def self_exit_path() -> str:
    """Where `session-self-exit.py` lives. It ships in vault-cli, NOT in this
    plugin, so a bare `scripts/session-self-exit.py` resolves to nothing from
    the receiving session. Order: `$VAULT_CLI_SCRIPTS`, then vault-cli's
    marketplace clone.

    ⚠️ **No cache-version lookup.** The newest installed cache dir is the newest
    on THIS host at send time, which is neither the recipient's loaded version nor
    stable across a release — the same staleness `park_event_handover_path()`
    removes. The marketplace clone is version-independent."""
    env = os.environ.get("VAULT_CLI_SCRIPTS")
    if env and (Path(env) / "session-self-exit.py").is_file():
        return str(Path(env) / "session-self-exit.py")
    return str(_home() / ".claude" / "plugins" / "marketplaces" / "vault-cli" / "scripts" / "session-self-exit.py")


def anchor_resolver():
    """`sid -> (task name | None, error | None)` over `close-worker.py::find_task_checked`.

    That function is the repo's `claude_session_id` join (with the `metrics_sessions`
    hand-off) and it reports a FAILED search apart from an absent anchor; a second
    reader here is how two legs would come to disagree about which task a session
    owns. Rows carry no cwd, so the search is the fleet-wide one (`cwd=None`)."""
    spec = importlib.util.spec_from_file_location("close_worker", HERE / "close-worker.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    def resolve(sid: str):
        found, error = mod.find_task_checked(sid, None)
        if found is not None:
            return Path(found[0]).stem, None
        return None, error

    return resolve


def nudge_text(row: dict, minutes: int, limit_min: int, exit_script: str, task_name: str) -> str:
    waits = row.get("waits_on") or "the machine event your panel names"
    task = json.dumps(task_name, ensure_ascii=False)
    return (
        f"PARK NOW: you have sat `watching` {minutes} min (limit {limit_min} min) on: {waits}. "
        f"A machine wait over {limit_min} min parks and exits; it never holds a slot. "
        f"(a) Register the wait: `python3 {park_event_handover_path()} register --task {task} "
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


def evaluate(sessions: list, ledger: dict, limit_s: int, exit_script: str,
             tier=None, anchor=None):
    """Pure decision over one reading. Returns (nudges, next_ledger, stats).

    The next ledger carries ONLY sessions seen in the candidate population
    (`watching` or `idle`) in this reading, so a session that left the population
    or the fleet loses its entry, and that loss is what lets a later re-entry be
    nudged again.

    `tier` is `(liveness, sessions_by_sid, known_slugs, now)` — the manager-tier
    test. `None` disables it, which is only correct for a caller that has already
    proved it is not looking at a manager; the CLI always passes one.

    `anchor` is `sid -> (task name | None, error | None)`. `None` disables the anchor
    gate and leaves the placeholder task name in the text — only for a caller that
    has resolved anchors itself; the CLI always passes one."""
    nudges, nxt = [], {}
    stats = dict(watching=0, over=0, suppressed=0, within=0, manager=0, manager_rows=[],
                 status_skipped=0, unanchored=0, anchor_unknown=0)
    limit_min = limit_s // 60
    for row in sessions:
        bucket = row.get("bucket")
        # An `idle` row is a clock/defer park whose background shell is not
        # running: it still holds a spawn slot and an open `🟡 WAITING` episode,
        # and it is exactly the population the old bucket gate skipped for hours.
        # It is admitted here and the duration check below decides, like any
        # other row. `gated` waits on the operator; `working`/`light` have no
        # open episode; `unclassified` names no declared machine wait.
        if bucket not in ("watching", "idle"):
            continue
        # ⚠️ THE REGISTRY STATUS GATE. The `idle` BUCKET is not the registry's
        # `idle`: `session-activity.py::bucket_for` lets a `status: busy` row with
        # no tool calls in its window fall through to `idle`. A pusher is exactly
        # that between rounds, and it was ordered to park six times while its
        # registry read `busy` (measured 2026-11-14 and 2026-11-18). The registry
        # status is the reading that says the session is idle, so an `idle` row is
        # a candidate only when it says so. A `watching` row is `shell` by
        # construction — its watcher is the target — so the gate is on `idle` only.
        # Not a role exclusion: the rows carry no role, and an exclusion list would
        # move the bug onto the next role that does not fit.
        if bucket == "idle" and row.get("status") != "idle":
            stats["status_skipped"] += 1
            continue
        # The summary's `watching` count reconciles with the reading's own
        # `counts.watching`, so it counts the watching bucket only — an admitted
        # `idle` row is not a `watching` row.
        if bucket == "watching":
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
        # ⚠️ THE TIER GATE, and it is checked before suppression so a manager is
        # never even recorded as nudged. A manager has no anchored task, so the
        # park this nudge asks for is inapplicable to it — and the exit it demands
        # closes the pane, which is a goal manager's entire wake path. A manager
        # that is already asleep cannot decline the nudge, so this has to be here.
        if tier is not None:
            is_manager, why = manager_tier(tier[0], row, tier[1], tier[2], tier[3])
            if is_manager:
                stats["manager"] += 1
                # Carried so the round can say WHY a manager was skipped, not only
                # that one was — the tier test is the fix, and an unobservable fix
                # cannot be told from a reading that had nothing to draft.
                stats["manager_rows"].append(
                    dict(sid=sid, name=row.get("name") or "-", why=why))
                # No ledger entry: it was never drafted, and leaving it out of `nxt`
                # keeps a later worker-tier reading of the same id from being
                # suppressed by an episode that never produced a nudge.
                continue
        if nudged:
            stats["suppressed"] += 1
            nxt[sid] = dict(nudged=True, last_quiet=quiet, episode=episode)
            continue
        # ⚠️ THE ANCHOR GATE, after suppression so an episode already nudged costs no
        # search. The park binds the wait to an ANCHORED TASK (`register --task`,
        # `defer_date`), so for a session no task claims it is inapplicable — the
        # same reasoning as the tier gate. The old text prescribed both commands
        # against the literal `"<your anchored task>"`, which has no valid target.
        task_name = "<your anchored task>"
        if anchor is not None:
            task_name, error = anchor(sid)
            if task_name is None:
                if error:
                    # A failed search proves nothing about absence. Not nudged — the
                    # text would carry no task — and counted apart, so an unreadable
                    # vault never reads as a fleet of unanchored sessions. The prior
                    # entry is carried so a later readable round does not re-nudge
                    # an episode that was already nudged.
                    stats["anchor_unknown"] += 1
                    if prev:
                        nxt[sid] = prev
                else:
                    stats["unanchored"] += 1
                continue
        nudges.append(dict(sid=sid, name=row.get("name") or "-", quiet_min=quiet,
                           message=nudge_text(row, quiet, limit_min, exit_script, task_name)))
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
    # The manager-tier test's inputs, read once for the whole reading.
    # ⚠️ A broken import is a BROKEN INSTALL, and the leg exits 2 rather than
    # running without the gate: without it, a manager is nudged and the output
    # cannot be told from a worker's. Skipping the leg is visible; nudging a
    # manager is not. Same rule as the threshold above.
    try:
        liveness = liveness_module()
        try:
            known_slugs = {n[: -len(".cadence")]
                           for n in os.listdir(liveness.STATE_DIR)
                           if n.endswith(".cadence")}
        except OSError:
            known_slugs = set()
        tier = (liveness, liveness.manager_sessions(), known_slugs, time.time())
    except Exception as e:
        print(f"park-nudge: unknown — manager-tier gate unreadable ({type(e).__name__}: {e})")
        return EXIT_UNKNOWN
    # The anchor gate's resolver. Same rule as the tier gate: `close-worker.py` ships
    # beside this script, so an import failure is a broken install, and running
    # without the gate would restore the placeholder-task order silently.
    try:
        anchor = anchor_resolver()
    except Exception as e:
        print(f"park-nudge: unknown — anchor gate unreadable ({type(e).__name__}: {e})")
        return EXIT_UNKNOWN
    try:
        with locked(state):
            ledger = read_ledger(state)
            nudges, nxt, stats = evaluate(sessions, ledger, limit_s, exit_script, tier, anchor)
            if not a.dry_run:
                write_ledger(state, nxt)
    except Exception as e:
        print(f"park-nudge: unknown — ledger unreadable ({type(e).__name__}: {e})")
        return EXIT_UNKNOWN

    summary = (f"park-nudge: {len(nudges)} nudged · {stats['suppressed']} suppressed · "
               f"{stats['within']} within {limit_s // 60}m · {stats['watching']} watching · "
               f"{len(sessions)} live · {stats['manager']} manager-skipped · "
               f"{stats['status_skipped']} status-skipped · {stats['unanchored']} unanchored-skipped · "
               f"{stats['anchor_unknown']} anchor-unknown")
    if a.json:
        print(json.dumps(dict(threshold_min=limit_s // 60, nudges=nudges, summary=summary, **stats), indent=1))
        return EXIT_OK
    for n in nudges:
        print(f"NUDGE {n['sid']} quiet {n['quiet_min']}m ({n['name']}) :: {n['message']}")
    print(summary)
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
