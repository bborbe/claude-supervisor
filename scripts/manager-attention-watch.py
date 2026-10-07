#!/usr/bin/env python3
"""A manager's doorbell for the gates the attention feed cannot carry.

Emits one line per CHANGE in the set of tracked workers parked on the operator,
and nothing otherwise. Armed as a `Monitor` from `manager-loop` step 7, scoped to
the manager's own tracked set.

WHY THIS EXISTS, AND WHY IT IS NOT THE FEED WATCHER
---------------------------------------------------
The ownership-filtered feed watcher (`who-needs-me.py --section needs-you` piped
into `gate-owner-filter.py`) reads the attention STORE. A worker that ends its
turn on a `👤 You:` closer panel never reaches that store's gate section — the
feed files it under `Rendered panels — a closer line, not a parked gate` — so a
manager scoped to `needs-you` is blind to it. Measured 2026-10-01: the feed
reported `Needs you (0)` while two tracked workers held non-`nothing` closers.
This watcher reads the workers' own transcripts instead, which is the only place
that class appears.

THE DISCRIMINATOR — THE PART THIS FILE EXISTS TO GET RIGHT
----------------------------------------------------------
`CLEARED` must mean *the gate was answered*. An earlier version of this watcher
keyed "gated" on the transcript alone — the last assistant text block carrying a
`👤 You:` line — and emitted `CLEARED` whenever that line stopped being the last
one. That is not the same event, and the difference is silent and dangerous: a
false `CLEARED` reads as progress and suppresses the manager's escalation, on a
worker that is in fact still blocked. Measured 2026-10-03: three false `CLEARED`
firings in ~25 minutes on one worker, each caught only because the runbook
requires a pane read before believing a clear.

The transcript records *what was last said*, never *whether the session is
currently blocked*, so no window, debounce or text heuristic can recover the
distinction from it — the information is not in the input. It is recoverable only
from a source that is about blocking. That source is the session registry
(`~/.claude/sessions/<pid>.json`, joined on `sessionId`), which the runbook
already names as the authority for whether a session is parked:

    gated := status == "waiting"                      # parked on a harness gate
          or (status == "idle" and non-empty closer)  # turn ended on an ask

Both halves are load-bearing. The registry alone under-reports: a worker idle on
a `👤 You:` panel is `idle`, indistinguishable from one that finished its turn
with `👤 You: nothing`. The transcript alone mis-reports, as above. The union is
the smallest input that separates the two states without a pane read.

Measured live 2026-10-04 against a purpose-built fixture: a park with no
preceding prose reads `waiting` AND keeps its closer, so both halves agree it is
gated — correctly, and this is why a prompt opening on its own is not the defect.
The defect needs new assistant TEXT (which displaces the closer) *and* the worker
still blocked. Also measured: `thinking` and `tool_use` records carry no text, so
raising a prompt does not by itself change what the transcript half reads.

A MID-TURN SESSION IS HELD, NOT CLEARED
---------------------------------------
The union above decides when a session IS gated; it does not by itself decide when
one STOPS being gated. The complement of the union is not "answered" — it is "not
currently in one of the two gated shapes", which a worker that simply starts a new
turn also satisfies.

Measured 2026-10-04, then reproduced against the deployed v0.109.0 copy on
2026-10-06: a tracked worker sat gated (`idle` + closer), then began new work
without answering — ordinary assistant prose displacing the closer, registry
`busy` — and the watcher emitted a bare `CLEARED` at the mid-turn point. Nothing
had been answered, and the worker had not finished with the operator.

So a status that is neither `waiting` nor `idle` is HELD, and HELD here means
**membership is unchanged**: no `CLEARED` is emitted (nothing was answered) and no
`NEW GATE` is raised (the session is already in the set). The gate stays up until
the worker settles. A `CLEARED` is justified only by a settle at `idle` carrying no
live ask.

⚠️ **HELD-mid-turn and HELD-unregistered are different facts and must not be
collapsed.** Both yield no `CLEARED`, but only UNREGISTERED drops the membership —
there is nothing left to hold for a worker that no longer exists. Dropping a
mid-turn session instead would forget the gate, and the answer that eventually
arrives would clear *silently*: a fix for one false clear that produces no true
one. `transitions` branches on the reason for exactly this.

⚠️ **The accepted cost, stated because it is the decision this limb exists to
make.** The registry cannot separate "the worker resumed BECAUSE the gate was
answered" from "the worker moved on WITHOUT answering it" — both read `busy` with
the closer displaced — so no registry-only predicate distinguishes them. Holding
therefore OVER-REPORTS: a worker that stays busy indefinitely remains listed as
gated until it next goes `idle`. That direction is chosen deliberately. A false
`CLEARED` reads as progress and suppresses the manager's escalation on a gate that
was never answered; a false `NEW GATE` costs one pane read. The asymmetry is the
argument — and a closer-text heuristic or a longer debounce is not an alternative
to it, because the distinction was never in that input (see THE DISCRIMINATOR).

THE THIRD LIMB — A PARKED MODAL, WHICH `held:` CANNOT RAISE
-----------------------------------------------------------
⚠️ **HELD is the right answer for a session already in the set, and no answer at all
for one that never entered it.** Membership unchanged means a worker that was never
gated stays ungated — and the incident's shape is exactly that: an `AskUserQuestion`
park whose assistant message ALSO carries prose reads `busy` in the registry AND has
its closer displaced by that prose, so from the FIRST poll it matches neither half of
the union. Measured 2026-10-05: such a worker sat unnoticed for 30+ minutes and the
manager noticed only because the operator asked.

A CLEAN park is unaffected: with no prose the registry reads `waiting` and the first
half catches it (`test_a_prompt_alone_does_not_displace_the_closer`, measured live
2026-10-04). The defect is the CONJUNCTION, which is why neither a registry-only nor
a transcript-only repair closes it.

    gated += status == "busy" and pending == "AskUserQuestion" and frozen

`pending` is the last `tool_use` with no matching `tool_result`; `frozen` is no
transcript write for `LIVE_WINDOW`. ⚠️ The NAME is load-bearing: a pending `Bash`
call is frozen too and is not a park — only a modal cannot proceed without the
operator.

⚠️ **This is not the registry-only predicate the section above says cannot exist.**
That argument is sound and stands: both cases read `busy` with the closer displaced,
so the REGISTRY cannot separate them. This limb does not read the registry for the
distinction — it reads the transcript's pending call, which is a different input, and
the pair is exactly what `CLAUDE.md` § Reading a worker prescribes: *"the transcript
answers which call, the registry answers is it moving."* `AskUserQuestion` is the one
call that collapses the two, because a pending one is not-moving **by construction**,
not by inference.

⚠️ **Consequence for the `held:` direction, stated so the two are not read as
contradicting.** This limb fires ONLY on the positive case, so it narrows the
over-report the section above accepts without weakening it: a mid-turn worker with no
pending modal is still HELD, exactly as before.

UNREGISTERED IS NOT CLEAR
-------------------------
A session the registry does not list yields `None`, which means *unregistered* —
a dead or never-spawned worker — never "no gate". Such a session is HELD rather
than cleared: no `CLEARED` is emitted, because nothing was answered.

  So a session that dies while gated emits **no** `CLEARED`; the transition is
  recorded to the event log as `HELD` instead. Nothing was answered, so nothing
  is announced — a dead worker is neither progress nor a gate, and reporting it
  as either is the silent direction this file exists to close.

  ⚠️ **The transcript-only predecessor got this wrong in the opposite way, and it
  is worth stating because it is the mirror of the defect above.** Keyed on the
  transcript alone, a session that died while gated stayed in the set forever: a
  frozen transcript still reads gated, `tracked_ids()` keeps resolving the task by
  its frontmatter `claude_session_id`, and the `NEW GATE` it raised was never
  cleared — a gate stuck open for a worker that no longer exists. Measured live
  2026-10-04 when a test fixture died mid-run. The `HELD` record exists so that
  condition is visible on evidence rather than inferred.
"""
import argparse
import glob
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import urllib.request

