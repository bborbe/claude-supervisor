#!/usr/bin/env python3
"""One pass over every live session: what it is parked on, why, and who may clear it.

`who-needs-me.py` answers *whether* a pane is parked and *why* (its cause class).
This answers the operator's three questions in one sweep, and adds the second axis
the runbook needs:

  1. **Sweep**      — every live session, parked or not. A parked row carries the
                      runbook's gate class (A-E, *who may clear*) AND the sibling's
                      cause class (*why it is parked*), plus any board flag.
  2. **Handover**   — the operator-bound rows only (classes C, D and E), ordered
                      prod first then closest-to-SC, each with a jump link and a
                      recommended answer.
  3. **Root cause** — cause classes seen on two or more distinct calendar days,
                      each with the mechanism that explains it.
  4. **Managers to query** — every live manager, its next SC/DoD item and its
                      blocker, so the calling command can put one question to each.

REPORT-ONLY ON GATES. This script answers no gate, drives no modal, relays no
approval and types into no pane. It reads `wezterm cli get-text` and nothing else
that touches another session. The one outward action of the *command* that calls
it is a `SendMessage` per live manager — an agent tool, so it is the command's
step 2 and not this script's, which is why the send *plan* is what this renders.

REUSE, NOT A SECOND READER. Every classification symbol is imported from
`who-needs-me.py` (hyphenated filename -> importlib, the repo's own idiom): the
cause enum and `cause_class()`, `board_flags()`, `read_causes()` /
`unowned_causes()`, the registry reads, the attention store reads, the pane map
and the task-link join. Nothing about *how* a pane is classified is re-derived
here; this script adds only the gate-class axis, the pane viewport read the
sibling does not perform, and the four-section render.

TWO CHANNELS, NEVER BY POSITION. Whether a session is parked comes from the
session registry, on the two limbs `scripts/manager-attention-watch.py` states:

    gated := status == "waiting"                      # parked on a harness gate
          or (status == "idle" and non-empty closer)  # turn ended on an ask

What it is asking comes from the pane viewport, matched on markers **anchored to
the start of a line**. No `tail -N`, no last-non-empty-line, no buffer-wide grep:
`65 Runbooks/Manager Session.md` § Gate triage measured all three wrong on
2026-09-21 and again on 2026-10-03 (pane 703, a live production prompt whose
footer sat 8 non-empty lines from the end).

⚠️ **The pane read detects MODALS, never closer-line gates.** A pane parked on
`approve: …` / `pick — …` / `you run: …` renders that line as ordinary text with
no footer anywhere on screen, so `pane_shape()` returns None for it and that None
must never be read as "not gated" — the session limb decides, and the closer's own
verb is the instrument for *what* it asks. See [[A Modal Scan Cannot See a
Closer-Line Gate]].
"""
import argparse
import glob
import importlib.util
import json
import os
import re
import subprocess
import sys
import urllib.parse

_HERE = os.path.dirname(os.path.abspath(__file__))


def _load(name, filename):
    """Import a hyphenated sibling script by path — the repo's own idiom."""
    spec = importlib.util.spec_from_file_location(name, os.path.join(_HERE, filename))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


wnm = _load("who_needs_me", "who-needs-me.py")

# Our own marker, not the sibling's: a consumer piping this feed has to be able to
# tell WHICH reader refused, and `WHO-NEEDS-ME-REFUSED` from this script would name
# the wrong one. Same contract as the sibling's `refuse()` — stdout carries the
# marker so it survives a pipe, stderr carries the same text for a human, exit 1.
REFUSAL_MARKER = "WHO-NEEDS-MY-HELP-REFUSED"


def refuse(reason):
    """Refuse the read on BOTH streams, then exit non-zero. Never returns."""
    sys.stdout.write(f"{REFUSAL_MARKER}: {reason}\n")
    sys.stdout.flush()
    sys.stderr.write(f"who-needs-my-help: {reason}\n")
    sys.stderr.flush()
    sys.exit(1)


# --- The runbook's gate classes (A-E) ---------------------------------------
#
# `65 Runbooks/Manager Session.md` § Gate triage, read in order, first match wins.
# A-E says WHO may CLEAR a gate; the sibling's cause class says WHY the pane is
# parked. The two co-occur, so neither folds into the other.
#
# The operator-bound set is C, D and E. That is the whole point of the axis: a
# manager reads the same letter the same way it reads it in the runbook, and the
# handover section is exactly this set.
GATE_CLASSES = ("A", "B", "C", "D", "E")
OPERATOR_BOUND = ("C", "D", "E")

