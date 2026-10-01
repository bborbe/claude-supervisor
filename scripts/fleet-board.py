#!/usr/bin/env python3
"""One table classifying every live session: running / idle / needs-input / problem.

Emits box-table JSON on stdout — `{"header", "rows", "widths", ...}` — for
`box-table.py`. Extra keys are safe there: it reads only `header`, `rows` and
`widths` (`data[...]` and `data.get(...)`), so the counts, the saturation ratio,
the per-row detail and the coverage assertion ride in the same document.

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

The Session column is a TREE, so a row's role is readable at a glance: who
manages whom, and who nobody manages. Four rules, applied in this order (the
operator chose them 2026-09-24; the frame they render into is owned by the Fleet
Manager Session runbook § Sweep output):

  1. manager   -- its name resolves to a `23 Topics` or `24 Goals` page, or its
                  colour is orange. The session named `Fleet Manager` is the root.
  2. worker    -- parent comes from the task's `goals:` -> that goal -> the topic
                  listing that goal -> the LIVE manager for that topic or goal.
  3. no goal link, or no live manager on the chain -> the root, since the fleet
                  layer covers it.
  4. no task file at all -> `Unmanaged`.

A manager's SUBJECT is resolved from two sources in order: the loop record its
manager wrote under `~/.claude/state/sweep-gate/<subject-slug>.*`, then its
registry name matching a topic or goal title (case-insensitively; a `<X> Manager`
suffix allowed). ⚠️ Never from the session's `claude_session_id` stamp — measured
2026-09-24, `Sample Agent`'s stamp points at an unrelated task. ⚠️ The two
sources are not independent: `manager-liveness.py`'s slug is
`re.sub(r"[^a-z0-9]+", "-", topic.lower()).strip("-")`, a lossy transform that
carries no session id, so source 1 narrows the SUBJECT and the session still comes
from the registry — the same name the fallback reads. A manager that resolves
neither way is still a manager (colour is the signal); it just has no subject, so
nothing nests under it.
"""

import argparse
import glob
import importlib.util
import json
import math
import os
import re
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
fc = _load("fleet_colours", "fleet-colours.py")

# --- the tree's inputs -------------------------------------------------------

# The vault subdirs the grouping reads. Both generations of the goals/tasks names
# are listed because the renumbering (2026-09-13: `23 Goals` -> `24 Goals`,
# `24 Tasks` -> `25 Tasks`) left older vaults on the previous pair, exactly as
# `fleet-sessions.py`'s PROBE_DIRS does — one reader must not disagree with another
# about where a vault keeps its pages.
TOPIC_SUBDIRS = ("23 Topics",)
GOAL_SUBDIRS = ("24 Goals", "23 Goals")
TASK_SUBDIRS = ("25 Tasks", "24 Tasks", "tasks")

# The task pass visits every task file in every vault, so it reads only the head:
# frontmatter is at the top by definition, and a `goals:` key below this many
# bytes is not frontmatter.
TASK_HEAD = 2048

# `manager-liveness.py`'s own namespace and slug, read not re-derived: the state
# dir is overridable there, so honouring the same override keeps the two readers
# of one directory from disagreeing.
SWEEP_GATE_DIR = os.path.expanduser(
    os.environ.get("MANAGER_LIVENESS_STATE_DIR", "~/.claude/state/sweep-gate")
)
LOOP_EXTS = ("cadence", "stopped")

ROOT_NAME = "Fleet Manager"
MANAGER_COLOUR = "orange"

ROLE_MANAGER, ROLE_WORKER, ROLE_UNMANAGED = "manager", "worker", "unmanaged"
UNMANAGED = "unmanaged"  # the `parent` value for a session no manager covers

_WIKILINK = re.compile(r"\[\[([^\]|#]+)")
_LIST_ITEM = re.compile(r"^\s*-\s")
_MANAGER_SUFFIX = re.compile(r"\s+manager$", re.IGNORECASE)


def slug(text):
    """`manager-liveness.py`'s slug, verbatim — one definition, two readers."""
    return re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")


def frontmatter(text):
    """The frontmatter block of a vault page, or `''` when it carries none."""
    if not text.startswith("---"):
        return ""
    end = text.find("\n---", 3)
    return text[3:end] if end != -1 else ""


def frontmatter_links(fm, key):
    """The wikilink targets under one frontmatter list key (`goals:`), in order.

    Reads only the list form. `goals: []` (the explicit "serves no goal" stamp)
    and an absent key both answer `[]`, which is the same answer the grouping
    wants for both — a task linked to no goal has no goal chain.
    """
    out, capturing = [], False
    key_re = re.compile(rf"^{re.escape(key)}\s*:")
    for line in fm.splitlines():
        if key_re.match(line):
            capturing = True
            out.extend(_WIKILINK.findall(line))
            continue
        if capturing:
            if _LIST_ITEM.match(line):
                out.extend(_WIKILINK.findall(line))
            elif line.strip():
                capturing = False
    return out