POLL_SECONDS = 60
SESSIONS_DIR = os.path.expanduser("~/.claude/sessions")
DEFAULT_STATE = os.path.expanduser("~/.claude/state/manager-attention-watch")
DEFAULT_PROJECTS_ROOT = os.path.expanduser("~/.claude/projects")
# The attention store. The same two variables `who-needs-me.py:82,86` and
# `attention-ask.py:105,191` resolve, so one deployment moves all three together.
STORE = os.environ.get("ATTENTION_STORE_URL", "http://localhost:18080").rstrip("/")
STORE_TIMEOUT = float(os.environ.get("ATTENTION_STORE_TIMEOUT", "3"))

# The supervisor's heartbeat store — the second liveness source, and the only
# world-readable one that covers a HEADLESS worker. A headless worker is an
# in-process SDK `query()` with no pid, so `SESSIONS_DIR` can never list it and
# every such worker reads UNREGISTERED forever without this. Constants mirror
# `server/heartbeat.mjs:47-48`; keep the two in step.
# Resolved exactly as `server/config.mjs:175` does — a hardcoded path silently reads
# every headless worker as UNREGISTERED on a host that sets either variable.
LIVE_DIR = os.environ.get("SUPERVISOR_HEARTBEAT_DIR") or os.path.join(
    os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state"),
    "claude-supervisor", "live")
HEARTBEAT_TTL_S = 60

# Stamps that are not session ids. `heartbeat.mjs:125` skips the reachability file
# for the same reason: read as a session it reports a phantom.
LIVE_SKIP = ("_cluster-reachability",)

# How many permission failures in a row mark a worker STUCK. ⚠️ Counted among
# PERMISSION OUTCOMES, not consecutive records — see `permission_failure_run`.
STUCK_PERM_FAILURES = 3
PERM_FAILURE_MARK = "permission request failed"

# The state line a worker writes when it gives up. Matched as a line PREFIX, not a
# substring: the e4fd1919 transcript also *quotes* the phrase while discussing it.
BLOCKED_PREFIX = "🔴 BLOCKED"

# `&nbsp;` is six literal characters, not whitespace, so `strip()` does not remove
# it. Copied verbatim from `who-needs-me.py:127` (`normalize_closer`) rather than
# imported: `manager-predispatch.py` carries the same pair for the same reason, and
# this script ships as a plugin artifact that must run with no sibling on the path.
# ⚠️ The two must stay ONE rule — if that file's entity set grows, grow it here too,
# or the two readers of the same closer line will disagree about what it says.
_ENTITY = re.compile(r"&(?:nbsp|#160|#xa0);", re.I)

# Closer verbs describing a parked wait rather than an open gate
# (`who-needs-me.py:107`). `later (on <trigger>):` names the event that resumes the
# work; until it fires there is nothing for the operator to answer. Measured
# 2026-09-19: two such closers sat at the top of `Needs you` for over five hours,
# reported as neglect. Not an ask here either, so it raises no gate.
PARKED_VERBS = ("later (on ",)

# A closer whose text could not be read at all — distinct from "read it, and it
# carries no closer". Collapsing those two is a false-`CLEARED` route; see `probe`.
_CLOSER_UNKNOWN = object()

# How long a transcript may go unwritten before the session counts as not moving
# (5 min). Copied from `who-needs-me.py`'s `LIVE_WINDOW` rather than imported, for
# the same reason `_ENTITY` is: this script ships as a plugin artifact that must run
# with no sibling on the path. ⚠️ Both siblings IMPORT it rather than copying it —
# `fleet-board.py` as `wnm.LIVE_WINDOW` and `restart-precheck.py` from
# `who-needs-me.py` — so this literal is a THIRD copy, and
# `agents/manager-drive.md:132` reads *"Reuse that shipped reading; do not add a
# fourth definition of 'idle'."* If that value changes, change it here in the same
# commit rather than letting the copies drift.
LIVE_WINDOW = 5 * 60

# The park-age escalation thresholds, in minutes. Each fires ONCE per continuous
# park, at the first poll whose age has crossed it.
PARK_AGE_THRESHOLDS = (15, 60)

# ⚠️ The three `is_gated` reasons that mean *parked on the operator* — and
# deliberately not a fourth. `idle+closer` is a park too, but it is the shape the
# ownership-filtered FEED already carries (`who-needs-me.py --section needs-you`),
# so it is not the class this arm exists to rescue: this watcher reads the
# registry and the transcript for the parks the store cannot carry.
#
# ⚠️ **`headless+closer` IS included, and it is the one of the three the registry
# cannot reach at all.** A headless worker is an in-process SDK `query()` with no
# pid, so `registry_status` returns `None` and `is_gated` decides on the
# `headless_live` branch — which is precisely why it needs this arm rather than a
# reason to skip it. `who-needs-me.py:822` records a measured case of that class
# (a parked headless worker's item, `pane: ""`) rendering no row in ANY feed
# section, so leaving it out would make the one park the feed provably cannot
# carry the one park this watcher ignores. The row's wording names two reasons
# because it was filed from a registry-`waiting` incident; its stated guarantee —
# *even when the store is lossy, a tracked worker's park must not go unescalated*
# — is not limited to the reasons that incident happened to show.
PARKED_REASONS = ("registry:waiting", "busy+parked-modal", "headless+closer")


def tracked_ids(tracked_path, tasks_dir):
    """{sid8: task name} for every tracked task carrying an id, read fresh each poll.

    A projection of `tracked_entries`, which holds the parse and its warnings —
    one implementation, two shapes, so the field-scoping rules pinned below
    cannot drift between the two callers.
    """
    return {sid8: name for sid8, (name, _) in
            tracked_entries(tracked_path, tasks_dir).items()}


def tracked_entries(tracked_path, tasks_dir):
    """{sid8: (task name, FULL session id)} for every tracked task carrying an id.

    The full id is what the re-post needs and `tracked_ids` cannot supply: the
    store's join key is `producer_id`, which `who-needs-me.py:268` reads as the
    row's session — a full UUID — while `sid8` is only its first eight
    characters. Read fresh each poll for the same reason `tracked_ids` is.

    Field-scoped, not file-wide: an unanchored `session_id:` regex over the whole
    file also matches ids quoted in a task's own Progress prose — measured
    2026-10-01, when it lifted a deliberately fake stub id out of a Progress entry
    and would have manufactured a phantom owner.

    Re-read every poll rather than cached: a hardcoded list goes stale in the
    direction that matters, because a worker spawned since the last tick is
    invisible and its gate is the one nobody is watching for.
    """
    out = {}
    try:
        with open(tracked_path) as fh:
            names = [l.strip() for l in fh if l.strip()]
    except OSError as exc:
        # NEVER return an empty result for a failed read. The tracked set is what
        # scopes this watcher, so an unreadable one means it is watching nothing
        # while looking exactly like a quiet sweep — the failure this repo names
        # as *a broken watcher looks exactly like a quiet one*. `docs/pane-reads.md`
        # states the same rule for transport reads: a failed read must never be
        # representable as an empty result.
        print(f"WATCH WARN: tracked set unreadable ({exc}) — NOTHING is being "
              f"watched; this is not a quiet sweep", file=sys.stderr, flush=True)
        return out
    if not names:
        print(f"WATCH WARN: tracked set is empty ({tracked_path}) — NOTHING is "
              f"being watched; this is not a quiet sweep",
              file=sys.stderr, flush=True)
    unresolved = []
    for name in names:
        path = os.path.join(tasks_dir, name + ".md")
        try:
            with open(path, encoding="utf-8") as fh:
                txt = fh.read()
        except OSError:
            # NOT a silent skip. A tracked name that resolves to no file
            # contributes no sessions, and `fleet-sweep-reader` resolves the same
            # names under `tasks_dir` **then `goals_dir` — may be a goal** — so a
            # manager whose tracked set holds a goal would otherwise watch
            # nothing for it, quietly.
            unresolved.append(name)
            continue
        fm = txt.split("---", 2)[1] if txt.startswith("---") else ""
        ids = []
        m = re.search(r'^claude_session_id:\s*(\S+)', fm, re.M)
        if m:
            ids.append(m.group(1).strip().strip('"\''))
        # metrics_sessions entries are indented `    - session_id: ...`, and the
        # scan is scoped to `fm` for the same reason as the field above: over the
        # whole file it also matches a `- session_id:` line quoted in a task's own
        # Progress prose, which is the phantom-owner case this function's
        # docstring names.
        ids += re.findall(r'^\s*-\s*session_id:\s*([0-9a-fA-F-]{8,})', fm, re.M)
        for i in ids:
            i = i.strip().strip('"\'')
            if len(i) >= 8:
                out[i[:8]] = (name, i)
    if unresolved:
        print(f"WATCH WARN: {len(unresolved)} tracked name(s) resolve to no file "
              f"under {tasks_dir} ({', '.join(sorted(unresolved)[:5])}"
              f"{'…' if len(unresolved) > 5 else ''}) — they contribute NO "
              f"sessions. A tracked name may be a GOAL rather than a task "
              f"(`fleet-sweep-reader` resolves both; this watcher reads "
              f"--tasks-dir only).", file=sys.stderr, flush=True)
    if names and not out:
        # The emptiness test is on the RESULT, not the name list. A tracked set
        # of goals, or of task files carrying no `claude_session_id`, leaves
        # `names` non-empty while nothing at all is watched — the exact shape
        # this module's docstring forbids: a broken watcher looking precisely
        # like a quiet sweep.
        print(f"WATCH WARN: {len(names)} tracked name(s) yielded ZERO sessions "
              f"({tracked_path}) — NOTHING is being watched; this is not a "
              f"quiet sweep", file=sys.stderr, flush=True)
    return out


