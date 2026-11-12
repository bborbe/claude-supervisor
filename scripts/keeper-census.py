#!/usr/bin/env python3
"""The fleet round's keeper census: every live session has exactly one keeper.

**Rule source: `docs/session-tiers.md` § Keep-alive** (the chain table, § Owner, § Adopt or
close) — this file implements it and never restates it. The block, the exit codes and the
card's contract are `commands/steps/readings.md` § The keeper-census reading; read both
before changing a rule here.

  exit 0   clean — the count line is the last line
  exit 1   at least one row is unkept or double-kept — each red row is named on its line
  exit 2   the registry or the vault could not be read — a refusal, never a clean fleet

⚠️ The fleet round's own liveness is not re-read: the census is *run by* the round, so a
stopped round runs no census.
⚠️ A page whose session is not live is `not-live`, never red — that is
`worker-keepalive.py`'s population.
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


#: `scripts/owner.py` — the one place ownership is decided (see `owner()` below).
OWNER = _load("owner", "owner.py")

#: `scripts/goals-declaration.py` is the one home for the `goals:` declaration, with five
#: live shapes and YAML's `''` escape undone. A local extractor here would be the fourth
#: copy, and its failure direction is a silent drop — a name that matches no page reads as
#: "this worker has no goal", which the census reports green.
GOALS = _load("goals_declaration", "goals-declaration.py")

SESSION_LIVENESS = os.path.join(HERE, "session-liveness.py")
ATTENTION_ASK = os.path.join(HERE, "attention-ask.py")

#: Where a keeper leg is declared: the actor that runs three of the four procedures, and every
#: `commands/steps/keepalive*.md`. Globbing is the point — a leg added in a new step file is
#: picked up without editing this list.
LEG_FILES = sorted(glob.glob(os.path.join(REPO, "commands", "steps", "keepalive*.md"))) + [
    os.path.join(REPO, "agents", "fleet-keepalive-actor.md"),
]

#: The branch token may be bare (`--branch topic`) or quoted (`"--branch", "topic"`).
LEG_PATTERNS = {
    "topic": re.compile(r"--branch[\"',\s]{0,12}topic"),
    "goal": re.compile(r"--branch[\"',\s]{0,12}goal"),
    "worker": re.compile(r"worker-keepalive\.py"),
}

#: Newest naming first; the satellite vaults still carry the pre-2026-09-13 layer names.
TOPIC_DIRS = ("23 Topics", "22 Topics")
GOAL_DIRS = ("24 Goals", "23 Goals")
TASK_DIRS = ("25 Tasks", "24 Tasks")

TERMINAL = {"completed", "aborted"}

STATE_DIR = os.path.expanduser(
    os.environ.get("KEEPER_CENSUS_DIR") or "~/.claude/state/keeper-census"
)
EPISODES = os.path.join(STATE_DIR, "episodes.json")

#: Not `$CLAUDE_CODE_SESSION_ID`: unset by hand and in a sub-agent, where `attention-ask.py`
#: REFUSES the post (the trap `fleet-keeper.py` documents).
PRODUCER_ID = "keeper-census"

#: `fleet-keeper.py` — the one home for the fleet manager's registration read.
FLEET_KEEPER = _load("fleet_keeper", "fleet-keeper.py")

PROBE_TIMEOUT = 30  # seconds; a hung probe must not wedge the round

# page reads

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


#: YAML's spellings of "nothing". Read as a literal, `null` joins every such page into one row.
NULLS = {"", "~", "null", "none", "nil"}


def scalar(fm, key):
    # `[ \t]*`, never `\s*`: `\s` eats the newline, so an empty key captured the NEXT key.
    m = re.search(rf"^{key}:[ \t]*(.*)$", fm, re.M)
    if not m:
        return ""
    value = m.group(1).strip().strip('"').strip("'")
    return "" if value.lower() in NULLS else value


def links(fm, key):
    """Wikilink targets of a list key. `goals:` goes through `GOALS.goal_names` instead."""
    m = re.search(rf"^{key}:[ \t]*(.*(?:\n[ \t-].*)*)", fm, re.M)
    return re.findall(r"\[\[([^\]|#]+)", m.group(1)) if m else []


def section_links(text, heading):
    """The wikilinks of a `## <heading>` bullet list — the topic page's declared goals.

    The section ends at the next H1 **or H2**, or at EOF: a topic page carries `# Split`
    sections after `## Goals`, and a `## Goals` that ends the file still declares.
    """
    m = re.search(rf"^## {re.escape(heading)}$(.*?)(?=^#{{1,2}} |\Z)", text, re.M | re.S)
    return re.findall(r"^[ \t]*[-*][ \t]+\[\[([^\]|#]+)", m.group(1), re.M) if m else []


def page_dirs(vault):
    """The page directories that exist under `vault` — the census's own layout probe."""
    return [os.path.join(vault, name)
            for name in TOPIC_DIRS + GOAL_DIRS + TASK_DIRS
            if os.path.isdir(os.path.join(vault, name))]