class VaultIndex:
    """Topic and goal titles, and the links between them — read once per run.

    Titles are matched case-insensitively (the design's own rule for the name
    fallback), so both the exact-title set and a lowered lookup are kept. A vault
    page's title is its filename stem, which is how every other reader in this
    repo resolves one.
    """

    def __init__(self, topics=(), goals=(), task_titles=(), task_goals=None, goal_topics=None):
        self.topics = {t.lower(): t for t in topics}
        self.goals = {g.lower(): g for g in goals}
        self.task_titles = {t.lower(): t for t in task_titles}
        self.task_goals = task_goals or {}
        self.goal_topics = goal_topics or {}

    def subject(self, title):
        """`('topic'|'goal', title)` when `title` names a page, else `None`."""
        if not title:
            return None
        key = title.strip().lower()
        if key in self.topics:
            return ("topic", self.topics[key])
        if key in self.goals:
            return ("goal", self.goals[key])
        return None

    def goals_of(self, task_title):
        """The goal titles a task's `goals:` frontmatter names, in order."""
        return self.task_goals.get((task_title or "").lower(), [])

    def topics_of(self, goal_title):
        """The topics whose `## Goals` lists this goal, in order."""
        return self.goal_topics.get((goal_title or "").lower(), [])

    def has_task_file(self, name):
        return bool(name) and name.strip().lower() in self.task_titles


def _vault_roots():
    """Every vault directory under `OBSIDIAN_DIR` that holds one of the subdirs."""
    root = os.path.expanduser(os.environ.get("OBSIDIAN_DIR", "~/Documents/Obsidian"))
    try:
        return [os.path.join(root, d) for d in sorted(os.listdir(root))
                if os.path.isdir(os.path.join(root, d))]
    except OSError:
        return []


def _pages(vault, subdirs):
    for sub in subdirs:
        d = os.path.join(vault, sub)
        if os.path.isdir(d):
            yield from sorted(glob.glob(os.path.join(d, "*.md")))


def _read(path, limit=None):
    """A page's text, or `''`. `limit` reads only the head — the task pass visits
    tens of thousands of files and needs nothing but their frontmatter."""
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            return fh.read(limit) if limit else fh.read()
    except OSError:
        return ""


def _goals_section_links(text):
    """The wikilinks under a page's `## Goals` heading, one per list item.

    Three narrowings, each closing a measured false positive:

    * **Stops at the next `## ` heading** — the section is the declaration, so a
      link in a later section cannot claim membership.
    * **List items only** — the section carries prose notes too, and a paragraph
      recording that a goal was *removed* from the list names it. Read whole,
      `a page in 23 Topics`'s removal note claimed that goal as a
      member, and it resolved to a live manager the goal no longer belongs to.
    * **The first link on the line only** — an entry is `- [[Goal]] — added …`,
      and the annotation after the dash routinely links other goals and tasks.
      The entry admits one member; the rest are provenance.
    """
    out, inside = [], False
    for line in text.splitlines():
        if line.startswith("## "):
            inside = line.strip().lower() == "## goals"
            continue
        if inside and _LIST_ITEM.match(line):
            links = _WIKILINK.findall(line)
            if links:
                out.append(links[0])
    return out


def vault_index():
    """Every vault read once: topic titles, goal titles, and the two link maps.

    A missing `~/Documents/Obsidian` degrades to an empty index rather than
    aborting the board: the buckets and the coverage assertion do not depend on
    the vault, and a board that renders flat beats no board at all.
    """
    topics, goals, task_titles, task_goals = {}, {}, {}, {}
    goal_pages = []  # (goal key, page text) deferred to the second pass
    topic_pages = []
    for vault in _vault_roots():
        for path in _pages(vault, TOPIC_SUBDIRS):
            topics[os.path.basename(path)[:-3].lower()] = os.path.basename(path)[:-3]
            topic_pages.append(path)
        for path in _pages(vault, GOAL_SUBDIRS):
            title = os.path.basename(path)[:-3]
            goals[title.lower()] = title
            goal_pages.append(title)
        for path in _pages(vault, TASK_SUBDIRS):
            title = os.path.basename(path)[:-3]
            task_titles[title.lower()] = title
            links = frontmatter_links(frontmatter(_read(path, TASK_HEAD)), "goals")
            if links:
                task_goals[title.lower()] = [l.strip() for l in links]

    goal_topics = {}
    for path in topic_pages:
        topic = os.path.basename(path)[:-3]
        for link in _goals_section_links(_read(path)):
            key = link.strip().lower()
            if key in goals:
                goal_topics.setdefault(key, [])
                if topic not in goal_topics[key]:
                    goal_topics[key].append(topic)

    return VaultIndex(topics.values(), goals.values(),
                      task_titles.values(), task_goals, goal_topics)


def loop_slugs(state_dir=None):
    """The subject slugs whose manager loop left a record, from the sweep-gate dir.

    `cadence` is written at every re-arm and `stopped` at a deliberate stand-down,
    so either one is evidence a manager loop owns that subject. An unreadable dir
    is an empty set — an absent record is "not recorded", never "not a manager".
    """
    d = state_dir or SWEEP_GATE_DIR
    try:
        names = os.listdir(d)
    except OSError:
        return set()
    return {f.rsplit(".", 1)[0] for f in names if f.rsplit(".", 1)[-1] in LOOP_EXTS}


