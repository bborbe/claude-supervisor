#!/usr/bin/env python3
"""Propose a manager for a goal that holds approved work and no live manager.

Why this exists
---------------
Measured 2026-10-12: the operator had to push the Fleet Manager to start a manager for
goal "Read CDB Data Through Weldall (MVP)" (seibert-brogrammers) — five approved
planning tasks, no manager. The approved work sat unmanaged until the operator noticed
it by hand.

The fleet round is structurally blind to this, and for the reason
`approved-not-started.py`'s header already gives: the sweep classifies **live sessions**,
so a goal whose work was approved but never opened has no session to sweep from. The
sweep's own unmanaged-topic candidate (`commands/steps/confirm-candidates.md` § Step 2d)
covers the other half — a topic whose workers are live and whose manager is not. Neither
reaches a goal holding approved work nobody started.

This script is that reverse index, at goal granularity: walk the task files, group the
approved non-terminal ones by their `goals:`, drop the goals that already have a live
manager, and emit one attention-board card per remaining goal — an ASK, never a spawn.

What counts as a qualifying goal
--------------------------------
A goal qualifies when it holds **at least `GOAL_THRESHOLD` approved, non-terminal tasks
in a live phase** and **no live manager**.

  * *approved* — the frontmatter carries `approved_at`. Written by `vault-cli task
    approve` and only by it, so it is the vault's own record of the operator's approval
    rather than a proxy inferred from `status`/`phase`. Same predicate, same reason, as
    `approved-not-started.py`.
  * *live phase* — `phase` in `planning` / `execution`. A row still at `phase: todo` has
    not been approved, and a row past `execution` is in review or done.
  * *non-terminal* — `status` is not completed / aborted / hold / backlog.
  * *no live manager* — no live session's name matches the goal title (a `<X> Manager`
    suffix allowed), which is the convention `docs/fleet-surface.md` § *Which manager owns
    a subject* states and `approved-not-started.py`'s `manager_for` already implements.

Threshold 2, not 1: the operator's ruling on this card was *">= 2 approved tasks"*. One
approved task is a row to open, not a subject to manage — the threshold is what keeps the
card a signal about a goal rather than a second copy of the `approved-not-started` reading.

The slot gate
-------------
The card is posted only when a manager slot is free, and the slot count comes from the
code that enforces it — never a number restated here.

  * the cap is `resolveMaxManagers` in `server/spawn-mode.mjs`, the same function
    `supervisor.mjs` applies at spawn time, called through node exactly as
    `scripts/worker-target.py` calls `resolveMaxConcurrent` for the worker pair.
  * ⚠️ **The gate reads the HARD cap, not the soft one.** The card's action is an
    OPERATOR-NAMED manager spawn, and `managerLimitRefusal` lets an operator-named spawn
    open through the whole soft/hard band — so the soft cap is not the count at which this
    card's offer stops being keepable. The hard cap is, and it is the cap whose refusal
    opens `the manager fleet is FULL` (`MANAGER_FLEET_FULL`). Gating on the soft cap would
    withhold a card whose `start manager` answer the server would have honoured.
  * the live-manager count is `scripts/fleet-board.py --json`'s own manager classification,
    Fleet Manager excluded — the instrument `server/manager-sessions.mjs` names. `null`
    (the board could not be read) is UNKNOWN and refuses; it never reads as zero, because
    an empty fleet is the answer that posts past a limit.

When several goals qualify and fewer slots are free, only the top `free` get a card,
ranked by goal priority then by approved-task count. Ranking rather than a bulk post is
the operator's own 2026-10-01 ruling for the sibling card: *"approving and starting all of
them is not a good idea. So we should always look at the next important tasks."*

The card is an ASK, never an act
--------------------------------
Nothing is spawned here and nothing is approved. The card carries two options —
`start manager` / `not now` — and the operator's answer is what releases it. The dedup key
is `fleet-propose-manager:<goal name>`, derived from the goal's **name alone**: a timestamp
in the key makes a re-post of an unchanged goal a *different* key, so the store's
open-scoped suppression never matches and the operator is asked the same question twice
(measured 2026-10-02 on the sibling under-target card).

Exit codes
----------
  0  one or more cards emitted (or, without `--post`, would be)
  3  nothing to do — no qualifying goal, or no free slot
  2  REFUSED — the cap or the live-manager count could not be read. Never read as 3.
"""
import argparse
import json
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)

