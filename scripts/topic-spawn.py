#!/usr/bin/env python3
"""The topic tier's spawn gate: may THIS topic start a goal manager right now?

`topic-spawn` is the one verb a topic manager owns that starts a session, and it starts
**exactly one level below itself** — a goal manager, never a worker. Every refusal is a
named token on the **first line of stdout**, so a probe can assert on it without parsing
prose, and the exit code is non-zero for every one of them.

  goal-not-in-scope      the goal is not in the topic page's declared `## Goals`
  goal-not-startable     the goal's `phase` is not `planning` or `execution`
  manager-cap            a bound is reached; the SECOND line names which:
                           goal-cap      — this topic already runs N goal managers
                           manager-cap   — the fleet-wide manager ceiling is reached
  workers are not this tier's to start

Exit 0 = allowed (prints `spawn-ok: <goal>` and, with `--print`, the `/supervisor:open`
line to run). Exit 2 = usage error, which is never a verdict.

⚠️ **The fleet ceiling always wins over N.** N = 3 is a per-topic share; the fleet-wide
`spawn.maxManagers` is the outer bound, and six topics at N = 3 would want eighteen
managers against a ceiling of ten. The cap is never raised to make room.

⚠️ **Startability is the operator's approval, read from disk, never inferred.** A goal at
`phase: todo` has not been approved; the `todo → planning` flip is the operator's own act
and this gate must not perform or assume it.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GOALS_DIR = "24 Goals"
DEFAULT_TOPICS_DIR = "23 Topics"
DEFAULT_N = 3
STARTABLE_PHASES = ("planning", "execution")
TERMINAL_STATUSES = ("completed", "aborted")

REFUSE_WORKER = "workers are not this tier's to start"
REFUSE_SCOPE = "goal-not-in-scope"
REFUSE_STARTABLE = "goal-not-startable"
REFUSE_CAP = "manager-cap"


def refuse(token: str, detail: str, bound: str | None = None) -> int:
    print(token)
    if bound:
        print(bound)
    print(detail)
    return 1


def frontmatter(text: str) -> str:
    if not text.startswith("---"):
        return ""
    end = text.find("\n---", 3)
    return text[3:end] if end != -1 else ""


def scalar(fm: str, key: str) -> str:
    m = re.search(rf"^{re.escape(key)}:\s*(.*)$", fm, re.MULTILINE)
    return (m.group(1).strip().strip("\"'") if m else "")


def section(text: str, heading: str) -> str:
    """The body under `## <heading>`, up to the next `## ` or `# ` heading."""
    m = re.search(rf"^##\s+{re.escape(heading)}\s*$", text, re.MULTILINE)
    if not m:
        return ""
    rest = text[m.end():]
    stop = re.search(r"^#{1,2}\s", rest, re.MULTILINE)
    return rest[: stop.start()] if stop else rest


def declared_goals(page: str) -> list[str]:
    """The goal names in the topic page's `## Goals` list, in declaration order.

    Handles a `[[wikilink]]` entry and a bare name alike, and strips a trailing
    ` — note` / ` (note)` the page may carry after the name.
    """
    body = section(page, "Goals")
    names = []
    for line in body.splitlines():
        s = line.strip()
        if not s.startswith(("-", "*")):
            continue
        s = re.sub(r"^[-*]\s+", "", s)
        m = re.search(r"\[\[([^\]|#]+)", s)
        name = m.group(1) if m else re.split(r"\s+[—–-]\s+|\s+\(", s, maxsplit=1)[0]
        name = name.strip().strip("\"'")
        if name:
            names.append(name)
    return names


def resolve_page(directory: str, name: str) -> str | None:
    """Exact basename, case-insensitive — `docs/subject-resolution.md` § The page test."""
    try:
        entries = os.listdir(directory)
    except OSError:
        return None
    want = name.lower() + ".md"
    for entry in entries:
        if entry.lower() == want:
            return os.path.join(directory, entry)
    return None


def live_goal_managers(vault: str, topics_dir: str, goals: list[str]) -> int:
    """Goal managers running on THIS topic, from each goal page's `manager_session_id`.

    § 4's own detection signal: the resolve step writes the id, and
    `session-liveness.py --check` answers LIVE / ABSENT / UNKNOWN. Only LIVE counts —
    an UNKNOWN is not a dead manager, but it is also not a running one, and counting it
    would refuse a start on evidence nothing produced.
    """
    count = 0
    for goal in goals:
        page = resolve_page(os.path.join(vault, GOALS_DIR), goal)
        if not page:
            continue
        try:
            with open(page, encoding="utf-8") as fh:
                sid = scalar(frontmatter(fh.read()), "manager_session_id")
        except OSError:
            continue
        if not sid:
            continue
        probe = subprocess.run(
            [sys.executable, os.path.join(REPO, "scripts", "session-liveness.py"),
             "--check", sid],
            capture_output=True, text=True,
        )
        if probe.returncode == 0:
            count += 1
    return count


def fleet_manager_ceiling() -> int | None:
    """`spawn.maxManagers` as the server resolves it, or None when it cannot be read."""
    cfg = os.path.expanduser("~/.config/claude-supervisor/config.json")
    try:
        with open(cfg, encoding="utf-8") as fh:
            spawn = (json.load(fh) or {}).get("spawn") or {}
    except (OSError, ValueError):
        return None
    value = spawn.get("maxManagers")
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return None
    return value


def fleet_manager_count() -> int | None:
    """Live managers fleet-wide, the fleet manager excluded. None when unreadable.

    Read from `fleet-board.py --json`, whose per-session `role` is the classification
    the cap counts — never from a name suffix, which a worker's label can also carry.
    """
    probe = subprocess.run(
        [sys.executable, os.path.join(REPO, "scripts", "fleet-board.py"), "--json"],
        capture_output=True, text=True,
    )
    if probe.returncode != 0:
        return None
    try:
        doc = json.loads(probe.stdout)
    except ValueError:
        return None
    sessions = doc.get("sessions")
    if not isinstance(sessions, list):
        return None
    return sum(
        1 for s in sessions
        if isinstance(s, dict)
        and s.get("role") == "manager"
        and (s.get("label") or "") != "Fleet Manager"
    )


def ready_work(vault: str, goal: str) -> int:
    """Approved, non-terminal, live-phase tasks naming `goal` — the § 1 ranking key 3.

    The same predicate `scripts/propose-managers.py` uses, with its threshold of 2
    lowered to 1 here: this **ranks**, it does not propose, so a goal with one ready
    task must outrank a goal with none.
    """
    tasks = os.path.join(vault, "25 Tasks")
    try:
        names = sorted(os.listdir(tasks))
    except OSError:
        return 0
    count = 0
    for filename in names:
        if not filename.endswith(".md"):
            continue
        try:
            with open(os.path.join(tasks, filename), encoding="utf-8") as fh:
                fm = frontmatter(fh.read())
        except OSError:
            continue
        if not fm or not scalar(fm, "approved_at"):
            continue
        if scalar(fm, "phase").lower() not in STARTABLE_PHASES:
            continue
        if scalar(fm, "status").lower() in TERMINAL_STATUSES:
            continue
        if goal in goals_of(fm):
            count += 1
    return count


def goals_of(fm: str) -> list[str]:
    """The goal names a task's `goals:` frontmatter names, across all three shapes."""
    m = re.search(r"^goals:(.*)$", fm, re.MULTILINE)
    if not m:
        return []
    inline = m.group(1).strip()
    if inline and inline not in ("[]", "null", "~"):
        names = re.findall(r"\[\[([^\]|#]+)", inline)
        return names or [inline.strip("\"' ")] if inline.strip("\"' ") else []
    # Block form: every following indented `- …` entry, with any `- - ` nesting.
    # ⚠️ Blank lines are SKIPPED, not treated as the end of the block: `m.end()` lands
    # before the newline that follows the `goals:` key, so the first item of
    # `splitlines()` is always empty and a `break` there returns `[]` for every block-form
    # task — the shape most of the vault uses.
    names = []
    for line in fm[m.end():].splitlines():
        if not line.strip():
            continue
        if not line.startswith((" ", "\t")):
            break
        s = line.strip()
        if not s.startswith("-"):
            continue
        s = re.sub(r"^-\s*", "", s)
        s = re.sub(r"^-\s*", "", s)
        w = re.search(r"\[\[([^\]|#]+)", s)
        name = (w.group(1) if w else s).strip().strip("\"' ")
        if name:
            names.append(name)
    return names


