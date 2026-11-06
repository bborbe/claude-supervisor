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
acted on. (Rule's single home: `agents/goal-sweep-reader.md` § step 4's
*Waiting-approval* bullet.)

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
import hashlib
import importlib.util
import json
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path

HERE = os.path.dirname(os.path.abspath(__file__))


def _load(name, filename):
    """Import a sibling script by path — the filenames carry hyphens."""
    spec = importlib.util.spec_from_file_location(name, os.path.join(HERE, filename))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


#: `defer_date` is an instant, not a date — see `defer-date.py`.
defer_date = _load("defer_date", "defer-date.py")

PLAN_DIR = "60 Periodic Notes"
CAP = 5
PROPOSAL_CAP = 3
TERMINAL = {"completed", "aborted"}

FM_LINE = re.compile(r"^([a-z_]+):\s*(.*)$")
# A block-style list entry under a key: `  - [[Some Task]]`. Column-0 keys never match
# FM_LINE when indented, so the two regexes cannot claim the same line.
FM_ITEM = re.compile(r"^\s+-\s+(.*)$")


# --------------------------------------------------------------------------- io


def read_text(path):
    """Return (text, None) or (None, reason). Never raises, never returns '' for a miss."""
    try:
        return Path(path).read_text(encoding="utf-8"), None
    except FileNotFoundError:
        return None, "missing"
    except (OSError, ValueError) as exc:
        # ValueError is load-bearing: a non-UTF-8 file raises UnicodeDecodeError, which
        # is NOT an OSError — catching only OSError crashed the every-tick reading
        # instead of rendering that plan as unread, the exact collapse the three-valued
        # rule forbids.
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
    pending = None
    for line in text[4:end].splitlines():
        item = FM_ITEM.match(line)
        if item and pending:
            more = item.group(1).strip().strip('"').strip("'")
            out[pending] = ", ".join(x for x in (out[pending], more) if x)
            continue
        m = FM_LINE.match(line)
        if m:
            key = m.group(1)
            value = m.group(2).strip().strip('"').strip("'")
            out[key] = value
            # A key with no inline value may carry a BLOCK-style list, and `blocked_by`
            # is written as a block even for a single entry (agents/goal-sweep-reader.md).
            # An inline-only read returns "" for it, so the hold-goal proposal class
            # silently never fired — the value's absence read as "no blocker".
            pending = key if not value else None
            continue
        pending = None
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
    """The page names a line links to — `#heading`, an alias and `.md` all stripped.

    A plan writes `[[Some Page#Section|label]]` as often as a bare `[[Some Page]]`, and
    the raw text is not a filename: left unstripped it resolves to no page and the row is
    silently dropped into `notes` instead of ranked.
    """
    return [link_target(raw) for raw in re.findall(r"\[\[([^\]|]+)", text)]


def link_target(raw):
    name = raw.split("#", 1)[0].strip()
    if name.lower().endswith(".md"):
        name = name[:-3]
    return name.rsplit("/", 1)[-1].strip()


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
            # Any enclosing heading closes the item. `# ` alone was not enough: a `##`
            # section between two numbered items left `cur` set, so a later `**Task:**`
            # was appended to the PREVIOUS item and the citation pointed at its line.
            if re.match(r"^#{1,2}\s", line):
                cur = None
                continue
            if line.startswith("**Task:**"):
                cur["names"] = wikilinks(line)
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
        # Containment: a plan page is vault content and can write any wikilink it likes.
        # Without this a `[[../../etc/passwd]]` link resolves outside every board dir.
        if not name or name.startswith(("/", "~")) or ".." in Path(name).parts:
            return None, None
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
    """Return (live, target, source). `unknown` is a value, never `0`.

    Each half is filled independently: a caller passing only `--live` must not have that
    value silently replaced by a re-run of the script it was meant to override.
    """
    script_dir = Path(plugin) / "scripts"
    from_flags = live is not None and target is not None
    try:
        if live is None:
            live = int(subprocess.run(
                [sys.executable, str(script_dir / "worker-sessions.py"), "--count"],
                capture_output=True, text=True, timeout=30).stdout.strip())
        if target is None:
            target = int(subprocess.run(
                [sys.executable, str(script_dir / "worker-target.py")],
                capture_output=True, text=True, timeout=30).stdout.strip())
    except (OSError, ValueError, subprocess.SubprocessError):
        # Keep whatever WAS read or supplied: a `--live 3` that the caller passed must not
        # be thrown away because the other half failed to resolve.
        return live, target, "unknown"
    return live, target, "flags" if from_flags else "scripts"