# Class E — live-trade / TDR. Any `40 Decisions/TDR *` field, or a funded-account
# trade. A conversational answer is never an ack, so the row points at the flow.
_TDR = re.compile(r"40 Decisions/TDR|trade-create|trade-close|vault-cli decision ack", re.I)

# Class D — production-touching or irreversible, the runbook's own list. Also
# reached by the pane shape: a permission modal (`Esc to cancel · Tab to amend`)
# is "class D or E, operator only" per § Gate triage.
_PROD = re.compile(
    r"make apply|make buca|make deploy|gh pr merge|launchctl|git push --force"
    r"|kubectl\s+\S+\s+(apply|delete|patch|rollout)|--force\b|\bdeploy\b|\brelease\b",
    re.I,
)

# Class A — the runbook's CLOSED list of six standing mandates. Matched on the
# mandate's own wording, never on a bare verb: a gate whose mandate cannot be
# recognised falls through to C, and that is the safe direction — C is
# operator-bound, so a missed A costs the operator one look, while a wrongly
# claimed A would have the manager act on a decision nobody pre-authorised.
_MANDATE = re.compile(
    r"spawn a ready-to-start worker|ready-to-start worker|auto-resume"
    r"|restart the stale worker|kill and restart|topic-page write-back"
    r"|auto-compaction|close the gate-met topic|gate-met topic",
    re.I,
)

# The `pick` menu a closer renders, and the two single-answer forms. All three
# name the answer set, which is what class B's mechanical test quotes against and
# what SC8's "recommended answer is one of that item's options" reads.
_PICK = re.compile(r"pick\s*[—–-]\s*(.+)")
_OPTION = re.compile(r"(\d+)\.\s*([^·]+)")
_APPROVE = re.compile(r"approve:\s*(\S.*)")
_YOU_RUN = re.compile(r"you run:\s*(\S.*)")
# The trigger and the watched mechanism, from the closer's own two lines.
_TRIGGER = re.compile(r"later\s*\(on\s+([^)]+)\)", re.I)
_WATCHER = re.compile(r"⏰\s*(?:Ends|Next):\s*(.+)")
# The vault's production-touching marker (`[[Task Writing Guide]]` § production-touching).
_PROD_MARKER = re.compile(r"production-touching", re.I)
_OPEN_BOX = re.compile(r"^\s*-\s*\[( |/)\]", re.M)
_UNCHECKED = re.compile(r"^\s*-\s*\[\s\]\s*(.+)$", re.M)

# A Personal-vault convention: an item whose task file lives in any other vault
# carries no marker and counts as non-prod. Stated rather than left to fall out.
PERSONAL_VAULT = os.path.expanduser("~/Documents/Obsidian/private-personal")

# Where a manager session records the goal/topic it manages — source 1 of
# `docs/subject-resolution.md`. A manager is anchored to a PAGE, not to a task
# file, which is why its next item cannot be read the way a worker's is.
WM_STATE_DIR = os.path.expanduser("~/.claude/state/worker-manager")

# The page test's two folders (`docs/subject-resolution.md` § The page test): an
# exact basename inside the folder, the folder being the discriminator. The
# state file's own `branch` picks which one to look in, so the goal/topic
# ambiguity that doc forbids guessing at is resolved by the record rather than
# by a preference.
GOAL_DIR = "24 Goals"
TOPIC_DIR = "23 Topics"

# The pane viewport's line-start anchors. `Tab to amend` is the discriminator
# WITHIN the permission line, never an anchor of its own.
MARK_PERMISSION = "Esc to cancel"
MARK_QUESTION = "Enter to select"

# Recurrence threshold, passed EXPLICITLY: the sibling's `unowned_causes` defaults
# to `OWNER_TASK_THRESHOLD = 3`, and this report's claim is "seen twice or more".
RECURRENCE_THRESHOLD = 2

# Why each cause class recurs — the mechanism, cited to a resolving `file:line` in
# the sibling. Carried on every root-cause row: a class named without its
# mechanism is a symptom the operator has to re-diagnose.
CAUSE_MECHANISM = {
    wnm.CAUSE_OPERATOR: "scripts/who-needs-me.py:1251 is_open_gate() — the closer holds a decision the operator owns",
    wnm.CAUSE_TASK: "scripts/who-needs-me.py:1249 names_peer_gate() — the closer restates another pane's gate",
    wnm.CAUSE_CLOCK: "scripts/who-needs-me.py:1247 is_parked_verb() — the closer names the event that resumes it",
    wnm.CAUSE_CAP: "scripts/who-needs-me.py:1254 _cap_blocked() — the spawn cap refused the manager",
    wnm.CAUSE_UNKNOWN: "scripts/who-needs-me.py:1256 — no classifier matched, so the park is unexplained",
}

