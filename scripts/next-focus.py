#!/usr/bin/env python3
"""Rank the next focus from the operator's own periodic plans.

Why this exists
---------------
A fleet round below its worker target had no way to answer "what should the free slot
do?". It swept live sessions and classified them; it never read the pages the operator
writes to rank work. So a free slot went to whatever happened to be filed last, and
today's most important item could wait behind a lower one.

This script reads the four current periodic plans and the task board and emits ONE
ranked list. It is read-only: it opens nothing, posts nothing and edits nothing. The
round renders its output and owns every act.

Ranking, first match winning
----------------------------
    1. a task due or flagged today
    2. the current weekly plan
    3. the current monthly plan
    4. the current quarterly plan
    5. the current yearly plan

A goal or topic sorts into the tier of the plan level that names it. One named by no
plan is never a *planned* candidate — it can only surface under `proposed`.

Two things this script will not do
----------------------------------
**It never opens a `phase: todo` row.** `todo` is the operator's approval inbox: agents
file there and stop, and the operator's `todo -> planning` flip *is* the approval. Such a
row renders `needs your approval`, carries no `action` field, and is reported rather than
acted on. (Rule's single home: the Fleet Manager Session runbook, Manager contract.)

**It never renders an unreadable read as an empty one.** A plan file that cannot be read
and an alert read that failed both surface as `unread`/`unavailable`; neither collapses
into "nothing to do". A round that found nothing and a round that could not look must not
look the same.

Usage
-----
    next-focus.py --vault <vault> --tasks-dir "25 Tasks" [--now ISO] [--json]
    next-focus.py ... --live 3 --target 30     # override the capacity read

Exit codes: 0 emitted · 2 refused (bad input, unreadable vault).
"""
import argparse
import datetime as dt
import json
import os
import re
import subprocess
import sys
from pathlib import Path

PLAN_DIR = "60 Periodic Notes"
CAP = 5
PROPOSAL_CAP = 3
TERMINAL = {"completed", "aborted"}

FM_LINE = re.compile(r"^([a-z_]+):\s*(.*)$")


# --------------------------------------------------------------------------- io


def read_text(path):
    """Return (text, None) or (None, reason). Never raises, never returns '' for a miss."""
    try:
        return Path(path).read_text(encoding="utf-8"), None
    except FileNotFoundError:
        return None, "missing"
    except OSError as exc:
        return None, f"unreadable ({exc.__class__.__name__})"


def frontmatter(text):
    """Read the leading `---` block only.

    Frontmatter-only on purpose: task bodies quote these keys in prose, so a whole-file
    scan reads a row as parked on the strength of a sentence *about* parking.
    """
    if not text.startswith("---\n"):
        return {}
    end = text.find("\n---", 4)
    if end < 0:
        return {}
    out = {}
    for line in text[4:end].splitlines():
        m = FM_LINE.match(line)
        if m:
            out[m.group(1)] = m.group(2).strip().strip('"').strip("'")
    return out


# ------------------------------------------------------------------- periods


def iso_week(day):
    year, week, _ = day.isocalendar()
    return f"{year}-W{week:02d}"


def period_keys(day):
    return {
        "weekly": iso_week(day),
        "monthly": f"{day.year}-{day.month:02d}",
        "quarterly": f"{day.year}-Q{(day.month - 1) // 3 + 1}",
        "yearly": f"{day.year}",
    }


def plan_path(vault, period, key):
    return Path(vault) / PLAN_DIR / period.capitalize() / f"{key}.md"


# -------------------------------------------------------------- plan parsing


def wikilinks(text):
    return re.findall(r"\[\[([^\]|]+)", text)


def parse_weekly(text):
    """`### N. <title>` blocks, each carrying `**Task:** [[name]]`.

    The heading line number is the citation: it is where a reader lands.
    """
    rows, cur = [], None
    for i, line in enumerate(text.splitlines(), 1):
        m = re.match(r"^###\s+(\d+)\.\s+(.*)$", line)
        if m:
            cur = {"title": m.group(2).strip(), "line": i, "names": []}
            rows.append(cur)
            continue
        if cur is not None:
            if line.startswith("# "):
                cur = None
                continue
            if line.startswith("**Task:**"):
                cur["names"] = wikilinks(line)
                prio = re.search(r"🔴|HIGH", line)
                cur["high"] = bool(prio)
            elif line.startswith("**Priority:**"):
                cur["high"] = "HIGH" in line
    return [r for r in rows if r["names"]]


