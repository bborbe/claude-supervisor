#!/usr/bin/env python3
"""The fleet round's keeper census: every live session has exactly one keeper.

**Rule source: `docs/session-tiers.md` § Keep-alive** — the chain table, § Owner and
§ Adopt or close. Read that section before changing a rule here; this file implements it
and never restates it. The two questions this script answers are the section's own:

  * which tier keeps a given session alive, and
  * does that keeper's leg exist, and is the keeper running?

Every keeper in the tree so far was added *after* an incident exposed its absence
(`scripts/fleet-keepalive.py`, `scripts/worker-keepalive.py`, `commands/steps/keepalive.md`).
Per-gap patches do not end that pattern; a census that asserts the invariant on every round
does, because the next missing keeper becomes a red line on the board instead of a 28-hour
park.

It reads, and never acts: it resumes nothing and closes nothing. What it finds is reported
through one attention card per red row, under an episode-stable dedup key, so a still-red
census does not re-ask and a row that went green and red again does.

  exit 0   every live session has exactly one keeper — the count line is the last line
  exit 1   at least one row is unkept or double-kept — each red row is named on its line
  exit 2   the registry or the vault could not be read — a refusal, never a clean fleet

⚠️ **The fleet round's own liveness is not re-read here.** The census is *run by* the fleet
round (`commands/steps/readings.md`), so the round is running by construction — a stopped
round runs no census. Reading the fleet manager's cadence here would turn every row the
fleet leg keeps red the moment the fleet loop idles, which is the wrong answer to a question
this script is not asked.

⚠️ **A row for a page whose session is not live is reported, never counted red.** The
invariant is about *live* sessions (`Every Live Session Has Exactly One Keeper`); a page
that records a dead session is the population `worker-keepalive.py` resumes, and it is
reported as `not-live` so the count line stays a statement about the live fleet.
"""

from __future__ import annotations

import argparse
import glob
import importlib.util
import json
import os
import re
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)


def _load(name, filename):
    """Import a sibling script by path — the filenames carry hyphens."""
    spec = importlib.util.spec_from_file_location(name, os.path.join(HERE, filename))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


#: `scripts/goals-declaration.py` is the one home for the `goals:` declaration, with five
#: live shapes and YAML's `''` escape undone. A local extractor here would be the fourth
#: copy, and its failure direction is a silent drop — a name that matches no page reads as
#: "this worker has no goal", which the census reports green.
GOALS = _load("goals_declaration", "goals-declaration.py")

SESSION_LIVENESS = os.path.join(HERE, "session-liveness.py")
MANAGER_LIVENESS = os.path.join(HERE, "manager-liveness.py")
ATTENTION_ASK = os.path.join(HERE, "attention-ask.py")

#: The files whose text decides whether a keeper leg EXISTS: the actor that runs three of the
#: four procedures, and every `commands/steps/keepalive*.md` — the step that dispatches it and
#: the per-leg steps beside it (`keepalive-topicless-goals.md` carries the goal leg, shipped
#: 2026-11-08). Globbing rather than naming them is the point: a leg added in a new step file
#: is picked up without editing this list, and a leg nobody ships is absent from all of them.
LEG_FILES = sorted(glob.glob(os.path.join(REPO, "commands", "steps", "keepalive*.md"))) + [
    os.path.join(REPO, "agents", "fleet-keepalive-actor.md"),
]

#: Matched against `LEG_FILE`. The invocations are Python argument lists, so the branch token
#: may be bare (`--branch topic`) or quoted and comma-separated (`"--branch", "topic"`).
LEG_PATTERNS = {
    "topic": re.compile(r"--branch[\"',\s]{0,12}topic"),
    "goal": re.compile(r"--branch[\"',\s]{0,12}goal"),
    "worker": re.compile(r"worker-keepalive\.py"),
}

#: Vault directories, newest naming first — `private-personal` renamed its layers 2026-09-13
#: (`23 Goals` → `24 Goals`, `24 Tasks` → `25 Tasks`) and the satellite vaults still carry the
#: old names, so both are probed rather than one assumed.
TOPIC_DIRS = ("23 Topics", "22 Topics")
GOAL_DIRS = ("24 Goals", "23 Goals")
TASK_DIRS = ("25 Tasks", "24 Tasks")

TERMINAL = {"completed", "aborted"}

#: `docs/session-tiers.md` § Owner: a manager owns a subject only when its session reads LIVE
#: and its loop reads OK or STALE. Every other cadence is a stood-down, sleeping, unarmed or
#: unreadable manager, and none of them runs the round that would keep its subjects alive.
OWNER_STATES = {"OK", "STALE"}