def manager_subject(name, index, slugs):
    """`('topic'|'goal', title)` for a manager, or `None` when neither source resolves.

    Source 1 is the manager loop's own record: its slug names the subject, and the
    subject's page is found by slugging candidate titles (the slug is lossy and
    not invertible, so the page is matched, never decoded). Source 2 is the
    registry name against a topic or goal title, exactly and case-insensitively,
    with a `<X> Manager` suffix allowed. A record that resolves to no page falls
    through to source 2 rather than answering `None` — the loop proves a manager
    exists, not what it covers.
    """
    if slug(name) in slugs:
        for key, title in list(index.topics.items()) + list(index.goals.items()):
            if slug(title) == slug(name):
                return index.subject(title)
    for candidate in (name, _MANAGER_SUFFIX.sub("", name or "")):
        found = index.subject(candidate)
        if found:
            return found
    return None

# Session 26 · Bucket 16 · Vault task 34 · Unblocks 18 · Inactive 9 -> sum 103,
# and `box-table.py` renders `sum(widths) + 3n + 1` = 119 of the operator's 119
# columns — exactly the ceiling, with nothing left over.
#
# `Project` was dropped to pay for `Unblocks` (2026-10-01): the Fleet Manager
# Session runbook § Sweep output names it as the column to cut when the task
# titles need room, and a wrapping box is worse than a truncated cell. The
# bucket column REPLACES the old Status column rather than joining it, and the
# raw busy/shell/idle counts stay in the marker line instead.
#
# ⚠️ `Inactive` is the former `Last` column, renamed: it always rendered the
# session's transcript age, and the rename is what makes it nameable as the
# inactive-for the operator asked for. The value did not change.
#
# ⚠️ Any further column, or any width bump, breaches 119. These widths ARE the
# budget — `test_fleet_board.py` asserts the rendered sum rather than trusting
# this comment to stay true.
HEADER = ["Session", "Bucket", "Vault task", "Unblocks", "Inactive"]
WIDTHS = [26, 16, 34, 18, 9]

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

# `Unblocks` — what would move this session forward. Display-only, in the
# operator's vocabulary: it answers "what do I do about this row", and it
# deliberately does NOT replace `agents/fleet-drive.md`'s
# revive/blocked/unverifiable/finished verdicts, which drive the fleet round's
# act leg over these same sessions. Two taxonomies over one population is a
# drift risk; this one is a column, that one is a decision.
UNBLOCKS_OPERATOR = "operator-keystroke"
UNBLOCKS_NUDGE = "nudge"
UNBLOCKS_REAP = "reap/close"
UNBLOCKS_WORKING = "working"
UNBLOCKS_VALUES = (UNBLOCKS_OPERATOR, UNBLOCKS_NUDGE, UNBLOCKS_REAP, UNBLOCKS_WORKING)

# Value-only alignment with `manager-predispatch.py`'s `STUCK_SECONDS` (30 min).
# ⚠️ The SIGNAL differs, and the shared number hides it: that rule measures the
# *task file's* mtime, this one the *transcript's* age. A session can carry a
# freshly-written task file and a stale transcript, or the reverse, so the two
# classify different populations despite agreeing on the threshold.
NUDGE_SECONDS = 30 * 60


def unblocks_for(sid, age, meta, panel_ids):
    """Which of the four `Unblocks` values this session carries.

    Order is the whole rule — first match wins, and the classes are disjoint by
    construction, so every live row lands in exactly one:

    1. `operator-keystroke` — the session is in who-needs-me's Rendered-panels
       set, i.e. it ended its turn on a closer line only the operator can clear.
       Membership is read from that module's own `rendered_panels()` rather than
       re-derived, which is what makes the board's set and the operator's
       `Needs you` list the same set *by construction* instead of by agreement
       that has to be maintained.
    2. `reap/close` — the anchored task is `completed`; nothing is left to nudge,
       so the session wants closing.
    3. `nudge` — inactive past the threshold, in `execution`, holding an open
       box: the one shape where "go on" is both allowed and useful.
    4. `working` — everything else, including a session with no stamped task
       (`meta` is None). ⚠️ `working` is the DEFAULT, not a finding: a session
       with no task cannot be shown to be idle, and inventing a nudge for it
       would be a guess wearing a classification's clothes.

    `age` of `inf` (no transcript at all) counts as past the threshold rather
    than as unknown — `session_transcript_age()`'s own reading, "nothing has been
    written", is the maximal inactive case, and the cell renders it `—`.
    """
    if sid in panel_ids:
        return UNBLOCKS_OPERATOR
    if meta:
        if meta.get("status") == "completed":
            return UNBLOCKS_REAP
        if (age is not None and age >= NUDGE_SECONDS
                and meta.get("phase") == "execution"
                and meta.get("open_boxes", 0) >= 1):
            return UNBLOCKS_NUDGE
    return UNBLOCKS_WORKING