def parse_monthly(text):
    """`## Must / Should / Could` numbered items, `**Must**` before `**Should**` before `**Could**`."""
    rows, bucket = [], None
    for i, line in enumerate(text.splitlines(), 1):
        if line.startswith("## "):
            bucket = None
            if "Must" in line or "Should" in line or "Could" in line:
                bucket = "list"
            continue
        if bucket != "list":
            continue
        m = re.match(r"^(\d+)\.\s+(.*)$", line)
        if not m:
            continue
        # First link only: a later `[[…]]` on the same line is usually the theme or a
        # supporting page, and ranking it would offer the theme as work.
        names = wikilinks(m.group(2))[:1]
        if names:
            rows.append({"title": m.group(2)[:80], "line": i, "names": names})
    return rows


def parse_quarterly(text):
    """The `Q4 Objectives` numbered list — `(P1)` before `(P2)`."""
    rows, inside = [], False
    for i, line in enumerate(text.splitlines(), 1):
        if re.match(r"^\*\*🚀 .*Objectives", line) or "Objectives (projects)" in line:
            inside = True
            continue
        if inside and line.startswith("# "):
            inside = False
        if not inside:
            continue
        m = re.match(r"^(\d+)\.\s+(.*)$", line)
        if not m:
            continue
        names = wikilinks(m.group(2))[:1]
        if names:
            rows.append({"title": m.group(2)[:80], "line": i, "names": names})
    return rows


def parse_yearly(text):
    """`## <year> Targets` subsections — one row per subsection, cited at its heading.

    The subsection's first wikilink is the item; the heading carries the theme it sits
    under, which is context rather than a candidate.
    """
    rows, inside, cur = [], False, None
    for i, line in enumerate(text.splitlines(), 1):
        if re.match(r"^##\s+\d{4}\s+Targets", line):
            inside, cur = True, None
            continue
        if inside and re.match(r"^##\s+[^#]", line):
            inside, cur = False, None
        if not inside:
            continue
        m = re.match(r"^###\s+(.*)$", line)
        if m:
            cur = {"title": m.group(1).strip(), "line": i, "names": []}
            rows.append(cur)
            continue
        if cur is not None and not cur["names"]:
            links = wikilinks(line)
            if links:
                cur["names"] = links[:1]
    return [r for r in rows if r["names"]]


# -------------------------------------------------------------------- lookup


class Board:
    """The vault's task/goal/topic pages, read lazily and cached."""

    def __init__(self, vault, tasks_dir):
        self.vault = Path(vault)
        self.dirs = {
            "task": self.vault / tasks_dir,
            "goal": self.vault / "24 Goals",
            "objective": self.vault / "22 Objectives",
            "topic": self.vault / "23 Topics",
        }
        self._cache = {}

    def find(self, name):
        for kind, d in self.dirs.items():
            path = d / f"{name}.md"
            if path.exists():
                return kind, path
        return None, None

    def read(self, name):
        if name in self._cache:
            return self._cache[name]
        kind, path = self.find(name)
        if path is None:
            self._cache[name] = (None, None, None)
            return self._cache[name]
        text, err = read_text(path)
        fm = frontmatter(text) if text else {}
        self._cache[name] = (kind, path, fm if not err else None)
        return self._cache[name]


# ------------------------------------------------------------------ capacity


def read_capacity(live, target, plugin):
    """Return (live, target, source). `unknown` is a value, never `0`."""
    if live is not None and target is not None:
        return live, target, "flags"
    script_dir = Path(plugin) / "scripts"
    try:
        live = int(subprocess.run(
            [sys.executable, str(script_dir / "worker-sessions.py"), "--count"],
            capture_output=True, text=True, timeout=30).stdout.strip())
        target = int(subprocess.run(
            [sys.executable, str(script_dir / "worker-target.py")],
            capture_output=True, text=True, timeout=30).stdout.strip())
        return live, target, "scripts"
    except (OSError, ValueError, subprocess.SubprocessError):
        return None, None, "unknown"


