#!/usr/bin/env python3
"""One table classifying every live session: running / idle / needs-input / problem.

Emits box-table JSON on stdout — `{"header", "rows", "widths", ...}` — for
`box-table.py`. Extra keys are safe there: it reads only `header`, `rows` and
`widths` (`data[...]` and `data.get(...)`), so the counts, the per-row detail and
the coverage assertion ride in the same document.

The row set is the SESSION REGISTRY, `~/.claude/sessions/<pid>.json` — one row per
entry, which is what makes the row count checkable at all. `fleet-sessions.py` is
a lookup (session id -> task title), never a roster: measured 2026-09-21 it
returns 2352 rows, every stamped task ever, so treating it as the row set renders
the table useless.

Four buckets, EACH WITH A SECOND SIGNAL the registry status cannot supply, so no
bucket is a guess off one ambiguous field:

  problem      -- inside one tool call >= --stuck-min (the tool record's `ts`)
  needs-input  -- an open gate in the attention store (independent of status)
  running      -- registry status `busy` or `shell`, the only two the status
                  table calls conclusive
  idle         -- everything else, including the transient `waiting`, carrying
                  the transcript age

Precedence is problem -> needs-input -> running -> idle, so the classification is
total: every registry entry lands in exactly one bucket.

Both second signals come from `who-needs-me.py` rather than from a second
definition of "live" or "stuck": `read_registry()`, `load("tool")`,
`load("needs")`, `is_open_gate()`, `is_live()`, `quiet_session_ids()` and
`session_transcript_age()`.

⚠️ An empty result must never be read as a clean fleet. The coverage assertion
below is the positive control: it fails loudly (exit 1) if a transcript-fresh
session the registry carries is missing from the rows, which is exactly the
silent-drop failure this table exists to prevent.
"""

import argparse
import glob
import importlib.util
import json
import math
import os
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))


def _load(name, filename):
    """Import a sibling script by path — the filenames carry hyphens."""
    spec = importlib.util.spec_from_file_location(name, os.path.join(_HERE, filename))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


wnm = _load("who_needs_me", "who-needs-me.py")
fs = _load("fleet_sessions", "fleet-sessions.py")

# Session 26 · Bucket 16 · Vault task 34 · Project 11 · Last 9 -> sum 96, and
# `box-table.py` renders `sum(widths) + 3n + 1` = 112 of the operator's 119
# columns. The bucket column REPLACES the old Status column rather than joining
# it: a sixth column lands at 132, and a wrapping box is worse than a truncated
# cell. The raw busy/shell/idle counts stay in the marker line instead.
HEADER = ["Session", "Bucket", "Vault task", "Project", "Last"]
WIDTHS = [26, 16, 34, 11, 9]

BUCKET_ICON = {
    "running": "🔄",
    "idle": "⏸️",
    "needs-input": "⌛",
    "problem": "⚠️",
}

# The two statuses the fleet-status status table calls conclusive. Everything
# else — `waiting` (transient, explicitly NOT blocked-on-a-human) and a blank —
# falls through to `idle`, which is why the classification stays total.
RUNNING_STATUSES = ("busy", "shell")

BUCKET_ORDER = ("problem", "needs-input", "running", "idle")


def registry_records(sessions_dir=None):
    """`session id -> {status, name, cwd}` from the registry; `None` if unreadable.

    Read here rather than through `who-needs-me.read_registry()` because that
    helper returns only the id -> name map, and this table needs `status` as well.
    It is the same directory, so `coverage_errors()` cross-checks the live set
    against `read_registry()`'s keys — two readers of one source must not disagree
    about which entries exist.

    `None` is a distinct answer from `{}`: an unreadable registry cannot prove a
    fleet is empty, and reporting `{}` would render a clean table for a probe that
    simply failed.
    """
    d = sessions_dir or os.environ.get("SESSIONS_DIR") or wnm.SESSIONS_DIR
    if not os.path.isdir(d):
        return None
    try:
        out = {}
        for path in glob.glob(os.path.join(d, "*.json")):
            with open(path, encoding="utf-8") as f:
                rec = json.load(f)
            sid = rec.get("sessionId")
            if sid:
                out[sid] = {
                    "status": (rec.get("status") or "").strip(),
                    "name": rec.get("name") or "",
                    "cwd": rec.get("cwd") or "",
                }
        return out
    except Exception:
        return None


def classify(status, sid, stuck_ids, gate_ids):
    """The bucket for one registry entry. Precedence is deliberate and total.

    `problem` outranks `running` on purpose: a `busy` session inside one tool call
    for >= --stuck-min is the stuck case, and reporting it as `running` would hide
    the very thing the bucket exists to surface. `needs-input` outranks `running`
    for the same reason — a gate is a blocker whatever the status field says.
    """
    if sid in stuck_ids:
        return "problem"
    if sid in gate_ids:
        return "needs-input"
    if status in RUNNING_STATUSES:
        return "running"
    return "idle"


