#!/usr/bin/env python3
"""manager-predispatch.py — the pre-dispatch gate for `/manager-drive` and `/manager-status`.

Why this exists
---------------
Both commands dispatch a full sweep agent on every run. Measured 2026-09-25 in the
Attention Routing manager session on a 26-task tree with 25 done: the
`supervisor:manager-sweep-reader` agent cost 91,598 tokens, the drive leg 71,275, and a
`manager-status` snapshot 125,423 — for zero new information. Three times in one session
the manager bypassed the agents by hand to avoid the cost.

The sweep is not the cost; the dispatch is. So this gate runs FIRST, model-free, and
answers one question: has anything the sweep would report actually moved since the last
persisted snapshot? If not, the command replays the stored table and dispatches nothing.

Contract — ported, not invented
-------------------------------
This is a PORT of `my-vault/.claude/scripts/sweep-gate.py`, which gates the *loop* tick.
The loop gate could not be reused as a file: it lives in the vault while this plugin is
vault-agnostic (commands resolve vaults via `vault-cli config` and reference notes by
title, never path). What carries over is the contract — the digest inputs, the state key,
the exit codes and the fail-open rule — reused verbatim so the two gates cannot drift
into disagreeing about what "unchanged" means.

  manager-predispatch.py --vault <path> --subject <name> --check
  manager-predispatch.py --vault <path> --subject <name> --print
  manager-predispatch.py --vault <path> --subject <name> --save     # dates from the payload

⚠️ Why this store is not the loop's store — checked before building, 2026-09-26
--------------------------------------------------------------------------------
A live per-subject snapshot already exists: `~/.claude/state/sweep-gate-loop/<vault>/
<subject>.snapshot.json`, written every 900 s by the vault-side launchd job
`com.bborbe.sweep-gate-notify`. It was read and compared field-for-field, and the result
is worth keeping because it cuts both ways:

  IT CARRIES the digest inputs — `status`, `phase`, `progress_hash`, `session`,
  `liveness`, `stuck` per task. That is the exact set `digest_of()` below hashes, so the
  contract is CONFIRMED rather than merely assumed: the loop's writer and this gate agree
  on what "unchanged" means, which is the property that keeps two gates from drifting.

  IT LACKS the rendered table, and it has no member list. The no-change branch must
  replay the table (the operator sees the frame on every run — silence would be
  ambiguous, and a quiet run and a dead run look identical from outside), and the loop
  snapshot carries no `table` key; the loop's table lives in the sibling
  `<subject>.tick.txt` instead.

  IT IS LOOP-ONLY AND VAULT-SIDE. It exists for subjects with an armed loop, in vaults
  where that launchd job is installed. This plugin is vault-agnostic and both commands
  run against arbitrary subjects, so reading it would fail open on every unarmed subject
  — a full sweep every run, reading as armed while saving nothing. That is the same
  failure `a frozen-tree remedy` closed for the
  loop, and reintroducing it here would be a regression dressed as reuse.

So: same contract, different surface. This gate owns its own store, and the two cannot
disagree about a tree because they hash the same inputs.

Exit codes (same three the loop gate uses)
  0   digest equal to the stored one AND no moved bucket set — nothing changed, dispatch
      no agent
  10  digest differs, OR a staged bucket set differs from the stored one, OR any fail-open
      case fired — run the full sweep

      The per-bucket half is a save input in its own right, not a passenger on the digest:
      `digest_of()` covers the tracked set only, so a corrected re-stage against an
      unchanged tree used to return 0 and be discarded — leaving a bad `bucket_sets` in
      the store until the tree next moved. `--save` therefore also writes when the staged
      set differs from `stored_bucket_sets`. The digest itself is unchanged by this: it
      stays a pure function of the tracked set.
  2   usage error

Fail-open, by design
--------------------
A missing state file (first-ever run), an unreadable state file, a parse error, an
unresolvable subject, and a failed write all force a full sweep rather than reporting
"no change". A gate that silently reports "no change" when it cannot tell makes a
manager blind to its own subject, which is the one failure worth spending a dispatch
to avoid. A failed `--save` is the same shape: it returns CHANGE rather than passing
quietly, because failing to record the digest must never read as "no change" next run.

Digest inputs — verbatim from the loop gate
  per tracked task: name, status, phase, claude_session_id, a hash of its `# Progress`
  section, its Session-column liveness word, and its stuck verdict — plus the sorted
  member-name list, so a task added to or removed from the tracked set moves the digest
  even if every remaining task is untouched.

⚠️ mtime is deliberately EXCLUDED as a raw digest input. A touch with no content change
is not a change the sweep reports, and including it would wake the model on every
unrelated rewrite.

⚠️ It reaches the digest through `stuck` alone, and that is deliberate rather than a
leak. Branch (b) of the stuck rule reads the task file's mtime — it IS the rule's
*"task file unchanged"* measure — so for the rows that rule actually measures (idle, in
`phase: execution`, holding an open box, not parked) a rewrite is not unrelated: it
resets the very clock the rule reads. Every other row is untouched, because
`apply_stuck` sets `stuck = False` for it and its mtime never enters the digest at all.

⚠️ Liveness is in the digest ON PURPOSE, and this is the criterion the whole gate is
graded on. A worker dying changes session liveness; a digest over `status`/`phase`/mtime
alone would see the dead worker's task as unchanged and replay the stored table straight
over the death. Liveness is therefore computed HERE, in the gate — never inside the
dispatched `manager-sweep-reader` agent, which is forbidden to probe liveness
(`agents/manager-sweep-reader.md`) and could not see it anyway.

⚠️ The liveness verdict must not be over-eager, and that is a graded case too. Deciding
"dead" by heartbeat age alone satisfies "a dead worker is reported" while mis-reporting
live workers as dead. The registry is consulted first and is authoritative when it holds
a live pid; the heartbeat is the fallback for headless workers the registry structurally
cannot see. Both halves are exercised by the paired probe in the task's SC3.

Liveness is model-free and read from plain files
------------------------------------------------
  `~/.claude/sessions/<pid>.json` — the registry. Pruned on exit, so a record with a live
  pid is a POSITIVE signal; absence proves nothing (a headless worker has none).
  `<XDG_STATE_HOME>/claude-supervisor/live/<sid>.json` — the headless heartbeat, written
  by this plugin's own `server/heartbeat.mjs`. The verdict is the stamp's AGE against the
  TTL, never the file's existence: a server killed with `kill -9` never clears its stamps,
  so a stale file is a DEAD worker.
  `~/.claude/state/attention/*.needs.json` — the park probe, using the feed's own rule. A
  park is a RAISED gate; `state: "open"` alone is not a park (the dominant record shape is
  `kind: idle, state: open`, "turn ended, waiting for a prompt").

Both subject kinds
------------------
The loop gate is `--topic`-required and fail-opens on a goal, which is correct there (its
remedy is topic-only by an already-taken decision). This gate is not topic-only: both
commands it serves auto-detect a goal OR a topic, so a goal-branch manager must get the
same saving rather than fail-opening on every run while reading as armed.

State
-----
`~/.claude/state/manager-predispatch/<slug>.json`, keyed on the subject slug. Never inside
a repo — a file in a repo tree trips dirty-tree checks during somebody else's commit. The
stored table is what a no-change run replays, so the operator still sees the frame on
every run.
"""

from __future__ import annotations

import argparse
import glob
import hashlib
import importlib.util
import json
import os
import re
import select
import sys
import time
from datetime import datetime