def heartbeat_live(sid8, live_dir=LIVE_DIR, ttl_s=HEARTBEAT_TTL_S, now=None):
    """True when the heartbeat store holds a FRESH stamp for `sid8`.

    The union's second half. It is the only liveness source that covers a headless
    worker: such a worker is an in-process SDK `query()` with no pid, so
    `registry_status` can never list it, and without this the registry half is
    UNAVAILABLE rather than negative — which is why `is_gated` needs to tell the
    two apart instead of reading both as UNREGISTERED.

    ⚠️ The stamp's **mtime** is the age authority, never the `at` field inside it
    (`server/heartbeat.mjs:62`). The write sets mtime, so it cannot disagree with
    itself; an `at` left behind by a crashed writer would report a dead session
    live for as long as the file survives.

    A missing or unreadable store is False rather than an exception: an absent
    heartbeat is exactly what every session on a host without the store produces,
    and the registry half already covers those.
    """
    now = time.time() if now is None else now
    try:
        names = os.listdir(live_dir)
    except OSError:
        return False
    for name in sorted(names):
        if not name.endswith(".json") or name[:-5] in LIVE_SKIP:
            continue
        if not name.startswith(sid8):
            continue
        try:
            age = now - os.path.getmtime(os.path.join(live_dir, name))
        except OSError:
            continue
        # A future-dated mtime (clock skew, restored backup) gives a negative age;
        # reading it live would turn an UNREGISTERED hold into an unearned CLEARED.
        if 0 <= age <= ttl_s:
            return True
    return False


def registry_status(sid8, sessions_dir=SESSIONS_DIR, warned=None):
    """Registry status for a session id or its 8-char prefix, or None if unlisted.

    Joined on `sessionId`, never on cwd: the `cwd` a spawn response reports is
    unreliable for a launcher-`cd` worker, while this join is not. An 8-char prefix
    is a legal lookup — it is the form the task files carry — and an ambiguous
    prefix resolves to the first match after sorting, so the choice is
    deterministic rather than filesystem-order. A collision at 8 hex characters is
    not practically reachable, and this status now decides `CLEARED` as well as "is
    it parked", so the sort is worth having.

    None is a three-way answer — UNREGISTERED — and callers must never read it as
    "no gate"; see the module docstring.

    ⚠️ A registry file that fails to PARSE is not the same as one that did not
    match, though both read as None here: a file caught mid-rewrite holds a live
    session as UNREGISTERED and re-fires its `NEW GATE` when the write lands. That
    is reported on stderr rather than left silent; `warned` dedupes it so a
    long-running poll loop does not repeat it every 60 s.
    """
    if warned is None:
        warned = set()
    try:
        names = os.listdir(sessions_dir)
    except OSError as exc:
        if "registry-unreadable" not in warned:
            warned.add("registry-unreadable")
            print(f"WATCH WARN: registry unreadable ({exc}) — every tracked "
                  f"session reads UNREGISTERED and is HELD; this is not a quiet "
                  f"sweep", file=sys.stderr, flush=True)
        return None
    for f in sorted(names):
        if not f.endswith(".json"):
            continue
        try:
            with open(os.path.join(sessions_dir, f)) as fh:
                d = json.load(fh)
        except Exception as exc:
            key = f"registry-parse:{f}"
            if key not in warned:
                warned.add(key)
                print(f"WATCH WARN: registry entry {f} unreadable ({exc}) — a "
                      f"session it lists reads UNREGISTERED this poll",
                      file=sys.stderr, flush=True)
            continue
        sid = str(d.get("sessionId") or "")
        if sid.startswith(sid8):
            return d.get("status")
    return None


def read_tail(path, window=400_000):
    """The decoded tail of the transcript, or None when it cannot be read.

    ⚠️ The window is lossy, and the loss is measured rather than theoretical. On
    the e4fd1919 transcript (3.8 MB, measured 2026-10-05): of 26 `Stream closed`
    records, 18 fall inside a 400 KB tail and 8 outside, the first sitting 69%
    through the file. Enough to fire on that fixture; a longer-lived worker drifts
    further out. Both readers below share this ONE window rather than paying for a
    second read on every poll.
    """
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as fh:
            fh.seek(max(0, size - window))
            return fh.read().decode("utf-8", "replace")
    except OSError:
        return None


def assistant_text_from_tail(tail):
    """Last assistant text block in a decoded tail, or None when there is none.

    ⚠️ `None` means "the tail window held no assistant text at all", which is NOT
    the same as "the last text block carries no closer". Collapsing the two is a
    false-`CLEARED` route: the window is a fixed 400 KB, so a transcript that
    appended more than that in `tool_result` records after the closer pushes the
    closer out of reach, and an `idle` worker still parked on a live ask would read
    as "no closer" and be cleared. The caller holds instead; see `_CLOSER_UNKNOWN`.
    """
    text = ""
    for line in tail.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if rec.get("type") != "assistant":
            continue
        content = (rec.get("message") or {}).get("content")
        if not isinstance(content, list):
            continue
        for blk in content:
            if isinstance(blk, dict) and blk.get("type") == "text" and blk.get("text"):
                text = blk["text"]
    return text or None


def last_assistant_text(path):
    """`assistant_text_from_tail` for a transcript path, or None when unreadable.

    Reads the tail only. A transcript is unbounded and this runs every poll for
    every tracked worker; the last message is at the end by definition.
    """
    tail = read_tail(path)
    return None if tail is None else assistant_text_from_tail(tail)


def transcript_frozen(path):
    """Whether the transcript has gone unwritten for at least `LIVE_WINDOW`.

    ⚠️ An unreadable mtime REFUSES the freeze rather than assuming it. A freeze is
    what raises the third limb's gate, so a failed `stat` must not manufacture one —
    the same fail-closed direction `_CLOSER_UNKNOWN` takes in the other half, and the
    opposite of a silent fire.
    """
    try:
        return (time.time() - os.path.getmtime(path)) >= LIVE_WINDOW
    except OSError:
        return False


