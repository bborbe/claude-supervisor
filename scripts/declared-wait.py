#!/usr/bin/env python3
"""The declared-wait reader — session ids whose newest `Stop` record declares `⏰ Ends:`.

**One home for the read, because three consumers take it.** `scripts/fleet-board.py`
(its `Unblocks: nudge` value), `scripts/manager-predispatch.py` (its `idle_stuck`
branch (b)), and the fleet sweep agent's `parked-on-watcher` class all ask the same
question. A private copy in each is the drift `agents/manager-drive.md:134` forbids
verbatim — *"Reuse that shipped reading; do not add a fourth definition of 'idle'"* —
and it is a separate script rather than a function inside `who-needs-me.py` for the
same reason `session-liveness.py` is: the consumers do not share a module, and only
one of them already loads that one.

⚠️ **The marker is a CONTRACT, not a text heuristic.** `~/.claude/CLAUDE.md`
§ Async State Closer scopes `⏰ Ends:` to the 🟡 WAITING panel alone — the one state
whose validity rule is *a machine produces the change*. Deliberately **not**
`⏰ Next:`, the general-purpose forward pointer, which appears on rows waiting on
nothing at all (measured 2026-10-04 over 21,349 `Stop` records: `⏰ Ends:` on 4,967,
`⏰ Next:` on 843).

⚠️ **The NEWEST `Stop` record is the rule.** The log is append-only, so an older
`⏰ Ends:` outlives the wait it declared and would pin the row forever.

⚠️ **A WELL-FORMED but STALE declaration is the case that rule does NOT reach, and it
is the expensive one.** If a declared wake never fires and the session writes no later
`Stop` record, the newest record keeps carrying the marker and both consumers suppress
on it **indefinitely** — `fleet-board.py` withholds `nudge`, `manager-predispatch.py`
returns `LIVENESS_PARKED` so `idle_stuck` exempts the row, with no re-check and no drift
back. `agents/manager-sweep-reader.md:149` names exactly this shape for the manager
tier's own `declared_wait:` field and records it as *"the honest gap"*: nothing in a
declaration says when the wait began, so a reader **cannot expire it**. That tier's
defence is two-part — the WRITER clears the field when the wait ends, and the cell
**renders the declaration's own text verbatim**, so a stale one reads as stale rather
than as a live wait.
⚠️ **Closed 2026-11-10: the declaration DOES carry its date.** The `Stop` record's own
`ts` is when the wait was rendered, so the gap above was a reading of the slot text
alone. `wait_record()` now gives every declaration a verdict, and only `declared`
suppresses:

| verdict | when | effect |
|---|---|---|
| `declared` | re-rendered within `MAX_AGE_S`, or naming a clock (ISO date/datetime) still in the future | suppresses the nudge, as before |
| `expired` | not re-rendered for `MAX_AGE_S` (default 3600 s, `SUPERVISOR_WAIT_MAX_AGE_S`) and naming no future clock | **not** a wait: the session is re-driven, re-checks its observable, and works or re-parks |

Measured 2026-11-10: of 27 idle 🟡 sessions, 16 would never wake — 6 whose event had
already happened, 5 whose watcher had died, 5 declaring nothing. A bounded re-check
reaches all three without probing each observable.

⚠️ **Two clocks, and the 24 h escalation reads the slow one.** `last_ts` is the newest
render and drives the 1 h re-check. `ts` is when the CURRENT wait began — the first
`Stop` of the trailing run whose `⏰ Ends:` names the same observable (compared with
digits stripped, so a re-render with a new ETA is the same wait). Were `ts` the newest
render, every hourly re-park would reset `scripts/drive-escalate.py`'s 24 h clock and a
dead wait would never be carded.

Each record also carries `mechanism`: `session-bound` when the slot names a wake living
inside the session (`Monitor`, `ScheduleWakeup`, `CronCreate`, a background shell / task
/ poll, a background sub-agent, a harness task id), else `external`. A FLAG, not a
verdict: a live session's background task does wake it, and `~/.claude/CLAUDE.md`
§ Async State Closer allows it in the `(mechanism, id)` slot — it just cannot outlive
the process, which is why a headless worker the reaper closes never hears it.

⚠️ **A `Stop` record is a rendered closer panel, never a raised gate** — the same
discriminator `who-needs-me.py`'s `is_open_gate()` reads to reach the *opposite*
conclusion. A session that is genuinely gated is therefore never claimed here, and
this reader cannot steal a `waiting-on-human` row from the gate path.

⚠️ **A failed read is not a negative.** A missing, unreadable or torn log leaves the
session **absent from the set**, which is the safe direction for every consumer: each
uses this to *remove* a false `stuck`/`problem`/`nudge` verdict, so a failed read
leaves today's behaviour exactly as it was rather than inventing a park.
"""
import glob
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta

STATE = os.environ.get("ATTENTION_STATE_DIR") or os.path.expanduser(
    "~/.claude/state/attention"
)

ENDS_MARKER = "⏰ Ends:"

MAX_AGE_S = 3600