# The operator's threshold for this card, in one place. `1` would make every approved row
# its own candidate and turn the card into a second `approved-not-started` reading.
GOAL_THRESHOLD = 2

# A row is in a live phase when the work is approved and still being planned or run.
LIVE_PHASES = {"planning", "execution"}

# Non-terminal scheduling states. A row that is completed, aborted, on hold or parked in
# backlog is not waiting work, whatever its approval record says. Same set, same reason, as
# `approved-not-started.py`'s LIVE_STATUSES.
LIVE_STATUSES = {"next", "todo", "in_progress", ""}

# The board's root. Mirrors `ROOT_NAME` in `scripts/fleet-board.py`, which owns it, and
# `server/manager-sessions.mjs`, which excludes it from the same count for the same reason:
# the operator's design puts the Fleet Manager outside the cap.
ROOT_NAME = "Fleet Manager"

FRONTMATTER = re.compile(r"\A---\n(.*?)\n---\n", re.DOTALL)

# Resolved the way `server/config.mjs` does — `XDG_CONFIG_HOME`, then `SUPERVISOR_CONFIG`
# over it. A reader resolving a different path than the server would report the default
# while the operator's file said otherwise, and the default is the answer that reads as
# healthy. Same resolution, same reason, as `scripts/worker-target.py`.
CONFIG_HOME = os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config")
CONFIG = os.environ.get("SUPERVISOR_CONFIG") or os.path.join(
    CONFIG_HOME, "claude-supervisor", "config.json"
)

# Calls the real resolver rather than restating it — the same discipline, and the same
# shape, as `scripts/worker-target.py`. `spawn-mode.mjs` imports nothing and reads nothing,
# which makes this a pure call: the one `supervisor.mjs` makes per manager spawn.
RESOLVE = (
    "import { readFileSync } from 'node:fs';"
    "import { resolveMaxManagers } from './spawn-mode.mjs';"
    "const path = process.env.MANAGER_CAP_CONFIG;"
    "let file = {}, readError = null;"
    "try { file = JSON.parse(readFileSync(path, 'utf8')); }"
    "catch (e) {"
    "  if (e.code === 'ENOENT') { file = {}; }"
    "  else { readError = e.message; }"
    "}"
    "if (readError) { console.log(JSON.stringify({ readError })); }"
    "else {"
    "  const r = resolveMaxManagers({ file, path });"
    "  console.log(JSON.stringify(r.error ? { resolveError: r.error }"
    "    : { limit: r.limit, source: r.source, hardLimit: r.hardLimit, hardSource: r.hardSource }));"
    "}"
)


def frontmatter(text):
    """Return the frontmatter block, or '' when the file has none."""
    match = FRONTMATTER.match(text)
    return match.group(1) if match else ""


def field(block, key):
    """First value of a scalar frontmatter key, or None."""
    match = re.search(rf"^{re.escape(key)}:\s*(.+)$", block, re.MULTILINE)
    if not match:
        return None
    return match.group(1).strip().strip('"').strip("'")


def list_field(block, key):
    """The items of a YAML block list under `key`, or []."""
    out = []
    collecting = False
    for line in block.splitlines():
        if re.match(rf"^{re.escape(key)}:\s*$", line):
            collecting = True
            continue
        if collecting:
            item = re.match(r"^\s+-\s+(.*)$", line)
            if item:
                out.append(item.group(1).strip().strip('"').strip("'"))
            elif line.strip() and not line.startswith(" "):
                break
    return out