STATE_DIR = os.path.expanduser(
    os.environ.get("MANAGER_PREDISPATCH_STATE_DIR", "~/.claude/state/manager-predispatch")
)
REGISTRY_DIR = os.path.expanduser("~/.claude/sessions")
FEED_DIR = os.path.expanduser("~/.claude/state/attention")

# The headless-worker heartbeat store, written by this plugin's `server/heartbeat.mjs`.
HEARTBEAT_DIR = os.environ.get("SUPERVISOR_HEARTBEAT_DIR") or os.path.join(
    os.environ.get("XDG_STATE_HOME")
    or os.path.join(os.path.expanduser("~"), ".local", "state"),
    "claude-supervisor",
    "live",
)
# Mirrors HEARTBEAT_TTL_MS in the server, exactly as the loop gate mirrors it. Two copies of
# one number, paid knowingly: this gate runs model-free and must not depend on a checkout
# being present, so it cannot import the constant. Changing one without the other fails in
# the safe direction — a longer TTL here reads a dead worker live, suppressing a report
# rather than inviting a duplicate spawn.
HEARTBEAT_TTL_SECONDS = 60

EXIT_NOCHANGE = 0
EXIT_CHANGE = 10
EXIT_USAGE = 2
# A stdin that never EOFs is neither a usage error nor the gate's verdict: nothing was
# read, so nothing was decided, and a caller must be able to tell that apart from both
# "unchanged" (0) and "changed" (10). Kept distinct from EXIT_USAGE deliberately — a
# caller branching on 2 is looking at an argument it got wrong, and this one is about the
# caller's stdin, which no argument describes.
EXIT_STDIN_TIMEOUT = 3
# `--write-tracked` is a different verb from the gate's three modes, so its success code
# is named separately even though it is also 0. Reusing EXIT_NOCHANGE would read to a
# caller branching on the gate's contract as "the digest is unchanged, dispatch no agent"
# — which is the opposite of what happened: a fresh set was just written.
EXIT_WRITE_OK = 0
# `--compare-tracked` reports a *disagreement between two memberships*, which is neither
# the gate's "changed" verdict nor a usage error. It shares 10 because the caller's
# response is the same shape — stop and look rather than proceed — while staying a
# separate name so a reader cannot mistake it for the digest verdict.
EXIT_DIVERGENT = 10

STUCK_SECONDS = 30 * 60

LIVENESS_LIVE = "live"
LIVENESS_PARKED = "parked"
LIVENESS_NONE = "none"

NO_CHANGE_MARKER = "NO-CHANGE"


# --------------------------------------------------------------------------- #
# vault reading
# --------------------------------------------------------------------------- #

_FM = re.compile(r"^---\n(.*?)\n---", re.S)
_PROGRESS = re.compile(r"^# Progress\s*\n(.*?)(?=\n# |\Z)", re.S | re.M)
_CHECKBOX = re.compile(r"^\s*-\s*\[([ xX/])\]", re.M)
_LIST_ITEM = re.compile(r"^\s*-\s*\[\[(.+?)\]\]")


def split_frontmatter(text: str) -> str | None:
    m = _FM.match(text)
    return m.group(1) if m else None


def fm_scalar(fm: str, key: str) -> str:
    m = re.search(rf"^{re.escape(key)}:\s*(.*)$", fm, re.M)
    return m.group(1).strip() if m else ""


def fm_wikilinks(fm: str, key: str) -> list[str]:
    """All [[...]] under `key:`, whether inline (`key: ['[[X]]']`) or block.

    All three shapes the vault writes must resolve: inline list, block list, and
    empty/absent. Block form is `key:` then indented `- '[[X]]'` lines.
    """
    m = re.search(rf"^{re.escape(key)}:(.*)$", fm, re.M)
    if not m:
        return []
    rest = m.group(1)
    if rest.strip() and rest.strip() != "[]":
        return re.findall(r"\[\[(.+?)\]\]", rest)
    block = re.search(rf"^{re.escape(key)}:\s*\n((?:[ \t]+-.*\n?)*)", fm, re.M)
    if not block:
        return []
    return re.findall(r"\[\[(.+?)\]\]", block.group(1))


def progress_hash(text: str) -> str:
    """A hash of the task's `# Progress` section — the sweep's change signal.

    Hashing the section is what lets the digest move on a Progress write that leaves
    status and phase alone, and what lets the stuck verdict honestly assert "no Progress
    entry" rather than "no frontmatter change".
    """
    m = _PROGRESS.search(text)
    return hashlib.sha256((m.group(1) if m else "").encode()).hexdigest()[:16]


def checkbox_count(text: str, section: str | None = None) -> str:
    """`n/m` ticked-vs-total checkboxes, optionally scoped to a `# <section>`.

    A section with no checkboxes returns `—` rather than `0/0`, which would read as a
    real reading of nothing.
    """
    body = text
    if section:
        m = re.search(rf"^# {re.escape(section)}\s*\n(.*?)(?=\n# |\Z)", text, re.S | re.M)
        if not m:
            return "—"
        body = m.group(1)
    boxes = _CHECKBOX.findall(body)
    if not boxes:
        return "—"
    return f"{sum(1 for b in boxes if b in 'xX')}/{len(boxes)}"


def read_task(path: str) -> dict | None:
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            text = fh.read()
            # `fstat` on the descriptor we just read, never a second `stat(path)`: the
            # path could be replaced between the two calls, and the idle clock has to
            # describe the file whose contents we actually hashed.
            mtime = os.fstat(fh.fileno()).st_mtime
    except OSError:
        return None
    fm = split_frontmatter(text)
    if fm is None:
        return None
    return {
        "name": os.path.basename(path)[:-3],
        "status": fm_scalar(fm, "status"),
        "phase": fm_scalar(fm, "phase"),
        "session": fm_scalar(fm, "claude_session_id").strip("'\""),
        "goals": fm_wikilinks(fm, "goals"),
        "progress_hash": progress_hash(text),
        "met": checkbox_count(text),
        "mtime": mtime,
    }


def declared_members(topic_path: str) -> list[str]:
    """Read a topic page's `## Goals` list — declared membership, never inferred.

    Only list items count. Prose wikilinks inside the section are NOT members: a topic
    page names sibling pages inside a bullet's prose, and a naive `[[...]]` sweep over
    the section admits those as members.
    """
    with open(topic_path, encoding="utf-8") as fh:
        text = fh.read()
    m = re.search(r"^## Goals\s*\n(.*?)(?=\n#{2,3} |\n# |\Z)", text, re.S | re.M)
    if not m:
        raise ValueError(f"no `## Goals` section in {topic_path}")
    members = []
    for line in m.group(1).splitlines():
        hit = _LIST_ITEM.match(line)
        if hit:
            members.append(hit.group(1).strip())
    return members


def resolve_tracked(vault: str, members: list[str]) -> list[dict]:
    """A member may be a goal (admits tasks whose `goals:` name it) or a task.

    Vault-only and pure: it never touches the registry, so a fixture can run it against a
    throwaway vault and get the same values the live run gets.
    """
    tasks_dir = os.path.join(vault, "25 Tasks")
    member_set = set(members)
    tracked: list[dict] = []
    for entry in sorted(os.listdir(tasks_dir)):
        if not entry.endswith(".md"):
            continue
        task = read_task(os.path.join(tasks_dir, entry))
        if task is None:
            continue
        if task["name"] in member_set:  # declared directly as a task entry
            tracked.append(task)
        elif member_set.intersection(task["goals"]):
            tracked.append(task)
    return sorted(tracked, key=lambda t: t["name"])


