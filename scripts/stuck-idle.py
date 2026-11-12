#!/usr/bin/env python3
"""Decide whether a headless worker is IDLE WITH NO WAIT — and never act on it.

The fleet round's stuck signal, read from disk. Three conjuncts, all required:

  1. the transcript has not moved for two consecutive rounds;
  2. no tool call is in flight;
  3. the session declared no wait.

A worker satisfying all three is idle rather than working, and nothing is watching
for it: the fleet's own end-of-life (`reap-idle-headless.py`) closes the process at
15 minutes, and no leg resumed it afterwards, so its task keeps open boxes with no
live owner. That is the gap this probe exists to name.

⚠️ **Every conjunct is a separate read because each has its own false positive.**
Transcript age alone flags a worker mid-analysis that has not written yet — hence
(2). A frozen transcript with a declared wait is a healthy park — hence (3), and
the fleet sweep already files those as `parked-on-watcher`, never as `parked`.
Dropping either turns this into a restarter that kills working sessions.

⚠️ **This script decides nothing about the remedy.** It prints a verdict; the
ladder is `scripts/stuck-heal.py` (`--kind idle-no-wait`) and the caller is the
fleet round, which holds `spawn_agent`. Keeping the two apart is what lets the
ladder be tested as a table and this probe be tested against fixtures.

⚠️ **`unchanged_rounds` is carried in, never counted here.** The previous round's
reading comes from the drive ledger through `--previous-mtime`; a probe that tried
to remember across rounds would need a store of its own, and two stores that both
remember one session disagree the first time either is pruned.

Exit codes: 0 for every verdict (the line carries it), 2 for a usage error. An
unreadable transcript is `unverifiable`, never `not-stuck` — the two are different
facts, and reading one as the other is how a wedged worker stays wedged.
"""
import argparse
import glob
import json
import os
import sys

TAIL_BYTES = 256 * 1024
UNCHANGED_ROUNDS_REQUIRED = 2

# The wait markers the fleet writes into a task's `# Progress` and its closer.
WAIT_MARKERS = ("\U0001f7e1 WAITING", "⏰ Ends:")

VERDICT_STUCK = "stuck"
VERDICT_NOT_STUCK = "not-stuck"
VERDICT_UNVERIFIABLE = "unverifiable"


def resolve_transcript(session_id, projects_root=None):
    """The session's transcript path, or None when no file carries the id.

    ⚠️ **A prefix is not a join.** The id handed in is a full uuid on this path (the
    drive leg reads it off the ledger), and a prefix match here would pick whichever
    sibling the shell expanded first. The exact basename is the only safe match.
    """
    root = projects_root or os.path.expanduser("~/.claude/projects")
    matches = glob.glob(os.path.join(root, "*", f"{session_id}.jsonl"))
    return matches[0] if len(matches) == 1 else None


def transcript_mtime(path):
    """The transcript's mtime in epoch seconds, or None when it cannot be read."""
    try:
        return int(os.path.getmtime(path))
    except OSError:
        return None


def _records(path):
    """The transcript's tail as parsed records, skipping lines that do not parse.

    A JSONL file can end mid-write, and a half-written last line is a normal read
    rather than a corrupt file — dropping it is the honest handling.
    """
    try:
        with open(path, "rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - TAIL_BYTES))
            tail = handle.read()
    except OSError:
        return []
    out = []
    for line in tail.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if isinstance(rec, dict):
            out.append(rec)
    return out


def in_flight_tool(path):
    """The name of the LAST tool call with no matching result, or None.

    ⚠️ **"In flight" means the last call, not any unmatched one.** An interrupted
    call leaves an unmatched `tool_use` behind and the conversation carries on, so
    a scan that reports any of them names a call the worker abandoned — the same
    rule `CLAUDE.md` § Reading a worker states for `agent_status`.
    """
    pending = None  # (id, name) of the last unmatched call
    for rec in _records(path):
        content = rec.get("message", {}).get("content") if isinstance(rec.get("message"), dict) else None
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_use":
                pending = (block.get("id"), block.get("name") or "tool")
            elif block.get("type") == "tool_result" and pending is not None:
                # ⚠️ **Match the id, never "the next result clears it".** Two calls in
                # flight answer out of order, so an id-blind clear reports the second call
                # finished while it is still running — and an idle verdict on a working
                # worker is the one this probe must never produce. A record with no
                # `tool_use_id` predates the field, so it still clears.
                if block.get("tool_use_id") in (None, pending[0]):
                    pending = None
    return pending[1] if pending else None


def declared_wait(task_file, window=40):
    """True when the task's own tail declares a wait the fleet should leave alone.

    The markers are the ones `async-state-closer` writes (`🟡 WAITING` and a
    `⏰ Ends:` line). Only the tail is read: a wait that cleared is superseded by a
    later entry, and scanning the whole `# Progress` would keep reading a park the
    session already left.
    """
    try:
        with open(task_file, encoding="utf-8") as handle:
            lines = handle.read().splitlines()
    except OSError:
        # ⚠️ **None, not False.** An unreadable task file proves nothing about whether a
        # wait was declared, and returning False would let a missing page read as "no
        # wait" — the clean direction — and resume a worker that is legitimately parked.
        return None
    tail = "\n".join(lines[-window:])
    return any(marker in tail for marker in WAIT_MARKERS)


def decide(mtime, previous_mtime, in_flight, wait):
    """The verdict, as a table — every conjunct required.

    `mtime` None means the transcript could not be read, which is `unverifiable`:
    an unreadable probe and a clean one must never render the same.
    """
    if mtime is None or wait is None:
        return VERDICT_UNVERIFIABLE
    unchanged = 1
    if previous_mtime is not None and int(previous_mtime) == int(mtime):
        unchanged = 2
    if unchanged < UNCHANGED_ROUNDS_REQUIRED:
        return VERDICT_NOT_STUCK
    if in_flight:
        return VERDICT_NOT_STUCK
    if wait:
        return VERDICT_NOT_STUCK
    return VERDICT_STUCK


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--session", required=True, help="the worker's full session id")
    ap.add_argument("--task", required=True, help="its task file")
    ap.add_argument("--transcript", default=None, help="override transcript resolution")
    ap.add_argument("--projects-root", default=None, help="override ~/.claude/projects")
    ap.add_argument("--previous-mtime", default=None,
                    help="last round's reading, from the drive ledger; enables the 2-round count")
    args = ap.parse_args(argv)

    sid = args.session.strip()
    if len(sid) < 8:
        # A short id prefix-matches every transcript; refuse rather than guess.
        ap.error("--session must be at least 8 characters of the session id")

    path = args.transcript or resolve_transcript(sid, args.projects_root)
    mtime = transcript_mtime(path) if path else None
    tool = in_flight_tool(path) if path else None
    wait = declared_wait(args.task)

    previous = None
    if args.previous_mtime not in (None, "", "none"):
        try:
            previous = int(args.previous_mtime)
        except ValueError:
            ap.error("--previous-mtime must be an integer epoch or 'none'")

    verdict = decide(mtime, previous, tool, wait)
    unchanged = 2 if (mtime is not None and previous == mtime) else 1
    print(
        f"STUCK {sid} task={os.path.basename(args.task)!r} "
        f"transcript={path or 'none'} mtime={mtime if mtime is not None else 'none'} "
        f"unchanged_rounds={unchanged} in_flight={tool or 'none'} "
        f"wait={'unknown' if wait is None else ('declared' if wait else 'none')} -> {verdict}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