# Mirrors `~/.claude/CLAUDE.md` § Async State Closer's session-bound wakes — widen both.
SESSION_BOUND = re.compile(
    r"\bMonitor\b|ScheduleWakeup|CronCreate|run_in_background"
    r"|\bbackground\s+(?:bash|shell|task|watcher|poll|sub-?agent)\b"
    r"|\bbg\s+(?:bash|shell|task|watcher)\b|\bharness\s+tasks?\b"
    # A Claude Code background-task id: `b` + 8 lower-case alphanumerics, at least one
    # outside hex so a short commit SHA never matches.
    r"|\bb(?=[a-z0-9]{8}\b)(?=[a-z0-9]*[g-z])[a-z0-9]{8}\b",
    re.IGNORECASE,
)

_CLOCK = re.compile(
    r"(\d{4}-\d{2}-\d{2})(?:[T ](\d{2}:\d{2})(?::\d{2})?(Z|[+-]\d{2}:?\d{2})?)?"
)


def _max_age():
    try:
        return float(os.environ.get("SUPERVISOR_WAIT_MAX_AGE_S") or MAX_AGE_S)
    except ValueError:
        return float(MAX_AGE_S)


def _latest_clock(text):
    """The latest ISO date/datetime the slot names, as epoch seconds, or None.

    A bare date means the END of that local day — `defer_date: 2026-11-11` resurfaces
    the task that day, so the wait holds through it. A datetime with no offset is local.
    """
    best = None
    for date, hm, tz in _CLOCK.findall(text):
        try:
            if not hm:
                dt = datetime.fromisoformat(date) + timedelta(days=1)
            else:
                off = "+00:00" if tz == "Z" else (tz[:3] + ":" + tz[-2:] if tz else "")
                dt = datetime.fromisoformat(f"{date}T{hm}{off}")
            t = dt.timestamp()
        except ValueError:
            continue
        best = t if best is None else max(best, t)
    return best


def _same_wait(a, b):
    """Two `⏰ Ends:` texts name the same observable — digits ignored (ETAs, ages)."""
    def norm(x):
        return re.sub(r"\s+", " ", re.sub(r"\d+", "", x)).strip().lower()
    return norm(a) == norm(b)


def wait_record(sid, state_dir=None, now=None):
    """`sid`'s current wait, or None.

    `{"session_id", "ts", "last_ts", "ends", "verdict", "mechanism"}` — `ts` is when
    this wait began, `last_ts` its newest render; see the module docstring.

    THIS FUNCTION OWNS BOTH CLOCKS. `ts` is when the current wait BEGAN — the first
    `Stop` of the trailing run naming the same observable — and is what
    `scripts/drive-escalate.py` compares against its 24 h threshold (it computes no clock
    of its own). `last_ts` is the newest render, and drives the 1 h `verdict`. A wait is
    stale for the escalation exactly when its observable has not changed for 24 h,
    however many times it was re-rendered in between.

    The boolean callers (`fleet-board.py`, `manager-predispatch.py`, the sweep) read
    the `verdict` through `declared_wait_ids()`; the escalation reads `ts`.

    Returns None — never raises — on the same failed-read rule as `declared_wait()`:
    a missing, unreadable or malformed log, or one holding no `Stop` record at all.
    """
    if not sid or "/" in sid or "\\" in sid:
        # ⚠️ **A sid is not a path.** `declared_wait_ids()` derives its ids from
        # `os.path.basename`, but this per-session form takes one from its caller, and an
        # id carrying a separator would read outside the store. No current caller supplies
        # one, so this is defence-in-depth rather than a live traversal — it costs a line
        # and keeps the failed-read contract total.
        return None
    path = os.path.join(state_dir or STATE, f"{sid}.events.jsonl")
    stops = []
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except Exception:
                    # A torn final line is expected, not exceptional: the hook
                    # appends while this reads.
                    continue
                # ⚠️ **A line that parses to a bare string or list is SKIPPED, not fatal
                # to the file.** `rec.get` on a non-dict raises, and letting that reach the
                # outer handler would discard a valid Stop record already collected earlier
                # in the same log — flipping the session back to "not declared", which is
                # the false-positive direction this reader exists to remove.
                # `who-needs-me.py`'s `load_events` guards the same way.
                if not isinstance(rec, dict):
                    continue
                if rec.get("event") == "Stop":
                    stops.append(rec)
    except Exception:
        return None
    if not stops or not _has_ends(stops[-1].get("detail")):
        return None
    ends = _ends_text(stops[-1]["detail"])
    last_ts = _number(stops[-1].get("ts"))
    start = stops[-1]
    for rec in reversed(stops[:-1]):
        detail = rec.get("detail")
        if not _has_ends(detail) or not _same_wait(_ends_text(detail), ends):
            break
        start = rec
    ts = _number(start.get("ts"))
    if ts is None:
        ts = last_ts
    now_s = time.time() if now is None else now
    clock = _latest_clock(ends)
    expired = (last_ts is not None and now_s - last_ts > _max_age()
               and not (clock is not None and clock > now_s))
    return {
        "session_id": sid,
        "ts": ts,
        "last_ts": last_ts,
        "ends": ends,
        "verdict": "expired" if expired else "declared",
        "mechanism": "session-bound" if SESSION_BOUND.search(ends) else "external",
    }