def read_alerts(vault, script=None):
    """`firing` / `clear` / `unread` / `unavailable` — never a number that could be `0`.

    ⚠️ **TRUST BOUNDARY, stated because it is load-bearing:** the default script lives
    INSIDE the vault, and the vault is writable by the workers this reading runs
    alongside. Executing it here therefore runs vault-resident code in the manager
    session every tick. That is accepted rather than accidental — the operator's own
    alert source is a vault script, and the same session already runs the vault's
    `sweep-gate.py` — but a caller that keeps its alert script outside the vault passes
    `--alerts-script <path>` and this reading never executes vault content at all.
    """
    script = Path(script) if script else (
        Path(vault) / ".claude" / "scripts" / "prometheus-alerts.sh")
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
        count = int(m.group(1))
        if count == 0:
            return "clear", []
        # The count line is a summary, not an alert: naming the proposal card after it
        # would ask the operator to add "total active alerts: 3" to the weekly plan.
        detail = [ln for ln in lines if not ln.lower().startswith("total active alerts:")]
        return "firing", detail[:3] or [f"{count} active alerts"]
    return "unread", []


# -------------------------------------------------------------------- ranking


def card_key(vault, kind, name):
    """The dedup key for one proposal card — stable across rounds, distinct per item.

    Stable on purpose: the store's suppression is open-scoped, so a key that moved on a
    re-post of an unchanged item would ask the operator the same question every round.
    """
    slug = re.sub(r"[^a-z0-9]+", "-", Path(vault).name.lower()).strip("-")
    item = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    # The slug ALONE collides: `A B` and `A-B` both become `a-b`, and the store
    # suppresses a re-push on producer_id + dedup_key — so the second proposal's card
    # was silently dropped, the exact "ask vanishes unasked" outcome the cap exists to
    # prevent. The digest is deterministic in the raw name, so the key stays stable
    # across rounds while distinct names stay distinct.
    digest = hashlib.sha256(name.encode("utf-8")).hexdigest()[:8]
    return f"next-focus:{slug}:{kind}:{item}-{digest}"


def candidate(name, kind, fm, source, why):
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
        row["action"] = f"/supervisor:open {shlex.quote(name)}"
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
        items = parser(text)
        # A tier with no parseable item is a fact about that tier, not a pass.
        if not items:
            notes.append(f"{period} {keys[period]}: no parseable items")
        for item in items:
            for name in item["names"]:
                kind, _, fm = board.read(name)
                if kind is None:
                    notes.append(f"{name}: no page in tasks/goals/topics")
                    continue
                out.append(candidate(
                    name, kind, fm,
                    {"period": period, "file": str(path), "line": item["line"]},
                    item.get("title", ""),
                ))
    return out