def registry_records(sessions_dir=None):
    """`session id -> {status, name, cwd}` from the registry; `None` if unreadable.

    Read through the plugin's single reader — `session-liveness.py` — rather than
    globbing the directory here. This function used to open it directly, on the
    reasoning that the shared reader returned only an id -> name map and this table
    needs `status` too. That reasoning expired: the shared reader now carries
    `status`, `cwd`, `formerNames` and `nameSource`, and a second reader over one
    directory is precisely the defect that reader's own header records — on
    2026-09-26 a hand-rolled copy read an 8-char prefix as `ABSENT` for two live
    sessions and published it as a confirmed verdict.

    `None` is a distinct answer from `{}`: an unreadable registry cannot prove a
    fleet is empty, and reporting `{}` would render a clean table for a probe that
    simply failed.
    """
    d = sessions_dir or os.environ.get("SESSIONS_DIR") or wnm.SESSIONS_DIR
    records = _load("session_liveness", "session-liveness.py").read_registry(d)
    if records is None:
        return None
    # Every entry, not only the `alive` ones: the board's row set has always been "one row per
    # registry entry", and `coverage_errors()` cross-checks that set. Filtering on the pid check
    # here would silently shrink the row set and turn the coverage assertion into a tautology.
    return {
        sid: {
            "status": (rec.get("status") or "").strip(),
            "name": rec.get("name") or "",
            "cwd": rec.get("cwd") or "",
        }
        for sid, rec in records.items()
    }


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


def gate_attribution(needs, gate_ids):
    """`session id -> "pane <id> — open <kind> gate"` for every gated session.

    Pure. `needs` are the live gate records `collect_signals()` already holds —
    each passed `is_live()`, so its pane was in `wezterm cli list` at read time and
    no second lookup is needed. The pane was being discarded, and every
    `needs-input` detail read the same literal sentence (measured 2026-09-23: 4 of
    4 rows), which ties no bucket to any pane — `/supervisor:fleet-verify` check 4
    reported UNKNOWN for want of one.

    The `pane <id>` form is the one `who-needs-me.py`'s `_PANE_REF` parses, so the
    string a reader joins on is the string the feed already speaks.
    """
    panes, kinds = {}, {}
    for r in needs:
        sid = r.get("session_id")
        if sid not in gate_ids or r.get("pane") in (None, ""):
            continue
        # Only a gate kind attributes a gate; an `idle` record of a gated session
        # is not one.
        if r.get("kind") not in ("permission", "question"):
            continue
        panes.setdefault(sid, set()).add(str(r["pane"]))
        kinds.setdefault(sid, set()).add(r["kind"])
    out = {}
    for sid, ps in panes.items():
        ids = sorted(ps, key=lambda p: (len(p), p))
        ks = sorted(kinds[sid])
        if len(ids) == 1 and len(ks) == 1:
            out[sid] = f"pane {ids[0]} — open {ks[0]} gate"
        else:
            noun = "panes" if len(ids) > 1 else "pane"
            out[sid] = f"{noun} {', '.join(ids)} — open {', '.join(ks)} gates"
    return out


def fleet_saturation(registry):
    """The fleet working ratio: how much of the live fleet is actually working.

    The numerator counts the RAW REGISTRY STATUS in `RUNNING_STATUSES`, never
    `counts["running"]`. The buckets apply a precedence (`problem` -> `needs-input`
    -> `running` -> `idle`), so a `busy` session holding an open gate is bucketed
    `needs-input` and a bucket-based numerator would drop it — undercounting
    precisely the sessions that are working. The raw status is also what
    `ListAgents` reports, which is what makes the printed numerator/denominator
    cross-checkable by hand against the same moment.

    `ratio` is `None`, not `0.0`, for an empty fleet: "no live sessions" and "no
    session working" are different answers and must not render alike.

    Offline by construction — no Prometheus, no credentials. The tok/s figure that
    sits beside this ratio is the caller's job (`claude-metrics.sh` resolves six
    credentialed sources); reading it here would make a script that runs every
    sweep tick depend on them.
    """
    numerator = sum(1 for rec in registry.values() if rec.get("status") in RUNNING_STATUSES)
    denominator = len(registry)
    return {
        "numerator": numerator,
        "denominator": denominator,
        "ratio": (numerator / denominator) if denominator else None,
    }