MANAGER_QUESTION = (
    "What is the next SC or DoD item you could complete soon, and what blocks it?"
)


# --- The pane viewport read -------------------------------------------------


def pane_lines(pane_id):
    """The pane's VISIBLE screen, as lines; `None` when the read failed.

    `wezterm cli get-text` defaults to `--start-line 0`, the first line of the
    terminal screen, so the read never reaches scrollback — which is what makes a
    line-start anchor meaningful here at all. A buffer-wide read would match dead
    scrollback and report a gate on a pane that is working.

    `None` is a distinct answer from `[]`: a failed read cannot prove a pane holds
    no modal, so no caller may read it as "not gated" — the same convention as the
    sibling's `wezterm_panes()` / `read_registry()`.
    """
    try:
        r = subprocess.run(
            ["wezterm", "cli", "get-text", "--pane-id", str(pane_id)],
            capture_output=True, text=True, timeout=5,
        )
    except Exception:
        return None
    if r.returncode != 0:
        return None
    return r.stdout.splitlines()


def pane_shape(pane_id):
    """`"permission"` | `"question"` | `None` — the modal the viewport renders.

    Anchored to the START of a line, deliberately. A bare containment match
    reports a gate on any pane that merely *discusses* one: measured 2026-10-03,
    `grep -cE 'Enter to select|Esc to cancel'` returned 1 on a pane holding no
    modal, the hit being a task page's own sentence rendered in the viewport.

    `None` covers both "no modal" and "could not read", and they are deliberately
    NOT distinguished to the caller: neither one licenses "not gated". Liveness is
    the registry's answer (see `parked()`), and for a closer-line gate the
    instrument is the store's open items plus the closer's own verb.
    """
    lines = pane_lines(pane_id)
    if lines is None:
        return None
    for line in lines:
        if line.startswith(MARK_PERMISSION):
            return "permission"
        if line.startswith(MARK_QUESTION):
            return "question"
    return None


# --- The two-channel parked predicate ---------------------------------------


def parked(rec, reg_record):
    """The registry's two-limb verdict — verbatim from `manager-attention-watch.py`.

    Both limbs are load-bearing. The registry alone under-reports: a worker idle
    on a `👤 You:` panel reads `idle`, indistinguishable from one that finished its
    turn with `👤 You: nothing`, and that `idle` limb is exactly the closer-line
    parks a one-limb `status == "waiting"` test would classify as closed.

    The pane NEVER decides liveness — that is the whole point of reading the two
    channels from different places.
    """
    status = ((reg_record or {}).get("status") or "").strip().lower()
    if status == "waiting":
        return True
    if status != "idle":
        return False
    text = wnm.normalize_closer(wnm.current_closer(rec))
    # `nothing` is a turn that ended with nothing to ask, not a park — the exclusion
    # `reclassify_idle()` already applies. A `later (on <trigger>):` closer IS a park
    # (a machine-bound one), so it is deliberately kept.
    return bool(text) and not text.startswith("nothing")


# --- The task-file join ------------------------------------------------------


def task_path_of(rec):
    """The vault task file this session anchored, or `None`.

    Read from the closer's own `📌 Task: [..](obsidian://open?vault=..&file=..)`
    line, so no vault scan is needed — the same join `task_status_from_closer()`
    performs, reached from the path side rather than the status side.
    """
    m = wnm._TASK_LINK.search(wnm.last_assistant_text(rec))
    if not m:
        return None
    rel = urllib.parse.unquote(m.group(1))
    for vault in sorted(glob.glob(os.path.join(wnm.OBSIDIAN, "*"))):
        path = os.path.join(vault, rel + ".md")
        if os.path.exists(path):
            return path
    return None


def read_task(path):
    """The task file's text, or `""` when it cannot be read."""
    if not path:
        return ""
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except Exception:
        return ""