def due_today(board, tasks_dir, day, now=None):
    """Tasks deferred or flagged for `day` — the highest rank there is."""
    iso = day.isoformat()
    # `now` is the instant the read is taken at; absent, it is the CURRENT instant, never
    # `day`'s start — local midnight would park a defer that already came due today.
    moment = defer_date.as_now(now)
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
        # `defer_date` is defer-UNTIL, not a due date: a row carrying a future one is
        # deliberately parked and must not surface, however old its `due_date` is. The two
        # dates answer different questions, so they are read apart rather than min()-ed —
        # collapsing them labelled a parked task "overdue since <due>" and an active task
        # "overdue since <defer>".
        defer_state, defer_reason = defer_date.defer_state(fm.get("defer_date"), moment)
        if defer_state == "invalid":
            # Unreadable is not "due now": the row is withheld, and the reason is said.
            print(f"next-focus: {defer_reason} — treating as deferred", file=sys.stderr)
        defer = defer_date.display(fm.get("defer_date"))
        due_d = str(fm.get("due_date", ""))[:10]
        parked = defer_state in ("deferred", "invalid")
        overdue = bool(due_d) and due_d <= iso
        active = defer_state == "due"
        due = not parked and (overdue or active)
        flagged = fm.get("flag") == "true" and fm.get("flag_set_by") == "operator"
        if not (due or flagged):
            continue
        if overdue and due_d < iso:
            why = f"overdue since {due_d}"
        elif due and (due_d == iso or defer == iso):
            why = "due today"
        elif due:
            why = f"active since {defer}"
        else:
            why = "operator-flagged"
        out.append(candidate(
            path.stem, "task", fm,
            {"period": "due-today", "file": str(path), "line": 1},
            why,
        ))
    return out


def proposed(vault, board, tasks_dir, planned_keys, alerts_state, alerts_lines):
    """Important items in NO current plan — at most PROPOSAL_CAP, one card each.

    `planned_keys` is a set of `(kind, name)`, not bare names: a planned GOAL called X
    must not suppress an unplanned `priority: 1` TASK called X, which a name-only set did.
    """
    rows = []
    # class 1: a firing prod alert — FIRST, and it can never be capped out.
    # Appended after the P1 tasks it was ranked behind, it fell into the overflow
    # whenever three or more unplanned P1 rows existed (38 were open on 2026-10-21),
    # and the round surfaced it nowhere: not as a proposal, not as a named overflow,
    # not as an `alerts:` line (which prints only for unread/unavailable).
    if alerts_state == "firing":
        rows.append({
            "kind": "alert",
            "name": alerts_lines[0] if alerts_lines else "prod alert firing",
            "source": {"period": "unplanned", "file": "prometheus-alerts.sh", "line": 1},
            "why": "alert firing, in no current plan",
            "phase": "", "status": "", "needs_approval": False,
            "action": "add to this week's plan",
        })
    # class 2: open P1 tasks
    d = Path(tasks_dir)
    if d.is_dir():
        for path in sorted(d.glob("*.md")):
            text, err = read_text(path)
            if err:
                continue
            fm = frontmatter(text)
            if fm.get("priority") != "1" or fm.get("status") in TERMINAL:
                continue
            if ("task", path.stem) in planned_keys:
                continue
            rows.append(candidate(
                path.stem, "task", fm,
                {"period": "unplanned", "file": str(path), "line": 1},
                "priority: 1, in no current plan",
            ))
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
            if ("goal", path.stem) in planned_keys:
                continue
            rows.append(candidate(
                path.stem, "goal", fm,
                {"period": "unplanned", "file": str(path), "line": 1},
                f"blocked on {fm['blocked_by']}, in no current plan",
            ))
    for row in rows:
        row["card_key"] = card_key(vault, row["kind"], row["name"])
        # Shell-quoted HERE, because the round pastes this into a `bash` command line.
        # `name` is a raw line of prometheus-alerts.sh stdout for an alert row, and a
        # task file's stem for a task/goal row — the latter is chosen by any worker that
        # can write a task file. Unquoted, a `"` breaks the caller's quoting and a
        # `$(…)` or backtick inside it EXECUTES when the round runs the command.
        row["payload"] = shlex.quote(
            f'{row["name"]} — {row["why"]} — '
            f'{row["source"]["file"]}:{row["source"]["line"]}'
        )
    return rows


# ---------------------------------------------------------------------- main