def collect_signals(stuck_min):
    """The two independent signals, keyed by session id, plus each gate's pane.

    Mirrors `who-needs-me.py`'s own pipeline (its `main()`), so the gate predicate
    and the stuck predicate are the same ones the operator's `Needs you` section
    is built from — one definition, two renderings.

    The fourth return value is whether the pane read succeeded. A failed read is
    `None`, which `is_live()` keeps every row on — the board degrades to the
    registry's own verdict rather than rendering a false empty — but it must say
    so, or a board whose pane filter never ran looks identical to one that ran.

    The fifth is the Rendered-panels session-id set — the `operator-keystroke`
    input. It is composed here rather than in `build_rows()` because it needs
    the same `needs`/`open_panes` pair the gate predicate above was built from,
    and re-deriving those in the row builder is exactly the second definition
    this function exists to prevent.
    """
    registry = wnm.read_registry()
    live_ids = None if registry is None else set(registry)
    records = wnm.load("needs")
    quiet = wnm.quiet_session_ids(records, live_ids)
    pmap = wnm.wezterm_panes()
    live = lambda r: wnm.is_live(r, pmap, quiet)
    needs = [wnm.reclassify_idle(r) for r in records if live(r)]
    open_panes = {
        str(r.get("pane"))
        for r in needs
        if wnm.is_open_gate(r, task_status=wnm.task_status_from_closer)
    }
    # The gate records themselves, not every record of a gated session: a gated
    # session also holds its `idle` record, which is not a gate and must not lend
    # its kind or its pane to the attribution.
    gates = [
        r for r in needs
        if wnm.is_open_gate(
            r, open_panes=open_panes, task_status=wnm.task_status_from_closer
        )
    ]
    gate_ids = {r["session_id"] for r in gates}
    cutoff = time.time() - stuck_min * 60
    stuck_ids = {r["session_id"] for r in wnm.load("tool") if live(r) and r["ts"] < cutoff}
    # The Rendered-panels set, read from who-needs-me's own composition so the
    # board's `operator-keystroke` rows and the operator's `Needs you` section
    # cannot drift apart. The `busy`/`resumed` sets are left at their empty
    # defaults deliberately: both can only *remove* a row, so a blind reader
    # lists more, never fewer — the safe direction for a column that tells the
    # operator to go press a key.
    panel_ids = {
        r["session_id"]
        for r in wnm.rendered_panels(needs, open_panes=open_panes,
                                     task_status=wnm.task_status_from_closer)
    }
    return (gate_ids, stuck_ids, gate_attribution(gates, gate_ids), pmap is not None,
            panel_ids)


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


def coverage_errors(row_ids, registry_ids, fresh_ids, heartbeat_ids=frozenset()):
    """SC3's assertion: (a) one row per source entry, (b) no carried row dropped.

    (a) alone is tautological when the rows are built from the registry, so (b) is
    the real control — it fails if a row is silently dropped, which is the failure
    this table exists to prevent. (c), the residual, is returned separately rather
    than asserted on: a transcript-fresh session holding no registry entry is a
    real population (a headless worker holds no entry at all — it is an in-process
    SDK query, not a process), so it is reported, never dropped and never
    asserted equal.

    ⚠️ **The row set now has TWO sources, and the `extra` assertion is written over
    both.** It used to read `rows - registry`, which was correct while the registry
    was the only source. Left unchanged it would fire on every heartbeat row; relaxed
    to `rows - (registry | heartbeat)` it stays a real control — a row invented by
    neither source is still caught. Dropping the assertion instead, because it "now
    fails", would be the same false-clean this function exists to catch, one level up.
    """
    errors = []
    if len(row_ids) != len(set(row_ids)):
        errors.append("duplicate rows for one session id")
    missing = registry_ids - set(row_ids)
    if missing:
        errors.append(f"registry entries with no row: {sorted(missing)}")
    beat_missing = heartbeat_ids - set(row_ids)
    if beat_missing:
        errors.append(f"heartbeat sessions with no row: {sorted(beat_missing)}")
    extra = set(row_ids) - registry_ids - heartbeat_ids
    if extra:
        errors.append(f"rows from neither the registry nor the heartbeat store: {sorted(extra)}")
    dropped = (fresh_ids & registry_ids) - set(row_ids)
    if dropped:
        errors.append(f"transcript-fresh sessions missing from the rows: {sorted(dropped)}")
    return errors


def tree_errors(ordered_ids, row_ids):
    """The assertion on the DRAWN tree: every session row appears exactly once.

    `coverage_errors()` proves the rows exist; this proves they reach the paper. A
    row that is built and then never drawn is the same silent drop one step later,
    and it is the failure a tree layout can introduce and a flat list cannot.
    """
    errors = []
    if len(ordered_ids) != len(set(ordered_ids)):
        errors.append("a session appears more than once in the tree")
    missing = set(row_ids) - set(ordered_ids)
    if missing:
        errors.append(f"session rows missing from the tree: {sorted(missing)}")
    extra = set(ordered_ids) - set(row_ids)
    if extra:
        errors.append(f"tree rows with no session row behind them: {sorted(extra)}")
    return errors


def colour_census():
    """`session id -> colour` from `fleet-colours.py`; `{}` when it cannot be read.

    Orange is the manager signal in rule 1, so a failed census costs the colour
    half of that rule and leaves the page half — it never costs the board. An
    empty map is "not recorded", which is why it must not be read as "no managers".
    """
    try:
        return {r["session_id"]: (r.get("colour") or "") for r in fc.census(None)[0]}
    except Exception:
        return {}