def session_ids(fm):
    """Every session id the page records — `claude_session_id` plus `metrics_sessions[].session_id`
    (the field set `scripts/session-ids.py` owns). 26 task pages on `private-personal` record
    only the latter, one of them live — reading one field reported it as a false red.

    ⚠️ This is the JOIN (which live session is this page?), never an ownership read: ownership
    is `OWNER.verdict_of`, and the manager a goal or topic page records is
    `OWNER.page_session_id`. Do not let a second ownership decision grow here.
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
    non-terminal page ("no such page" must not read like "a page with no session id");
    `by_session` indexes only pages that record one.
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
                    "fm": fm,
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


# probes — the seams every test patches

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
    """The manager's loop state, one token — `owner.py`'s own loop read, not a second copy."""
    state, _ = OWNER.loop_state(subject, branch, vault)
    return state or "UNREADABLE"


def legs(leg_file=None):
    """Which keeper legs exist, read from the leg files' own text."""
    paths = [leg_file] if leg_file else LEG_FILES
    text = "\n".join(_read(path) for path in paths)
    return {name: bool(pattern.search(text)) for name, pattern in LEG_PATTERNS.items()}


def fleet_session():
    """The newest registered fleet manager's session id — `fleet-keeper.py`'s own read.

    The fleet manager is the one row in `docs/session-tiers.md` § Keep-alive kept by the
    **operator**, and it holds no vault page, so this registration is the only way to know
    which live session it is. The reader has one home; this is a call, not a second copy.
    """
    return FLEET_KEEPER.fleet_session()


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


# ownership — decided by the shared resolver, never here

def owner(kind, name, world):
    """Whether the manager of `(kind, name)` is an owner — `scripts/owner.py` decides.

    ⚠️ **The verdict is `OWNER.verdict_of(session, loop)`, the one place in the tree that
    combines the two signals.** This function supplies the two *facts* and reads the answer;
    it never compares them itself, which is the defect the resolver exists to remove
    ([[Every Keeper Resolves Ownership Through One Resolver]]).

    The facts come from the census's own single reads rather than from `owner.of_page`, whose
    every call costs an HTTP `session-liveness.py --check`: the registry is already read once
    (`read_live`), so the session word is derived from it, and only the loop is probed — via
    `world["cadence"]`, the seam the tests inject.

    Returns `{"live", "cadence", "owns", "cannot", "why"}`. **`cannot` is a third answer, not
    `owns: False`** — a failed read never licenses a definite verdict, so it is counted in the
    header and never carded as a keeper gap. Cached per `(kind, name)`: one goal manager is
    consulted once per worker that names it (uncached, a 327-page vault took over two minutes).
    """
    cache = world.setdefault("owners", {})
    if (kind, name) not in cache:
        cache[(kind, name)] = _owner(kind, name, world)
    return cache[(kind, name)]


