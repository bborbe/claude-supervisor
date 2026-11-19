#!/usr/bin/env python3
"""Close stale `awaiting-operator` gates whose session no longer exists.

The attention event log is append-only (`<sid>.events.jsonl`, one `open` line per item, one
`close` line per item) and nothing ever clears an item whose session died. Measured 2026-11-09:
`who-needs-me.py` reported `Board card missing (31)`, and 30 of those rows belonged to sessions
`session-liveness.py --check` reports ABSENT — `72b2851a` and `d72dda9f` at 260 hours, `19561716`
at 221 hours. The count is not a work list; it buries the one live uncarded gate under 30 dead
ones. The cause has recurred 37 days with no owner (`~/.claude/state/who-needs-me/causes.json`).

  reap-stale-gates.py [--apply] [--max-age-hours N] [--state DIR]

Default is a dry run: it classifies every stale item and prints one row per item, then the count
it would close. `--apply` appends one `close` line per reapable item.

⚠️ **It never deletes or rewrites an `.events.jsonl`.** That file is the open-state source
`who-needs-me.py` folds (`load_events`), so rewriting it would destroy history and could race the
hook that appends. A reap is one appended `close` line, nothing more.

⚠️ **`.open.json` is not the open-state source.** Measured 2026-11-19: all 984 `*.open.json` files
carry `cleared_by`, so that suffix never means "open". The event log is the only source read here.

⚠️ **Reapable means stale AND ABSENT AND `awaiting-operator`.** A live session's gate is never
touched however old it is — age alone says nothing (`A Park Signal Is Not a Liveness Signal`).
The cause class is the one `who-needs-me.py` derives, reused here rather than re-implemented:
this script imports that file's own `cause_class`, `row`, `wezterm_panes` and `read_registry`, so
the classification can never drift from the report it is cleaning.

⚠️ **A failed liveness read is not ABSENT.** `session-liveness.py` exiting non-zero, or printing
anything other than `ABSENT`, keeps the item. Reaping on an unreadable probe would close live
gates during an outage.

Exit codes: 0 ok (dry run or applied), 2 unreadable input.
"""

import argparse
import glob
import importlib.util
import json
import os
import subprocess
import sys
import time

DEFAULT_STATE = os.path.expanduser("~/.claude/state/attention")
DEFAULT_MAX_AGE_HOURS = 24
HERE = os.path.dirname(os.path.abspath(__file__))
WHO_NEEDS_ME = os.path.join(HERE, "who-needs-me.py")
LIVENESS = os.path.join(HERE, "session-liveness.py")
REAPER = "reap-stale-gates"