def build(vault, tasks_dir, day, live=None, target=None, plugin=None, alerts_script=None,
          now=None):
    board = Board(vault, tasks_dir)
    notes = []

    live, target, capacity_source = read_capacity(live, target, plugin)
    alerts_state, alerts_lines = read_alerts(vault, alerts_script)

    today = due_today(board, Path(vault) / tasks_dir, day, now=now)
    if today is None:
        notes.append(f"tasks dir {tasks_dir}: missing")
        today = []

    planned = plan_candidates(vault, board, day, notes)
    ranked = today + planned

    seen, ordered = set(), []
    for row in ranked:
        # Keyed on (kind, name): a task and a goal can share a filename, and deduping on
        # the name alone silently dropped whichever sorted second.
        key = (row["kind"], row["name"])
        if key in seen:
            continue
        seen.add(key)
        ordered.append(row)

    overflow = ordered[CAP:]
    emitted = ordered[:CAP]
    for i, row in enumerate(emitted, 1):
        row["rank"] = i

    proposed_rows = proposed(
        vault, board, Path(vault) / tasks_dir,
        {(row["kind"], row["name"]) for row in ordered}, alerts_state, alerts_lines,
    )
    proposed_overflow = proposed_rows[PROPOSAL_CAP:]

    result = {
        "as_of": day.isoformat(),
        "capacity": {
            "live": live, "target": target, "source": capacity_source,
            # `target: 0` is the documented "unlimited" form, never "at target".
            "below_cap": (live is not None and target is not None
                          and (target == 0 or live < target)),
        },
        "alerts": alerts_state,
        "candidates": emitted,
        "overflow": [r["name"] for r in overflow],
        "proposed": proposed_rows[:PROPOSAL_CAP],
        "proposed_overflow": [r["name"] for r in proposed_overflow],
        "notes": list(dict.fromkeys(notes)),
    }
    # The block rides in the JSON too: a round that must post proposal cards needs the
    # card keys AND the rendered block, and without this it ran the whole script twice
    # per tick — re-globbing the task board and re-running both helpers and the alerts
    # script each time.
    result["render"] = render(result)
    return result


def render(result):
    cap = result["capacity"]
    if cap["live"] is None or cap["target"] is None:
        head = "Next focus: held — workers unknown"
    elif cap["target"] == 0:
        head = f"Next focus ({cap['live']}/unlimited)"
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
        # NAMED, not a bare count: readings.md promises "the remainder is named", and a
        # bare `(cap 3)` line left a dropped item indistinguishable from one never found.
        named = ", ".join(result["proposed_overflow"][:5])
        rest = len(result["proposed_overflow"]) - 5
        tail = f" and {rest} more" if rest > 0 else ""
        lines.append(f'  left for the next round (cap {PROPOSAL_CAP}): {named}{tail}')
    if result["alerts"] in ("unread", "unavailable"):
        lines.append(f'  alerts: {result["alerts"]}')
    for note in result["notes"]:
        lines.append(f"  note: {note}")
    return "\n".join(lines)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Rank the next focus from the periodic plans.")
    ap.add_argument("--vault", required=True)
    ap.add_argument("--tasks-dir", default="25 Tasks")
    ap.add_argument("--now", default=None,
                    help="ISO date or datetime; default now (a date means its local start)")
    ap.add_argument("--live", type=int, default=None)
    ap.add_argument("--target", type=int, default=None)
    ap.add_argument("--plugin", default=os.environ.get("CLAUDE_PLUGIN_ROOT") or str(Path(__file__).resolve().parent.parent))
    ap.add_argument("--alerts-script", default=None,
                    help="alert script to run; default is <vault>/.claude/scripts/prometheus-alerts.sh")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    if not Path(args.vault).is_dir():
        print(f"next-focus: REFUSED — vault not found: {args.vault}", file=sys.stderr)
        return 2
    try:
        moment = defer_date.as_now(args.now)
    except ValueError:
        print(f"next-focus: REFUSED — bad --now: {args.now}", file=sys.stderr)
        return 2
    day = moment.date()

    try:
        result = build(args.vault, args.tasks_dir, day, args.live, args.target,
                       args.plugin, args.alerts_script, now=moment)
    except Exception as exc:  # noqa: BLE001 — deliberate: the tick must always render
        # Any non-zero exit is a REFUSAL the round must render, so an unexpected bug
        # has to reach that contract. Unguarded, it exited 1 with a bare traceback —
        # a state Output shape item 2 defines no render for, which is how a broken
        # reading becomes an omitted block.
        print(f"next-focus: FAILED — {exc.__class__.__name__}: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2) if args.json else result["render"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