def _owner(kind, name, world):
    page = world["by_name"].get((kind, name))
    if page is None:
        # Not an ownership question the resolver can answer: there is no page to resolve.
        return {"live": None, "cadence": "NO-PAGE", "owns": False, "cannot": True,
                "why": f"no {kind} page named {name}"}
    sid = OWNER.page_session_id(page["fm"])
    if not sid:
        session = OWNER.NONE
    elif sid in world["live"]:
        session = "LIVE"
    else:
        session = "ABSENT"
    loop = world["cadence"](name, kind, world["vault"])
    verdict, reason = OWNER.verdict_of(session, loop)
    return {"live": session == "LIVE", "cadence": loop,
            "owns": verdict == OWNER.OWNER,
            "cannot": verdict == OWNER.CANNOT_TELL,
            "why": f"{kind} {name} {verdict} ({reason})"}


# the census

#: Page kind → the tier it makes the session. When several pages record one session the
#: highest tier decides (pages stay stamped after their work ends), and the overlap is
#: reported as a `MISMATCH`.
TIER_BY_KIND = {"topic": "topic-mgr", "goal": "goal-mgr", "task": "worker", "fleet": "fleet-mgr"}
PRECEDENCE = {"topic": 0, "goal": 1, "task": 2, "fleet": 3, "none": 4}

STALE_LIST_LIMIT = 10   #: stale-page lines printed before the remainder is counted
CARD_ROW_LIMIT = 8      #: red rows listed on the card before the remainder is counted
NOT_LIVE_LIMIT = 10     #: dead-session pages printed before the remainder is counted

#: Sessions the operator opens and nothing else keeps (`docs/session-tiers.md` § The four
#: tiers): the **Fleet Manager** and the **Fleet Pusher**. Neither holds a vault page.
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
        # ⚠️ The fleet goal leg covers goals in NO topic, and only those
        # (`keepalive-topicless-goals.md`) — a declared goal whose topic manager is stood
        # down is kept by nobody, and calling it fleet-kept is the false green this exists
        # to remove.
        if not verdicts:
            if not world["legs"]["goal"]:
                return [], ("goal is in no topic and the fleet round has no goal keep-alive "
                            "leg"), False
            # The leg's population gate (`crashed-managers.fleet_keeps`): `in_progress` /
            # `execution` only, so any other row is a green with no keeper.
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


#: Cadence probes are one subprocess each (~0.9 s) and a real fleet consults ~60: serial that
#: was 55 s of wall clock for a reading taken every tick. Only the scheduling is parallel.
PROBE_WORKERS = 8


def prefetch_owners(world):
    """Warm the owner cache concurrently, so `census()` is a lookup pass.

    The shared dict is written without a lock deliberately: a dict assignment is atomic, so
    the worst case is two threads probing one subject — a wasted probe, never a wrong verdict.
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
            # ⚠️ `unrecorded`, not `unkept`: a live session no page records is outside the
            # tree (§ Adopt or close says nothing about one). Counting those made the clean
            # line unreachable — 19 of 78 live sessions on 2026-11-08.
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
        # No keeper read: the session is gone, and a probe per dead page is pure cost.
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


# reporting

def render(rows, mismatches, live_count, leg_state, vault, card=None):
    """The round's text block. Its LAST line is the count line, byte-stable — the `card:` line
    and every row sit above it."""
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
            # A real vault carries hundreds of these; printing all would bury the fleet.
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
    """Persist the record — or delete it when empty, so a green fleet leaves no stale file
    (`fleet-keeper.py` § `end_episodes` ends an episode the same way)."""
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

    ⚠️ The key carries the episode's start time and day (`fleet-keeper.py` § `raise_once` has
    the measurements): a constant key lets an ANSWERED card swallow every later episode, and a
    card expires after 24 h, so a fleet still red a day later must be asked again.
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
        # ⚠️ An unwritable record cannot remember the key, so every tick would mint a new
        # episode and post again. One card says so instead.
        notify(f"keeper-census-state-unwritable-{int(now) // 86400}",
               f"The keeper census cannot write {EPISODES}, so it cannot remember which red "
               "reading it already reported; it may repost that card every round.",
               ["acknowledge"], context="keeper census")
    return item


def main(argv=None):
    """CLI. An internal failure exits 2, never 1 — `1` means "a red row", and a traceback
    read as a finding loses the count line."""
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
        # An unrecognised layout has no pages to join, so every live session would read
        # unrecorded — a false fleet produced by a wrong path.
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