def goal_name(link):
    """`[[Some Goal]]` / `'[[Some Goal]]'` -> `Some Goal`."""
    return link.strip().strip('"').strip("'").strip("[]").strip()


def vaults():
    """Every configured vault that declares a `tasks_dir`, newest config read.

    A vault that cannot answer is skipped and named, never fatal — the same rule
    `commands/open.md` § Step 0.5 carries for the batch scan, and for the same measured
    reason: one unreadable vault truncated every vault after it in the loop order.
    """
    try:
        proc = subprocess.run(
            ["vault-cli", "config", "list", "--output", "json"],
            capture_output=True, text=True,
        )
    except OSError:
        return []
    if proc.returncode != 0:
        return []
    try:
        entries = json.loads(proc.stdout)
    except ValueError:
        return []
    if not isinstance(entries, list):
        return []
    out = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        path, tasks_dir = entry.get("path"), entry.get("tasks_dir")
        if not path or not tasks_dir:
            continue
        out.append({
            "name": entry.get("name") or os.path.basename(path),
            "tasks_dir": os.path.join(path, tasks_dir),
            "goals_dir": os.path.join(path, entry["goals_dir"]) if entry.get("goals_dir") else None,
        })
    return out


def goal_priority(goals_dir, name):
    """The goal page's `priority`, or None when it has none.

    `1` is the highest (see `50 Knowledge Base/Page Type Taxonomy.md`: *"priority: 1 # 1=high,
    2=medium, 3=low"*). An absent page or an absent key is not an error — the goal simply
    ranks last, which is the honest reading of "nobody said this mattered".
    """
    if not goals_dir:
        return None
    path = os.path.join(goals_dir, f"{name}.md")
    try:
        with open(path, encoding="utf-8") as handle:
            block = frontmatter(handle.read())
    except OSError:
        return None
    raw = field(block, "priority")
    if raw is None:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def scan(tasks_dirs):
    """Every approved, non-terminal, live-phase task row, as `(goal, vault, title)` triples."""
    rows = []
    for root in tasks_dirs:
        try:
            names = sorted(os.listdir(root["tasks_dir"]))
        except OSError:
            continue
        for filename in names:
            if not filename.endswith(".md"):
                continue
            path = os.path.join(root["tasks_dir"], filename)
            try:
                with open(path, encoding="utf-8") as handle:
                    block = frontmatter(handle.read())
            except OSError:
                continue
            if not block or not field(block, "approved_at"):
                continue
            if (field(block, "phase") or "").strip().lower() not in LIVE_PHASES:
                continue
            if (field(block, "status") or "").strip().lower() not in LIVE_STATUSES:
                continue
            title = filename[:-3]
            for link in list_field(block, "goals"):
                rows.append({"goal": goal_name(link), "vault": root, "title": title})
    return rows


def live_manager_labels(board_json):
    """The live managers' labels, Fleet Manager excluded — or None when unreadable.

    `None` is "unknown", never "none": the refusal turns it into an error rather than a
    zero, because an empty fleet is the answer that posts past a limit.
    """
    try:
        parsed = json.loads(board_json)
    except ValueError:
        return None
    if not isinstance(parsed, dict) or not isinstance(parsed.get("sessions"), list):
        return None
    return [
        (s.get("label") or "").strip()
        for s in parsed["sessions"]
        if isinstance(s, dict)
        and s.get("role") == "manager"
        and (s.get("label") or "").strip() != ROOT_NAME
    ]


def managed(goal, labels):
    """Whether a live manager's label names this goal.

    The `<X> Manager` suffix is allowed, and the match is case-insensitive — the convention
    `docs/fleet-surface.md` § *Which manager owns a subject* states, which
    `approved-not-started.py`'s `_manager_named` and `fleet-board.py` both read the same way.
    """
    title = goal.strip().lower()
    for label in labels:
        lowered = label.lower()
        if lowered in (title, title + " manager"):
            return True
    return False