def is_prod(task_path, task_text):
    """True when the item's own task file carries the production-touching marker.

    Derived, never judged. The marker is per-subtask and lives in the Personal
    vault's `[[Task Writing Guide]]`, so an item whose task file is in another
    vault carries no marker and counts as **non-prod** — stated explicitly here
    rather than left to fall out silently.
    """
    if not task_path or not task_text:
        return False
    if not os.path.abspath(task_path).startswith(PERSONAL_VAULT + os.sep):
        return False
    return bool(_PROD_MARKER.search(task_text))


def open_boxes(task_text):
    """Unchecked boxes left in the task file — the closeness key, lower is closer."""
    return len(_OPEN_BOX.findall(task_text or ""))


def next_item(task_text):
    """The first unchecked item in the task file, or "".

    The manager's own answer to "what is your next SC/DoD item" — read off its
    anchored task file rather than invented, because the whole point of the query
    is that the script does not know it.
    """
    m = _UNCHECKED.search(task_text or "")
    return m.group(1).strip() if m else ""


def manager_subject_page(session_id):
    """The goal/topic page a manager's subject names, or `None`.

    Source 1 of `docs/subject-resolution.md`: the session's own state file. A
    manager session is anchored to a PAGE, never to a task file, so this is the
    only thing that can answer "what is your next item" for it — read off the
    goal it declared rather than off whatever task its transcript last named.

    ⚠️ **Two traps the doc names, both honoured here.** The `vault` is compared
    **case-insensitively**, because state files written before 2026-09-23 may
    carry display case while the vault-cli config name is lowercase, and a strict
    match drops every one of them to a lower-priority source. And a subject that
    no longer resolves to a page is a **silent miss** — this returns `None` and
    the caller falls through, never errors: a manager whose goal was renamed or
    completed is an ordinary fleet event, not a defect.

    The state file's own `branch` picks the folder, so the goal-vs-topic
    ambiguity the page test forbids guessing at is settled by the record. A
    record carrying no branch probes both and takes an exact match in either —
    never a substring glob, which a topic's member goals would satisfy in both
    folders at once.
    """
    try:
        with open(os.path.join(WM_STATE_DIR, f"{session_id}.json"),
                  encoding="utf-8") as f:
            state = json.load(f)
    except Exception:
        return None
    subject = (state.get("subject") or "").strip()
    if not subject:
        return None
    want_vault = (state.get("vault") or "").strip().lower()
    branch = (state.get("branch") or "").strip().lower()
    folders = {"goal": (GOAL_DIR,), "topic": (TOPIC_DIR,)}.get(
        branch, (GOAL_DIR, TOPIC_DIR))
    want_file = subject.lower() + ".md"
    for vault in sorted(glob.glob(os.path.join(wnm.OBSIDIAN, "*"))):
        if want_vault and os.path.basename(vault).lower() != want_vault:
            continue
        for folder in folders:
            d = os.path.join(vault, folder)
            if not os.path.isdir(d):
                continue
            for entry in sorted(os.listdir(d)):
                if entry.lower() == want_file:
                    return os.path.join(d, entry)
    return None


def anchor_page(rec, manager):
    """The page a session's next item is read off.

    A manager is anchored to its SUBJECT page; anything else to its own task
    file. A manager whose subject no longer resolves falls back to the task file,
    so it is reported exactly as it was before this rule existed rather than
    losing the line entirely.
    """
    if manager:
        page = manager_subject_page(rec.get("session_id"))
        if page:
            return page
    return task_path_of(rec)


# --- Answer extraction -------------------------------------------------------


def options_of(closer):
    """The answer set the closer names, and the one it recommends.

    Three shapes, all of which name their options on the closer line itself:

      * `pick — 1. X · 2. Y` — the menu. The recommended answer is option 1, the
        closer's own recommendation.
      * `approve: <cmd>` and `you run: <cmd>` — one option, itself.

    Returns `(options, recommended)`. Both empty when the closer names none, which
    is honest: a gate whose answer the closer does not state is one this reader
    cannot recommend for.
    """
    text = wnm.normalize_closer(closer or "")
    if not text:
        return [], ""
    m = _PICK.search(text)
    if m:
        found = [o[1].strip() for o in _OPTION.findall(m.group(1))]
        return (found, found[0]) if found else ([], "")
    for rx in (_APPROVE, _YOU_RUN):
        m = rx.search(text)
        if m:
            return [m.group(1).strip()], m.group(1).strip()
    return [], ""


def wait_reason(rec):
    """What the pane is waiting on, from the closer's own operator-facing verb.

    This is the instrument for a closer-line gate — the pane cannot answer the
    question for that shape, because there is no footer to match.
    """
    text = wnm.normalize_closer(wnm.current_closer(rec))
    return text[:120]