def pending_tool_call(tail):
    """The name of the last `tool_use` with no matching `tool_result`, or None.

    ⚠️ **A pending call does NOT mean parked, and this function does not claim it
    does.** `CLAUDE.md` § Reading a worker measures the trap: across 25 live sessions
    a pending call read identically whether the worker was executing a tool or parked
    on a prompt — *"the transcript answers which call, the registry answers is it
    moving."* The call NAME is the half this adds: a `Bash` call may legitimately run
    for minutes, but an `AskUserQuestion` cannot proceed without the operator, so a
    pending one is a wait by construction. The caller supplies the "is it moving"
    half from `transcript_frozen`.

    ⚠️ **Takes the DECODED TAIL, never a path.** `probe` already reads that window once
    for the closer half and this reads the same bytes, so taking a path here cost a
    second 400 KB read per tracked session per poll while the docstring claimed the
    two shared one window. Passing the tail in is what makes that claim true.

    ⚠️ **`pending` holds ONE slot — the last `tool_use` seen.** For an assistant message
    carrying `[AskUserQuestion, Bash]` in one record, the `Bash` result clears the slot
    and masks the still-unanswered modal. That is the MISS direction, which is the
    safer one here, but it is not the set-based rule the sentence above could be read
    as describing.

    A `tool_use` whose `tool_result` fell outside the window reads as pending — the
    false-positive direction. The caller's freeze requirement bounds it, though not
    perfectly: a result outside the window followed by a long silent tool would read
    frozen and gate spuriously.
    """
    if tail is None:
        return None
    pending = None
    for line in tail.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        content = (rec.get("message") or {}).get("content")
        if not isinstance(content, list):
            continue
        for blk in content:
            if not isinstance(blk, dict):
                continue
            if blk.get("type") == "tool_use":
                pending = (blk.get("id"), blk.get("name"))
            elif blk.get("type") == "tool_result":
                # Only the call this result belongs to is answered; a result for an
                # older call must not clear a newer pending one.
                if pending and blk.get("tool_use_id") == pending[0]:
                    pending = None
    return pending[1] if pending else None


def closer_body(text):
    """The body of the LAST `👤 You:` line in `text`, or None when there is none.

    Last, not first: a worker may quote an earlier closer while writing about it,
    and only the final line is the panel a manager would read.
    """
    lines = [l.strip() for l in text.splitlines() if l.strip().startswith("👤 You:")]
    if not lines:
        return None
    return lines[-1].split("👤 You:", 1)[1].strip()


def normalize_closer(text):
    """Strip HTML entities and collapse whitespace before a verb comparison.

    Panes have emitted `👤 You: &nbsp;&nbsp;&nbsp;&nbsp;nothing`. The entity is
    text, so a raw `startswith("nothing")` missed it and a session that had closed
    clean was reclassified as an open question (`who-needs-me.py:127`, pinned at
    `test_who_needs_me.py:114`). The same miss here has the mirror cost: the session
    is held as gated for as long as the registry lists it `idle` — a gate stuck open
    for a worker that already closed.
    """
    return " ".join(_ENTITY.sub(" ", text or "").split()).strip()


def is_ask(body):
    """Whether a closer body is a live ask.

    Not a live ask: the `nothing` panel, and a `later (on <trigger>):` parked wait —
    neither is something the operator can answer now. Both tests run on the
    NORMALIZED body, because the raw one carries entity padding that defeats a
    prefix match.
    """
    verb = normalize_closer(body)
    if not verb:
        return False
    return not (verb.lower().startswith("nothing")
                or verb.startswith(PARKED_VERBS))


def permission_outcomes(tail):
    """Every `tool_result` in the tail, in order, as `(perm_failure, stream_closed)`.

    ⚠️ Only TOOL RESULTS count, never assistant prose. A worker discussing a closed
    channel — quoting the error, or writing about this very detector — carries the
    words in its text blocks, and a substring match over the whole tail read that
    worker as STUCK. The heal ladder would then resume a healthy session.

    A permission failure is a `tool_result` carrying `is_error` and the harness's own
    wording, e.g. `Tool permission request failed: AbortError: Stream closed`.
    """
    out = []
    if not tail:
        return out
    for line in tail.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        content = (rec.get("message") or {}).get("content")
        if not isinstance(content, list):
            continue
        for blk in content:
            if not isinstance(blk, dict) or blk.get("type") != "tool_result":
                continue
            body = str(blk.get("content") or "")
            perm = bool(blk.get("is_error")) and PERM_FAILURE_MARK in body
            out.append((perm, perm and "Stream closed" in body))
    return out


def trailing_failures(outcomes):
    """The run of permission failures since the last tool that actually RAN.

    ⚠️ **"Consecutive" counts among PERMISSION OUTCOMES, not consecutive records.**
    Assistant text and attachments between failures do not break the run; a
    record-consecutive reading scores the e4fd1919 death as 1 and the ≥3 trigger
    never fires. The operator's call, 2026-10-05.

    ⚠️ TRAILING, not longest. Any `tool_result` that is not a permission failure —
    success, or an ordinary tool error — means the tool executed, so the channel
    works again: the run resets there. A longest-run reading kept a recovered worker
    STUCK until its evidence scrolled out of the window.
    """
    run = 0
    for perm, _ in outcomes:
        run = run + 1 if perm else 0
    return run


def permission_failure_run(tail):
    """`trailing_failures` over a raw tail."""
    return trailing_failures(permission_outcomes(tail))


def stuck_reason(text, tail):
    """Why a worker reads STUCK, or None. Three triggers, OR'd, most specific first.

    Ordering is not cosmetic: when a worker both lost its channel and counted three
    failures, the reason names the CAUSE (`stream-closed`) rather than the symptom
    (`permission-failures`).

    `stream-closed` fires only when the trailing failure run contains a closed
    channel — read from tool results, and cleared by any tool that ran after it.
    `blocked-closer` matches the state line as a line PREFIX: the phrase also occurs
    mid-line in a worker's prose about it, and a substring match would fire on a
    worker that is merely discussing a block.
    """
    outcomes = permission_outcomes(tail)
    run = trailing_failures(outcomes)
    if run and any(sc for _, sc in outcomes[len(outcomes) - run:]):
        return "stream-closed"
    if text and any(l.strip().startswith(BLOCKED_PREFIX) for l in text.splitlines()):
        return "blocked-closer"
    if run >= STUCK_PERM_FAILURES:
        return "permission-failures"
    return None


def is_gated(status, body, headless_live=False, stuck=None, pending=None,
             frozen=False):
    """The composite predicate. Returns (gated, reason).

    See the module docstring for why it is a union rather than either half, its
    A MID-TURN SESSION IS HELD section for the `held:<status>` third answer, and
    its THE THIRD LIMB section for `pending` / `frozen`.

    Three answers, and the caller must keep all three apart: `True` gated,
    `False` NOT gated, `None` HELD — the watcher cannot justify a clear. `None`
    has two causes and both mean exactly that: UNREGISTERED (the registry cannot
    list this session) and a status that is neither `waiting` nor `idle` (the
    worker is mid-turn, so nothing has been answered yet). Collapsing either into
    `False` emits a `CLEARED` that nothing earned.

    ⚠️ The two `None` causes are NOT interchangeable downstream: a mid-turn hold
    keeps its gated membership and an unregistered one drops it. `transitions`
    branches on the `held:` reason prefix — see the module docstring.

    ⚠️ UNREGISTERED and HEADLESS are different facts and must not collapse. A
    `None` status with a fresh heartbeat means the registry cannot list this
    session — a headless worker, which has no pid — so the registry half is
    UNAVAILABLE rather than negative, and the transcript half decides alone.
    Reading both as UNREGISTERED is what holds every headless worker forever.
    """
    # Deliberately ahead of the registry half: a dead permission channel is the
    # more specific fact, even on a worker the registry also reads as waiting.
    # ⚠️ A registered worker is a TAB worker, and the heal ladder's first rung is a
    # HEADLESS resume — so it gets its own `stuck-tab:` reason, which earns the card
    # and never the ladder. Only an unregistered (headless) worker reads `stuck:`.
    if stuck:
        return True, ("stuck:" if status is None else "stuck-tab:") + stuck
    if status is None:
        if not headless_live:
            return None, "unregistered"
        if body is _CLOSER_UNKNOWN:
            return None, "closer-unknown"
        if is_ask(body):
            return True, "headless+closer"
        return False, "headless:no-ask"
    if status == "waiting":
        return True, "registry:waiting"
    if status == "busy" and pending == "AskUserQuestion" and frozen:
        # ⚠️ THE THIRD LIMB, and it is a POSITIVE answer where the fallthrough HOLDs.
        # HELD keeps membership unchanged, so a worker that was never in the gated
        # set — the incident's shape: `busy` with its closer already displaced on the
        # first poll — stays ungated for as long as it is parked. A pending
        # `AskUserQuestion` on a frozen transcript is the fact the registry cannot
        # supply: the worker is waiting on the operator, not mid-tool. See the module
        # docstring § THE THIRD LIMB.
        #
        # ⚠️ Deliberately AHEAD of the `_CLOSER_UNKNOWN` hold below. This limb does
        # not read the closer at all — that is its whole point — so an unreadable
        # tail must not demote a park it can see directly to HELD.
        return True, "busy+parked-modal"
    if body is _CLOSER_UNKNOWN:
        # The transcript read reached no text block at all, so "no closer" is
        # unknown rather than observed. HELD, never cleared: this is a failed read,
        # and a failed read must not be representable as an empty result.
        return None, "closer-unknown"
    if status == "idle":
        # Only a SETTLED `idle` decides either way: `idle` + a live ask is a gate,
        # `idle` + no ask is the one state that justifies a `CLEARED`.
        return (True, "idle+closer") if is_ask(body) else (False, "idle:no-ask")
    # ⚠️ Anything else — `busy` (mid-turn), `shell` — is HELD, never cleared.
    # A gated worker that starts a NEW TURN reads `busy`, which is neither
    # `waiting` nor `idle`; returning False here emitted a bare `CLEARED` for a
    # worker that had answered nothing and was still mid-turn. See the module
    # docstring's A MID-TURN SESSION IS HELD section for the measured sequence and
    # for the over-report this direction accepts.
    return None, "held:" + str(status)