def qualify(rows, labels, threshold=GOAL_THRESHOLD):
    """The qualifying goals, ranked — a pure function over already-read inputs.

    Grouping is by `(vault name, goal name)`: goal titles are not namespaced across vaults,
    so the same title in two vaults is two different goals and must not be summed.
    """
    groups = {}
    for row in rows:
        key = (row["vault"]["name"], row["goal"])
        groups.setdefault(key, {"goal": row["goal"], "vault": row["vault"], "titles": []})
        groups[key]["titles"].append(row["title"])

    candidates = []
    for (_, goal), group in groups.items():
        if len(group["titles"]) < threshold:
            continue
        if managed(goal, labels):
            continue
        priority = goal_priority(group["vault"]["goals_dir"], goal)
        candidates.append({
            "goal": goal,
            "vault": group["vault"]["name"],
            "approved": len(group["titles"]),
            "tasks": sorted(group["titles"]),
            "priority": priority,
        })

    # Priority ascending (1 = high), then the most approved work first, then the goal name —
    # the last key only so the order is stable between two otherwise-identical goals.
    candidates.sort(key=lambda c: (c["priority"] if c["priority"] is not None else 99,
                                   -c["approved"], c["goal"].lower()))
    return candidates


def cap_cards(candidates, free):
    """The top `free` candidates, in rank order. A pure slice — never a re-rank."""
    if free <= 0:
        return []
    return candidates[:free]


def dedup_key(goal):
    """`fleet-propose-manager:<goal name>` — membership only, never a timestamp."""
    return f"fleet-propose-manager:{goal}"


def resolve_cap():
    """`(limit, hard_limit, source, hard_source)` — or None when unreadable or invalid."""
    env = dict(os.environ, MANAGER_CAP_CONFIG=CONFIG)
    try:
        proc = subprocess.run(
            ["node", "--input-type=module", "-e", RESOLVE],
            capture_output=True, text=True, cwd=os.path.join(REPO, "server"), env=env,
        )
    except OSError as error:
        print(f"UNKNOWN — cannot run node to resolve the manager cap: {error}", file=sys.stderr)
        return None
    try:
        facts = json.loads(proc.stdout.strip())
    except ValueError:
        detail = (proc.stderr.strip().splitlines() or ["(no output)"])[-1]
        print(f"UNKNOWN — resolving the manager cap produced no answer: {detail}", file=sys.stderr)
        return None
    if facts.get("readError"):
        print(f"UNKNOWN — cannot read {CONFIG}: {facts['readError']}", file=sys.stderr)
        return None
    if facts.get("resolveError"):
        print(f"UNKNOWN — {facts['resolveError']}", file=sys.stderr)
        return None
    return facts["limit"], facts["hardLimit"], facts["source"], facts["hardSource"]


def live_manager_count():
    """`(count, labels)` from the board — or `(None, None)` when it could not be read."""
    board = os.path.join(HERE, "fleet-board.py")
    try:
        proc = subprocess.run(["python3", board, "--json"], capture_output=True, text=True)
    except OSError:
        return None, None
    if proc.returncode != 0:
        return None, None
    labels = live_manager_labels(proc.stdout or "")
    if labels is None:
        return None, None
    return len(labels), labels


def card_for(candidate, free, cap):
    """The card's fields — one shape, so the printed line and the post cannot disagree."""
    return {
        "dedup_key": dedup_key(candidate["goal"]),
        "payload": (
            f"Goal \"{candidate['goal']}\" ({candidate['vault']}) holds "
            f"{candidate['approved']} approved tasks in planning/execution and has no live "
            f"manager. Start a manager for it?"
        ),
        "context": (
            f"approved: {candidate['approved']} · priority: "
            f"{candidate['priority'] if candidate['priority'] is not None else 'unset'} · "
            f"manager slots free: {free} of {cap} · tasks: "
            + "; ".join(candidate["tasks"][:3])
            + ("; …" if len(candidate["tasks"]) > 3 else "")
        ),
        "options": ["start manager", "not now"],
        "recommend": "start manager",
    }