def load_who_needs_me():
    """Import the report's own classifier so the tags here cannot drift from it."""
    spec = importlib.util.spec_from_file_location("who_needs_me", WHO_NEEDS_ME)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def open_items(state_dir):
    """Yield `(path, rec)` for every item still open in the event log.

    Folding matches `who-needs-me.py`'s `load_events`: an `open` line registers an item, any
    later non-`open` line for the same `item_id` closes it. A torn final line is expected
    (the hook appends while this reads) and is skipped, not fatal.
    """
    for path in sorted(glob.glob(os.path.join(state_dir, "*.events.jsonl"))):
        opened, closed = {}, set()
        try:
            with open(path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except Exception:
                        continue
                    iid = rec.get("item_id")
                    if not iid:
                        continue
                    if rec.get("type") == "open":
                        opened[iid] = rec
                    else:
                        closed.add(iid)
        except OSError:
            continue
        for iid, rec in opened.items():
            if iid not in closed:
                yield path, rec


def liveness(session_id):
    """`ABSENT` / `LIVE` / `UNKNOWN` — UNKNOWN on any failed or unparseable read."""
    if not session_id:
        return "UNKNOWN"
    try:
        out = subprocess.run(
            [sys.executable, LIVENESS, "--check", session_id],
            capture_output=True, text=True, timeout=60,
        )
    except (OSError, subprocess.SubprocessError):
        return "UNKNOWN"
    text = (out.stdout + out.stderr).strip()
    if text.startswith("ABSENT"):
        return "ABSENT"
    if text.startswith("LIVE"):
        return "LIVE"
    return "UNKNOWN"


def select(candidates, mod, open_panes):
    """Annotate each stale candidate with its cause, liveness and reap verdict.

    Liveness is cached per session id: a dead session owns many items, and one probe per
    item would turn a few hundred sessions into tens of thousands of subprocesses.
    """
    rows = []
    seen = {}
    for path, rec in candidates:
        cause = mod.cause_class(rec, open_panes, mod.task_status_from_closer)
        sid = rec.get("session_id", "")
        if sid not in seen:
            seen[sid] = liveness(sid)
        verdict = seen[sid]
        rows.append({
            "path": path,
            "rec": rec,
            "cause": cause,
            "verdict": verdict,
            "reapable": cause == mod.CAUSE_OPERATOR and verdict == "ABSENT",
        })
    return rows


def render(row, mod, pmap, registry):
    """One line per item: the report's own tag, then this reaper's verdict."""
    what = f"{row['cause']} · liveness {row['verdict']}"
    return f"{'REAP' if row['reapable'] else 'keep'} {mod.row(row['rec'], pmap, what, registry)}"


def close_line(rec, now):
    """The one line `--apply` appends. Any non-`open` type closes the item."""
    return json.dumps({
        "item_id": rec.get("item_id"),
        "session_id": rec.get("session_id"),
        "type": "close",
        "ts": now,
        "cleared_by": REAPER,
    })


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--apply", action="store_true",
                        help="append the close lines (default is a dry run)")
    parser.add_argument("--max-age-hours", type=float, default=DEFAULT_MAX_AGE_HOURS,
                        help=f"minimum age in hours (default {DEFAULT_MAX_AGE_HOURS})")
    parser.add_argument("--state", default=DEFAULT_STATE,
                        help=f"attention state directory (default {DEFAULT_STATE})")
    args = parser.parse_args(argv)

    if not os.path.isdir(args.state):
        sys.stderr.write(f"reap-stale-gates: no such state dir: {args.state}\n")
        return 2

    mod = load_who_needs_me()
    # ⚠️ `wezterm_panes()` answers None on a mux timeout or an unreadable socket —
    # a distinct answer from `{}` (a reachable mux with no panes). A failed read
    # cannot prove a pane is gone, and `is_routable` would raise `TypeError` on
    # `str(pane) not in None`. Measured: the query takes up to ~88 s under load,
    # which is exactly the loaded machine this tool targets. Refuse, never guess.
    pmap = mod.wezterm_panes()
    if pmap is None:
        sys.stderr.write(
            "reap-stale-gates: pane read failed (wezterm unreachable or timed out) — "
            "refusing; an unroutable classification needs the live pane table\n")
        return 2
    registry = mod.read_registry()
    now = time.time()
    cutoff = now - args.max_age_hours * 3600

    # The pane set comes from EVERY open item, not just the stale candidates: the
    # report this mirrors derives `open_panes` from the whole record set, and a
    # candidate-only set would call a live session's pane unroutable.
    all_items = list(open_items(args.state))
    open_panes = {str(r.get("pane")) for _, r in all_items if r.get("pane")}
    candidates = [(p, r) for p, r in all_items
                  if float(r.get("ts") or 0) <= cutoff]
    rows = select(candidates, mod, open_panes)
    reapable = [r for r in rows if r["reapable"]]

    for row in sorted(rows, key=lambda r: float(r["rec"].get("ts") or 0)):
        print(render(row, mod, pmap, registry))

    if not args.apply:
        print(f"dry run — would reap {len(reapable)} of {len(rows)} stale items "
              f"(pass --apply to close them)")
        return 0

    for row in reapable:
        with open(row["path"], "a", encoding="utf-8") as f:
            f.write(close_line(row["rec"], now) + "\n")
        print(f"reaped: {row['rec'].get('item_id')}")
    print(f"reaped {len(reapable)} stale awaiting-operator records")
    return 0


if __name__ == "__main__":
    sys.exit(main())
