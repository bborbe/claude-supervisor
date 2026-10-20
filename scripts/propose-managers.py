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
  * *live phase* — `phase` in `planning` / `execution`. ⚠️ This is the operator's own
    threshold ("approved (phase planning/execution)"), not a claim that a `phase: todo`
    row is unapproved — rows carrying `approved_at` at `phase: todo` do exist, and they
    are excluded here by the phase test rather than by the approval test.
  * *non-terminal* — `status` is not completed / aborted / hold / backlog.
  * *no live manager* — see below.

Threshold 2, not 1: the operator's ruling on this card was *">= 2 approved tasks"*. One
approved task is a row to open, not a subject to manage — the threshold is what keeps the
card a signal about a goal rather than a second copy of the `approved-not-started` reading.

Which manager owns a goal
-------------------------
**Delegated to `scripts/fleet-board.py`, never re-derived here.** That file already owns
"does this session name this subject" (`_norm_session_name` + `_matches_one_title`, which
normalise the registry's `⚙ ` decoration and accept a truncated title) and the two-hop
goal→topic→manager chain. This script imports those helpers rather than restating them,
because a second matcher is a second answer.

⚠️ **An exact-equality match is not enough, and the counter-example is this change's own
motivating case.** The live manager for the CDB goal is named `Read CDB Data Through
Weldall Manager` against a goal titled `Read CDB Data Through Weldall (MVP)`: an
abbreviated label, which `_matches_one_title`'s length-gated prefix branch accepts and
equality never does. Measured 2026-10-12 — the naive form listed that goal as unmanaged
with its manager running.

A goal also reaches a manager through its **topic**: a live manager whose subject is the
topic page listing the goal under `## Goals` owns it. Both hops are read, and a goal
matching neither is `unmanaged` — a fact about the fleet, not a failure.

The slot gate
-------------
The card is posted only when a manager slot is free, and the slot count comes from the
code that enforces it — never a number restated here.

  * the cap is `resolveMaxManagers` in `server/spawn-mode.mjs`, called through node
    exactly as `scripts/worker-target.py` calls `resolveMaxConcurrent` for the worker
    pair. No Python reimplementation: that would be a second counter.
  * ⚠️ **The gate reads the HARD cap, not the soft one, and the reason is the operator's
    ceiling rather than the server's refusal.** The card's spawn is an operator-named
    manager, and the `docs/fleet-surface.md` band argument does **not** carry here: the
    recipe this card's answer uses (`commands/open.md` § Step 2C) spawns a wezterm tab
    directly, which bypasses `spawn_agent` and therefore never reaches
    `managerLimitRefusal` at all. So this gate is the fleet's own, not the server's, and
    the number it applies is the operator's stated ceiling for managers — the hard cap
    (`spawn.maxManagersHard`, default 5), the same 5 the operator's own ruling names.
    Gating on the soft cap would silently substitute a number the operator did not choose
    for this decision.
  * ⚠️ **A `0` on the soft key is the documented OFF SWITCH and disables BOTH thresholds**
    (`server/spawn-mode.mjs`), so the resolver returns `hardLimit: null` there. That is
    **unlimited**, not unknown: every candidate is posted. Reading it as "no free slot"
    would turn the operator's own off switch into permanent silence.
  * the live-manager count is `scripts/fleet-board.py --json`'s own manager
    classification, Fleet Manager excluded — the instrument `server/manager-sessions.mjs`
    names. `null` (the board could not be read) is UNKNOWN and refuses; it never reads as
    zero, because an empty fleet is the answer that posts past a limit.

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

⚠️ **The key carries no vault, so two same-titled goals in two vaults would collide** —
and the store's open-scoped suppression would swallow the second silently while this run
counted it as posted. `qualify()` deliberately keeps those two goals separate (goal titles
are not namespaced across vaults), so the collision is detected here and **refused** (exit
`2`) rather than lost. The key form is the operator's, stated in the runbook; changing it
is a spec change, not a bug fix.

Exit codes
----------
  0  one or more cards emitted (or, without `--post`, would be)
  3  nothing to do — no qualifying goal, or no free slot
  2  REFUSED — the cap, the live-manager count, a tasks dir or the dedup keys could not
     be resolved. Never read as 3.
"""
import argparse
import importlib.util
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

# The topic dirs a vault may keep its topic pages in. Probed, never assumed: a vault that
# has neither simply contributes no topic hop, which can only fail to *suppress* a card —
# the loud direction, never the silent one.
TOPIC_DIRS = ("23 Topics", "22 Topics")

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


def _board():
    """`scripts/fleet-board.py` as a module — its own name-match helpers, imported.

    ⚠️ **Imported, not copied.** `_norm_session_name` / `_matches_one_title` /
    `_MANAGER_SUFFIX` are the single definition of "does this session name this subject",
    shared with the fleet board the operator reads. A copy here would be a second answer
    that drifts, which is the failure this repo's `check-*` guards exist to refuse.
    """
    spec = importlib.util.spec_from_file_location("fleet_board", os.path.join(HERE, "fleet-board.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


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
    """Every configured vault that declares a `tasks_dir`.

    ⚠️ **A vault that cannot answer is skipped AND NAMED**, never silent — the rule
    `commands/open.md` § Step 0.5 states for the batch scan, and the one this file's
    earlier revision claimed without doing. A skipped vault is a hole in the scan, and a
    hole and a clean vault must not render the same.
    """
    try:
        proc = subprocess.run(
            ["vault-cli", "config", "list", "--output", "json"],
            capture_output=True, text=True,
        )
    except OSError as error:
        print(f"UNKNOWN — cannot run vault-cli to list vaults: {error}", file=sys.stderr)
        return None
    if proc.returncode != 0:
        print("UNKNOWN — `vault-cli config list` failed; the vault set is unknown", file=sys.stderr)
        return None
    try:
        entries = json.loads(proc.stdout)
    except ValueError:
        print("UNKNOWN — `vault-cli config list` did not return JSON", file=sys.stderr)
        return None
    if not isinstance(entries, list):
        print("UNKNOWN — `vault-cli config list` did not return a list", file=sys.stderr)
        return None

    out, skipped = [], []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        path, tasks_dir = entry.get("path"), entry.get("tasks_dir")
        if not path or not tasks_dir:
            continue
        root = os.path.join(path, tasks_dir)
        if not os.path.isdir(root):
            skipped.append(f"{entry.get('name') or path} ({root})")
            continue
        topics_dir = next((os.path.join(path, d) for d in TOPIC_DIRS
                           if os.path.isdir(os.path.join(path, d))), None)
        out.append({
            "name": entry.get("name") or os.path.basename(path),
            "tasks_dir": root,
            "goals_dir": os.path.join(path, entry["goals_dir"]) if entry.get("goals_dir") else None,
            "topics_dir": topics_dir,
        })
    for name in skipped:
        print(f"⚠️ skipped vault with no tasks dir: {name}", file=sys.stderr)
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


def goal_phase(goals_dir, name):
    """The goal page's `phase`, or None when it has none.

    `todo` is the operator's approval inbox: a goal carrying it has NOT been approved for
    management, and `vault-cli goal approve` is the only writer that leaves it while
    recording `approved_by` / `approved_at`. Every other value — including an absent key —
    reads as approved-or-exempt, which is the contract `vault-cli goal approve` states:
    an absent phase does not mean todo.

    Returns the raw string, unvalidated: an unknown value is still not `todo`, so it
    neither gates a card nor claims an approval.
    """
    if not goals_dir:
        return None
    path = os.path.join(goals_dir, f"{name}.md")
    try:
        with open(path, encoding="utf-8") as handle:
            block = frontmatter(handle.read())
    except OSError:
        return None
    return field(block, "phase")


def topics_by_goal(vault):
    """`{goal title: {topic title, …}}` for one vault's topic pages.

    A topic page lists its member goals under `## Goals` (in this vault's shape, inside
    `# Scope`). The hop exists because a goal's manager is often named for the **topic**,
    not the goal — the chain `docs/fleet-surface.md` § *Which manager owns a subject*
    states and `fleet-board.py`'s `manager_on_chain` reads.
    """
    topics_dir = vault.get("topics_dir")
    if not topics_dir:
        return {}
    out = {}
    try:
        names = sorted(os.listdir(topics_dir))
    except OSError:
        return {}
    for filename in names:
        if not filename.endswith(".md"):
            continue
        try:
            with open(os.path.join(topics_dir, filename), encoding="utf-8") as handle:
                text = handle.read()
        except OSError:
            continue
        collecting = False
        for line in text.splitlines():
            if line.startswith("#"):
                collecting = line.strip().lower().lstrip("#").strip() == "goals"
                continue
            if not collecting:
                continue
            for link in re.findall(r"\[\[([^\]]+)\]\]", line):
                out.setdefault(goal_name(link), set()).add(filename[:-3])
    return out


def scan(roots):
    """Every approved, non-terminal, live-phase task row, as `(goal, vault, title)` triples."""
    rows = []
    for root in roots:
        try:
            names = sorted(os.listdir(root["tasks_dir"]))
        except OSError:
            continue
        for filename in names:
            if not filename.endswith(".md"):
                continue
            try:
                with open(os.path.join(root["tasks_dir"], filename), encoding="utf-8") as handle:
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


def managed(titles, labels, board):
    """Whether a live manager names any of `titles`.

    `titles` is the goal plus every topic that lists it. The match is `fleet-board.py`'s
    own — `_MANAGER_SUFFIX` stripped, `_norm_session_name` applied, `_matches_one_title`
    compared — so a decorated label, a case difference and an abbreviated title all match
    exactly as they do on the board the operator reads.
    """
    normalized = {board._norm_session_name(t): t for t in titles if t}
    for label in labels:
        base = board._MANAGER_SUFFIX.sub("", label)
        if board._matches_one_title(board._norm_session_name(base), normalized):
            return True
    return False


def qualify(rows, labels, topics, board, threshold=GOAL_THRESHOLD):
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
        titles = [goal] + sorted(topics.get(goal, ()))
        if managed(titles, labels, board):
            continue
        priority = goal_priority(group["vault"]["goals_dir"], goal)
        candidates.append({
            "goal": goal,
            "vault": group["vault"]["name"],
            "approved": len(group["titles"]),
            "tasks": sorted(group["titles"]),
            "priority": priority,
            "phase": goal_phase(group["vault"]["goals_dir"], goal),
        })

    # Priority ascending (1 = high), then the most approved work first, then the goal name —
    # the last key only so the order is stable between two otherwise-identical goals.
    candidates.sort(key=lambda c: (c["priority"] if c["priority"] is not None else 99,
                                   -c["approved"], c["goal"].lower()))
    return candidates


def cap_cards(candidates, free):
    """The top `free` candidates, in rank order. A pure slice — never a re-rank.

    `free` is `None` for an UNLIMITED fleet (the operator's `0` off switch), which posts
    every candidate — never zero. A number `<= 0` posts none.
    """
    if free is None:
        return list(candidates)
    if free <= 0:
        return []
    return candidates[:free]


def dedup_key(goal):
    """`fleet-propose-manager:<goal name>` — membership only, never a timestamp."""
    return f"fleet-propose-manager:{goal}"


def approve_dedup_key(goal):
    """`fleet-approve-manager:<goal name>` — the todo goal's key, distinct from the start card.

    Two keys rather than one because the two cards ask different questions of the same
    goal. Sharing a key would make the store's open-scoped suppression swallow whichever
    asked second: an operator who declined to start a manager would never be asked to
    approve the goal, and vice versa.
    """
    return f"fleet-approve-manager:{goal}"


def card_key(candidate):
    """The dedup key for this candidate — the approve key when the goal is in the inbox."""
    if candidate.get("phase") == "todo":
        return approve_dedup_key(candidate["goal"])
    return dedup_key(candidate["goal"])


def key_collisions(candidates):
    """`{key: [goal, …]}` for keys two or more candidates share.

    The key carries the goal's name and no vault, so two same-titled goals in two vaults
    collide. The store's open-scoped suppression would swallow the second and this run
    would still count it as posted — a card silently never asked. Refusing is the honest
    answer; see the header.
    """
    seen = {}
    for candidate in candidates:
        seen.setdefault(card_key(candidate), []).append(candidate["goal"])
    return {key: goals for key, goals in seen.items() if len(goals) > 1}


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
    """The card's fields — one shape, so the printed line and the post cannot disagree.

    Two shapes, chosen by the goal's phase. A goal at `phase: todo` sits in the operator's
    approval inbox, so the loop asks for the APPROVAL and never for the manager — asking
    to start a manager on an unapproved goal would make the approval step skippable by
    answering the wrong card. Every other phase, including an absent key, keeps the
    start-manager ask: an absent phase is exempt, not unapproved.
    """
    if candidate.get("phase") == "todo":
        return {
            "dedup_key": approve_dedup_key(candidate["goal"]),
            "payload": (
                f"Goal \"{candidate['goal']}\" ({candidate['vault']}) holds "
                f"{candidate['approved']} approved tasks in planning/execution, sits at "
                f"phase: todo, and has no live manager. Approve it for management?"
            ),
            "context": (
                f"approved: {candidate['approved']} · phase: todo (approval inbox) · "
                f"priority: {candidate['priority'] if candidate['priority'] is not None else 'unset'} · "
                f"tasks: "
                + "; ".join(candidate["tasks"][:3])
                + ("; …" if len(candidate["tasks"]) > 3 else "")
                + " · approving records approved_by/approved_at and flips it to planning: "
                + f'vault-cli goal approve "{candidate["goal"]}"'
            ),
            "options": ["approve", "not now"],
            "recommend": "approve",
        }
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
            f"manager slots free: {'unlimited' if free is None else free} of "
            f"{'unlimited' if cap is None else cap} · tasks: "
            + "; ".join(candidate["tasks"][:3])
            + ("; …" if len(candidate["tasks"]) > 3 else "")
        ),
        "options": ["start manager", "not now"],
        "recommend": "start manager",
    }


def post(card):
    """Post one card through `attention-ask.py`, returning its item id or None.

    ⚠️ **The id is PARSED, never the whole stdout.** `attention-ask.py post` prints
    `ITEM_ID: <id>` and then a `POLL:` line, so returning stdout whole would hand the
    caller a two-line blob where it expects one id — and the step file tells the reader to
    poll exactly that value. Same parse as `scripts/open-items.py`.
    """
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
    for line in (proc.stdout or "").splitlines():
        if line.startswith("ITEM_ID:"):
            return line[len("ITEM_ID:"):].strip()
    print(f"❌ post returned no ITEM_ID: {(proc.stdout or '').strip()[:120]}", file=sys.stderr)
    return None


def roots_from_args(tasks_dirs):
    """`--tasks-dir` roots, or None when one of them is not a tasks dir.

    ⚠️ **A typo'd path must not read as a clean round.** A missing dir is REFUSED (exit 2),
    the same answer `approved-not-started.py` gives (`❌ tasks dir not found`), because
    "no qualifying goal" and "I could not look" must never render the same.
    """
    roots = []
    for raw in tasks_dirs:
        path = os.path.abspath(raw)
        if not os.path.isdir(path):
            print(f"UNKNOWN — tasks dir not found: {path}", file=sys.stderr)
            return None
        parent = os.path.dirname(path)
        roots.append({
            "name": os.path.basename(parent),
            "tasks_dir": path,
            "goals_dir": next((os.path.join(parent, g) for g in ("24 Goals", "23 Goals")
                               if os.path.isdir(os.path.join(parent, g))), None),
            "topics_dir": next((os.path.join(parent, d) for d in TOPIC_DIRS
                                if os.path.isdir(os.path.join(parent, d))), None),
        })
    return roots


def main(argv):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--tasks-dir", action="append", default=[],
                        help="scan this tasks dir instead of every configured vault's "
                             "(repeatable; the vault is its parent directory)")
    parser.add_argument("--post", action="store_true",
                        help="post the cards; without it the run is a dry print")
    parser.add_argument("--json", action="store_true",
                        help="emit the payload as JSON instead of the printed lines "
                             "(implies --dry-run: it never posts)")
    parser.add_argument("--threshold", type=int, default=GOAL_THRESHOLD,
                        help=f"approved-task count a goal must reach (default {GOAL_THRESHOLD})")
    args = parser.parse_args(argv)

    if args.post and args.json:
        print("UNKNOWN — --json implies a dry run and cannot be combined with --post",
              file=sys.stderr)
        return 2

    if args.tasks_dir:
        roots = roots_from_args(args.tasks_dir)
    else:
        roots = vaults()
    if roots is None:
        return 2
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

    # `hard is None` is the resolver's UNLIMITED (the operator's `0` off switch), never
    # "unknown" — `cap_cards` posts every candidate for it. An unreadable board or cap
    # refused above, so nothing here can collapse unknown into a number.
    free = None if hard is None else max(0, hard - count)

    board = _board()
    topics = {}
    for root in roots:
        for goal, names in topics_by_goal(root).items():
            topics.setdefault(goal, set()).update(names)

    candidates = qualify(scan(roots), labels, topics, board, args.threshold)
    collisions = key_collisions(candidates)
    if collisions:
        for key, goals in sorted(collisions.items()):
            print(f"UNKNOWN — dedup key {key} is shared by {len(goals)} goals "
                  f"({', '.join(sorted(goals))}); the key carries no vault, so one card "
                  f"would be silently swallowed", file=sys.stderr)
        return 2

    cards = cap_cards(candidates, free)

    if args.json:
        print(json.dumps({
            "cap": {"soft": soft, "hard": hard, "source": source, "hardSource": hard_source},
            "liveManagers": count,
            "free": free,
            "qualifying": candidates,
            "cards": [card_for(c, free, hard) for c in cards],
        }, indent=2))
        return 0 if cards else 3

    print(f"managers: {count} live / {'unlimited' if hard is None else hard} hard cap "
          f"(soft {soft if soft is not None else 'unlimited'}, source: {hard_source})")
    print(f"qualifying goals: {len(candidates)} · free slots: "
          f"{'unlimited' if free is None else free}")

    if not cards:
        # A suppressed round and a round that found nothing must never render the same, so
        # the reason is named — the same discipline `at target: no card` carries.
        reason = ("at the manager cap" if candidates and free == 0
                  else "no qualifying goal" if not candidates
                  else "no free slot")
        print(f"no card — {reason}")
        return 3

    for candidate in cards:
        print(f"  card: {card_key(candidate)} — {candidate['goal']} "
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
