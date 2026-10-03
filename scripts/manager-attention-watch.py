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
    for name in names:
        path = os.path.join(tasks_dir, name + ".md")
        try:
            with open(path, encoding="utf-8") as fh:
                txt = fh.read()
        except OSError:
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
    return out


def registry_status(sid8, sessions_dir=SESSIONS_DIR):
    """Registry status for a session id or its 8-char prefix, or None if unlisted.

    Joined on `sessionId`, never on cwd: the `cwd` a spawn response reports is
    unreliable for a launcher-`cd` worker, while this join is not. An 8-char
    prefix is a legal lookup — it is the form the task files carry — and an
    ambiguous prefix is resolved to the first match, which is acceptable here
    because the caller only needs "is this session parked".

    None is a three-way answer — UNREGISTERED — and callers must never read it as
    "no gate"; see the module docstring.
    """
    try:
        names = os.listdir(sessions_dir)
    except OSError:
        return None
    for f in names:
        if not f.endswith(".json"):
            continue
        try:
            with open(os.path.join(sessions_dir, f)) as fh:
                d = json.load(fh)
        except Exception:
            continue
        sid = str(d.get("sessionId") or "")
        if sid.startswith(sid8):
            return d.get("status")
    return None


def last_assistant_text(path):
    """Last assistant text block in the transcript, or ''.

    Reads the tail only. A transcript is unbounded and this runs every poll for
    every tracked worker; the last message is at the end by definition.
    """
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as fh:
            fh.seek(max(0, size - 400_000))
            tail = fh.read().decode("utf-8", "replace")
    except OSError:
        return ""
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
    return text


def closer_body(text):
    """The body of the LAST `👤 You:` line in `text`, or None when there is none.

    Last, not first: a worker may quote an earlier closer while writing about it,
    and only the final line is the panel a manager would read.
    """
    lines = [l.strip() for l in text.splitlines() if l.strip().startswith("👤 You:")]
    if not lines:
        return None
    return lines[-1].split("👤 You:", 1)[1].strip()


def is_ask(body):
    """Whether a closer body is a live ask rather than the `nothing` panel."""
    return bool(body) and not body.lower().startswith("nothing")


def is_gated(status, body):
    """The composite predicate. Returns (gated, reason).

    See the module docstring for why it is a union rather than either half.
    `None` status means UNREGISTERED, which is held rather than cleared and is
    reported as such so the caller can record it.
    """
    if status is None:
        return None, "unregistered"
    if status == "waiting":
        return True, "registry:waiting"
    if status == "idle" and is_ask(body):
        return True, "idle+closer"
    return False, "registry:" + str(status)


def probe(tracked_path, tasks_dir, projects_root, sessions_dir=SESSIONS_DIR,
          warned=None):
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
        text = last_assistant_text(cands[0])
        body = closer_body(text)
        verdict, reason = is_gated(registry_status(sid8, sessions_dir), body)
        state[sid8] = (label, (body or reason)[:150], reason, verdict)
    return state


def gated_keys(state):
    """The session ids the watcher should hold, from a `probe()` result."""
    return sorted(sid8 for sid8, (_, _, _, v) in state.items() if v is True)


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
    args = ap.parse_args(argv)

    state_path = state_path_for(args.state, args.tracked)
    log_path = os.path.join(args.state, "events.jsonl")

    try:
        with open(state_path) as fh:
            prev = json.load(fh)
    except Exception:
        prev = None

    # STABILITY GATE. A worker mid-turn flips between "has a closer" and
    # "doesn't" as records interleave, and a single poll reads that as a gate
    # opening and closing. A genuine gate holds for minutes; churn does not. So
    # require the new state to survive one poll before announcing it.
    pending = None
    warned = set()
    while True:
        try:
            state = probe(args.tracked, args.tasks_dir, args.projects_root,
                          args.sessions_dir, warned)
            key = gated_keys(state)
            if args.once:
                for sid8 in key:
                    label, detail, reason, _ = state[sid8]
                    print(f"GATED {sid8} [{reason}] {label}: {detail}")
                held = sorted(s for s, v in state.items() if v[3] is None)
                print(f"gated: {len(key)}  unregistered(held): {len(held)}")
                return 0
            if key == pending and key != prev:
                # Diff PER SESSION, not per key-set: announcing every member of
                # the new set re-fires an unchanged gate whenever any OTHER
                # session leaves it — measured 2026-10-01, one session dropped
                # out and another, untouched, was re-printed as NEW GATE.
                prevset = set(prev or [])
                for sid8 in key:
                    if sid8 not in prevset:
                        label, detail, reason, _ = state[sid8]
                        print(f"NEW GATE  {sid8}  {label[:46]}  ::  {detail}",
                              flush=True)
                        log_event(log_path, "NEW GATE", sid8, label,
                                  f"{reason} :: {detail}")
                for sid8 in sorted(prevset - set(key)):
                    # Three-valued: a session that left the gated set because it
                    # became UNREGISTERED was not answered, so it must not be
                    # announced as a clear. It is recorded instead — a dead
                    # worker is neither progress nor a gate, and reporting it as
                    # either is the silent direction this file exists to close.
                    if sid8 in state and state[sid8][3] is None:
                        log_event(log_path, "HELD", sid8, state[sid8][0],
                                  "unregistered; no CLEARED emitted")
                        continue
                    print(f"CLEARED  {sid8}", flush=True)
                    log_event(log_path, "CLEARED", sid8, "",
                              "left the gated set")
                prev = key
                os.makedirs(os.path.dirname(state_path), exist_ok=True)
                with open(state_path, "w") as fh:
                    json.dump(prev, fh)
            pending = key
        except Exception as exc:  # never let one bad poll kill the watch
            print(f"WATCH ERROR: {type(exc).__name__}: {exc}", flush=True)
        time.sleep(args.interval)


if __name__ == "__main__":
    sys.exit(main())