def read_alerts(vault):
    """`firing` / `clear` / `unread` / `unavailable` — never a number that could be `0`."""
    script = Path(vault) / ".claude" / "scripts" / "prometheus-alerts.sh"
    if not script.exists():
        return "unavailable", []
    try:
        proc = subprocess.run(["bash", str(script)], capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return "unread", []
    if proc.returncode != 0:
        return "unread", []
    lines = [ln.strip() for ln in proc.stdout.splitlines() if ln.strip()]
    total = [ln for ln in lines if ln.lower().startswith("total active alerts:")]
    if total:
        m = re.search(r"(\d+)", total[-1])
        if not m:
            return "unread", []
        return ("clear" if int(m.group(1)) == 0 else "firing"), lines[:3]
    return "unread", []


# -------------------------------------------------------------------- ranking


def card_key(vault, kind, name):
    """The dedup key for one proposal card — stable across rounds, distinct per item.

    Stable on purpose: the store's suppression is open-scoped, so a key that moved on a
    re-post of an unchanged item would ask the operator the same question every round.
    """
    slug = re.sub(r"[^a-z0-9]+", "-", Path(vault).name.lower()).strip("-")
    item = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return f"next-focus:{slug}:{kind}:{item}"


def candidate(name, kind, path, fm, source, why):
    phase = (fm or {}).get("phase", "")
    status = (fm or {}).get("status", "")
    row = {
        "kind": kind,
        "name": name,
        "source": source,
        "why": why,
        "phase": phase,
        "status": status,
        "needs_approval": phase == "todo",
    }
    # A `todo` row carries NO action field at all — not an empty one. An empty action
    # reads as "acted on and found nothing to do"; the key's absence is the rule.
    if not row["needs_approval"]:
        row["action"] = f'/supervisor:open "{name}"'
    return row


def plan_candidates(vault, board, day, notes):
    """Walk the four tiers in rank order, returning (rows, unread list)."""
    keys = period_keys(day)
    tiers = [
        ("weekly", parse_weekly),
        ("monthly", parse_monthly),
        ("quarterly", parse_quarterly),
        ("yearly", parse_yearly),
    ]
    out = []
    for period, parser in tiers:
        path = plan_path(vault, period, keys[period])
        text, err = read_text(path)
        if err:
            notes.append(f"{period} {keys[period]}: {err}")
            continue
        for item in parser(text):
            for name in item["names"]:
                kind, ppath, fm = board.read(name)
                if kind is None:
                    notes.append(f"{name}: no page in tasks/goals/topics")
                    continue
                out.append(candidate(
                    name, kind, ppath, fm,
                    {"period": period, "file": str(path), "line": item["line"]},
                    item.get("title", ""),
                ))
        # A tier with no parseable item is a fact about that tier, not a pass.
        if not parser(text):
            notes.append(f"{period} {keys[period]}: no parseable items")
    return out


def due_today(board, tasks_dir, day):
    """Tasks deferred or flagged for `day` — the highest rank there is."""
    iso = day.isoformat()
    out = []
    d = Path(tasks_dir)
    if not d.is_dir():
        return None
    for path in sorted(d.glob("*.md")):
        text, err = read_text(path)
        if err:
            continue
        fm = frontmatter(text)
        if fm.get("status") in TERMINAL:
            continue
        due = fm.get("defer_date", "")[:10] == iso or fm.get("due_date", "")[:10] == iso
        flagged = fm.get("flag") == "true" and fm.get("flag_set_by") == "operator"
        if not (due or flagged):
            continue
        out.append(candidate(
            path.stem, "task", path, fm,
            {"period": "due-today", "file": str(path), "line": 1},
            "due today" if due else "operator-flagged",
        ))
    return out


def proposed(vault, board, tasks_dir, planned_names, alerts_state, alerts_lines, day):
    """Important items in NO current plan — at most PROPOSAL_CAP, one card each."""
    rows = []
    # class 1: open P1 tasks
    d = Path(tasks_dir)
    if d.is_dir():
        for path in sorted(d.glob("*.md")):
            text, err = read_text(path)
            if err:
                continue
            fm = frontmatter(text)
            if fm.get("priority") != "1" or fm.get("status") in TERMINAL:
                continue
            if path.stem in planned_names:
                continue
            rows.append(candidate(
                path.stem, "task", path, fm,
                {"period": "unplanned", "file": str(path), "line": 1},
                "priority: 1, in no current plan",
            ))
    # class 2: a firing prod alert
    if alerts_state == "firing":
        rows.append({
            "kind": "alert",
            "name": alerts_lines[0] if alerts_lines else "prod alert firing",
            "source": {"period": "unplanned", "file": "prometheus-alerts.sh", "line": 1},
            "why": "alert firing, in no current plan",
            "phase": "", "status": "", "needs_approval": False,
            "action": "add to this week's plan",
        })
    # class 3: a goal blocked on a bug
    goals = Path(board.dirs["goal"])
    if goals.is_dir():
        for path in sorted(goals.glob("*.md")):
            text, err = read_text(path)
            if err:
                continue
            fm = frontmatter(text)
            if fm.get("status") != "hold" or not fm.get("blocked_by"):
                continue
            if path.stem in planned_names:
                continue
            rows.append(candidate(
                path.stem, "goal", path, fm,
                {"period": "unplanned", "file": str(path), "line": 1},
                f"blocked on {fm['blocked_by']}, in no current plan",
            ))
    for row in rows:
        row["card_key"] = card_key(vault, row["kind"], row["name"])
    return rows


# ---------------------------------------------------------------------- main


def build(vault, tasks_dir, day, live=None, target=None, plugin=None):
    board = Board(vault, tasks_dir)
    notes = []

    live, target, capacity_source = read_capacity(live, target, plugin)
    alerts_state, alerts_lines = read_alerts(vault)

    today = due_today(board, Path(vault) / tasks_dir, day)
    if today is None:
        notes.append(f"tasks dir {tasks_dir}: missing")
        today = []

    planned = plan_candidates(vault, board, day, notes)
    ranked = today + planned

    seen, ordered = set(), []
    for row in ranked:
        if row["name"] in seen:
            continue
        seen.add(row["name"])
        ordered.append(row)

    overflow = ordered[CAP:]
    emitted = ordered[:CAP]
    for i, row in enumerate(emitted, 1):
        row["rank"] = i

    proposed_rows = proposed(vault, board, Path(vault) / tasks_dir, seen, alerts_state, alerts_lines, day)
    proposed_overflow = proposed_rows[PROPOSAL_CAP:]

    return {
        "as_of": day.isoformat(),
        "capacity": {
            "live": live, "target": target, "source": capacity_source,
            "below_cap": (live is not None and target is not None and live < target),
        },
        "alerts": alerts_state,
        "candidates": emitted,
        "overflow": [r["name"] for r in overflow],
        "proposed": proposed_rows[:PROPOSAL_CAP],
        "proposed_overflow": [r["name"] for r in proposed_overflow],
        "notes": notes,
    }


def render(result):
    cap = result["capacity"]
    if cap["live"] is None or cap["target"] is None:
        head = "Next focus: held — workers unknown"
    elif not cap["below_cap"]:
        head = f"Next focus: held — at target ({cap['live']}/{cap['target']})"
    else:
        head = f"Next focus ({cap['live']}/{cap['target']})"
    lines = [head]

    for row in result["candidates"]:
        src = row["source"]
        # A due-today row cites its own task file, not a plan line: there is no plan line
        # for it, and inventing one would make the citation unresolvable.
        tag = src["file"] if src["period"] == "due-today" else f'{src["file"]}:{src["line"]}'
        if row["needs_approval"]:
            lines.append(f'  {row["rank"]}. {row["name"]} — needs your approval — {tag}')
        else:
            lines.append(f'  {row["rank"]}. {row["name"]} — {tag} — {row["why"]}')
    if not result["candidates"]:
        lines.append("  (no candidate: " + "; ".join(result["notes"] or ["plans empty"]) + ")")

    if result["overflow"]:
        lines.append(f'  left for the next round (cap {CAP})')
    if result["proposed"]:
        lines.append("Proposed for this week's plan:")
        for row in result["proposed"]:
            lines.append(f'  - {row["name"]} — {row["why"]}')
    if result["proposed_overflow"]:
        lines.append(f'  left for the next round (cap {PROPOSAL_CAP})')
    if result["alerts"] in ("unread", "unavailable"):
        lines.append(f'  alerts: {result["alerts"]}')
    for note in result["notes"]:
        lines.append(f"  note: {note}")
    return "\n".join(lines)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Rank the next focus from the periodic plans.")
    ap.add_argument("--vault", required=True)
    ap.add_argument("--tasks-dir", default="25 Tasks")
    ap.add_argument("--now", default=None, help="ISO date or datetime; default today")
    ap.add_argument("--live", type=int, default=None)
    ap.add_argument("--target", type=int, default=None)
    ap.add_argument("--plugin", default=os.environ.get("CLAUDE_PLUGIN_ROOT") or str(Path(__file__).resolve().parent.parent))
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    if not Path(args.vault).is_dir():
        print(f"next-focus: REFUSED — vault not found: {args.vault}", file=sys.stderr)
        return 2
    try:
        day = dt.date.fromisoformat(args.now[:10]) if args.now else dt.date.today()
    except ValueError:
        print(f"next-focus: REFUSED — bad --now: {args.now}", file=sys.stderr)
        return 2

    result = build(args.vault, args.tasks_dir, day, args.live, args.target, args.plugin)
    print(json.dumps(result, indent=2) if args.json else render(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