def watcher_of(rec):
    """The watched mechanism and its end, from the session's own `⏰` line.

    Read from the whole tail, not from the closer alone: `⏰ Ends:` is a SEPARATE
    line below the `👤 You:` panel, and `closer_from_transcript()` keeps only the
    `👤 You:` line — so a closer-only read reports a machine-bound wait with no
    watcher at all, which is the one thing that distinguishes it from a gate.
    `last_assistant_text()` is the last text block, so this is the current panel
    rather than a line the session has since scrolled past.
    """
    text = wnm.last_assistant_text(rec) or wnm.current_closer(rec) or ""
    m = _WATCHER.search(text)
    return m.group(1).strip() if m else ""


def eta_of(rec):
    """The clock a `later (on <trigger>):` closer names."""
    m = _TRIGGER.search(wnm.current_closer(rec) or "")
    return m.group(1).strip() if m else ""


# --- The gate class ----------------------------------------------------------


def quoteable(task_text, options):
    """Class B's mechanical test: the task file QUOTES a line naming an option.

    The runbook's rule is *"quote the line, or escalate"* — "the task file
    determines it" is a claim, and a wrong claim is a decision wearing a citation.
    A gate whose determining line cannot be quoted is therefore class C, which is
    where this returns False.

    The length floor keeps a one-word option ("yes") from matching any prose
    incidentally; below it the gate escalates rather than being guessed at.
    """
    if not task_text or not options:
        return False
    body = task_text.lower()
    for opt in options:
        probe = wnm.normalize_closer(opt).lower()
        if len(probe) >= 12 and probe in body:
            return True
    return False


# The operator-facing verbs `65 Runbooks/Manager Session.md` § Gate triage names as
# the closer-line instruments — the ones that make a pane's own text the answer to
# *what is it asking*. A closer that carries none of them is not an ask, however
# parked the session is.
ASK_VERBS = ("approve:", "you run:", "review:", "pick —", "pick -", "pick–")


def is_operator_ask(rec):
    """True when the session's closer is an operator-facing ask.

    ⚠️ **Read from `closer_from_transcript`, never `current_closer`.** The latter
    falls back to the record's `detail` when the transcript holds no `👤 You:` line,
    and that fallback is arbitrary assistant prose. Measured 2026-10-15 on the first
    live run: with the fallback consulted, panes 1557 (`📌 Conventions: run /recall …`)
    and 1619 (*"Session left open, no files touched, no commands run."*) both read as
    operator gates while all three instruments said otherwise — no open store item,
    no operator verb, no modal footer. Prose is not an ask.
    """
    text = wnm.normalize_closer(wnm.closer_from_transcript(rec))
    if not text:
        return False
    return any(text.startswith(v) for v in ASK_VERBS) or bool(_PICK.search(text))


def gate_class(rec, cause, shape, task_text):
    """Which of the runbook's A-E classes this gate is, or `None` for a non-gate.

    Read in the runbook's order, first match wins — with one deliberate
    reordering: the pane's permission-modal shape names D or E *before* the text
    is consulted, because § Gate triage makes the modal shape the classifier for
    that shape ("permission modal -> class D or E, operator only").

    `None` is returned for a machine-bound wait: a session parked on a watcher or
    a clock is not a gate, so it carries no gate class. It still carries its cause
    class, its watcher and its ETA.
    """
    if cause == wnm.CAUSE_CLOCK:
        return None
    text = wnm.normalize_closer(wnm.current_closer(rec))
    options, _ = options_of(text)
    # E before D: a TDR or a funded-account trade is E whichever modal it renders
    # in, and E's route (the ack flow) is narrower than D's.
    if _TDR.search(text):
        return "E"
    if shape == "permission" or _PROD.search(text):
        return "D"
    if _MANDATE.search(text):
        return "A"
    if quoteable(task_text, options):
        return "B"
    return "C"


# --- The sweep ---------------------------------------------------------------