def page_type_of(path: str) -> str:
    """The `page_type:` frontmatter value, scoped to the frontmatter block.

    Unscoped it would match a guide's YAML template quoted in prose.
    """
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            fm = split_frontmatter(fh.read())
    except OSError:
        return ""
    return fm_scalar(fm, "page_type") if fm else ""


# The two directories `resolve_subject` reads. A root holding neither is not a vault this
# gate can read, and saying so beats reporting every subject as an unresolvable one.
VAULT_MARKERS = ("24 Goals", "23 Topics")


def vault_root_error(vault: str) -> str | None:
    """-> a message when `vault` is not a vault root, else None.

    A vault NAME is a relative path, so it resolves against the caller's cwd: from the
    vault's own directory it names nothing and every subject reads as a missing page,
    while from the vault's parent it silently resolves to whatever `<cwd>/<name>` is.
    Both are caller errors, and reporting either as an unresolvable *subject* sends the
    reader hunting for a page that is present on disk. Caught at the boundary so the
    subject message below stays true to its own words.
    """
    if not os.path.isabs(vault):
        return (
            f"--vault must be an absolute path: {vault!r} — a relative path (a vault "
            f"NAME included) resolves against the caller's cwd, so the same call gates a "
            f"different tree depending on where it runs"
        )
    if not os.path.isdir(vault):
        return f"--vault is not a directory: {vault!r}"
    if not any(os.path.isdir(os.path.join(vault, m)) for m in VAULT_MARKERS):
        return (
            f"--vault is not a vault root: {vault!r} — it holds no "
            + " or ".join(repr(m) for m in VAULT_MARKERS)
        )
    return None


def resolve_subject(vault: str, subject: str) -> tuple[str, list[str]]:
    """-> (branch, members). Raises ValueError unless exactly one page resolves.

    Both commands auto-detect goal vs topic; this gate must resolve the same way or it
    would gate a different tree than the command sweeps. Both-match and no-match are both
    refusals — never a silent preference, which is the failure the commands' own
    resolution section exists to prevent.

    The goal branch keys on the folder alone; the topic branch additionally requires
    `page_type: topic`. The asymmetry is measured, not stylistic. `page_type` was required
    on both sides to reject the convention guides that describe these page kinds, but
    those guides live outside the folders and an exact `-iname "$SUBJECT.md"` match inside
    the folder already excludes them. Measured 2026-09-27 in the primary vault: **88 of 258** goal
    pages carried no `page_type: goal` and were therefore unresolvable to this gate *and*
    to the commands that defer to it, while **11 of 11** topic pages carried
    `page_type: topic`. Requiring a field a third of the goal pages lack bought no
    discrimination and cost the whole goal branch.
    """
    goal_path = os.path.join(vault, "24 Goals", f"{subject}.md")
    topic_path = os.path.join(vault, "23 Topics", f"{subject}.md")
    is_goal = os.path.exists(goal_path)
    is_topic = os.path.exists(topic_path) and page_type_of(topic_path) == "topic"
    if is_goal and is_topic:
        raise ValueError(f"ambiguous subject: both {goal_path} and {topic_path} resolve")
    if is_goal:
        return "goal", [subject]
    if is_topic:
        return "topic", declared_members(topic_path)
    raise ValueError(f"no subject page for {subject!r} in 24 Goals/ or 23 Topics/")


# --------------------------------------------------------------------------- #
# liveness — model-free, from the registry, the heartbeat store and the feed
# --------------------------------------------------------------------------- #

PARKED_VERBS = ("later (on ",)
_ENTITY = re.compile(r"&(?:nbsp|#160|#xa0);", re.I)

# Distinguishes "read the store for me" from a caller passing its own verdict — including
# one that is `None`, which is a real answer ("could not read") and must not be mistaken
# for "you read it".
_HEARTBEAT_UNREAD = object()


def heartbeat_live(sid: str, now: float | None = None) -> bool | None:
    """True / False / None for one session id. None is "could not read", never "not live".

    ⚠️ The verdict is the stamp's AGE against the TTL, never the file's existence — see the
    module docstring. None and False stay distinct so an I/O error is never folded into a
    confident negative.
    """
    now = time.time() if now is None else now
    try:
        age = now - os.stat(os.path.join(HEARTBEAT_DIR, f"{sid}.json")).st_mtime
    except FileNotFoundError:
        return False
    except OSError:
        return None
    return age < HEARTBEAT_TTL_SECONDS


_LIVENESS = None