def probe(tracked_path, tasks_dir, projects_root, sessions_dir=SESSIONS_DIR,
          warned=None, live_dir=LIVE_DIR, tracked=None):
    """{sid8: (task name, detail, reason, verdict)} for every tracked worker
    that has a transcript.

    `verdict` is three-valued and the caller must keep all three apart:
    `True` gated, `False` not gated, `None` UNREGISTERED. Collapsing `None` into
    `False` is how a dead worker gets a `CLEARED` it did not earn — the same
    silent direction as the defect this file exists to fix.

    `projects_root` is the ROOT (`~/.claude/projects`), never one munged cwd
    directory. A tracked worker may run in any cwd, and every sibling resolves
    transcripts across the root for exactly that reason (`who-needs-me.py`,
    `reap-closer.py`, `voice-ask-pairing.py`). A per-cwd path silently bounds
    coverage to one cwd's workers *and* fails quietly when the cwd is munged
    wrongly — so the root is the default and there is no escaping rule to get
    wrong.
    """
    if warned is None:
        warned = set()
    if tracked is None:
        # The poll loop parses the tracked set once per poll anyway, because the
        # re-post needs the FULL session ids for its `producer_id` join and
        # `tracked_ids` projects those away. Taking the projection as an argument
        # keeps that to one parse — and one set of WATCH WARN lines — per poll.
        tracked = tracked_ids(tracked_path, tasks_dir)
    state = {}
    for sid8, label in tracked.items():
        cands = sorted(glob.glob(os.path.join(projects_root, "*",
                                              sid8 + "*.jsonl")))
        if not cands:
            # Never silent. A tracked id with no transcript is a scope gap, and
            # an empty result here must not read as a quiet sweep. Warned once
            # per id per process, because a poll would otherwise repeat it every
            # 60 s for a task that is simply not running.
            if sid8 not in warned:
                warned.add(sid8)
                print(f"WATCH WARN: no transcript for tracked session {sid8} "
                      f"({label}) under {projects_root} — NOT watched; this is "
                      f"not a quiet sweep", file=sys.stderr, flush=True)
            continue
        tail = read_tail(cands[0])
        text = None if tail is None else assistant_text_from_tail(tail)
        if text is None:
            # The tail window held no assistant text at all. Warned, because this
            # is the one degraded read here that would otherwise be silent — and
            # `is_gated` holds it rather than reading it as "no closer".
            if sid8 not in warned:
                warned.add(sid8)
                print(f"WATCH WARN: no assistant text in the transcript tail for "
                      f"{sid8} ({label}) — closer unknown; held, not cleared",
                      file=sys.stderr, flush=True)
            body = _CLOSER_UNKNOWN
        else:
            body = closer_body(text)
        stuck = stuck_reason(text, tail)
        verdict, reason = is_gated(
            registry_status(sid8, sessions_dir, warned), body,
            headless_live=heartbeat_live(sid8, live_dir), stuck=stuck,
            # The third limb's two halves. `tail` is the window already read above for
            # the closer half — passed in rather than re-read, so the two readers share
            # ONE 400 KB read per session per poll. Both are taken unconditionally
            # rather than only for `busy` rows: gating them on the registry word would
            # put one decision in two places.
            pending=pending_tool_call(tail),
            frozen=transcript_frozen(cands[0]))
        if stuck:
            # The reason is the signal; the closer body is context for whoever
            # reads the card, so it is appended rather than replacing it.
            detail = f"{reason} — {body}" if isinstance(body, str) and body else reason
        else:
            detail = body if isinstance(body, str) and body else reason
        state[sid8] = (label, detail[:150], reason, verdict)
    return state


def gated_keys(state):
    """The session ids the watcher should hold, from a `probe()` result."""
    return sorted(sid8 for sid8, (_, _, _, v) in state.items() if v is True)


def stable_sessions(prev, pending, key):
    """The sessions whose gated membership has held across the last two polls.

    The stability gate's whole decision, extracted so the honest-`CLEARED`
    question can be tested directly rather than only by driving the poll loop.

    ⚠️ **Per session, never whole-set.** A set alternating `{A}` / `{A,B}` never
    equals its own previous value, so a whole-set test withholds EVERY session's
    transition for as long as ANY one of them churns — including A's own
    `CLEARED` when A is answered. That is this file's own defect direction (a gate
    stuck open) reached through a peer's churn. Found in review 2026-10-04.

    `prev` is in the universe on purpose: a departure is only stable once the
    absence is two polls old, and a session that has left both `key` and `pending`
    is still in `prev` and still owes its transition.
    """
    key_set, pending_set = set(key), set(pending or [])
    return {s for s in key_set | pending_set | set(prev or [])
            if (s in key_set) == (s in pending_set)}


def is_hold(reason):
    """Whether a `None` verdict's reason means the membership must be PRESERVED.

    Two shapes, one meaning — nothing was answered, so the gate stays up and
    `transitions` emits no record for the session.

    - `held:<status>` — the worker is mid-turn. It will settle, and that settle
      is what earns the one `CLEARED` a genuine answer is owed.
    - `closer-unknown` — the transcript tail held no text block at all, so "no
      closer" is unknown rather than observed. `is_gated` already returns HELD
      for this ("a failed read must not be representable as an empty result");
      this predicate is what makes `transitions` agree with it. Without it the
      reason failed the `held:` prefix test, took the UNREGISTERED branch, and
      DROPPED a gate that was still up — so the genuine answer that arrived
      later was not a transition at all and emitted no `CLEARED`. A false clear
      was replaced by no clear.

    ⚠️ `unregistered` is deliberately NOT here, and that is why this is a named
    predicate rather than a `verdict is None` test. A dead worker answered
    nothing either, but there is nothing left to hold — its membership is
    dropped and its HELD record IS emitted. Collapsing every `None` reason into
    the preserving set would swallow that record: a different defect from the
    one this closes. See `test_unregistered_is_held_never_cleared`.

    ⚠️ The two qualifying reasons are the same fact — a read that carried no
    answer — and NOT a unified code path. Each producer keeps its own return
    site in `is_gated` and its own reason string, so a later divergence stays
    possible without unpicking a shared branch.
    """
    return reason.startswith("held:") or reason == "closer-unknown"