def collect_signals(stuck_min):
    """The two independent signals, keyed by session id.

    Mirrors `who-needs-me.py`'s own pipeline (its `main()`), so the gate predicate
    and the stuck predicate are the same ones the operator's `Needs you` section
    is built from — one definition, two renderings.
    """
    registry = wnm.read_registry()
    live_ids = None if registry is None else set(registry)
    records = wnm.load("needs")
    quiet = wnm.quiet_session_ids(records, live_ids)
    pmap = wnm.panes()
    live = lambda r: wnm.is_live(r, pmap, quiet)
    needs = [wnm.reclassify_idle(r) for r in records if live(r)]
    open_panes = {
        str(r.get("pane"))
        for r in needs
        if wnm.is_open_gate(r, task_status=wnm.task_status_from_closer)
    }
    gate_ids = {
        r["session_id"]
        for r in needs
        if wnm.is_open_gate(
            r, open_panes=open_panes, task_status=wnm.task_status_from_closer
        )
    }
    cutoff = time.time() - stuck_min * 60
    stuck_ids = {r["session_id"] for r in wnm.load("tool") if live(r) and r["ts"] < cutoff}
    return gate_ids, stuck_ids


def transcript_fresh_ids(window=None):
    """Session ids whose transcript was written within `window` seconds.

    The independently-derived half of the coverage assertion. Deliberately NOT the
    registry: a check that re-reads the source it is checking proves nothing.
    """
    window = wnm.LIVE_WINDOW if window is None else window
    now = time.time()
    out = set()
    for path in glob.glob(os.path.join(wnm.PROJECTS_DIR, "*", "*.jsonl")):
        try:
            if now - os.path.getmtime(path) < window:
                out.add(os.path.basename(path)[:-6])
        except OSError:
            continue
    return out


def coverage_errors(row_ids, registry_ids, fresh_ids):
    """SC3's assertion: (a) one row per registry entry, (b) no carried row dropped.

    (a) alone is tautological when the rows are built from the registry, so (b) is
    the real control — it fails if a row is silently dropped, which is the failure
    this table exists to prevent. (c), the residual, is returned separately rather
    than asserted on: a transcript-fresh session holding no registry entry is a
    real population (a headless worker holds no entry at all — it is an in-process
    SDK query, not a process), so it is reported, never dropped and never
    asserted equal.
    """
    errors = []
    if len(row_ids) != len(set(row_ids)):
        errors.append("duplicate rows for one session id")
    missing = registry_ids - set(row_ids)
    if missing:
        errors.append(f"registry entries with no row: {sorted(missing)}")
    extra = set(row_ids) - registry_ids
    if extra:
        errors.append(f"rows with no registry entry: {sorted(extra)}")
    dropped = (fresh_ids & registry_ids) - set(row_ids)
    if dropped:
        errors.append(f"transcript-fresh sessions missing from the rows: {sorted(dropped)}")
    return errors


def build_rows(registry, gate_ids, stuck_ids, task_titles, ages, widths=None):
    """One row per registry entry, sorted by bucket precedence then name.

    Pure: every input is passed in, so the bucket fixtures in
    `scripts/tests/test_fleet_board.py` exercise this directly without a live
    fleet.
    """
    widths = widths or WIDTHS
    rows, details = [], {}
    for sid, rec in registry.items():
        status = rec.get("status") or ""
        bucket = classify(status, sid, stuck_ids, gate_ids)
        # `session_transcript_age()` returns `inf` for a session with no transcript
        # — the honest "nothing has been written" answer — which is not a duration
        # and must render as an absent value, never as `inf`.
        age = ages.get(sid)
        last = "—" if age is None or not math.isfinite(age) else fs.human_age(age)
        titles = task_titles.get(sid) or []
        rows.append(
            {
                "session_id": sid,
                "bucket": bucket,
                "cells": [
                    # Glyph-stripped, matching `who-needs-me.py`'s display: the
                    # registry's `name` may carry a leading `⚙` the operator did not
                    # type, and a name rendered two ways across two readers of the
                    # same field is a defect waiting to be mistaken for a rename.
                    wnm.strip_status_glyph(rec.get("name") or "") or "—",
                    f"{BUCKET_ICON[bucket]} {bucket}",
                    titles[0] if titles else "—",
                    fs.project_label("-" + (rec.get("cwd") or "").strip("/").replace("/", "-")),
                    last,
                ],
            }
        )
        if bucket == "needs-input":
            details[sid] = "waiting on an open gate"
    rows.sort(key=lambda r: (BUCKET_ORDER.index(r["bucket"]), r["cells"][0].lower()))
    return rows, details


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--stuck-min", type=int, default=20)
    ap.add_argument("--json", action="store_true", help="emit the document, not just the box JSON")
    a = ap.parse_args()

    registry = registry_records()
    if registry is None:
        print("fleet-board: session registry unreadable — refusing to render a table", file=sys.stderr)
        return 1

    gate_ids, stuck_ids = collect_signals(a.stuck_min)
    task_titles = fs.build_work_map()
    ages = {sid: wnm.session_transcript_age(sid) for sid in registry}
    rows, details = build_rows(registry, gate_ids, stuck_ids, task_titles, ages)

    fresh = transcript_fresh_ids()
    errors = coverage_errors([r["session_id"] for r in rows], set(registry), fresh)
    counts = {b: sum(1 for r in rows if r["bucket"] == b) for b in BUCKET_ORDER}

    doc = {
        "header": HEADER,
        "rows": [r["cells"] for r in rows],
        "widths": WIDTHS,
        "counts": counts,
        "registry_count": len(registry),
        "row_count": len(rows),
        "residual": sorted(fresh - set(registry)),
        "details": details,
        "coverage_ok": not errors,
        "coverage_errors": errors,
    }
    print(json.dumps(doc if a.json else {k: doc[k] for k in ("header", "rows", "widths")}))

    if errors:
        # Loud, never silent: a table that renders correctly and omits sessions is
        # the exact failure this script exists to prevent.
        for e in errors:
            print(f"fleet-board: COVERAGE FAILURE: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
