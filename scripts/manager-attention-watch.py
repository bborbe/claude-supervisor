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
import sys
import time

POLL_SECONDS = 60
SESSIONS_DIR = os.path.expanduser("~/.claude/sessions")
DEFAULT_STATE = os.path.expanduser("~/.claude/state/manager-attention-watch")
DEFAULT_PROJECTS_ROOT = os.path.expanduser("~/.claude/projects")

# The supervisor's heartbeat store — the second liveness source, and the only
# world-readable one that covers a HEADLESS worker. A headless worker is an
# in-process SDK `query()` with no pid, so `SESSIONS_DIR` can never list it and
# every such worker reads UNREGISTERED forever without this. Constants mirror
# `server/heartbeat.mjs:47-48`; keep the two in step.
LIVE_DIR = os.path.expanduser("~/.local/state/claude-supervisor/live")
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


def tracked_ids(tracked_path, tasks_dir):
    """{sid8: task name} for every tracked task carrying an id, read fresh each poll.

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
                out[i[:8]] = name
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


def is_gated(status, body, headless_live=False, stuck=None):
    """The composite predicate. Returns (gated, reason).

    See the module docstring for why it is a union rather than either half.
    `None` status means UNREGISTERED, which is held rather than cleared and is
    reported as such so the caller can record it.

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
    if body is _CLOSER_UNKNOWN:
        # The transcript read reached no text block at all, so "no closer" is
        # unknown rather than observed. HELD, never cleared: this is a failed read,
        # and a failed read must not be representable as an empty result.
        return None, "closer-unknown"
    if status == "idle" and is_ask(body):
        return True, "idle+closer"
    return False, "registry:" + str(status)


def probe(tracked_path, tasks_dir, projects_root, sessions_dir=SESSIONS_DIR,
          warned=None, live_dir=LIVE_DIR):
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
    state = {}
    for sid8, label in tracked_ids(tracked_path, tasks_dir).items():
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
            headless_live=heartbeat_live(sid8, live_dir), stuck=stuck)
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


def transitions(prev, key, state):
    """The transitions one poll emits — `(kind, sid8, label, detail)` records.

    Returned rather than printed so the diff, which is the part that decides
    whether a `CLEARED` is honest, can be tested directly instead of only by
    constructing inputs that avoid it.

    The `HELD` branch is the reason this is three-valued, and it has TWO causes,
    both of which mean "the watcher cannot justify a clear":

    - the session is in `state` with a `None` verdict — it became UNREGISTERED, so
      nothing was answered;
    - the session is absent from `state` entirely — the watcher lost sight of it,
      which is equally not an answer.

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
            out.append(("HELD", sid8, state[sid8][0],
                        "unregistered; no CLEARED emitted"))
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
    log_path = os.path.join(args.state, "events.jsonl")

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
    pending = None          # the previous poll's gated set
    warned = set()
    polls = 0
    while True:
        try:
            state = probe(args.tracked, args.tasks_dir, args.projects_root,
                          args.sessions_dir, warned, args.live_dir)
            key = gated_keys(state)
            if args.once:
                for sid8 in key:
                    label, detail, reason, _ = state[sid8]
                    print(f"GATED {sid8} [{reason}] {label}: {detail}")
                held = sorted(s for s, v in state.items() if v[3] is None)
                print(f"gated: {len(key)}  unregistered(held): {len(held)}")
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
            if pending is not None:
                stable = stable_sessions(prev, pending, key)
                committed = False
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
            pending = key
        except Exception as exc:  # never let one bad poll kill the watch
            # stderr, never stdout: stdout is the event stream the `Monitor`
            # reads, and a diagnostic sharing that stream is a line a consumer
            # has to learn to ignore — the same reasoning that puts the WATCH
            # WARN lines on stderr.
            print(f"WATCH ERROR: {type(exc).__name__}: {exc}",
                  file=sys.stderr, flush=True)
        polls += 1
        if args.max_polls is not None and polls >= args.max_polls:
            return 0
        time.sleep(args.interval)


if __name__ == "__main__":
    sys.exit(main())