#: The loop states the contract names as **not** an owner (`docs/session-tiers.md` § Owner).
#: Any other token — `UNREADABLE`, or a vocabulary this file has not learned — is a
#: cannot-tell, and the census reports it without counting it against the fleet.
NOT_OWNER_STATES = {"STOPPED", "SLEEPING", "UNARMED", "INVALID"}

STATE_DIR = os.path.expanduser(
    os.environ.get("KEEPER_CENSUS_DIR") or "~/.claude/state/keeper-census"
)
EPISODES = os.path.join(STATE_DIR, "episodes.json")

#: The census's identity on the attention board. A stable string, not `$CLAUDE_CODE_SESSION_ID`:
#: the census is also run by hand and from a sub-agent, where that variable is unset and
#: `attention-ask.py` REFUSES the post — the same trap `fleet-keeper.py` documents.
PRODUCER_ID = "keeper-census"

PROBE_TIMEOUT = 30  # seconds; a hung probe must not wedge the round

#: `fleet-register.py` writes one record per fleet session here, named by session id.
REGISTER_DIR = os.path.expanduser(
    os.environ.get("SUPERVISOR_WORKER_MANAGER_DIR")
    or os.environ.get("WORKER_MANAGER_STATE_DIR")
    or "~/.claude/state/worker-manager"
)


# --------------------------------------------------------------------------------------
# page reads
# --------------------------------------------------------------------------------------

def _read(path):
    try:
        with open(path, encoding="utf-8") as fh:
            return fh.read()
    except OSError:
        return ""


def frontmatter(text):
    """The frontmatter block, or "" — a page with none carries no session id either."""
    m = re.match(r"---\r?\n(.*?)\r?\n---", text, re.S)
    return m.group(1) if m else ""


#: YAML's ways of writing "nothing". A page stamped `claude_session_id: null` carries no
#: session, and reading it as the literal id `null` joins every such page into one row —
#: measured 2026-11-08 on `private-personal`, where two task pages shared the session `null`.
NULLS = {"", "~", "null", "none", "nil"}


def scalar(fm, key):
    # `[ \t]*`, never `\s*`: `\s` matches the newline, so an empty-valued key captured the
    # NEXT line's key as its value — `claude_session_id:` followed by `goals:` read as the
    # id `goals:`, which is the same collapse-into-one-row defect the NULLS set prevents.
    m = re.search(rf"^{key}:[ \t]*(.*)$", fm, re.M)
    if not m:
        return ""
    value = m.group(1).strip().strip('"').strip("'")
    return "" if value.lower() in NULLS else value


def links(fm, key):
    """The wikilink targets of a list-valued key — `topics:` here; `goals:` goes through
    `GOALS.goal_names`, which carries the shapes and the escape."""
    m = re.search(rf"^{key}:[ \t]*(.*(?:\n[ \t-].*)*)", fm, re.M)
    return re.findall(r"\[\[([^\]|#]+)", m.group(1)) if m else []


def section_links(text, heading):
    """The wikilinks of a `## <heading>` bullet list — the topic page's declared goals.

    The section runs to the next `## ` heading **or the end of the file** (`\\Z`). Requiring
    a following heading made a topic page whose `## Goals` list is its last section declare
    nothing, so every goal it named read topic-less and went red — a false violation of the
    very invariant this script asserts.
    """
    # The section ends at the next heading of level 1 or 2 — a topic page carries its
    # `# Split` sections after `## Goals`, and stopping only at `## ` swallowed their
    # bullets as declared goals (`manager-predispatch.declared_members` carries the same
    # boundary for the same reason).
    m = re.search(rf"^## {re.escape(heading)}$(.*?)(?=^#{{1,2}} |\Z)", text, re.M | re.S)
    return re.findall(r"^[ \t]*[-*][ \t]+\[\[([^\]|#]+)", m.group(1), re.M) if m else []


def page_dirs(vault):
    """The page directories that exist under `vault` — the census's own layout probe."""
    return [os.path.join(vault, name)
            for name in TOPIC_DIRS + GOAL_DIRS + TASK_DIRS
            if os.path.isdir(os.path.join(vault, name))]


