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
⚠️ **Closed 2026-11-10: the declaration DOES carry its date.** The `Stop` record's
own `ts` is the moment the wait was declared, so the gap above was a reading of the slot
text alone. `declaration()` now gives every newest-`Stop` declaration a verdict, and only
`declared` suppresses:

| verdict | when | effect |
|---|---|---|
| `declared` | a non-empty slot younger than `MAX_AGE_S`, or naming a clock (ISO date/datetime) still in the future | suppresses the nudge, as before |
| `expired` | older than `MAX_AGE_S` (default 3600 s, `SUPERVISOR_WAIT_MAX_AGE_S`) and naming no future clock | **not** a wait: the session is re-driven, re-checks its observable, and either works or re-parks — re-parking writes a fresh `Stop` with a fresh `ts`, so a genuine long wait costs one cheap re-check per hour |

Each row also carries `mechanism`: `session-bound` when the slot names a wake that lives
inside the session (`Monitor`, `ScheduleWakeup`, `CronCreate`, a `run_in_background` /
background shell or task, a background sub-agent), else `external`. It is a FLAG, not a
verdict: a live session's background task does wake it, and `~/.claude/CLAUDE.md` § Async
State Closer allows such a wake in the `(mechanism, id)` slot. What it cannot do is outlive
the process — so for a headless worker, which the reaper closes at 15 min idle, it is the
wake that never fires. The age bound is what re-drives it; the flag is what names why.

Measured 2026-11-10: of 27 idle 🟡 sessions, 16 would never wake — 6 whose event had
already happened, 5 whose watcher had died, 5 declaring nothing. A bounded re-check is
the one rule that reaches all three without probing each observable. A record with no
`ts` keeps the pre-change behaviour (`declared`) — the failed-read rule below, applied to
the clock.

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

STATE = os.environ.get("ATTENTION_STATE_DIR") or os.path.expanduser(
    "~/.claude/state/attention"
)

ENDS_MARKER = "⏰ Ends:"

MAX_AGE_S = 3600

# A mechanism that lives inside the session that armed it. Matched against the slot
# text only (case-insensitive); the list mirrors `~/.claude/CLAUDE.md` § Async State
# Closer's banned wakes, so widen both together.
SESSION_BOUND = re.compile(
    r"\bMonitor\b|ScheduleWakeup|CronCreate|run_in_background"
    r"|\bbackground\s+(?:bash|shell|task|watcher|sub-?agent)\b"
    r"|\bbg\s+(?:bash|shell|task|watcher)\b"
    r"|\bbackground\s+poll\b|\bharness\s+tasks?\b"
    # A Claude Code background-task id: `b` + 8 lower-case alphanumerics, at least one
    # outside hex so a short commit SHA never matches.
    r"|\bb(?=[a-z0-9]{8}\b)(?=[a-z0-9]*[g-z])[a-z0-9]{8}\b",
    re.IGNORECASE,
)


def _max_age():
    try:
        return float(os.environ.get("SUPERVISOR_WAIT_MAX_AGE_S") or MAX_AGE_S)
    except ValueError:
        return float(MAX_AGE_S)


def declared_wait(sid, state_dir=None, now=None):
    """True when `sid`'s newest `Stop` record carries a LIVE `⏰ Ends:` declaration.

    Live means verdict `declared` — see `declaration()`. A log that is missing,
    unreadable or malformed, or that holds no `Stop` record at all, is **False** — see
    the module docstring's failed-read rule.
    """
    d = declaration(sid, state_dir=state_dir, now=now)
    return d is not None and d["verdict"] == "declared"


def declaration(sid, state_dir=None, now=None):
    """The newest `Stop` record's `⏰ Ends:` declaration with its verdict, or None.

    Returns `{"ends", "ts", "age_s", "verdict"}`; `ts`/`age_s` are None when the record
    carries no `ts`. None when nothing is declared or the read fails.
    """
    newest = _newest_stop(sid, state_dir)
    if newest is None or not _has_ends(newest.get("detail")):
        return None
    detail = newest["detail"]
    ends = detail[detail.find(ENDS_MARKER) + len(ENDS_MARKER):].strip().splitlines()[0]
    ts = newest.get("ts")
    ts = float(ts) if isinstance(ts, (int, float)) else None
    age = None if ts is None else (time.time() if now is None else now) - ts
    clock = _latest_clock(ends)
    now_s = time.time() if now is None else now
    if age is not None and age > _max_age() and not (clock and clock > now_s):
        verdict = "expired"
    else:
        verdict = "declared"
    mechanism = "session-bound" if SESSION_BOUND.search(ends) else "external"
    return {"ends": ends, "ts": ts, "age_s": age, "verdict": verdict,
            "mechanism": mechanism}


_CLOCK = re.compile(
    r"(\d{4}-\d{2}-\d{2})(?:[T ](\d{2}:\d{2})(?::\d{2})?(Z|[+-]\d{2}:?\d{2})?)?"
)


def _latest_clock(text):
    """The latest ISO date/datetime the slot names, as epoch seconds, or None.

    A bare date means the END of that local day — `defer_date: 2026-11-11` resurfaces
    the task that day, so the wait holds through it. A datetime with no offset is local.
    """
    from datetime import datetime, timedelta
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


def _newest_stop(sid, state_dir=None):
    """The newest `Stop` record in `sid`'s log, or None on no record / failed read."""
    if not sid or "/" in sid or "\\" in sid:
        # ⚠️ **A sid is not a path.** `declared_wait_ids()` derives its ids from
        # `os.path.basename`, but this per-session form takes one from its caller, and an
        # id carrying a separator would read outside the store. No current caller supplies
        # one, so this is defence-in-depth rather than a live traversal — it costs a line
        # and keeps the failed-read contract total.
        return None
    path = os.path.join(state_dir or STATE, f"{sid}.events.jsonl")
    newest = None
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
                    newest = rec
    except Exception:
        return None
    return newest


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


def declarations(state_dir=None, now=None):
    """`{sid: declaration}` for every session whose newest `Stop` declares a wait."""
    base = state_dir or STATE
    out = {}
    for path in glob.glob(os.path.join(base, "*.events.jsonl")):
        sid = os.path.basename(path)[: -len(".events.jsonl")]
        d = declaration(sid, state_dir=base, now=now)
        if d is not None:
            out[sid] = d
    return out


def main(argv):
    if "--verdicts" in argv:
        rows = declarations()
        if "--json" in argv:
            print(json.dumps(rows, sort_keys=True))
            return 0
        for sid, d in sorted(rows.items()):
            age = "?" if d["age_s"] is None else f"{int(d['age_s'] // 60)}m"
            print(f"{sid}\t{d['verdict']}\t{d['mechanism']}\t{age}\t{d['ends'][:100]}")
        counts = {}
        for d in rows.values():
            for k in (d["verdict"], d["mechanism"]):
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