def transitions(prev, key, state):
    """The transitions one poll emits — `(kind, sid8, label, detail)` records.

    Returned rather than printed so the diff, which is the part that decides
    whether a `CLEARED` is honest, can be tested directly instead of only by
    constructing inputs that avoid it.

    The `HELD` branch is the reason this is three-valued, and it has TWO causes,
    both of which mean "the watcher cannot justify a clear":

    - the session is in `state` with a `None` verdict and a reason `is_hold`
      REJECTS — it became UNREGISTERED, so nothing was answered;
    - the session is absent from `state` entirely — the watcher lost sight of it,
      which is equally not an answer.

    ⚠️ A `None` verdict whose reason `is_hold` ACCEPTS is the THIRD case and takes
    NEITHER branch: nothing was answered, so the gate stays up and the membership
    is left exactly as it was — no `CLEARED`, no `NEW GATE`, and no record.
    Recording a hold on every poll would fill the event log with a state that has
    not changed. Two reasons qualify — `held:<status>` (mid-turn) and
    `closer-unknown` (a failed transcript read); see `is_hold` for why they share
    a direction, and why `unregistered` is not among them. See also the module
    docstring's A MID-TURN SESSION IS HELD section.

    ⚠️ The second case is the one the earlier version got wrong: absent-from-`state`
    fell through to a bare `CLEARED`. That is the shared-state-file failure — with a
    state file belonging to another manager, every one of that manager's sessions
    lands in `prevset - set(key)` with no entry here — but it is reachable with no
    shared file at all, via a transcript that vanished or a `claude_session_id` that
    changed. `state_path_for` removes the shared-file cause; this guard removes the
    whole class.
    """
    out = []
    prevset = set(prev or [])
    for sid8 in key:
        if sid8 not in prevset:
            label, detail, reason, _ = state[sid8]
            out.append(("NEW GATE", sid8, label, f"{reason} :: {detail}"))
    for sid8 in sorted(prevset - set(key)):
        if sid8 not in state:
            # Lost sight of it: no transcript under `--projects-root`, or the id
            # left `tracked_ids()` because the task file went away or its
            # `claude_session_id` changed. Neither is an answer, so neither is a
            # clear — this was the last path emitting a `CLEARED` the code cannot
            # justify.
            out.append(("HELD", sid8, "",
                        "no longer resolved (no transcript, or no longer "
                        "tracked); no CLEARED emitted"))
            continue
        if state[sid8][3] is None:
            # Two causes, and they need OPPOSITE handling.
            reason = state[sid8][2]
            if is_hold(reason):
                # NOT AN ANSWER — mid-turn (`held:<status>`) or a failed closer
                # read (`closer-unknown`). The worker has not answered, so the
                # gate stays up. Membership is left EXACTLY as it was — no
                # `CLEARED` (nothing was answered) and no `NEW GATE` (it is
                # already in the set).
                # ⚠️ Keeping it in `prev` is what lets the eventual settle at
                # `idle` emit the one `CLEARED` a genuine answer earns; discarding
                # here would forget the gate, and the answer would then clear
                # SILENTLY — a fix for one false clear that produces no true one.
                # That was `closer-unknown`'s own defect: a failed read failed the
                # `held:` prefix test, took the UNREGISTERED branch below, and
                # dropped a gate that was still up.
                continue
            # UNREGISTERED: a dead or never-spawned worker. Nothing was answered
            # here either, but there is nothing left to hold, so the membership is
            # dropped and the transition is recorded rather than announced.
            out.append(("HELD", sid8, state[sid8][0],
                        f"{reason}; no CLEARED emitted"))
            continue
        out.append(("CLEARED", sid8, "", "left the gated set"))
    return out


def state_path_for(state_dir, tracked_path):
    """The state file for one manager's scope — keyed by its tracked set.

    ⚠️ **Not a bare `state.json`, and this is a correctness requirement rather
    than tidiness.** Two managers sharing the default state directory is the
    *ordinary* case — every manager on the machine resolves `DEFAULT_STATE` —
    and with one shared file each poll reads the OTHER manager's gated set as its
    own `prev` and then overwrites it. Every one of that manager's sessions then
    lands in `prevset - set(key)`, misses the `HELD` guard (they are not in this
    manager's `state`), and prints a **bare `CLEARED`** — the same silent
    direction this whole file exists to close, reintroduced by a filename. Two
    managers on the same tracked set still share, which is correct: that is one
    scope, and `manager-loop` already enforces one manager per subject.
    """
    scope = hashlib.sha1(os.path.abspath(tracked_path).encode()).hexdigest()[:8]
    return os.path.join(state_dir, f"state-{scope}.json")


def park_path_for(state_dir, tracked_path):
    """The park-age state file for one manager's scope.

    ⚠️ **A SIBLING of `state_path_for`'s file, never a wider record inside it.**
    The gated-set file is a plain JSON list of session ids and is read back with
    `set(json.load(fh))`; widening it to a dict would make every existing state
    file unreadable, and this file's own read path answers an unreadable state
    with a `NEW GATE` burst for the whole tracked set. Same scope key, same
    directory, same tmp + `os.replace` convention.
    """
    scope = hashlib.sha1(os.path.abspath(tracked_path).encode()).hexdigest()[:8]
    return os.path.join(state_dir, f"park-{scope}.json")


def open_store_items(store=STORE, timeout=STORE_TIMEOUT):
    """`({producer_id}, {dedup_key})` over the store's OPEN items, or `None`.

    `GET /api/1.0/attention` is the store's item read — the same one
    `who-needs-me.py:213` uses — and it answers with items in **every** state, not
    only open ones.

    ⚠️ **So the `state` filter below is load-bearing, not belt-and-braces.**
    `who-needs-me.py:271` maps a non-`open` `state` to `answered`, and that branch
    is only reachable if the endpoint can return one — so three sibling docstrings
    that call this endpoint "open items only" are wrong. An ANSWERED card counted
    as open would suppress the re-post for a worker that is still parked on a
    fresh question, which is a **missed** escalation rather than a noisy one.

    ⚠️ **`None` on failure, never two empty sets.** An empty result is a positive
    claim — "the store holds nothing for anyone" — and the re-post acts on it, so
    a store that is merely unreachable would re-post a card for every parked
    worker on every poll. Same direction the rest of this file closes: a failed
    read must not be representable as an empty one.

    ⚠️ **The projection sits INSIDE the guard for the same reason.** `json.loads`
    succeeding does not mean the body is a list — `agent_status`-style error
    payloads are objects — and an `AttributeError` raised out there would escape
    the `None` contract the caller depends on.
    """
    try:
        with urllib.request.urlopen(store + "/api/1.0/attention",
                                    timeout=timeout) as resp:
            items = json.loads(resp.read().decode("utf-8") or "[]")
        open_items = [i for i in items if i.get("state") == "open"]
        return ({i.get("producer_id") or "" for i in open_items},
                {i.get("dedup_key") or "" for i in open_items})
    except Exception as exc:
        print(f"WATCH WARN: store read failed ({exc}) — re-post suppressed this "
              f"poll; this is NOT a reading that the store holds nothing",
              file=sys.stderr, flush=True)
        return None


def parked_keys(state):
    """The session ids PARKED on the operator right now, from a `probe()` result.

    Distinct from `gated_keys`: that is the set the watcher HOLDS, whose
    membership survives a mid-turn `busy` by design, while this is the set that
    is parked *this poll*. The two answer different questions, and the park-age
    clock needs this one — see `advance_park_ages`.
    """
    return sorted(sid8 for sid8, (_, _, reason, v) in state.items()
                  if v is True and reason in PARKED_REASONS)


def advance_park_ages(ages, parked, now):
    """Advance `{sid8: {"since": epoch, "aged": [thresholds]}}` by one poll.

    ⚠️ **Keyed on CONTINUOUS PARK per registry status, never on the gated-set
    transition.** A worker that answers one question and asks the next stays in
    the watcher's gated set the whole time — `is_gated` holds it through the
    `busy` interval — so a clock keyed on set membership would run once from the
    first park and never restart, and the SECOND park would never surface. That
    is the measured gap this limb closes: 2026-10-07, two tracked sessions parked
    on fresh `AskUserQuestion`s inside a 30-minute window and the arm delivered 0
    events, because neither park was a set transition.

    So a non-parked poll ENDS the park and the next parked poll STARTS a new one.

    ⚠️ **The clock starts at first observation, so restarting this arm resets
    every age.** The registry carries no timestamp for when a session entered
    `waiting`, so a park already an hour old when the arm starts reads as age 0
    and earns its `AGED` late. Stated rather than hidden — the alternative is
    inventing an entry time, and an invented age is worse than a late one.

    Mutates `ages` in place and returns it.
    """
    parked = set(parked)
    for sid8 in list(ages):
        if sid8 not in parked:
            del ages[sid8]
    for sid8 in parked:
        ages.setdefault(sid8, {"since": now, "aged": []})
    return ages