def with_live_closer(rec):
    """The record with its `detail` refreshed from the transcript's live closer.

    `detail` is written once by the hook and never refreshed, so a record whose
    session cleared one gate and raised another keeps the OLD text — the staleness
    `current_closer()` exists to correct. It matters here rather than cosmetically:
    `cause_class()` reads `detail` and nothing else, so a stale empty `detail`
    classifies a `later (on <trigger>):` park as `unknown` instead of
    `waiting-on-clock`, which then hands the operator a machine-bound wait as an
    operator gate. Measured 2026-10-14 on the live fleet: every clock park in the
    sweep read `unknown` before this refresh.

    The `kind` is re-derived too, and that IS a departure from the sibling — stated
    rather than smuggled. `reclassify_idle()` declines to promote an `idle` record
    whose closer equals the one this session already posted a card for, because
    promoting it would put the SAME ask on the attention board twice. This reader
    posts nothing: it reports what a session is parked ON, and a session parked on
    an ask that already has a card is still parked on it. Without the promotion
    `cause_class()` sees `kind: idle`, `is_open_gate()` returns False, and every
    such park renders `unknown` — measured 2026-10-14 on the live fleet, where 4 of
    6 parked rows read `unknown` for exactly this reason.

    A record whose transcript is unreadable keeps its `detail` — `current_closer()`
    already falls back to it, and inventing a closer would be worse than a stale one.
    """
    text = wnm.closer_from_transcript(rec)
    if not text:
        return rec
    out = dict(rec, detail=text)
    if (out.get("kind") == "idle" and not text.startswith("nothing")
            and not wnm.is_parked_verb(text)):
        out["kind"] = "question"
    return out


def representative(recs):
    """The one record that stands for a session — a session is one sweep row.

    A session holds one attention record per item, so iterating records renders the
    same pane many times. The park is a property of the SESSION (the registry says
    so), so exactly one row is rendered per session and this picks which record
    describes it: a record carrying an ask beats one whose closer is empty, and
    among equals the oldest wins, matching the feed's own oldest-first triage.
    """
    asks = [r for r in recs if wnm.normalize_closer(wnm.current_closer(r))]
    pool = asks or recs
    # A record whose item the hook has already marked answered reads `answered()`
    # True, and `is_open_gate()` then refuses it — so a session parked on a NEW ask
    # would classify `unknown` off its oldest, closed item. Prefer an unanswered
    # gate record, since `with_live_closer()` stamps every record of a session with
    # the SAME live closer and the record's own identity therefore carries only its
    # `state` and `kind`.
    open_recs = [r for r in pool if not wnm.answered(r)
                 and r.get("kind") in ("permission", "question")]
    return min(open_recs or pool, key=lambda r: r.get("ts") or 0)


def jump_link(pane_id):
    """The clickable jump target for a pane, from the shipped emitter."""
    try:
        r = subprocess.run(
            [sys.executable, os.path.join(_HERE, "jump-link.py"), str(pane_id)],
            capture_output=True, text=True, timeout=10,
        )
        if r.returncode == 0 and r.stdout.strip():
            return r.stdout.strip().splitlines()[0]
    except Exception:
        pass
    return f"/supervisor:jump {pane_id}"