def _ends_text(detail):
    """The text after the `⏰ Ends:` marker, stripped. "" when there is none."""
    idx = detail.find(ENDS_MARKER)
    return detail[idx + len(ENDS_MARKER):].strip()


def _number(value):
    """`ts` as a float, or None.

    ⚠️ **A non-numeric `ts` is None, never 0.** Zero is a real epoch (1970) and would
    make a record with no readable timestamp look like a wait declared half a century
    ago — the escalation would card it immediately on no evidence. None makes the
    caller treat the row as unreadable instead, which is the safe direction.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def declared_wait(sid, state_dir=None, now=None):
    """True when `sid`'s newest `Stop` record carries a LIVE `⏰ Ends:` declaration.

    Live means verdict `declared` — see `wait_record()`.

    Returns a bool. A log that is missing, unreadable or malformed, or that holds no
    `Stop` record at all, is **False** — see the module docstring's failed-read rule.
    """
    rec = wait_record(sid, state_dir=state_dir, now=now)
    return rec is not None and rec["verdict"] == "declared"


def _has_ends(detail):
    """Whether a `Stop` record's `detail` carries a NON-EMPTY `⏰ Ends:` slot.

    `detail` holds one rendered line, so the slot is non-empty exactly when text
    follows the marker. A bare marker with nothing after it declares no wait.

    ⚠️ **A non-string `detail` is "no declaration", never an exception.** The field is
    hook-written, but this reader's own contract is that a malformed log leaves the
    session absent from the set rather than raising — and a truthy dict or list would
    sail through `detail or ""` and raise on `.find`, escaping into `fleet-board.py`'s
    `collect_signals` and into every `manager-predispatch.py` row. Guarding the type at
    the one place the value is read is what keeps that contract true.
    """
    if not isinstance(detail, str):
        return False
    idx = detail.find(ENDS_MARKER)
    if idx < 0:
        return False
    return bool(detail[idx + len(ENDS_MARKER):].strip())


def declared_wait_ids(state_dir=None, now=None):
    """Every session id whose newest `Stop` record declares a non-empty `⏰ Ends:`.

    The whole-store form, for the renderers: they classify every live row at once and
    a per-session call would re-open the directory for each. Reading one session's log
    per file is still O(files), which is what `who-needs-me.py`'s own `load_events()`
    pays — this is the same shape, not a new cost.

    ⚠️ **The cost is O(total log bytes), and unbounded over a session's lifetime** —
    the logs are append-only and never truncated, so a long-lived store only grows.
    Both production consumers call this once per process or render (`manager-predispatch.py`
    memoises it; `fleet-board.py` calls it once in `collect_signals`), so it is paid
    once rather than per row. A tail-read is the optimisation if that ever stops being
    true, and this note exists so the next reader measures rather than assumes.
    """
    base = state_dir or STATE
    out = set()
    for path in glob.glob(os.path.join(base, "*.events.jsonl")):
        sid = os.path.basename(path)[: -len(".events.jsonl")]
        if declared_wait(sid, state_dir=base, now=now):
            out.add(sid)
    return out


def main(argv):
    if "--detail-json" in argv:
        # The 24 h escalation's input: one record per declaring session, carrying the
        # `ts` and `⏰ Ends:` text `declared_wait_ids()` throws away. A record whose `ts`
        # is unreadable still renders, with `ts: null`, so the caller can name it as
        # unverifiable rather than silently dropping the row.
        records = []
        base = STATE
        for path in sorted(glob.glob(os.path.join(base, "*.events.jsonl"))):
            sid = os.path.basename(path)[: -len(".events.jsonl")]
            rec = wait_record(sid, state_dir=base)
            if rec is not None:
                records.append(rec)
        print(json.dumps(records, indent=2, sort_keys=True))
        return 0
    if "--verdicts" in argv:
        counts = {}
        for path in sorted(glob.glob(os.path.join(STATE, "*.events.jsonl"))):
            sid = os.path.basename(path)[: -len(".events.jsonl")]
            rec = wait_record(sid, state_dir=STATE)
            if rec is None:
                continue
            age = "?" if rec["last_ts"] is None else f"{int((time.time() - rec['last_ts']) // 60)}m"
            print(f"{sid}\t{rec['verdict']}\t{rec['mechanism']}\t{age}\t{rec['ends'][:100]}")
            for k in (rec["verdict"], rec["mechanism"]):
                counts[k] = counts.get(k, 0) + 1
        print(" · ".join(f"{k}={v}" for k, v in sorted(counts.items())), file=sys.stderr)
        return 0
    ids = declared_wait_ids()
    if "--json" in argv:
        print(json.dumps(sorted(ids)))
        return 0
    for sid in sorted(ids):
        print(sid)
    print(f"{len(ids)} session(s) declare a wait", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