def _session_liveness():
    """Import session-liveness.py (hyphenated filename -> importlib) — the one registry reader."""
    global _LIVENESS
    if _LIVENESS is None:
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "session-liveness.py")
        spec = importlib.util.spec_from_file_location("session_liveness", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _LIVENESS = mod
    return _LIVENESS


def read_registry() -> dict[str, dict]:
    """`~/.claude/sessions/<pid>.json` -> {sessionId: {pid, status, name, alive}}.

    The registry is pruned on exit, so a record with a live pid is a positive signal. A
    missing record proves nothing: a headless worker has none.

    ⚠️ **One reader for the whole plugin.** The glob, the pid check and the `alive` rule now
    live in `scripts/session-liveness.py`. A second instrument over the same registry is what
    let an 8-char prefix read as `ABSENT` on 2026-09-26 while its session was live, and this
    reader was one of the copies.

    ⚠️ **`None` is flattened to `{}` here, preserving this caller's existing contract.** An
    unreadable registry therefore reads as "no live session" — the dangerous direction the
    shared reader refuses. Recorded as a residual rather than changed silently inside a
    conversion: flipping it is a rule change for this caller, not a refactor.
    """
    return _session_liveness().read_registry(REGISTRY_DIR) or {}


def read_feed() -> dict[str, dict]:
    """`~/.claude/state/attention/*.needs.json` -> {sessionId: record}."""
    out: dict[str, dict] = {}
    for path in glob.glob(os.path.join(FEED_DIR, "*.needs.json")):
        try:
            with open(path, encoding="utf-8") as fh:
                d = json.load(fh)
        except (OSError, json.JSONDecodeError):
            continue
        sid = d.get("session_id")
        if sid:
            out[sid] = d
    return out


def is_open_gate(rec: dict | None) -> bool:
    """The attention feed's own park rule. A park is a RAISED gate the operator has not
    answered — `state: "open"` alone is not a park, since the dominant record shape is
    `kind: idle, state: open`, meaning "turn ended, waiting for a prompt"."""
    if not rec:
        return False
    if rec.get("state") == "answered":
        return False
    if rec.get("kind") not in ("permission", "question"):
        return False
    detail = " ".join(_ENTITY.sub(" ", rec.get("detail") or "").split())
    return not detail.startswith(PARKED_VERBS)


def liveness_of(
    sid: str, registry: dict, feed: dict, heartbeat: bool | None = _HEARTBEAT_UNREAD
) -> str:
    """live / parked / none for one session id, from the registry PLUS the heartbeat.

    The registry answers for every session that holds a socket — every interactive one —
    and the heartbeat answers for the headless workers it structurally cannot see.
    Neither substitutes for the other, and the registry is consulted FIRST: it carries a
    real pid and a real status, so it outranks a heartbeat age threshold and is what keeps
    a live worker with a stale heartbeat from being reported dead.
    """
    if not sid:
        return LIVENESS_NONE
    rec = registry.get(sid)
    if not rec or not rec["alive"]:
        beat = heartbeat_live(sid) if heartbeat is _HEARTBEAT_UNREAD else heartbeat
        if beat is not True:
            return LIVENESS_NONE
        return LIVENESS_PARKED if is_open_gate(feed.get(sid)) else LIVENESS_LIVE
    if is_open_gate(feed.get(sid)) or rec["status"] == "waiting":
        return LIVENESS_PARKED
    return LIVENESS_LIVE


def enrich_liveness(tracked: list[dict], registry: dict, feed: dict) -> None:
    for t in tracked:
        t["liveness"] = liveness_of(t["session"], registry, feed)


def open_box_count(met: str) -> int:
    """Open boxes from the `n/m` reading `checkbox_count` emits.

    `checkbox_count` returns `—` for a section holding no boxes. That is zero open
    boxes, not a parse failure, and the runbook's qualifier is `>=1 open box`, so both
    read the same way here.
    """
    if "/" not in met:
        return 0
    done, _, total = met.partition("/")
    try:
        return int(total) - int(done)
    except ValueError:
        return 0


def idle_stuck(t: dict, now_ts: float) -> bool:
    """Branch (b): idle past the threshold, in `execution`, holding an open box.

    Four gates, all required. The `phase: execution` and `>=1 open box` qualifiers are
    the runbook's own. The idle duration is the task file's mtime — the rule's
    *"task file unchanged"* measure, since a write to the file is precisely what makes
    it changed, and the reader's shipped branch (b) derives the same duration from the
    same signal.

    ⚠️ The WAITING guard is not decoration. The runbook is explicit that a row
    demonstrably parked on a human or a dependency is NOT stuck, and that the
    file-unchanged proxy cannot tell the two apart. The parked carrier is already
    computed in this file (`liveness_of` -> `LIVENESS_PARKED`), so a branch (b) that
    ignored it would flag exactly the workers the rule exists to leave alone.
    """
    if t.get("liveness") == LIVENESS_PARKED:
        return False
    if t.get("phase") != "execution":
        return False
    if open_box_count(t.get("met", "—")) < 1:
        return False
    mtime = t.get("mtime")
    if mtime is None:
        return False
    return (now_ts - float(mtime)) >= STUCK_SECONDS


def apply_stuck(tracked: list[dict], registry: dict, prev_busy: dict, now_ts: float) -> dict:
    """Set `t['stuck']` and return the busy-since map to persist.

    ⚠️ Both branches of the runbook's rule, not just the busy one.

    **(a) busy > ~30 min** — the registry gives a session's current status but not how
    long it has held it, so the clock is observed across runs; a Progress write resets
    it, which is what keeps a working worker off the stuck row.

    **(b) idle > ~30 min in `phase: execution` with an open box** — see `idle_stuck`.
    It carries no persisted map, because its duration is the task file's mtime and an
    mtime is absolute: there is no clock to observe across runs.

    The branches are disjoint — (b) is reached only when the session is not busy — so
    no row can be flagged by both.
    """
    new_busy: dict[str, dict] = {}
    for t in tracked:
        rec = registry.get(t["session"]) if t["session"] else None
        busy = bool(rec and rec["alive"] and rec["status"] == "busy")
        if not busy:
            t["stuck"] = idle_stuck(t, now_ts)
            continue
        prev = prev_busy.get(t["name"])
        if prev and prev.get("progress") == t["progress_hash"]:
            since = float(prev.get("since", now_ts))
            t["stuck"] = (now_ts - since) >= STUCK_SECONDS
            new_busy[t["name"]] = {"since": since, "progress": t["progress_hash"]}
        else:
            t["stuck"] = False
            new_busy[t["name"]] = {"since": now_ts, "progress": t["progress_hash"]}
    return new_busy


# --------------------------------------------------------------------------- #
# digest
# --------------------------------------------------------------------------- #


HOLDS_PATH = os.path.expanduser(
    os.environ.get("SUPERVISOR_SESSION_HOLDS", "~/.claude/state/session-holds.json")
)


def read_holds() -> dict:
    """The operator's session holds, keyed on session id. `{}` on any read failure.

    Inlined rather than shelling out to session-holds.py: the plugin's scripts do not
    import each other, and a subprocess dependency on a plugin path is a fail-open.
    Reads are lock-free because the writer lands every change through `os.replace`.

    A missing or corrupt store reads as *nothing held*. Here that direction is the safe
    one: inventing a hold would move the digest and wake the loop for a hold nobody set,
    while reading a real store as empty merely leaves the digest unchanged.
    """
    try:
        with open(HOLDS_PATH, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return {}
    holds = data.get("holds") if isinstance(data, dict) else None
    return holds if isinstance(holds, dict) else {}


def is_held(session_id: str) -> bool:
    """True when this session carries a hold. Never raises."""
    if not session_id:
        return False
    return isinstance(read_holds().get(session_id), dict)


def digest_of(tracked: list[dict]) -> str:
    """What the sweep would render, plus the Progress signal it reports.

    `liveness` and `stuck` are set by the caller before this runs; both default to absent
    so a caller that has not enriched them (a fixture) still gets a stable digest.

    ⚠️ **The hold is a digest input, and deliberately NOT a suppression here.** This gate
    decides whether the sweep runs at all, so suppressing it for a held session would
    stop the sweep and the held row would never render -- the exact failure the hold
    design forbids, where a row that disappears from a sweep is indistinguishable from a
    row that got fixed. Including it as an input is what makes a hold being ADDED or
    RELEASED move the digest, so the loop wakes and re-renders the row.
    """
    h = hashlib.sha256()
    for t in sorted(tracked, key=lambda x: x["name"]):
        h.update(
            (
                f"{t['name']}|{t['status']}|{t['phase']}|{t['session']}"
                f"|{t.get('progress_hash', '')}|{t.get('liveness', LIVENESS_NONE)}"
                f"|{int(bool(t.get('stuck')))}|{int(is_held(t['session']))}\n"
            ).encode()
        )
    h.update(b"--members--\n")
    for t in sorted(tracked, key=lambda x: x["name"]):
        h.update(f"{t['name']}\n".encode())
    return h.hexdigest()


# --------------------------------------------------------------------------- #
# bounded stdin
# --------------------------------------------------------------------------- #

# How long `--save` will wait for stdin to reach EOF before giving up on it.
# Deliberately small: every legitimate caller (`< /dev/null`, a regular file, a closed
# fd 0) EOFs in microseconds, so only a stdin that is never closed reaches this. The
# number is a ceiling on a stall, not a budget for one.
STDIN_WAIT_SECONDS = 5


def read_stdin_bounded(timeout: float | None = None) -> str | None:
    """Read stdin to EOF, but never wait longer than `timeout`. `None` means it did not end.

    `sys.stdin.read()` blocks until EOF, and a caller whose stdin is an open pipe that is
    never closed blocks it forever. Measured 2026-09-30 against the Manager Layer store:
    45 013 ms and killed, versus 654 ms for the same call with `< /dev/null` — and the
    store was left byte-for-byte unchanged, because the save never ran. That is the whole
    cost: the caller's tick gets a stale record, and the next drive leg holds its batch.

    Reading in a `select` loop rather than one `select` followed by `read()`: a caller that
    writes part of a table and holds the pipe open passes a single readability check and
    then blocks *inside* `read()` — the same stall, one layer down.

    The harness's own stdin wiring is not stable across calls (measured in one session,
    minutes apart: `sock` with a read that blocked past 3 s, then the same read returning
    0 bytes immediately), so a caller cannot be asked to redirect its way out of this.
    The bound has to live here.
    """
    try:
        fd = sys.stdin.fileno()
    except (AttributeError, OSError, ValueError):
        # No fd behind stdin: a closed fd 0, or an in-process caller that swapped
        # `sys.stdin` for a plain object — the test suite does exactly that with
        # `io.StringIO`, and `save_with_table` relies on it to drive the refusal below.
        # Neither can stall, because neither is a pipe, so the plain read is correct
        # here; only a real fd can be an open pipe that never EOFs.
        try:
            return sys.stdin.read()
        except (AttributeError, OSError, ValueError):
            return ""
    # Resolved here rather than as a default argument so a test can lower the module
    # constant and exercise the deadline in milliseconds instead of seconds.
    if timeout is None:
        timeout = STDIN_WAIT_SECONDS
    chunks: list[bytes] = []
    deadline = time.monotonic() + timeout
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None
        try:
            ready, _, _ = select.select([fd], [], [], remaining)
        except (OSError, ValueError):
            return ""
        if not ready:
            return None
        try:
            # `os.read` on the fd rather than `sys.stdin.read()`: the text wrapper would
            # re-block waiting for EOF, which is the very thing this bounds.
            chunk = os.read(fd, 65536)
        except OSError:
            return ""
        if not chunk:
            return b"".join(chunks).decode("utf-8", errors="replace")
        chunks.append(chunk)


# --------------------------------------------------------------------------- #
# stored state + fail-open
# --------------------------------------------------------------------------- #


def slug(subject: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", subject.lower()).strip("-")


def state_path(subject: str) -> str:
    return os.path.join(STATE_DIR, f"{slug(subject)}.json")


def tracked_path(subject: str) -> str:
    """Where the caller's own tracked set lands, keyed by the same subject slug.

    Deliberately a sibling of `state_path`, not a field inside it: the two hold
    different things. The state file holds this gate's *verdict* — digest, table,
    `recorded_at` — while this holds the caller's *input*, the names it resolved
    from the page. They can disagree, and that disagreement is the point.
    """
    return os.path.join(STATE_DIR, f"{slug(subject)}.tracked.txt")


def buckets_path(subject: str) -> str:
    """Where the caller's per-bucket classification lands before `--save` folds it in.

    A staging file rather than an argument because a bucket set is a dict of every
    tracked name the caller classified — hundreds of names, which cannot ride a
    command line, and which must not be concatenated onto `--save`'s stdin, whose
    whole content is the table the no-change branch replays verbatim.
    """
    return os.path.join(STATE_DIR, f"{slug(subject)}.buckets.json")


def verdicts_path(subject: str) -> str:
    """Where the drive leg's `Audit` block lands, keyed by the same subject slug.

    The cache clause (1) names — `<slug>.verdicts.json`, beside the `<slug>.buckets.json`
    the caller already writes its `bucket_sets` into — and the counterpart of
    `buckets_path`. Clause (1) assigns the write to the *caller* ("The write is the
    caller's … let the caller persist them") and the leg holds no write tool, so the
    caller needs a writer it can reach without `Write` or a shell redirect: that is
    `--write-verdicts`.

    Unlike `buckets_path` this is not a staging file — nothing folds it into the record.
    The store is the gate's verdict over the *tree*; this is the leg's verdict over *rows*,
    and clause (1) reads it back on the next tick to decide what to re-audit.
    """
    return os.path.join(STATE_DIR, f"{slug(subject)}.verdicts.json")


def payload_path(subject: str) -> str:
    """Where the render writes the table it just rendered, for `--save` to read back.

    A sibling of `state_path` for the same reason `tracked_path` is one: the two hold
    different things and can disagree. The state file holds the gate's *verdict*; this
    holds the render's *output*, and it carries the render's own mtime — which is what
    lets a save be dated from when the table was produced rather than from when the
    caller got round to storing it. That gap is the whole defect: a payload held for 23
    minutes and then saved lands as a fresh record unless the two moments are tied.
    """
    return os.path.join(STATE_DIR, f"{slug(subject)}.table")


def loop_snapshot_path(vault: str, subject: str) -> str:
    """The model-free loop's own snapshot for this subject.

    Derived rather than passed, because the whole point of the comparison is to
    check the caller's set against a membership the caller did NOT produce. A
    caller that hands over the path can hand over the wrong one, and a wrong path
    that reads clean is indistinguishable from agreement.

    The convention is the loop tick's, read from `sweep-gate-notify-tick.sh`:
    `<basename(vault) lowercased>/<slug(subject)>.snapshot.json` under
    `$SWEEP_GATE_BASE` — the same env var that script honours, so a host running
    the loop elsewhere is compared against the loop it actually runs.
    """
    base = os.path.expanduser(
        os.environ.get("SWEEP_GATE_BASE", "~/.claude/state/sweep-gate-loop")
    )
    vname = os.path.basename(os.path.normpath(vault)).lower()
    return os.path.join(base, vname, f"{slug(subject)}.snapshot.json")


def load_state(subject: str) -> dict:
    """The raw stored payload, or {} — used for the cross-run busy-since map."""
    try:
        with open(state_path(subject), encoding="utf-8") as fh:
            data = json.loads(fh.read())
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def load_stored(subject: str) -> tuple[str | None, str, str | None]:
    """-> (digest, stored_table, fail_reason). fail_reason set => force a full sweep."""
    path = state_path(subject)
    if not os.path.exists(path):
        return None, "", "first run — no state file"
    try:
        with open(path, encoding="utf-8") as fh:
            raw = fh.read()
    except OSError as exc:
        return None, "", f"state unreadable ({exc.strerror or exc})"
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        return None, "", f"state parse error ({exc.msg})"
    if not isinstance(data, dict) or "digest" not in data:
        return None, "", "state parse error (no digest key)"
    table = data.get("table") or ""
    if not isinstance(table, str) or not table.strip():
        # A snapshot with no table cannot be replayed, so it cannot make a run free.
        return None, "", "state has no stored table"
    return str(data["digest"]), table, None


def strip_links(text: str) -> str:
    """Drop OSC 8 hyperlink escapes, keeping the text they wrapped.

    The jump link's URI carries the jump token, so anything written to disk must not
    carry the escape — and the stored table IS written to disk, to be copied, synced or
    pasted. `box-table.py` emits OSC8_OPEN + url + OSC8_ST + text + OSC8_OPEN + OSC8_ST,
    so removing the sequences leaves the visible table intact and the token absent. Same
    function, same reason, as the loop gate's.
    """
    return re.sub(r"\x1b\]8;[^\x07\x1b]*(?:\x07|\x1b\\)", "", text)


def save_stored(
    subject: str,
    branch: str,
    digest: str,
    table: str,
    tracked: list[dict],
    busy: dict,
    bucket_sets: dict | None = None,
    recorded_at: str | None = None,
) -> None:
    """Persist atomically (tmp + os.replace).

    The replace is the same discipline `fleet-snapshot.py` documents: a plain
    `open(path, "w")` truncates first, so a crash or a serialisation error part-way
    through leaves a truncated snapshot where a valid one stood — and the next run then
    diffs against nothing. `os.replace` is atomic within a filesystem: a reader sees
    either the old snapshot or the new one, never a half-written file.
    """
    os.makedirs(STATE_DIR, exist_ok=True)
    payload = {
        "subject": subject,
        "branch": branch,
        "digest": digest,
        # Stored link-free — see strip_links. The replay prints this text, so a stored
        # OSC 8 escape would hand the operator a jump coordinate resolved at the earlier
        # render, and pane ids are renumbered by a WezTerm restart without moving any
        # digest input. The no-change branch therefore replays no coordinates at all.
        "table": strip_links(table),
        "members": len(tracked),
        "busy_since": busy,
        # Dated from the payload's own mtime whenever the caller read the table back from
        # `payload_path`, so a record can never postdate the render it stores — the
        # invariant that turns a stale payload into a visible old date instead of a
        # silent fresh one. Falls back to now for a caller still piping the table in.
        "recorded_at": recorded_at
        or datetime.now().astimezone().isoformat(timespec="seconds"),
    }
    # The caller's per-bucket classification, persisted so it survives a compaction and a
    # fresh manager. This is the half the drive leg's clause (0) requires and the snapshot
    # schema cannot supply — buckets are the *caller's* classification, so a per-bucket set
    # is never derivable from the snapshot (single home: `docs/fleet-surface.md` § Session
    # end). Omitted rather than defaulted when the caller passed none, so a record that
    # lacks the half reads as "not persisted" instead of as "persisted and empty".
    if bucket_sets is not None:
        payload["bucket_sets"] = bucket_sets
    tmp = state_path(subject) + ".tmp"
    try:
        # 0600 at creation rather than by a chmod after it: the file is never
        # world-readable, not even for the instant between the write and the chmod.
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2)
        os.replace(tmp, state_path(subject))
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


# --------------------------------------------------------------------------- #
# verdict
# --------------------------------------------------------------------------- #


def evaluate(vault: str, subject: str) -> tuple[bool, str, dict, str]:
    """-> (changed, reason, payload, stored_table).

    Every failure path returns `changed=True` — the fail-open rule. A gate that reports
    "no change" when it cannot tell is the one failure worth a dispatch to avoid.
    """
    try:
        branch, members = resolve_subject(vault, subject)
    except (ValueError, OSError) as exc:
        return True, f"fail-open: subject unresolvable ({exc})", {}, ""

    try:
        tracked = resolve_tracked(vault, members)
    except OSError as exc:
        return True, f"fail-open: tracked set unreadable ({exc})", {}, ""

    registry = read_registry()
    feed = read_feed()
    stored, stored_table, fail_reason = load_stored(subject)
    prev = load_state(subject)
    prev_busy = prev.get("busy_since") or {}

    now_ts = time.time()
    enrich_liveness(tracked, registry, feed)
    busy = apply_stuck(tracked, registry, prev_busy, now_ts)
    digest = digest_of(tracked)

    changed = fail_reason is not None or stored != digest
    reason = fail_reason or ("digest differs" if changed else "digest equal")
    payload = {
        "branch": branch,
        "digest": digest,
        "tracked": tracked,
        "busy": busy,
        "recorded_at": prev.get("recorded_at", ""),
        # The per-bucket half as it stands in the record, so `--save` can tell a freshly
        # staged classification from the one already stored. It is deliberately NOT folded
        # into `digest_of`: that function's contract is "what the sweep would render", a
        # pure function of the tracked set, and a caller-supplied argument has no business
        # moving it. The comparison lives at the save decision instead — see `--save`.
        "stored_bucket_sets": prev.get("bucket_sets"),
    }
    return changed, reason, payload, stored_table


def bucket_shape_error(parsed) -> str | None:
    """Why `parsed` cannot gate, or None when it is a usable classification.

    A dict of bucket -> non-empty list of names is the only shape that can satisfy the
    drive leg's clause (0). A count, a bare list, or a bucket mapped to nothing would all
    *look* like a classification and gate nothing — and the empty list is the one that got
    through: `all(...)` over `[]` is vacuously True, so `{"done": []}` passed the check
    whose own message says "non-empty". Measured 2026-09-28, staged at exit 0 and saved.

    Shared by BOTH doors into the record. Validating only `--write-buckets` left the
    `--save --buckets` read path a bare `json.load`, so a hand-written or stale staging
    file reached `save_stored` with an all-empty set — the same defect by the other door.
    """
    if not isinstance(parsed, dict) or not parsed:
        return "bucket sets must be a non-empty JSON object"
    for bucket, names in parsed.items():
        if (
            not isinstance(names, list)
            or not names
            or not all(isinstance(n, str) and n.strip() for n in names)
        ):
            return f"bucket {bucket!r} must map to a non-empty list of names"
    return None


def verdicts_shape_error(parsed) -> str | None:
    """Why `parsed` cannot serve as a verdicts cache, or None when it is usable.

    A dict of task name -> `{verdict, score, content_key}` is the only shape clause (1)
    can read. The load-bearing key is `content_key`: clause (1) re-audits a row when its
    *file content* changes, so a stored verdict carrying no key can never be told from a
    stale one — and it would be read back as a cache hit on every tick forever. `score` is
    deliberately allowed to be null, because `blocked` rows carry none and the sibling
    caches on disk show exactly that shape.

    The empty-object case is refused for the reason `bucket_shape_error` gives: `{}` gates
    nothing while looking like a cache that was written.
    """
    if not isinstance(parsed, dict) or not parsed:
        return "verdicts must be a non-empty JSON object"
    for name, entry in parsed.items():
        if not isinstance(name, str) or not name.strip():
            return "each key must be a non-blank task name"
        if not isinstance(entry, dict):
            return f"entry {name!r} must be an object"
        verdict = entry.get("verdict")
        if not isinstance(verdict, str) or not verdict.strip():
            return f"entry {name!r} must carry a non-blank verdict"
        score = entry.get("score")
        if score is not None and (isinstance(score, bool) or not isinstance(score, int)):
            return f"entry {name!r} score must be an integer or null"
        key = entry.get("content_key")
        if not isinstance(key, str) or not key.strip():
            return f"entry {name!r} must carry a non-blank content_key"
    return None


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="manager pre-dispatch change gate")
    ap.add_argument("--subject", required=True, help="goal or topic name")
    ap.add_argument(
        "--vault", required=True, help="vault root PATH (never the vault name)"
    )
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true", help="verdict only")
    mode.add_argument(
        "--print", action="store_true", help="verdict; replay the stored table on no-change"
    )
    mode.add_argument(
        "--save",
        action="store_true",
        help=(
            "persist the digest + the payload `--write-payload` wrote (a table on stdin is "
            f"refused, and a stdin that does not EOF within {STDIN_WAIT_SECONDS} s exits "
            f"{EXIT_STDIN_TIMEOUT})"
        ),
    )
    mode.add_argument(
        "--write-tracked",
        action="store_true",
        help="write the caller's own tracked set (names on stdin) to <slug>.tracked.txt",
    )
    mode.add_argument(
        "--write-buckets",
        action="store_true",
        help="stage the caller's per-bucket classification (JSON on stdin) for --save",
    )
    mode.add_argument(
        "--write-verdicts",
        action="store_true",
        help="persist the drive leg's Audit block (JSON on stdin) to <slug>.verdicts.json",
    )
    mode.add_argument(
        "--write-payload",
        action="store_true",
        help="write the rendered table (on stdin) to <slug>.table, for --save to read",
    )
    mode.add_argument(
        "--compare-tracked",
        action="store_true",
        help="compare the staged tracked set against the loop snapshot's membership",
    )
    ap.add_argument(
        "--buckets",
        default=None,
        help="with --save: path to the staged bucket JSON (see --write-buckets)",
    )
    args = ap.parse_args(argv)

    vault_error = vault_root_error(args.vault)
    if vault_error:
        # A usage error is not a verdict. Exit 2 is the code the commands' own branch
        # already documents as "fix the call; do not read it as changed", so the caller
        # is told the argument was wrong instead of being handed a fail-open sweep.
        print(vault_error, file=sys.stderr)
        return EXIT_USAGE

    if args.write_tracked:
        # The caller's OWN scan is the only producer of this file — it is never derived
        # here, because `evaluate()` computes a set from the same declarations and the two
        # can disagree (measured 2026-09-27: 154 declared vs 153 derived, differing by a
        # case-only name mismatch nothing else could see). What this mode buys is the
        # *transport*: routing the write through this script keeps the first token
        # `python3`, which matches the `Bash(python3:*)` grant all three manager commands
        # already hold — so a 15-minute loop writes its set without a permission prompt,
        # and never needs `Write` (granted by none of them) or a shell redirect.
        names = [ln.strip() for ln in sys.stdin.read().splitlines() if ln.strip()]
        if not names:
            # Same discipline as `--save`'s empty-table guard: a failed read must not
            # clobber a good set, and a set with no names can never be dispatched against.
            print(
                "refusing to write an empty tracked set: nothing on stdin, so the "
                "previous file is left unchanged.",
                file=sys.stderr,
            )
            return EXIT_USAGE
        path = tracked_path(args.subject)
        tmp = path + ".tmp"
        os.makedirs(STATE_DIR, exist_ok=True)
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                fh.write("\n".join(names) + "\n")
            # Atomic, for the same reason the snapshot write is: a reader sees either the
            # old set or the new one, never a half-written file — and the whole point of
            # this artifact is that a dispatch can trust what it reads.
            os.replace(tmp, path)
        except BaseException:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise
        # The path on stdout is the value the dispatch carries; print it alone so the
        # caller can capture it without parsing.
        print(path)
        return EXIT_WRITE_OK

    if args.write_buckets:
        # Staged, not stored: `--save` is what folds this into the record, and it runs
        # later in the same tick. Keeping the two apart means a caller that classifies but
        # never saves leaves no half-written record, and `--save`'s no-change branch —
        # which writes nothing at all — cannot silently drop a bucket set it was handed.
        try:
            parsed = json.loads(sys.stdin.read())
        except json.JSONDecodeError as exc:
            print(f"bucket sets must be JSON: {exc}", file=sys.stderr)
            return EXIT_USAGE
        shape_error = bucket_shape_error(parsed)
        if shape_error:
            print(shape_error, file=sys.stderr)
            return EXIT_USAGE
        path = buckets_path(args.subject)
        tmp = path + ".tmp"
        os.makedirs(STATE_DIR, exist_ok=True)
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(parsed, fh, indent=2, sort_keys=True)
            os.replace(tmp, path)
        except BaseException:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise
        print(path)
        return EXIT_WRITE_OK

    if args.write_verdicts:
        # The writer clause (1) delegates to the caller and the tree never provided. The
        # leg reports each row's verdict, score and content key under its `Audit` block
        # and holds no write tool; the caller is told to persist them, but the only tool
        # the manager commands are granted is `Bash(python3:*)` — no `Write`, no shell
        # redirect. So the write is routed through this script for the same transport
        # reason `--write-tracked` is, and the first token stays `python3`.
        #
        # Stored as its own file rather than folded into the record: the store is the
        # gate's verdict over the *tree*, this is the leg's verdict over *rows*, and
        # clause (1) reads it back by content key on the next tick.
        try:
            parsed = json.loads(sys.stdin.read())
        except json.JSONDecodeError as exc:
            print(f"verdicts must be JSON: {exc}", file=sys.stderr)
            return EXIT_USAGE
        shape_error = verdicts_shape_error(parsed)
        if shape_error:
            # A refused write must not clobber a good cache. The reason is sharper here
            # than for the other staging files: a cache that reads back as present but
            # unusable is indistinguishable from one that was never written, and every
            # later tick then re-audits cold while believing it has a cache — the exact
            # state this writer exists to end.
            print(f"refusing to write these verdicts: {shape_error}", file=sys.stderr)
            return EXIT_USAGE
        path = verdicts_path(args.subject)
        tmp = path + ".tmp"
        os.makedirs(STATE_DIR, exist_ok=True)
        try:
            # 0600 at creation rather than by a chmod after it, matching `save_stored`:
            # the file is never world-readable, not even for the instant between the
            # write and the chmod.
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(parsed, fh, indent=2, sort_keys=True)
            os.replace(tmp, path)
        except BaseException:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise
        print(path)
        return EXIT_WRITE_OK

    if args.write_payload:
        # The render's own transport, the same shape `--write-tracked` uses and for the
        # same reason: routing the write through this script keeps the first token
        # `python3`, which matches the `Bash(python3:*)` grant the sweep reader already
        # holds. What it changes is *when* the write happens — at render time, to a path
        # derived here rather than passed in — so `--save` can read the table back
        # instead of depending on the caller to hand-pipe it, and the two moments the
        # record's halves are stamped stop coming apart.
        table = sys.stdin.read()
        if not table.strip():
            # Same discipline as `--save`'s empty-table guard and `--write-tracked`'s
            # empty-set guard: a failed render must not clobber a good payload, because a
            # payload with no table in it can never be replayed.
            print(
                "refusing to write an empty payload: nothing on stdin, so the previous "
                "file is left unchanged.",
                file=sys.stderr,
            )
            return EXIT_USAGE
        path = payload_path(args.subject)
        tmp = path + ".tmp"
        os.makedirs(STATE_DIR, exist_ok=True)
        try:
            # Both halves of the pattern `save_stored` already holds, and for its reasons.
            # The rendered frame's OSC 8 jump link carries the jump token, and this file is
            # a disk artifact in a shared state dir — so it is written link-free and 0600
            # *at creation*, never world-readable even for the instant between the write and
            # the replace. Stripping here rather than at the caller is the point: one
            # `render_table()` frame serves both the operator's screen and this file, so a
            # caller-side strip would take the on-screen links with it. The frame the caller
            # prints keeps its links; only the copy on disk loses them.
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(strip_links(table))
            # Atomic, for the reason `--write-tracked` gives: a reader sees either the old
            # payload or the new one, never a half-written file.
            os.replace(tmp, path)
        except BaseException:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise
        print(path)
        return EXIT_WRITE_OK

    if args.compare_tracked:
        # A dispatch whose tracked set disagrees with the gate's own membership is
        # reported, not accepted. The two memberships are produced by different code from
        # the same declarations, so each is self-consistent while they disagree — measured
        # 2026-09-27: 154 declared against 153 in the snapshot, differing by one name whose
        # only fault was a capital `The` against a case-sensitive membership compare.
        # Nothing else could see it: APFS is case-insensitive, so `os.path.exists`,
        # `open()`, Obsidian's link resolver and `ls` all succeed on the capitalised path,
        # and no sweep notices because neither source is internally inconsistent.
        try:
            with open(tracked_path(args.subject), encoding="utf-8") as fh:
                mine = {ln.strip() for ln in fh if ln.strip()}
        except OSError as exc:
            print(f"no staged tracked set to compare ({exc})", file=sys.stderr)
            return EXIT_USAGE
        snap = loop_snapshot_path(args.vault, args.subject)
        try:
            with open(snap, encoding="utf-8") as fh:
                gate = set(json.load(fh).get("tasks") or {})
        except (OSError, json.JSONDecodeError) as exc:
            # Fail-open in the gate's own sense: an unverifiable membership is never
            # reported as agreement. "I could not check" and "they match" are precisely
            # the two states this mode exists to tell apart, so collapsing them would
            # reintroduce the defect one level down.
            print(f"COMPARE unavailable — {snap} unreadable ({exc})")
            print(f"  caller {len(mine)} names · gate unknown — NOT compared")
            return EXIT_DIVERGENT
        if not gate:
            print(f"COMPARE unavailable — {snap} carries no `tasks` membership")
            print(f"  caller {len(mine)} names · gate 0 — NOT compared")
            return EXIT_DIVERGENT
        only_mine = sorted(mine - gate)
        only_gate = sorted(gate - mine)
        print(f"COMPARE caller {len(mine)} names · gate {len(gate)} names")
        if not only_mine and not only_gate:
            print("  memberships identical")
            return EXIT_NOCHANGE
        print(
            f"⚠️ DIVERGENCE: {len(only_mine)} in the caller's set only, {len(only_gate)} in "
            "the gate's only — a dispatch on either is a dispatch on a set nothing has "
            "reconciled, and both sources read as self-consistent"
        )
        for label, names in (("caller-only", only_mine), ("gate-only", only_gate)):
            for n in names[:10]:
                print(f"  {label}: {n}")
            if len(names) > 10:
                print(f"  … and {len(names) - 10} more {label}")
        return EXIT_DIVERGENT

    changed, reason, payload, stored_table = evaluate(args.vault, args.subject)

    if args.save:
        table = read_stdin_bounded()
        if table is None:
            print(
                f"refusing to wait on stdin: no EOF within {STDIN_WAIT_SECONDS} s. `--save` "
                f"reads the payload the render wrote ({payload_path(args.subject)}), so it "
                "does not need a table on stdin — re-run it with `< /dev/null`, or with no "
                "redirect at all.",
                file=sys.stderr,
            )
            return EXIT_STDIN_TIMEOUT
        # A table on stdin is refused, never recorded. `--save` reads the payload the
        # render wrote and dates the record from *its* mtime — the hop this gate used to
        # leave to the caller's memory. A caller that hands the table over itself moves the
        # stamp to save time instead, so a stale table reads as fresh and the next tick
        # replays it. Measured 2026-09-30 on the MDM Bugs record: `recorded_at` 11:41:29
        # against a payload written 11:35:22, written by a `--save … < <payload>.table`
        # redirect. The shape is in no shipped command block (the heredoc left the three at
        # 0.63.0), so nothing legitimate depends on it — but the CLI's own help and
        # docstring still advertised it, which is what invited the call.
        #
        # The read is bounded (see `read_stdin_bounded`). It used to be a bare
        # `sys.stdin.read()`, which stalled a manager tick for 45 s on a stdin that never
        # EOFs and left the store a full tick stale — [[The Pre-Dispatch Save Blocks on an
        # Open Stdin Pipe and Silently Stalls the Loop]]. Refusing on a non-empty read is
        # what makes the caller-supplied table impossible; the bound is what makes the
        # refusal reachable at all.
        if table.strip():
            print(
                "refusing a table on stdin: `--save` reads the payload the render wrote "
                f"({payload_path(args.subject)}) so the record is dated from its mtime, not "
                "from save time. Write the table with `--write-payload`, then re-run "
                "`--save` with no stdin.",
                file=sys.stderr,
            )
            return EXIT_USAGE
        # An absent payload falls through to the empty-table guard below, which already
        # refuses rather than clobbering a good snapshot.
        rendered_at = None
        try:
            with open(payload_path(args.subject), encoding="utf-8") as fh:
                table = fh.read()
        except OSError:
            table = ""
        else:
            rendered_at = (
                datetime.fromtimestamp(os.path.getmtime(payload_path(args.subject)))
                .astimezone()
                .isoformat(timespec="seconds")
            )
        if not payload:
            # `evaluate()` returns an empty payload on both of its fail-open paths, where
            # there is no branch, digest or tracked set to record. `--check` already
            # reports that as a CHANGE verdict; `--save` must report the same one rather
            # than dereferencing `payload["branch"]` and raising. The snapshot cannot be
            # written either way, and a traceback reads to the caller as a failure rather
            # than as a verdict to gate on — so the subject would be re-swept on every
            # tick, which is the cost the fail-open rule exists to avoid. On stdout, to
            # match `--check`; the two refusals below stay on stderr, where they belong.
            print(f"CHANGE {reason}")
            return EXIT_CHANGE
        if not table.strip():
            # Same discipline as fleet-snapshot.py's empty guard: a failed render must not
            # clobber a good snapshot, and a snapshot with no table can never be replayed.
            print(
                "refusing to save an empty table: the snapshot is left unchanged, and this "
                "run reports CHANGE so the next one re-sweeps rather than replaying nothing.",
                file=sys.stderr,
            )
            return EXIT_CHANGE
        bucket_sets = None
        if args.buckets:
            try:
                with open(args.buckets, encoding="utf-8") as fh:
                    bucket_sets = json.load(fh)
            except (OSError, json.JSONDecodeError) as exc:
                # A usage error, not a verdict — and deliberately NOT a fail-open. The
                # gate's fail-open rule exists so an unreadable *state* file re-sweeps
                # rather than reporting a false "no change"; here the caller has handed
                # over a path it believes holds the classification. Writing the record
                # without the half would let it read back as persisted when it is not,
                # and clause (0) would then hold an entire batch on a half the caller
                # thought it had supplied. Refuse, and let the caller fix the path.
                print(
                    f"could not read bucket sets from {args.buckets!r}: {exc}",
                    file=sys.stderr,
                )
                return EXIT_USAGE
            # The same refusal `--write-buckets` makes, and for the same reason — this is
            # the other door into the record, and validating only the staging path left a
            # hand-written or stale file able to reach `save_stored` with a set that
            # cannot gate. A record written that way reads back as persisted when it is
            # not, which is precisely what clause (0) would then hold a batch on.
            shape_error = bucket_shape_error(bucket_sets)
            if shape_error:
                print(
                    f"bucket sets at {args.buckets!r} cannot gate: {shape_error}",
                    file=sys.stderr,
                )
                return EXIT_USAGE
        # The bucket half is a save input in its own right, not a passenger on the digest.
        # `digest_of` covers the tracked set only, so a tick whose tree did not move reports
        # `digest equal` and used to write nothing at all — discarding a freshly staged,
        # *correct* classification and leaving a corrected re-stage unable to heal a bad
        # record. Measured 2026-09-29 on the Attention Routing loop: a correct 21/4/89 set
        # was staged, `--save` returned `SAVED no-change (digest equal)` at exit 0, and the
        # store kept the previous tick's sets, missing a task filed that minute.
        buckets_moved = bucket_sets is not None and bucket_sets != payload.get(
            "stored_bucket_sets"
        )
        if changed or buckets_moved:
            try:
                save_stored(
                    args.subject,
                    payload["branch"],
                    payload["digest"],
                    table,
                    payload["tracked"],
                    payload["busy"],
                    bucket_sets,
                    rendered_at,
                )
            except OSError as exc:
                print(f"fail-open: could not record state ({exc})", file=sys.stderr)
                return EXIT_CHANGE
            print(f"SAVED {reason if changed else 'bucket sets differ'}")
            return EXIT_CHANGE
        print(f"SAVED no-change ({reason})")
        return EXIT_NOCHANGE

    if args.check:
        print(("CHANGE " + reason) if changed else "NOCHANGE")
        return EXIT_CHANGE if changed else EXIT_NOCHANGE

    # --print: the whole point. On no-change the operator still sees the frame — silence
    # would be ambiguous, and a quiet run and a dead run look identical from outside.
    if changed:
        print(f"CHANGE {reason}")
        return EXIT_CHANGE

    print(
        f"⏸ {NO_CHANGE_MARKER} since {payload['recorded_at'] or 'the last sweep'} — "
        "prior sweep replayed, 0 agents dispatched"
    )
    print(stored_table.rstrip("\n"))
    return EXIT_NOCHANGE


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except KeyboardInterrupt:
        sys.exit(130)