def post(card):
    """Post one card through `attention-ask.py`, returning its item id or None."""
    ask = os.path.join(HERE, "attention-ask.py")
    cmd = [
        "python3", ask, "post",
        "--dedup-key", card["dedup_key"],
        "--payload", card["payload"],
        "--context", card["context"],
        "--recommend", card["recommend"],
    ]
    for option in card["options"]:
        cmd += ["--option", option]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True)
    except OSError as error:
        print(f"❌ post failed: {error}", file=sys.stderr)
        return None
    if proc.returncode != 0:
        detail = (proc.stderr.strip().splitlines() or ["(no output)"])[-1]
        print(f"❌ post failed: {detail}", file=sys.stderr)
        return None
    return (proc.stdout or "").strip() or None


def main(argv):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--tasks-dir", action="append", default=[],
                        help="scan this tasks dir instead of every configured vault's "
                             "(repeatable; the vault is its parent directory)")
    parser.add_argument("--post", action="store_true",
                        help="post the cards; without it the run is a dry print")
    parser.add_argument("--json", action="store_true",
                        help="emit the payload as JSON instead of the printed lines")
    parser.add_argument("--threshold", type=int, default=GOAL_THRESHOLD,
                        help=f"approved-task count a goal must reach (default {GOAL_THRESHOLD})")
    args = parser.parse_args(argv)

    if args.tasks_dir:
        roots = [{"name": os.path.basename(os.path.dirname(d)),
                  "tasks_dir": d,
                  "goals_dir": next((os.path.join(os.path.dirname(d), g)
                                     for g in ("24 Goals", "23 Goals")
                                     if os.path.isdir(os.path.join(os.path.dirname(d), g))), None)}
                 for d in args.tasks_dir]
    else:
        roots = vaults()
    if not roots:
        print("UNKNOWN — no vault declares a tasks_dir", file=sys.stderr)
        return 2

    resolved = resolve_cap()
    if resolved is None:
        return 2
    soft, hard, source, hard_source = resolved

    count, labels = live_manager_count()
    if count is None:
        print("UNKNOWN — the live-manager count could not be taken (fleet-board.py --json)",
              file=sys.stderr)
        return 2

    # The HARD cap gates the card — see the header. `soft` is carried only for the report.
    if hard is None:
        free = None
    else:
        free = max(0, hard - count)

    candidates = qualify(scan(roots), labels, args.threshold)
    cards = [] if free is None else cap_cards(candidates, free)

    if args.json:
        print(json.dumps({
            "cap": {"soft": soft, "hard": hard, "source": source, "hardSource": hard_source},
            "liveManagers": count,
            "free": free,
            "qualifying": candidates,
            "cards": [card_for(c, free, hard) for c in cards],
        }, indent=2))
        return 0 if cards else 3

    print(f"managers: {count} live / {hard if hard is not None else 'unlimited'} hard cap "
          f"(soft {soft if soft is not None else 'unlimited'}, source: {hard_source})")
    print(f"qualifying goals: {len(candidates)} · free slots: "
          f"{free if free is not None else 'unknown'}")

    if not cards:
        # A suppressed round and a round that found nothing must never render the same, so
        # the reason is named — the same discipline `at target: no card` carries.
        reason = ("at the manager cap" if candidates and free == 0
                  else "no qualifying goal" if not candidates
                  else "no free slot")
        print(f"no card — {reason}")
        return 3

    for candidate in cards:
        card = card_for(candidate, free, hard)
        print(f"  card: {card['dedup_key']} — {candidate['goal']} "
              f"({candidate['vault']}, {candidate['approved']} approved)")

    if not args.post:
        print("(dry run — pass --post to put these on the board)")
        return 0

    posted = 0
    for candidate in cards:
        item = post(card_for(candidate, free, hard))
        if item:
            posted += 1
            print(f"  posted: {item}")
    return 0 if posted else 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