def session_ids(fm):
    """Every session id the page records — `claude_session_id` and each `metrics_sessions`
    entry, the field set `scripts/session-ids.py` is the home of.

    A page can record its session only under `metrics_sessions`: 26 non-terminal task pages
    on `private-personal` do, and one of them was live while this was written. Reading only
    `claude_session_id` reported that session as one no page records — a false red.
    """
    ids = set()
    primary = scalar(fm, "claude_session_id")
    if primary:
        ids.add(primary)
    in_block = False
    for line in fm.splitlines():
        if line and not line[0].isspace():
            in_block = line.partition(":")[0].strip() == "metrics_sessions"
            continue
        if not in_block:
            continue
        key, sep, value = line.strip().lstrip("- ").partition(":")
        if sep and key.strip() == "session_id":
            value = value.strip().strip("'\"")
            if value and value.lower() not in NULLS:
                ids.add(value)
    return ids


def scan(vault):
    """Every non-terminal page, its session ids, and the topic→goal edges.

    Returns `(pages, by_session, by_name, goal_topics)`. `by_name` indexes **every**
    non-terminal page — a topic page with no session id is still a real page, and "no such
    page" must not read the same as "a page that records no session". `by_session` indexes
    only pages that record at least one id.
    """
    pages, by_session, by_name = [], {}, {}
    goal_topics = {}

    for kind, dirnames in (("topic", TOPIC_DIRS), ("goal", GOAL_DIRS), ("task", TASK_DIRS)):
        for dirname in dirnames:
            directory = os.path.join(vault, dirname)
            if not os.path.isdir(directory):
                continue
            for filename in sorted(os.listdir(directory)):
                if not filename.endswith(".md"):
                    continue
                text = _read(os.path.join(directory, filename))
                fm = frontmatter(text)
                if scalar(fm, "status") in TERMINAL:
                    continue
                ids = session_ids(fm)
                page = {
                    "kind": kind,
                    "name": filename[:-3],
                    "sid": scalar(fm, "claude_session_id"),
                    "sids": ids,
                    "status": scalar(fm, "status"),
                    "phase": scalar(fm, "phase"),
                    "goals": GOALS.goal_names(fm) if kind == "task" else [],
                    "topics": links(fm, "topics") if kind == "goal" else [],
                }
                pages.append(page)
                by_name.setdefault((kind, page["name"]), page)
                for sid in ids:
                    by_session.setdefault(sid, []).append(page)
                if kind == "topic":
                    for goal in section_links(text, "Goals"):
                        goal_topics.setdefault(goal, []).append(page["name"])

    # A goal's `topics:` frontmatter is the declaring side that survives its topic page being
    # archived, so it is unioned with the topic's own `## Goals` list rather than replacing it.
    for page in pages:
        if page["kind"] != "goal":
            continue
        for topic in page["topics"]:
            if topic not in goal_topics.setdefault(page["name"], []):
                goal_topics[page["name"]].append(topic)

    return pages, by_session, by_name, goal_topics


# --------------------------------------------------------------------------------------
# probes — the seams every test patches
# --------------------------------------------------------------------------------------