def operator_pin(topic_text: str, goal_fm: str, goal: str) -> bool:
    """§ 1 ranking key 1 — a written marking, matched semantically, never inferred."""
    if scalar(goal_fm, "manager_pin").lower() in ("true", "yes"):
        return True
    return bool(re.search(rf"\[\[{re.escape(goal)}\]\][^\n]*\bpinned\b", topic_text, re.IGNORECASE))


def rank(vault: str, topic_text: str, goals: list[str], n: int) -> list[dict]:
    """The § 1 ranking: first key that discriminates wins."""
    rows = []
    for goal in goals:
        page = resolve_page(os.path.join(vault, GOALS_DIR), goal)
        if not page:
            continue
        try:
            with open(page, encoding="utf-8") as fh:
                fm = frontmatter(fh.read())
        except OSError:
            continue
        phase = scalar(fm, "phase").lower()
        if phase not in STARTABLE_PHASES:
            continue
        try:
            priority = int(scalar(fm, "priority") or 999)
        except ValueError:
            priority = 999
        rows.append({
            "goal": goal,
            "pin": operator_pin(topic_text, fm, goal),
            "ready": ready_work(vault, goal),
            "priority": priority,
            "order": goals.index(goal),
        })
    # pin desc, ready desc, priority asc, then declaration order (oldest first).
    rows.sort(key=lambda r: (not r["pin"], -r["ready"], r["priority"], r["order"]))
    for i, r in enumerate(rows):
        r["slot"] = "start" if i < n else "held"
    return rows


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--vault", required=True, help="the vault ROOT path")
    ap.add_argument("--topic", help="the topic manager's subject")
    ap.add_argument("--goal", help="the goal to start a manager for")
    ap.add_argument("--worker", help="refused outright — this tier never starts a worker")
    ap.add_argument("--topics-dir", default=DEFAULT_TOPICS_DIR)
    ap.add_argument("--max-goal-managers", type=int, default=DEFAULT_N)
    ap.add_argument("--print", dest="emit", action="store_true",
                    help="print the /supervisor:open line on success")
    ap.add_argument("--rank", action="store_true",
                    help="print the § 1 ranking for the topic and which goals take the N slots")
    a = ap.parse_args(argv)

    # (1) The tier boundary, checked first: it is the refusal a caller most often gets
    # wrong, and it must not depend on any page resolving.
    if a.worker:
        return refuse(REFUSE_WORKER, f"<{a.worker}> is a task, and a task is a worker's "
                                     f"anchor. A topic manager starts goal managers only.")

    if not a.topic:
        ap.error("--topic is required (with --goal, --rank, or --worker)")

    vault = os.path.abspath(os.path.expanduser(a.vault))
    topics_dir = os.path.join(vault, a.topics_dir)

    # (2) The topic page must exist and must actually be a topic page.
    topic_page = resolve_page(topics_dir, a.topic)
    if not topic_page:
        return refuse("topic-not-found", f"no page at {topics_dir}/{a.topic}.md")
    with open(topic_page, encoding="utf-8") as fh:
        topic_text = fh.read()
    if not re.search(r"^page_type:\s*topic\s*$", frontmatter(topic_text), re.MULTILINE):
        return refuse("topic-not-found",
                      f"{topic_page} does not declare `page_type: topic` in its frontmatter")

    # (2b) `--rank` answers "which goals get the N slots", and needs no goal argument.
    if a.rank:
        goals = declared_goals(topic_text)
        rows = rank(vault, topic_text, goals, a.max_goal_managers)
        print(f"rank: {len(rows)} startable of {len(goals)} declared · N = {a.max_goal_managers}")
        for i, r in enumerate(rows, 1):
            print(f"{i} {r['slot']:<5} {r['goal']} "
                  f"pin={'yes' if r['pin'] else 'no'} ready={r['ready']} priority={r['priority']}")
        return 0

    if not a.goal:
        ap.error("--goal is required (with --topic, or use --rank / --worker)")

    # (3) The goal page must exist.
    goal_page = resolve_page(os.path.join(vault, GOALS_DIR), a.goal)
    if not goal_page:
        return refuse("goal-not-found", f"no page at {vault}/{GOALS_DIR}/{a.goal}.md")
    with open(goal_page, encoding="utf-8") as fh:
        goal_fm = frontmatter(fh.read())

    # (4) Membership — declared, never re-derived.
    goals = declared_goals(topic_text)
    if a.goal not in goals:
        return refuse(
            REFUSE_SCOPE,
            f"<{a.goal}> is not in {a.topic}'s `## Goals` list "
            f"({len(goals)} declared). The topic's declared membership is the boundary.",
        )

    # (5) Startability — the operator's approval, read from disk.
    phase = scalar(goal_fm, "phase")
    if phase not in STARTABLE_PHASES:
        return refuse(
            REFUSE_STARTABLE,
            f"<{a.goal}> is at phase `{phase or '(none)'}`; only "
            f"{' or '.join(STARTABLE_PHASES)} is startable. `todo → planning` is the "
            f"operator's own flip.",
        )

    # (6) The inner bound, then the outer one — the ceiling always wins.
    running = live_goal_managers(vault, topics_dir, goals)
    if running >= a.max_goal_managers:
        return refuse(
            REFUSE_CAP,
            f"<{a.topic}> already runs {running} goal manager(s); N = "
            f"{a.max_goal_managers}. Rank the goals (operator pin, startability, ready "
            f"work, priority, oldest) and start one of those instead.",
            bound="goal-cap",
        )
    ceiling = fleet_manager_ceiling()
    live = fleet_manager_count()
    if ceiling is not None and live is not None and live >= ceiling:
        return refuse(
            REFUSE_CAP,
            f"{live} managers live fleet-wide, ceiling spawn.maxManagers = {ceiling}. "
            f"N is a per-topic share; the ceiling is the outer bound and is never raised "
            f"to make room.",
            bound="manager-cap",
        )

    print(f"spawn-ok: {a.goal}")
    if a.emit:
        print(f'/supervisor:open --goal "{a.goal}"')
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