class Grouping:
    """Who each session answers to, and in what role. Pure data — tests build one.

    `parents[sid]` is a session id, `UNMANAGED`, or `None` (a top-level row).
    `subjects[sid]` is `(kind, title)` for a manager whose scope resolved, else
    `None`. Absent entries default to a top-level worker, which is what a caller
    with no vault to read gets — the board degrades flat rather than failing.
    """

    def __init__(self, root=None, roles=None, parents=None, subjects=None):
        self.root = root
        self.roles = roles or {}
        self.parents = parents or {}
        self.subjects = subjects or {}

    def role_of(self, sid):
        return self.roles.get(sid, ROLE_WORKER)

    def parent_of(self, sid):
        return self.parents.get(sid)

    def subject_of(self, sid):
        return self.subjects.get(sid)


def manager_on_chain(goal_titles, index, roles, subjects):
    """The live manager covering any of a task's goals, or `None`.

    A goal reaches a manager two ways: a manager whose subject IS the goal, or a
    manager whose subject is a topic listing that goal. The goal is tried before
    its topics, so the more specific parent wins when both exist.
    """
    for goal in goal_titles:
        for subject in [goal] + index.topics_of(goal):
            for sid, found in subjects.items():
                if roles.get(sid) != ROLE_MANAGER or not found:
                    continue
                if found[1].lower() == subject.lower():
                    return sid
    return None


def build_grouping(registry, index, colours, slugs, task_titles):
    """Roles and parents for every registry entry, per the Design rules 1-4.

    Pure: the vault index, the colour census, the sweep-gate slugs and the work
    map are all passed in, so the grouping fixtures in
    `scripts/tests/test_fleet_board.py` exercise it without a live fleet.
    """
    names = {sid: (rec.get("name") or "").strip() for sid, rec in registry.items()}
    root = next((sid for sid, n in names.items() if n == ROOT_NAME), None)

    roles, parents, subjects = {}, {}, {}

    # Rule 1 — managers. Detection is by colour OR by a page the name resolves to,
    # so a manager whose SUBJECT cannot be resolved is still a manager; it simply
    # has no subject, and nothing nests under it.
    for sid, name in names.items():
        if sid == root:
            roles[sid], parents[sid], subjects[sid] = ROLE_MANAGER, None, ("root", ROOT_NAME)
            continue
        found = manager_subject(name, index, slugs)
        if found or (colours.get(sid) or "").strip().lower() == MANAGER_COLOUR:
            roles[sid], parents[sid], subjects[sid] = ROLE_MANAGER, root, found

    for sid, name in names.items():
        if sid in roles:
            continue
        titles = task_titles.get(sid) or []
        # Rule 4 — no task file at all. A stamped title and a `<name>.md` in a
        # tasks dir are the two ways a session has a task; neither is a guess.
        if not titles and not index.has_task_file(name):
            roles[sid], parents[sid] = ROLE_UNMANAGED, UNMANAGED
            continue
        # Rule 2 — the goal chain; rule 3's fallback to the root is the default.
        roles[sid], parents[sid] = ROLE_WORKER, root
        for title in titles:
            manager = manager_on_chain(index.goals_of(title), index, roles, subjects)
            if manager:
                parents[sid] = manager
                break
    return Grouping(root, roles, parents, subjects)


def build_rows(registry, gate_ids, stuck_ids, task_titles, ages, widths=None, grouping=None,
               gate_attribution=None, task_meta=None, panel_ids=frozenset()):
    """One row per registry entry, sorted by bucket precedence then name.

    Pure: every input is passed in, so the bucket fixtures in
    `scripts/tests/test_fleet_board.py` exercise this directly without a live
    fleet. `grouping` is optional so a caller with no vault to read still gets a
    complete, flat board. `gate_attribution` maps a gated session to its own pane
    (see `gate_attribution()`); a gated session missing from it is reported with
    its own reason, never a sentence shared by every row.
    """
    gate_attribution = gate_attribution or {}
    task_meta = task_meta or {}
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
        meta = task_meta.get(sid)
        unblocks = unblocks_for(sid, age, meta, panel_ids)
        titles = task_titles.get(sid) or []
        # Glyph-stripped, matching `who-needs-me.py`'s display: the registry's
        # `name` may carry a leading `⚙` the operator did not type, and a name
        # rendered two ways across two readers of the same field is a defect
        # waiting to be mistaken for a rename.
        label = wnm.strip_status_glyph(rec.get("name") or "") or "—"
        rows.append(
            {
                "session_id": sid,
                "bucket": bucket,
                "label": label,
                "role": grouping.role_of(sid) if grouping else ROLE_WORKER,
                "parent": grouping.parent_of(sid) if grouping else None,
                # Raw seconds, kept beside the rendered cell: the sibling-group
                # ordering reads this, never the width-9 string — "20h ago" and
                # "3h59m ago" do not sort the way their durations do.
                "age": age,
                "unblocks": unblocks,
                "cells": [
                    label,
                    f"{BUCKET_ICON[bucket]} {bucket}",
                    titles[0] if titles else "—",
                    unblocks,
                    last,
                ],
            }
        )
        if bucket == "needs-input":
            details[sid] = gate_attribution.get(sid) or (
                f"no pane — gate for {sid[:8]} carries no pane id in the attention store"
            )
        # `operator-keystroke` is 18 chars — the whole column — so the pending
        # closer cannot ride in the cell. It goes on the action line below the
        # box, the same place a `needs-input` row's gate already reports itself.
        #
        # ⚠️ `and sid not in details` is load-bearing, not defensive. The two
        # sets overlap — a session holding an open gate has also ended its turn,
        # so it can be `needs-input` and `operator-keystroke` at once — and the
        # gate detail carries the *pane*, which is what the blocked-by-you list
        # jumps to. Overwriting it with the closer text would trade an
        # actionable link for a restatement of the cell.
        if unblocks == UNBLOCKS_OPERATOR and sid not in details:
            closer = wnm.closer_from_transcript({"session_id": sid})
            details[sid] = (
                f"waiting on your keystroke: {closer}" if closer else
                f"waiting on your keystroke for {sid[:8]} — closer not readable from the transcript"
            )
    rows.sort(key=lambda r: (BUCKET_ORDER.index(r["bucket"]), r["label"].lower()))
    return rows, details