def build(pmap, registry, reg_records, records, causes_state, shape_of=pane_shape,
          link_of=jump_link):
    """Classify every live session. Pure over its inputs, so it is testable.

    Returns `{"live": [...], "parked": [...], "not_parked": [...], "managers": [...]}`.
    Each parked row carries both axes, its wait reason, its flags, its ordering
    keys, its jump target and its recommended answer.
    """
    reg_records = reg_records or {}
    carded = wnm.store_session_ids(records)
    store_ok = wnm.STORE_OK is True
    fresh = [with_live_closer(r) for r in records]
    # The pane set that carries an open gate, computed WITHOUT peer-dedup — the
    # sibling's own two-pass shape, so `names_peer_gate()` can drop a restating
    # pane only when the pane it names is itself a gate.
    open_panes = {str(r.get("pane")) for r in fresh
                  if wnm.is_open_gate(r, task_status=wnm.task_status_from_closer)}
    by_pane = {}
    for rec in fresh:
        by_pane.setdefault(str(rec.get("pane")), []).append(rec)

    # One row per SESSION: a session holds one attention record per item, and the
    # park is a property of the session, not of an item.
    groups = {}
    for rec in fresh:
        groups.setdefault(rec.get("session_id"), []).append(rec)

    parked_rows, not_parked = [], []
    for sid, recs in groups.items():
        rec = representative(recs)
        name = wnm.name_of(rec, pmap, registry)
        if not parked(rec, reg_records.get(sid)):
            pane = rec.get("pane")
            manager = is_manager(rec, registry)
            not_parked.append({"session_id": sid, "name": name,
                               "pane": pane,
                               "manager": manager,
                               "next_item": next_item(read_task(anchor_page(rec, manager))),
                               "wait": "",
                               "jump": link_of(pane) if pane else ""})
            continue
        peers = by_pane.get(str(rec.get("pane")), []) or fresh
        cause = wnm.cause_class(rec, open_panes, wnm.task_status_from_closer)
        flags = wnm.board_flags(rec, cause, carded, store_ok=store_ok)
        pane = rec.get("pane")
        shape = shape_of(pane) if pane else None
        task_path = task_path_of(rec)
        task_text = read_task(task_path)
        manager = is_manager(rec, registry)
        # SC8 pins prod and closeness to the item's OWN task file, so `task_text`
        # stays that file; only the next item moves to the page the session is
        # anchored to. For a manager that page is its subject, not a task — read
        # off the anchored task file it came back empty on every live manager.
        next_text = read_task(anchor_page(rec, manager)) if manager else task_text
        options, recommended = options_of(wnm.current_closer(rec))
        parked_rows.append({
            "session_id": sid,
            "name": name,
            "pane": pane,
            "cause": cause,
            "cause_label": wnm.cause_label(rec, cause, peers, pmap, registry),
            # ⚠️ A park is a GATE only when the operator could actually clear it —
            # its closer carries an operator-facing verb, or the store holds an open
            # item for it. Without this clause `gate_class()` returns C (the
            # everything-else bucket, and C is operator-bound) for any park at all,
            # so a session sitting on arbitrary prose is handed to the operator.
            # Measured 2026-10-15: that is exactly how panes 1557 and 1619 reached
            # `Handover` while all three instruments agreed they were not gated.
            "gate": (gate_class(rec, cause, shape, task_text)
                     if is_operator_ask(rec) or sid in carded else None),
            "flags": flags,
            "wait": wait_reason(rec),
            "watcher": watcher_of(rec),
            "eta": eta_of(rec),
            "jump": link_of(pane) if pane else "",
            "options": options,
            "answer": recommended,
            "prod": is_prod(task_path, task_text),
            "open_boxes": open_boxes(task_text),
            "task_path": task_path,
            "manager": manager,
            "next_item": next_item(next_text),
            "shape": shape,
        })

    managers = [r for r in parked_rows + not_parked if r.get("manager")]
    return {"live": groups, "parked": parked_rows, "not_parked": not_parked,
            "managers": managers, "causes": causes_state,
            "source_note": wnm.SOURCE_NOTE}


def is_manager(rec, registry):
    """True when this session is a manager — by the registry's own name.

    The registry's name is the session's current name (it is rewritten on rename),
    and the fleet names managers `<Topic> Manager` / `Fleet Manager`. A record that
    carries its own `role` is honoured too, since that is the field the sibling's
    `_cap_blocked()` reads. Anything else is not a manager: a wrong name here would
    send a question to a worker, which is the one leg of this command that touches
    every live manager.
    """
    if (rec.get("role") or "").lower() == "manager":
        return True
    name = wnm.current_session_name(rec, registry)
    return bool(name) and name.strip().split()[-1] == "Manager"


# --- Render ------------------------------------------------------------------


def handover_order(rows):
    """Operator-bound rows: prod first, then closest to SC.

    *Prod* is derived from the task file's own marker; *closeness* is the count of
    unchecked boxes left in that same file, fewer being closer. Prod outranks
    closeness, so a prod item that is LESS close still sorts first — the ordering
    the operator acts on is risk before convenience.
    """
    rows = [r for r in rows if r["gate"] in OPERATOR_BOUND]
    return sorted(rows, key=lambda r: (not r["prod"], r["open_boxes"], r["name"]))


def spoken_line(count):
    """The one line the operator is told aloud. Never ends on `?`.

    The count is the operator-bound set only — classes C, D and E. Classes A and B
    are the manager's and are never counted here, so an implementation that counted
    every classified row would overstate what needs the operator.
    """
    return f"{count} items need you"