def aged_events(ages, now, thresholds=PARK_AGE_THRESHOLDS):
    """`[(sid8, minutes)]` for thresholds crossed since the last call, ONCE each.

    Pure apart from the `aged` marker it appends to, so the once-each contract is
    testable directly rather than only by driving the poll loop — the same reason
    `transitions` and `stable_sessions` are extracted.
    """
    out = []
    for sid8, rec in ages.items():
        # `max(0, ...)` rather than a raw subtraction: a backwards clock step
        # (NTP, a manual set) would otherwise yield a negative age. That is benign
        # — it simply does not fire — but the direction should be stated rather
        # than left incidental.
        minutes = max(0, int((now - rec["since"]) // 60))
        for t in thresholds:
            if minutes >= t and t not in rec["aged"]:
                rec["aged"].append(t)
                out.append((sid8, t))
    return out


def post_repost(sid8, full, label, script_dir=None, timeout=30):
    """Post one re-registered card via `attention-ask.py post`. True on success.

    Shelled out rather than re-implemented: that script owns the dedup key, the
    `owner:` liveness default and the TTL bound, and a second POST site here
    would be a second place for all three to drift — with the store's pruning
    rule turning a wrong `liveness_ref` into a silently deleted card rather than
    an error anyone would see.
    """
    script = os.path.join(
        script_dir or os.path.dirname(os.path.abspath(__file__)),
        "attention-ask.py")
    cmd = [sys.executable, script, "post",
           "--producer-id", full,
           "--dedup-key", f"park:{sid8}",
           "--payload",
           f"{label}: this session is parked on the operator and the attention "
           f"store holds no open item for it. Re-registered by the tracked-set "
           f"watcher — answer it and the session can continue."]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except Exception as exc:
        print(f"WATCH WARN: re-post for {sid8} could not run ({exc})",
              file=sys.stderr, flush=True)
        return False
    if proc.returncode != 0:
        print(f"WATCH WARN: re-post for {sid8} refused (exit {proc.returncode}): "
              f"{proc.stdout.strip()} {proc.stderr.strip()}",
              file=sys.stderr, flush=True)
        return False
    return True


def repost_empty_parks(parked, entries, log_path, post=None):
    """Re-post a card for every parked session the store no longer holds.

    Returns the `sid8`s re-posted. `post(sid8, full, label)` is injectable, so
    the tests assert the call without a live store.

    ⚠️ **The producer is the WORKER, not this watcher's manager.** A store item's
    session is its `producer_id` (`who-needs-me.py:268`), so posting as the
    worker is what makes the re-posted row attribute to the session that is
    actually parked — and what routes the operator's answer back to it. It also
    collapses "is there already a card?" to one field: an open item whose
    `producer_id` is this worker, or whose `dedup_key` is our own stable
    `park:<sid8>`, IS the card this call would have posted.

    ⚠️ **`liveness_ref` must stay the `owner:` default.**
    `attention-ask.py:506-532` records that the store PRUNES an open asked item
    whose liveness subject is not live, so a `session:<id>` subject deletes the
    card on the first read after the poster exits. `owner:` is unconditionally
    live, which is what lets a re-posted card outlive the turn that posted it —
    and a re-post is by construction posted by something other than the parked
    worker.

    ⚠️ A store read that FAILS re-posts nothing — `open_store_items` returns
    `None` — rather than reading as "the store holds nothing".
    """
    if post is None:
        post = post_repost
    if not parked:
        # ⚠️ The store is not read at all when nothing is parked. Two reasons, and
        # the second is the load-bearing one: a poll with no parked session has no
        # card to re-post, and — more importantly — an idle arm must not depend on
        # the store being up. Reading it anyway would make this arm's own liveness
        # a function of the store's, which is the coupling the LIVENESS record
        # exists to break.
        return []
    items = open_store_items()
    if items is None:
        return []
    producers, dedup_keys = items
    out = []
    for sid8 in parked:
        if f"park:{sid8}" in dedup_keys:
            continue
        name, full = entries.get(sid8, ("", ""))
        if not full or full in producers:
            continue
        if not post(sid8, full, name or sid8):
            continue
        out.append(sid8)
        log_event(log_path, "REPOST", sid8, name,
                  f"store held no open item for {sid8}; re-posted as park:{sid8}")
    return out


def log_event(log_path, kind, sid8, label, detail):
    """Append one transition to the durable event log.

    The watcher's stdout is captured by the `Monitor` and nothing persists it, so
    without this file a `CLEARED` or a `NEW GATE` cannot be shown after the fact —
    which is what left the filing row's criteria with no evidence surviving the
    arm.
    """
    try:
        os.makedirs(os.path.dirname(log_path), exist_ok=True)
        with open(log_path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({
                "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                "kind": kind,
                "session": sid8,
                "task": label,
                "detail": detail,
            }) + "\n")
    except OSError as exc:
        print(f"WATCH WARN: could not write event log: {exc}",
              file=sys.stderr, flush=True)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--tracked", required=True,
                    help="tracked-set file written by manager-predispatch.py")
    ap.add_argument("--tasks-dir", required=True, help="the vault's tasks directory")
    ap.add_argument("--projects-root", default=DEFAULT_PROJECTS_ROOT,
                    help="root holding one directory per cwd; transcripts are "
                         "resolved across all of them (default: %(default)s)")
    ap.add_argument("--sessions-dir", default=SESSIONS_DIR,
                    help="the session registry (default: %(default)s)")
    ap.add_argument("--live-dir", default=LIVE_DIR,
                    help="the supervisor's heartbeat store — the second liveness "
                         "source, and the only one that covers a headless worker "
                         "(default: %(default)s)")
    ap.add_argument("--state", default=DEFAULT_STATE,
                    help="state + event-log directory (default: %(default)s). "
                         "⚠️ The state FILE is keyed by --tracked, so two "
                         "managers with different tracked sets cannot collide "
                         "even when they share this directory — but pass a "
                         "per-manager directory anyway, so the event logs are "
                         "separable.")
    ap.add_argument("--interval", type=int, default=POLL_SECONDS)
    ap.add_argument("--once", action="store_true",
                    help="one poll, print the verdict, exit (diagnostic only)")
    ap.add_argument("--max-polls", type=int, default=None,
                    help="stop after N polls (diagnostic/test only; default: "
                         "run until killed). ⚠️ `--once` returns BEFORE the "
                         "stability gate and the commit loop, so it cannot "
                         "exercise the path that decides whether a `CLEARED` is "
                         "honest — this flag is how that path is driven.")
    args = ap.parse_args(argv)

    state_path = state_path_for(args.state, args.tracked)
    park_path = park_path_for(args.state, args.tracked)
    log_path = os.path.join(args.state, "events.jsonl")
    # The tracked set's own name, for the liveness record's subject slot — the
    # same `<topic>` the runbook's tick marker carries (§ Sweep output). Derived
    # from `--tracked`, so two managers sharing a `--state` still write
    # attributable lines.
    tracked_name = os.path.basename(args.tracked)
    for suffix in (".tracked.txt", ".tracked.new", ".txt"):
        if tracked_name.endswith(suffix):
            tracked_name = tracked_name[: -len(suffix)]
            break

    try:
        with open(state_path) as fh:
            prev = json.load(fh)
    except FileNotFoundError:
        prev = None  # first run — the ordinary case, not a degraded read
    except Exception as exc:
        # A state file that exists but cannot be read is a degraded read, and its
        # consequence is loud rather than silent: with `prev = None`, `key != prev`
        # holds for every gated session, so the next poll re-emits `NEW GATE` for
        # all of them. Say so instead of letting that read as a fresh start.
        print(f"WATCH WARN: state file unreadable ({exc}) — treating as a first "
              f"run; every currently-gated session will re-emit NEW GATE once",
              file=sys.stderr, flush=True)
        prev = None

    # STABILITY GATE. A worker mid-turn flips between "has a closer" and
    # "doesn't" as records interleave, and a single poll reads that as a gate
    # opening and closing. A genuine gate holds for minutes; churn does not. So
    # require the new state to survive one poll before announcing it.
    # The last COMMITTED gated set, held as a set because it now advances PER
    # SESSION rather than by whole-set replacement — see the stability gate below.
    prev = set(prev or [])

    # PARK AGES — a sibling file, read the same way and failing in the same
    # direction. ⚠️ The consequence differs from the gated-set read and is worth
    # naming: a park-age file that cannot be read restarts every clock, which
    # makes an escalation LATE rather than suppressing it. Late is the safe
    # direction here, so this warns and continues rather than refusing.
    park_ages = {}
    try:
        with open(park_path) as fh:
            park_ages = json.load(fh)
    except FileNotFoundError:
        pass
    except Exception as exc:
        print(f"WATCH WARN: park-age state unreadable ({exc}) — every park-age "
              f"clock restarts this run; escalations are LATE, not lost",
              file=sys.stderr, flush=True)
        park_ages = {}

    pending = None          # the previous poll's gated set
    warned = set()
    polls = 0
    while True:
        try:
            entries = tracked_entries(args.tracked, args.tasks_dir)
            state = probe(args.tracked, args.tasks_dir, args.projects_root,
                          args.sessions_dir, warned, args.live_dir,
                          tracked={s: n for s, (n, _) in entries.items()})
            key = gated_keys(state)
            # Counted every poll, not only under `--once`: the liveness record
            # reports both halves, and a HELD session is exactly what makes a
            # quiet poll correct rather than suspicious.
            held = sorted(s for s, v in state.items() if v[3] is None)
            if args.once:
                for sid8 in key:
                    label, detail, reason, _ = state[sid8]
                    print(f"GATED {sid8} [{reason}] {label}: {detail}")
                print(f"gated: {len(key)}  held: {len(held)}")
                return 0
            # STABILITY GATE — PER SESSION. A worker mid-turn flips between "has
            # a closer" and "doesn't" as records interleave, and a single poll
            # reads that as a gate opening and closing; a genuine gate holds for
            # minutes. So each session's membership must survive one poll before
            # its transition is announced.
            #
            # ⚠️ **PER SESSION, never whole-set.** The earlier form was
            # `key == pending and key != prev`, which withholds EVERY session's
            # transition for as long as ANY one of them churns: a set alternating
            # `{A}` / `{A,B}` never equals its own previous value, so `prev` never
            # advanced and A's own `CLEARED` was never emitted. That is this
            # file's own defect direction — a gate stuck open — reached through a
            # peer's churn rather than the session's own. The original comment's
            # premise ("a genuine gate holds for minutes; churn does not") is true
            # per session and false for the set. Found in review 2026-10-04.
            #
            # The diff stays PER SESSION too: announcing every member of the new
            # set re-fires an unchanged gate whenever any OTHER session leaves it
            # — measured 2026-10-01, one session dropped out and another,
            # untouched, was re-printed as NEW GATE.
            committed = False
            if pending is not None:
                stable = stable_sessions(prev, pending, key)
                for kind, sid8, label, detail in transitions(sorted(prev), key, state):
                    if sid8 not in stable:
                        continue
                    # A HELD transition is recorded, never announced: a dead
                    # worker is neither progress nor a gate, and reporting it as
                    # either is the silent direction this file exists to close.
                    if kind == "NEW GATE":
                        print(f"NEW GATE  {sid8}  {label[:46]}  ::  {detail}",
                              flush=True)
                        prev.add(sid8)
                    elif kind == "HELD":
                        prev.discard(sid8)
                    else:
                        print(f"CLEARED  {sid8}", flush=True)
                        prev.discard(sid8)
                    log_event(log_path, kind, sid8, label, detail)
                    committed = True
                if committed:
                    # tmp + os.replace, never a bare `open(state_path, "w")`:
                    # that truncates first, so a kill or a serialisation error
                    # part-way through leaves a truncated or zero-byte file —
                    # which the read path above cannot distinguish from a first
                    # run, and answers with a `NEW GATE` burst. Same convention
                    # as `fleet-snapshot.py:56`.
                    os.makedirs(os.path.dirname(state_path), exist_ok=True)
                    tmp_path = state_path + ".tmp"
                    with open(tmp_path, "w") as fh:
                        json.dump(sorted(prev), fh)
                    os.replace(tmp_path, state_path)
            # LIVENESS — one record per poll, written whether or not a gate was
            # found, and whether or not anything was committed.
            #
            # ⚠️ **This is the fix for a silence that was CORRECT and still read as
            # death.** Measured 2026-10-06: an arm emitted nothing for 82 minutes
            # across a live window and the filing could not tell an inert arm from
            # an over-suppressing gate from a genuinely quiet one. It was the third
            # — `busy`/`shell` are HELD, so no `CLEARED` was owed, and the one
            # transition that WAS owed (a genuine clear) was emitted on time. The
            # commit path was never wrong; the arm simply had no way to say "I
            # polled and nothing happened", so the log's newest line was the last
            # transition and its AGE was unreadable as anything but a fault.
            #
            # ⚠️ **`events.jsonl`, never stdout.** Every stdout line is a `Monitor`
            # notification and therefore a full model turn — a per-poll line there
            # would cost 30 turns per 30-minute arm. The event log is the surface
            # the manager is already told to read (`commands/manager-loop.md`
            # step 7), and nothing consumes it programmatically, so a fourth `kind`
            # is additive rather than a schema change. `HELD` is recorded but never
            # announced for the same reason; this is its counterpart on the
            # liveness axis.
            #
            # Read it as the runbook reads the loop's own marker (§ Sweep output):
            # the line's PRESENCE answers *alive or stuck?* and its AGE is what
            # answers it after the arm has died — a dead arm stops writing here,
            # so the newest record's timestamp is the arm's last heartbeat.
            log_event(log_path, "LIVENESS", "", tracked_name,
                      f"{len(key)} gated · {len(held)} held · "
                      f"{'change' if committed else 'no change'}")

            # PARK AGE + RE-POST — this row's two limbs. Both read the same
            # `parked` set, so the age clock and the re-post can never disagree
            # about which sessions are parked.
            #
            # ⚠️ Deliberately NOT gated on the stability gate or on `committed`.
            # That gate withholds a set TRANSITION for a poll; a park is a park
            # whether or not its transition has been committed, and gating either
            # limb on it would delay both by a poll for no reason. It is also why
            # neither limb touches `committed`: the LIVENESS line reports whether
            # the GATED SET changed, and a re-post does not change it.
            parked = parked_keys(state)
            now = time.time()
            advance_park_ages(park_ages, parked, now)
            for sid8, minutes in aged_events(park_ages, now):
                label, reason = "", ""
                if sid8 in state:
                    label, _, reason, _ = state[sid8]
                print(f"AGED  {sid8}  {minutes}m", flush=True)
                log_event(log_path, "AGED", sid8, label,
                          f"{minutes}m parked [{reason}]")
            repost_empty_parks(parked, entries, log_path)
            # tmp + os.replace, same convention and same reason as the gated-set
            # write above: a truncated park file restarts every clock, and the
            # read path cannot tell that from a first run.
            try:
                os.makedirs(os.path.dirname(park_path), exist_ok=True)
                tmp_path = park_path + ".tmp"
                with open(tmp_path, "w") as fh:
                    json.dump(park_ages, fh)
                os.replace(tmp_path, park_path)
            except OSError as exc:
                print(f"WATCH WARN: could not write park-age state: {exc}",
                      file=sys.stderr, flush=True)
            pending = key
        except Exception as exc:  # never let one bad poll kill the watch
            # stderr, never stdout: stdout is the event stream the `Monitor`
            # reads, and a diagnostic sharing that stream is a line a consumer
            # has to learn to ignore — the same reasoning that puts the WATCH
            # WARN lines on stderr.
            print(f"WATCH ERROR: {type(exc).__name__}: {exc}",
                  file=sys.stderr, flush=True)
            # ⚠️ **A FAILED poll is the one case where staying quiet is MOST
            # misleading, so it gets a liveness record too.** The record above
            # exists so a quiet arm is distinguishable from a dead one; an arm
            # that raises on every poll writes no record at all, which is the
            # same blank surface — and this is the path where the reader is most
            # likely to conclude "dead" about a process that is very much alive.
            # stderr is not durable for a `Monitor`-captured arm, so without this
            # the failure survives only in a stream nobody persists.
            log_event(log_path, "LIVENESS", "", tracked_name,
                      f"poll failed: {type(exc).__name__}")
        polls += 1
        if args.max_polls is not None and polls >= args.max_polls:
            return 0
        time.sleep(args.interval)


if __name__ == "__main__":
    sys.exit(main())