TREE_BRANCH, TREE_LAST, TREE_PIPE, TREE_BLANK = "├ ", "└ ", "│   ", "    "
UNMANAGED_LABEL = "Unmanaged"


def _subject_suffix(found):
    """` (topic)` / ` (goal)` on a manager row, so its scope reads at a glance."""
    return f" ({found[0]})" if found and found[0] in ("topic", "goal") else ""


def _age_key(row):
    """Sort key ordering a sibling group longest-inactive first.

    Reads the row's **raw seconds**, never the rendered cell: the widths are 9
    characters, so `20h ago` and `3h59m ago` do not sort the way their durations
    do, and a probe that eyeballed the column would be checking the formatter.

    `inf` — a session with no transcript — is the most stale reading there is,
    so it leads its group. A **missing** age sorts last and is kept distinct
    from `inf` rather than coerced into it: "nothing was ever written" and "we
    could not read it" are different facts and must not tie.
    """
    age = row.get("age")
    return (1, 0.0) if age is None else (0, -age)


def build_tree(rows, grouping):
    """The drawable rows: group headers plus one row per session, in tree order.

    Returns `(render_rows, ordered_ids, sids)`. A header row is a label in the
    Session column with every other cell blank — it names a group, not a session,
    so it is deliberately absent from `ordered_ids`, which is what the coverage
    assertion counts. Every session row appears exactly once.

    `sids` is parallel to `render_rows` — the session id per drawn row, `None`
    for a group header — and exists so the renderer can link each Session cell
    without parsing the glyphs back into ids.

    Glyphs follow the operator's chosen design: the root is flush left, its
    children branch from it, and a grandchild carries its parent's continuation
    bar. `Unmanaged` is a second root-level group — its members are the sessions
    no manager covers, so they draw as its children rather than under a manager.

    Within each sibling group the order is **longest-inactive first**, read from
    the raw transcript age (`_age_key`); managers still lead their group. The
    global order stays tree order, and that is deliberate — the grouping is the
    operator's own choice (2026-09-24) and carries the manager/worker/unmanaged
    reading, which a globally age-sorted table would trade away for an ordering
    the per-group rule already delivers.

    ⚠️ With no live root (the `Fleet Manager` session is not running) every
    top-level row draws flush, so the tree loses its single spine but stays
    complete. That is the honest degradation: inventing a root row would put a
    session in the table that does not exist.
    """
    by_parent = {}
    for r in rows:
        by_parent.setdefault(r["parent"], []).append(r)
    for kids in by_parent.values():
        kids.sort(key=lambda r: (r["role"] != ROLE_MANAGER, _age_key(r), r["label"].lower()))

    out, ordered, sids = [], [], []

    def draw(row, prefix, last, top=False):
        cells = list(row["cells"])
        cells[0] = (row["label"] if top
                    else f"{prefix}{TREE_LAST if last else TREE_BRANCH}{row['label']}"
                         f"{_subject_suffix(grouping.subject_of(row['session_id']))}")
        out.append(cells)
        ordered.append(row["session_id"])
        # Parallel to `out`, so the renderer can link the Session cell — whose
        # text is a name and therefore carries nothing it could resolve from.
        sids.append(row["session_id"])
        kids = by_parent.get(row["session_id"], [])
        child_prefix = "" if top else prefix + (TREE_BLANK if last else TREE_PIPE)
        for i, kid in enumerate(kids):
            draw(kid, child_prefix, i == len(kids) - 1)

    root_row = next((r for r in rows if r["session_id"] == grouping.root), None) if grouping.root else None
    if root_row:
        draw(root_row, "", True, top=True)
    else:
        tops = by_parent.get(None, [])
        for i, r in enumerate(tops):
            draw(r, "", i == len(tops) - 1, top=True)

    unmanaged = by_parent.get(UNMANAGED, [])
    if unmanaged:
        out.append([UNMANAGED_LABEL, "", "", "", ""])
        sids.append(None)  # a group header, not a session — nothing to link
        for i, r in enumerate(unmanaged):
            draw(r, "", i == len(unmanaged) - 1)
    return out, ordered, sids


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--stuck-min", type=int, default=20)
    ap.add_argument("--json", action="store_true", help="emit the document, not just the box JSON")
    a = ap.parse_args()

    registry = registry_records()
    if registry is None:
        print("fleet-board: session registry unreadable — refusing to render a table", file=sys.stderr)
        return 1

    gate_ids, stuck_ids, attribution, panes_read, panel_ids = collect_signals(a.stuck_min)
    if not panes_read:
        print(
            "fleet-board: ⚠️ `wezterm cli list` unreadable — pane data withheld. Every row is kept "
            "on the session registry's verdict alone: a pane that cannot be checked is UNKNOWN, "
            "not gone. The table below is not evidence that the fleet is quiet.",
            file=sys.stderr,
        )
    # The heartbeat store's half of the row set. A headless or cluster worker holds no registry
    # entry at all — a headless one is an in-process SDK query, a cluster one runs as a pod on
    # another machine — so a row set built from the registry alone omits both, and an omitted
    # session is the exact failure this board exists to prevent.
    #
    # A live stamp is the store saying the worker is still being worked, which is precisely what
    # the registry's `busy` means, so those rows classify as `running`.
    #
    # ⚠️ **A stamp in the `unknown` state is deliberately NOT a row.** That state means the
    # cluster could not be read, so neither `running` nor `idle` is true of the worker and every
    # bucket would assert something false about it. It is surfaced where it belongs:
    # `session-liveness.py --check` returns `UNKNOWN` for it, and `UNKNOWN` is what SC4 tests.
    beats = _load("live_workers", "live-workers.py").read_live(
        _load("live_workers", "live-workers.py").heartbeat_dir()
    )
    beat_ids = set()
    merged = dict(registry)
    for beat in beats or []:
        sid = beat.get("session_id")
        if beat.get("state", "live") != "live" or not sid or sid in merged:
            continue
        beat_ids.add(sid)
        merged[sid] = {"status": "busy", "name": "", "cwd": ""}

    task_titles = fs.build_work_map()
    # The stamped task's own state — `status`, `phase`, its open-box count. The
    # `Unblocks` classification needs all three and a title alone answers none.
    task_meta = fs.build_task_meta()
    ages = {sid: wnm.session_transcript_age(sid) for sid in merged}
    grouping = build_grouping(merged, vault_index(), colour_census(), loop_slugs(), task_titles)
    rows, details = build_rows(merged, gate_ids, stuck_ids, task_titles, ages, grouping=grouping,
                               gate_attribution=attribution, task_meta=task_meta,
                               panel_ids=panel_ids)
    tree, ordered, sids = build_tree(rows, grouping)

    fresh = transcript_fresh_ids()
    # The coverage assertion counts SESSION rows; a group header names a group, not
    # a session, so it is deliberately absent from `ordered`. The tree assertion is
    # the second control: a row that exists but is never drawn is the same silent
    # drop one step later, and only this one can see it.
    errors = coverage_errors([r["session_id"] for r in rows], set(registry), fresh, beat_ids)
    errors += tree_errors(ordered, [r["session_id"] for r in rows])
    counts = {b: sum(1 for r in rows if r["bucket"] == b) for b in BUCKET_ORDER}
    # Every value in the fixed set, always present — a class with no rows reads
    # `0`, never an absent key, so a consumer cannot mistake "none this round"
    # for "this build does not know that class".
    unblocks_counts = {u: sum(1 for r in rows if r["unblocks"] == u) for u in UNBLOCKS_VALUES}

    doc = {
        "header": HEADER,
        "rows": tree,
        "widths": WIDTHS,
        # Parallel to `rows`: each drawn row's session id, or `None` for a group
        # header. `box-table.py` links the Session cell from this, because that
        # cell holds a name and a name resolves to nothing on its own.
        "urls": sids,
        "counts": counts,
        "unblocks_counts": unblocks_counts,
        "saturation": fleet_saturation(registry),
        "registry_count": len(registry),
        "row_count": len(rows),
        "tree_rows": len(tree),
        "residual": sorted(fresh - set(registry)),
        "details": details,
        "sessions": [
            {
                "session_id": r["session_id"],
                "label": r["label"],
                "role": r["role"],
                "parent": r["parent"],
                "bucket": r["bucket"],
                "unblocks": r["unblocks"],
                # The raw seconds the `Inactive` cell was rendered from, so the
                # sibling-group ordering is checkable against the value that
                # decided it rather than against the 9-char string it printed.
                # `inf` (no transcript) is emitted as `null`: it is not a
                # duration, and a literal `Infinity` is not valid JSON — `jq`
                # and every strict parser reject it.
                "age": (None if r["age"] is None or not math.isfinite(r["age"])
                        else round(r["age"], 3)),
            }
            for r in rows
        ],
        "coverage_ok": not errors,
        "coverage_errors": errors,
        # The drawn tree's session ids in emission order — a row's position in
        # this list is its position in the box. Published because the ordering
        # claim is otherwise only checkable by parsing the glyphs, and a probe
        # that reads the rendered cell is testing the formatter, not the sort.
        "order": ordered,
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