def render(sweep):
    """The four sections, in order, as one string."""
    parked = sweep["parked"]
    live = sweep["live"]
    not_parked = sweep["not_parked"]
    out = []

    out.append(f"Sweep ({len(live)} live, {len(parked)} parked)")
    # Which source answered, printed once before any row — a silent fallback would
    # be indistinguishable from a healthy store, and a failed store read makes
    # `board_flags()` emit nothing, so without this line an outage reads as a
    # board with no defects.
    if sweep.get("source_note"):
        out.append(f"  {sweep['source_note']}")
    for r in sorted(parked, key=lambda r: r["name"]):
        head = f"  [{r['pane'] or '?':>4}] {r['name'][:50]:<50}  {r['cause_label']}"
        if r["gate"]:
            head += f" · gate {r['gate']}"
        else:
            # A park with no gate class is recorded as exactly that — never left
            # blank, which reads as "unclassified" rather than "not gated".
            head += (" · machine-bound" if r["cause"] == wnm.CAUSE_CLOCK
                     else " · not gated")
        if r["flags"]:
            head += f" [{' '.join(r['flags'])}]"
        out.append(head)
        if r["watcher"]:
            out.append(f"         watcher: {r['watcher']}")
        if r["eta"]:
            out.append(f"         ETA: {r['eta']}")
        if r["wait"]:
            out.append(f"         waiting on: {r['wait']}")
        if r["jump"]:
            out.append(f"         {r['jump']}")
    # A live pane all three instruments agree is NOT gated is recorded as such, so
    # the negative direction is assessable at all rather than vacuous.
    if not_parked:
        names = ", ".join(f"[{r['pane'] or '?'}] {r['name']}" for r in not_parked)
        out.append(f"  not parked ({len(not_parked)}): {names}")

    rows = handover_order(parked)
    out.append("")
    out.append("Handover")
    out.append(spoken_line(len(rows)))
    for r in rows:
        out.append(f"  [{r['pane'] or '?':>4}] {r['name'][:50]:<50}  gate {r['gate']}"
                   f"{' · prod' if r['prod'] else ''} · {r['cause_label']}")
        if r["wait"]:
            out.append(f"         waiting on: {r['wait']}")
        if r["answer"]:
            out.append(f"         recommended: {r['answer']}")
        if r["jump"]:
            out.append(f"         {r['jump']}")

    out.append("")
    out.append("Root cause")
    state = sweep["causes"]
    recurring = wnm.unowned_causes(state, threshold=RECURRENCE_THRESHOLD)
    # A recurring class with no mechanism is dropped, and the emptiness test reads
    # the SURVIVORS rather than `recurring` — otherwise a class filtered out here
    # would leave the section silently blank instead of saying it has nothing.
    # A class named without the mechanism that explains it is a symptom the
    # operator has to re-diagnose from scratch, which is the work this section
    # exists to have already done.
    listed = [(c, state.get("counts", {}).get(c, 0), CAUSE_MECHANISM[c])
              for c in recurring if CAUSE_MECHANISM.get(c)]
    if not listed:
        out.append("  none — no cause class has recurred on two or more days")
    for cause, days, mechanism in listed:
        out.append(f"  {cause} — {days} days · mechanism: {mechanism}")

    out.append("")
    out.append(f"Managers to query ({len(sweep['managers'])})")
    for r in sorted(sweep["managers"], key=lambda r: r["name"]):
        out.append(f"  {r['name']}  [{r['pane'] or '?'}]")
        if r.get("next_item"):
            out.append(f"         next: {r['next_item']}")
        if r.get("wait"):
            out.append(f"         blocked by: {r['wait']}")
        if r["jump"]:
            out.append(f"         {r['jump']}")
    if sweep["managers"]:
        out.append(f"Question: {MANAGER_QUESTION}")

    return "\n".join(out) + "\n"


# --- Entry -------------------------------------------------------------------


def sweep_live(shape_of=pane_shape, link_of=jump_link):
    """Read the live fleet through the sibling's own readers, then classify it."""
    pmap = wnm.wezterm_panes()
    if pmap is None:
        refuse("WezTerm pane list unreadable — the mux socket is not answering, so "
               "no session can be proven live. Refusing to print a sweep that would "
               "read as an empty fleet.")
    registry = wnm.read_registry()
    reg_records = wnm.registry_records()
    live_ids = None if registry is None else set(registry)
    records = wnm.load("needs")
    wnm.resolve_missing_panes(records, pmap, registry)
    quiet = wnm.quiet_session_ids(records, live_ids)
    live = [wnm.reclassify_idle(r) for r in records
            if wnm.is_live(r, pmap, quiet)]
    return build(pmap, registry, reg_records, live, wnm.read_causes(),
                 shape_of=shape_of, link_of=link_of)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--stuck-min", type=int, default=20,
                    help="accepted for parity with the sibling; unused by the sweep")
    ap.parse_args()
    sys.stdout.write(render(sweep_live()))


if __name__ == "__main__":
    main()