def read_live():
    """`{session_id: record}` from the registry, or `None` when it could not be read.

    `None` is a refusal and the caller exits 2: an unreadable registry must never render as
    a fleet with no live sessions, which has the same shape as a clean census.
    """
    try:
        out = subprocess.run(
            [sys.executable, SESSION_LIVENESS, "--list", "--json"],
            capture_output=True, text=True, timeout=PROBE_TIMEOUT,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    try:
        records = json.loads(out.stdout or "[]")
    except ValueError:
        return None
    return {r["sessionId"]: r for r in records if r.get("sessionId")}


def cadence(subject, branch, vault):
    """The manager's loop state, one token — `OK` / `STALE` / `STOPPED` / … / `UNREADABLE`."""
    try:
        out = subprocess.run(
            [sys.executable, MANAGER_LIVENESS, "--state", "--subject", subject,
             "--branch", branch, "--vault", vault],
            capture_output=True, text=True, timeout=PROBE_TIMEOUT,
        )
    except (OSError, subprocess.SubprocessError):
        return "UNREADABLE"
    token = (out.stdout or "").split()
    return token[0] if token else "UNREADABLE"


def legs(leg_file=None):
    """Which keeper legs exist, read from the leg files' own text."""
    paths = [leg_file] if leg_file else LEG_FILES
    text = "\n".join(_read(path) for path in paths)
    return {name: bool(pattern.search(text)) for name, pattern in LEG_PATTERNS.items()}


def fleet_session():
    """The newest registered fleet manager's session id, or `None`.

    Read from `fleet-register.py`'s registration — the same record `fleet-keeper.py` treats
    as the identity assertion. The fleet manager is the one row in
    `docs/session-tiers.md` § Keep-alive kept by the **operator**, so the census has to know
    which live session it is; nothing else identifies it, because it holds no vault page.
    """
    try:
        names = os.listdir(REGISTER_DIR)
    except OSError:
        return None
    newest = None
    for name in names:
        if not name.endswith(".json"):
            continue
        path = os.path.join(REGISTER_DIR, name)
        try:
            with open(path, encoding="utf-8") as fh:
                record = json.load(fh)
            mtime = os.path.getmtime(path)
        except (OSError, ValueError):
            continue
        if not isinstance(record, dict):
            continue
        if (record.get("subject") or "").strip() != "Fleet Manager":
            continue
        if (record.get("branch") or "").strip() != "fleet":
            continue
        if newest is None or mtime > newest[0]:
            newest = (mtime, name[: -len(".json")])
    return newest[1] if newest else None


def notify(dedup_key, payload, options, context=""):
    """Post one attention card. Returns its item id, or `None` when the post failed."""
    argv = [sys.executable, ATTENTION_ASK, "post",
            "--producer-id", PRODUCER_ID, "--producer-kind", "manager",
            "--dedup-key", dedup_key, "--payload", payload]
    if context:
        argv += ["--context", context]
    for option in options:
        argv += ["--option", option]
    try:
        out = subprocess.run(argv, capture_output=True, text=True, timeout=PROBE_TIMEOUT)
    except (OSError, subprocess.SubprocessError):
        return None
    for line in (out.stdout or "").splitlines():
        if line.startswith("ITEM_ID:"):
            return line.split(":", 1)[1].strip() or None
    return None


# --------------------------------------------------------------------------------------
# ownership — the one provisional read
# --------------------------------------------------------------------------------------

def owner(kind, name, world):
    """Resolve whether the manager of `(kind, name)` is an owner.

    ⚠️ **ADAPTER — replace with `scripts/owner.py`, do not grow this function.**
    [[Every Keeper Resolves Ownership Through One Resolver]] ships the single ownership read
    at `scripts/owner.py`; it is still in progress, so the census carries this provisional
    implementation of the *same* definition (`docs/session-tiers.md` § Owner: LIVE **and**
    loop `OK`/`STALE`) rather than blocking on it. When that module lands, this function's
    body becomes its call and this docstring goes with it. Nothing else in this file decides
    ownership, so the swap is one function.

    Returns `{"live", "cadence", "owns", "cannot", "why"}`. **`cannot` is a third answer and
    it is not `owns: False`** — `scripts/owner.py` returns `CANNOT_TELL` for an unreadable
    probe, and the rule it inherits is the fleet-wide one: a failed read must never license a
    definite verdict (`docs/pane-reads.md` § The rule). A row whose keeper could not be read
    is reported and counted in the header; it is never counted unkept and never carded on its
    own. `live is None` (no page at all) is a cannot-tell for the same reason.

    Cached per `(kind, name)` for the run: the cadence read is a subprocess, and one goal
    manager is consulted once per worker that names it — measured 2026-11-08, an uncached
    pass over a 327-page vault took over two minutes and would have blocked the round.
    """
    cache = world.setdefault("owners", {})
    if (kind, name) not in cache:
        cache[(kind, name)] = _owner(kind, name, world)
    return cache[(kind, name)]


def _owner(kind, name, world):
    page = world["by_name"].get((kind, name))
    if page is None:
        return {"live": None, "cadence": "NO-PAGE", "owns": False, "cannot": True,
                "why": f"no {kind} page named {name}"}
    if not page["sids"]:
        return {"live": False, "cadence": "NO-ID", "owns": False, "cannot": False,
                "why": f"{kind} {name} records no session id"}
    if not (page["sids"] & set(world["live"])):
        return {"live": False, "cadence": "ABSENT", "owns": False, "cannot": False,
                "why": f"{kind} {name} session is not live"}
    state = world["cadence"](name, kind, world["vault"])
    if state not in OWNER_STATES and state not in NOT_OWNER_STATES:
        # An unreadable probe, or a token this file does not know: cannot tell, never "no
        # owner". Reading it as `not-owner` would card a failed read as a keeper gap.
        return {"live": True, "cadence": state, "owns": False, "cannot": True,
                "why": f"{kind} {name} loop unreadable ({state})"}
    return {"live": True, "cadence": state, "owns": state in OWNER_STATES, "cannot": False,
            "why": f"{kind} {name} loop {state}"}


# --------------------------------------------------------------------------------------
# the census
# --------------------------------------------------------------------------------------

TIER_BY_KIND = {"topic": "topic-mgr", "goal": "goal-mgr", "task": "worker"}
#: The tier a session belongs to when several pages record it. Pages stay stamped after the
#: work they name has ended — one session can hold a completed goal and a live topic at once
#: — so the highest tier decides, and the overlap is reported as a `MISMATCH` rather than
#: silently dropped.
PRECEDENCE = {"topic": 0, "goal": 1, "task": 2, "fleet": 3, "none": 4}

#: Up to this many stale-page lines are printed one by one; beyond it they are summarised.
STALE_LIST_LIMIT = 10

#: Red rows listed on the card before the remainder is counted — a card is read, not parsed.
CARD_ROW_LIMIT = 8

#: Dead-session pages printed before the remainder is counted.
NOT_LIVE_LIMIT = 10

#: The sessions the operator opens and nothing else keeps — `docs/session-tiers.md` § The four
#: tiers: the **Fleet Manager** (kept by the operator, rule 1's one exception) and the **Fleet
#: Pusher** (`/supervisor:fleet-triage`, a dedicated session the operator opens for one pass).
#: Neither holds a vault page, so the page join cannot find them; the fleet manager's own
#: registration is read separately, and the Pusher is matched by the name the operator gives it.
OPERATOR_KEPT_NAMES = {"Fleet Manager", "fleet pusher", "Fleet Pusher"}


def _page_for(sid, by_session):
    pages = by_session.get(sid) or []
    if not pages:
        return None
    return sorted(pages, key=lambda p: PRECEDENCE[p["kind"]])[0]


def keepers_of(page, world):
    """`(keepers, why, cannot)` — who keeps this page's session alive, and whether the answer
    is a definite one. `cannot` means a keeper read failed: report it, never count it red."""
    kind = page["kind"]

    if kind == "topic":
        if world["legs"]["topic"]:
            return ["fleet round"], "", False
        return [], "the fleet round has no topic keep-alive leg", False

    if kind == "goal":
        verdicts = [(topic, owner("topic", topic, world))
                    for topic in world["goal_topics"].get(page["name"], [])]
        claimants = [f"topic mgr: {topic}" for topic, v in verdicts if v["owns"]]
        if claimants:
            return claimants, "", False
        # ⚠️ **The fleet goal leg covers goals in NO topic, and only those.**
        # `commands/steps/keepalive-topicless-goals.md` selects the topic-less set, and
        # `topic-keepalive.py` leaves a `STOPPED`/`SLEEPING` topic manager alone — so a goal
        # a topic *does* declare, whose topic manager is stood down, is kept by nobody. An
        # unguarded `legs["goal"]` branch reported it "kept by fleet round", which is the
        # false green this census exists to remove.
        if not verdicts:
            if not world["legs"]["goal"]:
                return [], ("goal is in no topic and the fleet round has no goal keep-alive "
                            "leg"), False
            # The leg's own population gate, mirrored from `crashed-managers.fleet_keeps`:
            # it selects a topic-less goal only at `in_progress` / `execution`. Any other
            # row is not resumed by it, so calling it kept would be a green with no keeper.
            if page["status"] != "in_progress" or page["phase"] != "execution":
                return ([], "goal is in no topic and outside the fleet leg's population "
                        f"(status {page['status'] or '—'}, phase {page['phase'] or '—'})",
                        False)
            return ["fleet round"], "", False
        states = ", ".join(f"{t} [{v['cadence']}]" for t, v in verdicts)
        return ([], f"declaring topic(s) have no live armed manager: {states}",
                any(v["cannot"] for _, v in verdicts))

    if kind == "task":
        claimants, deferred = [], []
        for goal in page["goals"]:
            verdict = owner("goal", goal, world)
            if verdict["owns"]:
                claimants.append(f"goal mgr: {goal}")
            elif verdict["live"]:
                # `worker-keepalive.py` defers whenever the goal manager's session is merely
                # LIVE, loop unread — so the fleet leg does not pick this worker up either.
                # `docs/session-tiers.md` § Keep-alive names that as the standing gap.
                deferred.append(f"{goal} [{verdict['cadence']}]")
        if claimants:
            return claimants, "", False
        # A goal name that resolves to no page is a page-record defect, and the answer it
        # leaves behind is cannot-tell — not "kept by the fleet round".
        unresolved = any(owner("goal", g, world)["cannot"] for g in page["goals"])
        if deferred:
            return ([], "goal manager session LIVE but its loop is not running: "
                    + ", ".join(deferred), unresolved)
        if world["legs"]["worker"]:
            return (["fleet round"], "", unresolved)
        if not page["goals"]:
            return [], "task has no goal and the fleet round has no worker keep-alive leg", False
        states = ", ".join(f"{g} [{owner('goal', g, world)['cadence']}]"
                           for g in page["goals"])
        return ([], "goal manager(s) absent and the fleet round has no worker keep-alive "
                f"leg: {states}", unresolved)

    return [], f"unhandled page kind {kind}", False


def _red(keepers):
    if not keepers:
        return "unkept"
    if len(keepers) > 1:
        return "double-kept"
    return None


#: Cadence probes run in parallel. `manager-liveness.py --state` answers one subject per
#: process and takes ~0.9 s, and a real fleet consults ~60 of them: serial, that was 55 s of
#: wall clock for a reading the round takes every tick (measured 2026-11-08, 73 live
#: sessions). The probe itself is unchanged — only its scheduling.
PROBE_WORKERS = 8


def prefetch_owners(world):
    """Fill the owner cache concurrently, so `census()` is a lookup pass.

    The set is exactly the pairs `census()` will consult: every live topic or goal page, and
    every goal named by a live worker's task page. A pair outside it is still resolved on
    demand by `owner()`; this only warms what the rows need.

    The workers write into one shared dict without a lock, deliberately: keys are disjoint
    per pair and a dict assignment is atomic, so the worst case is two threads probing the
    same subject — a wasted probe, never a wrong verdict.
    """
    pairs = set()
    for sid in world["live"]:
        page = _page_for(sid, world["by_session"])
        if page is None:
            continue
        if page["kind"] in ("topic", "goal"):
            pairs.add((page["kind"], page["name"]))
        elif page["kind"] == "task":
            pairs.update(("goal", goal) for goal in page["goals"])
        if page["kind"] == "goal":
            # A goal row consults every topic that declares it — the expensive half the
            # first version left to run serially inside `census()`.
            pairs.update(("topic", topic)
                         for topic in world["goal_topics"].get(page["name"], []))
    if not pairs:
        return
    with ThreadPoolExecutor(max_workers=PROBE_WORKERS) as pool:
        list(pool.map(lambda pair: owner(pair[0], pair[1], world), sorted(pairs)))


def census(world):
    """Every row the census prints, red ones first, plus the mismatch lines."""
    rows, mismatches, stale = [], [], []

    for sid, record in world["live"].items():
        page = _page_for(sid, world["by_session"])
        name = record.get("name") or sid[:8]
        if sid == world.get("fleet") or name in OPERATOR_KEPT_NAMES:
            rows.append({"state": "live", "tier": "fleet-mgr", "name": name, "sid": sid,
                         "keepers": ["operator"], "red": None,
                         "why": "kept by the operator by design"})
            continue
        if page is None:
            # ⚠️ **`unrecorded`, not `unkept`.** A live session no page records is outside
            # the session tree, and `docs/session-tiers.md` § Adopt or close says nothing
            # about one: it is an operator-opened ad-hoc tab, a drill, or a worker that never
            # stamped its page. Reporting it as a keeper gap made the clean line unreachable
            # (19 of 78 live sessions on 2026-11-08) and fired the card every episode-day
            # forever. It is printed, counted in the header and named in the MISMATCH block,
            # and `unkept` keeps its meaning: a page exists and nobody keeps it.
            rows.append({"state": "unrecorded", "tier": "none", "name": name, "sid": sid,
                         "keepers": [], "red": None,
                         "why": "no topic, goal or task page records this session"})
            mismatches.append(f"live session {sid} ({name}) — no page records it")
            continue
        keepers, why, cannot = keepers_of(page, world)
        rows.append({"state": "cannot-tell" if cannot else "live",
                     "tier": TIER_BY_KIND[page["kind"]],
                     "name": page["name"], "sid": sid, "keepers": keepers,
                     "red": None if cannot else _red(keepers), "why": why})

    for sid, pages in world["by_session"].items():
        if sid in world["live"]:
            continue
        page = _page_for(sid, world["by_session"])
        # No keeper read: the session is gone, so who would have kept it is not a question
        # the round acts on, and computing it costs one cadence probe per page.
        rows.append({"state": "not-live", "tier": TIER_BY_KIND[page["kind"]],
                     "name": page["name"], "sid": sid, "keepers": [],
                     "red": None, "why": "session is not live"})
        stale.append(f"page \"{page['name']}\" records session {sid} — "
                     "the registry does not hold it")
        if len(pages) > 1:
            others = ", ".join(f"{p['kind']} \"{p['name']}\"" for p in pages[1:])
            mismatches.append(f"page \"{page['name']}\" shares session {sid} with {others}")

    for sid, pages in world["by_session"].items():
        if sid not in world["live"]:
            continue
        kinds = sorted({p["kind"] for p in pages})
        if len(kinds) > 1:
            named = ", ".join(f"{p['kind']} \"{p['name']}\"" for p in pages)
            mismatches.append(f"session {sid} is recorded by pages of {len(kinds)} kinds: {named}")

    # A page recording a session the registry no longer holds is the population
    # `worker-keepalive.py` resumes, and a real vault carries a hundred of them — printing
    # one line each would bury the red rows. The fixture that proves the join names its
    # session; a real vault gets the count and the first few.
    if len(stale) <= STALE_LIST_LIMIT:
        mismatches.extend(stale)
    elif stale:
        mismatches.append(
            f"{len(stale)} page(s) record a session the registry does not hold — "
            + "; ".join(stale[:3]) + f"; … and {len(stale) - 3} more")

    rows.sort(key=lambda r: (r["red"] is None, PRECEDENCE[_kind_of(r["tier"])], r["name"]))
    return rows, mismatches


def _kind_of(tier):
    return {"fleet-mgr": "fleet", "topic-mgr": "topic", "goal-mgr": "goal",
            "worker": "task", "none": "none"}[tier]


# --------------------------------------------------------------------------------------
# reporting
# --------------------------------------------------------------------------------------

def render(rows, mismatches, live_count, leg_state, vault, card=None):
    """The round's text block. Its LAST line is the count line, byte-stable.

    Everything else — including the `card:` line — is emitted above it, because the count
    line is what the goal's own criterion reads and what `commands/steps/readings.md`
    documents as the block's last line.
    """
    unkept = sum(1 for r in rows if r["red"] == "unkept")
    double = sum(1 for r in rows if r["red"] == "double-kept")
    cannot = sum(1 for r in rows if r["state"] == "cannot-tell")
    unrecorded = sum(1 for r in rows if r["state"] == "unrecorded")
    out = [f"keeper-census: {vault}",
           "  legs: " + " ".join(f"{k}={'yes' if v else 'NO'}" for k, v in leg_state.items())
           + f" · live={live_count} rows={len(rows)} · unrecorded={unrecorded}"
           + f" · cannot-tell={cannot}"]
    dead_printed = dead_hidden = 0
    for row in rows:
        if row["red"]:
            out.append(f"  RED {row['red']:<11} {row['tier']:<10} {row['name'][:56]:<56} — "
                       f"{row['why'] or ', '.join(row['keepers'])}")
            continue
        if row["state"] == "not-live":
            # Every page that records a dead session is a row, but a real vault carries
            # hundreds and printing them would bury the live fleet. The count is kept.
            dead_printed += 1
            if dead_printed > NOT_LIVE_LIMIT:
                dead_hidden += 1
                continue
        detail = (f" · {row['why']}" if row["state"] == "cannot-tell" else "")
        out.append(f"      {row['state']:<12} "
                   f"{row['tier']:<10} {row['name'][:56]:<56} keeper: "
                   f"{', '.join(row['keepers']) or '—'}{detail}")
    if dead_hidden:
        out.append(f"      … and {dead_hidden} more page(s) whose session is not live "
                   "(worker-keepalive.py's population — never counted red)")
    for line in mismatches:
        out.append(f"MISMATCH {line}")
    if card:
        out.append(f"  card: {card}")
    out.append(f"keeper-census: unkept={unkept} double-kept={double} sessions={live_count}")
    return "\n".join(out), unkept, double


def _load_episodes():
    try:
        with open(EPISODES, encoding="utf-8") as fh:
            record = json.load(fh)
    except (OSError, ValueError):
        return {}
    return record if isinstance(record, dict) else {}


def _save_episodes(record):
    """Persist the episode record — or delete it when there is none.

    An empty record is removed rather than rewritten, the same way
    `fleet-keeper.py` § `end_episodes` ends an episode: a green fleet then leaves no file to
    go stale, and the next red run starts a fresh episode by construction.
    """
    if not record:
        try:
            os.remove(EPISODES)
        except FileNotFoundError:
            pass
        except OSError:
            return False
        return True
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        tmp = EPISODES + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(record, fh)
        os.replace(tmp, EPISODES)
        return True
    except OSError:
        return False


def card_payload(rows):
    """The one card's text: every red row, capped so the card stays readable."""
    red = [r for r in rows if r["red"]]
    lines = [f"Keeper census: {len(red)} row(s) break the invariant that every live session "
             f"has exactly one keeper (`docs/session-tiers.md` § Keep-alive)."]
    for row in red[:CARD_ROW_LIMIT]:
        lines.append(f"• {row['red']} {row['tier']} \"{row['name']}\" — "
                     f"{row['why'] or ', '.join(row['keepers'])}")
    if len(red) > CARD_ROW_LIMIT:
        lines.append(f"… and {len(red) - CARD_ROW_LIMIT} more (run scripts/keeper-census.py).")
    return "\n".join(lines)


def raise_card(rows, now):
    """Post ONE card for a red census, once per episode-day. Returns its item id or `None`.

    ⚠️ **The key carries the episode's start time and its day**, for the two reasons
    `fleet-keeper.py` § `raise_once` measures against the store's own source: a constant key
    and payload makes an ANSWERED card swallow every later episode (`suppressAnsweredTwins`),
    and a card expires after 24 h, so a fleet still red a day later needs a fresh identity to
    be asked again. Within one episode-day the key is constant, so a second consecutive red
    census posts nothing.
    """
    red = [r for r in rows if r["red"]]
    if not red:
        _save_episodes({})          # green: every episode ends
        return None
    episode = _load_episodes().get("red")
    since = episode["since"] if episode and isinstance(episode.get("since"), (int, float)) else now
    day = int((now - since) // 86400)
    if episode and episode.get("item") and episode.get("day", 0) == day:
        return episode["item"]
    dedup = f"keeper-census-red-{int(since)}-d{day}"
    item = notify(dedup, card_payload(rows), ["acknowledge"],
                  context=f"keeper census · unkept={sum(1 for r in red if r['red'] == 'unkept')}"
                          f" double-kept={sum(1 for r in red if r['red'] == 'double-kept')}")
    if not _save_episodes({"red": {"since": since, "item": item, "day": day}}):
        # ⚠️ **An unwritable record means the dedup key cannot be remembered**, so every
        # tick would mint a new episode and post again — the flooding `fleet-keeper.py`
        # § `raise_once` exists to prevent. One card says so instead.
        notify(f"keeper-census-state-unwritable-{int(now) // 86400}",
               f"The keeper census cannot write {EPISODES}, so it cannot remember which red "
               "reading it already reported; it may repost that card every round.",
               ["acknowledge"], context="keeper census")
    return item


def main(argv=None):
    """CLI. An internal failure exits 2, never 1: `1` means "at least one red row" and the
    round reads it as a finding — a traceback reported as a finding loses the count line and
    sends the operator after a keeper gap that does not exist."""
    try:
        return _main(argv)
    except (OSError, ValueError, KeyError, TypeError) as exc:  # noqa: BLE001 — see docstring
        print(f"keeper-census: refused — internal error: {exc!r}", file=sys.stderr)
        return 2


def _main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--vault", required=True, help="the vault ROOT path")
    parser.add_argument("--no-card", action="store_true",
                        help="print only — never post (the tests and a hand read use this)")
    parser.add_argument("--leg-file", help="override the keeper-leg file (tests only)")
    parser.add_argument("--now", type=float, help="epoch seconds (tests only)")
    args = parser.parse_args(argv)

    now = args.now if args.now is not None else time.time()

    if not os.path.isdir(args.vault):
        print(f"keeper-census: refused — no vault at {args.vault}", file=sys.stderr)
        return 2
    live = read_live()
    if live is None:
        print("keeper-census: refused — the session registry could not be read, so the "
              "live fleet is unknown (never 0)", file=sys.stderr)
        return 2
    if not page_dirs(args.vault):
        # A vault whose layout this script does not recognise has no pages to join, so every
        # live session would read unrecorded — a fleet-wide false red produced by a wrong
        # path. The two must not print the same, so this refuses.
        print(f"keeper-census: refused — no topic, goal or task page directory under "
              f"{args.vault} (looked for {', '.join(TOPIC_DIRS + GOAL_DIRS + TASK_DIRS)}); "
              "a vault this script cannot read must not read as a fleet with no pages",
              file=sys.stderr)
        return 2

    _, by_session, by_name, goal_topics = scan(args.vault)
    world = {
        "vault": args.vault,
        "live": live,
        "by_session": by_session,
        "by_name": by_name,
        "goal_topics": goal_topics,
        "legs": legs(args.leg_file),
        "cadence": cadence,
        "fleet": fleet_session(),
    }

    prefetch_owners(world)
    rows, mismatches = census(world)
    # The card is posted BEFORE the block is rendered so its line lands above the count
    # line: the count line is the block's last line, by contract with the goal's criterion.
    item = None if args.no_card else raise_card(rows, now)
    text, unkept, double = render(rows, mismatches, len(live), world["legs"], args.vault,
                                  card=item)
    print(text)
    return 1 if (unkept or double) else 0


if __name__ == "__main__":
    sys.exit(main())
