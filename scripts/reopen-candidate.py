#!/usr/bin/env python3
"""The drive leg's reopen rule: may this dead session's task be resumed?

The drive leg already finds every disappeared session and then does nothing with the
finding, because its revive path is gated on an observed `CLOSERS` row -- and a headless
worker ends its turn at READY and prints no closer, so most of the fleet reads
`unverifiable` every round. Measured 2026-10-27, rounds 76-80: 79-83 orphans per round,
`revive=0`.

This script is the rule that replaces the closer gate. It answers one question for one
candidate: **is this a session that died with work still open, and may we resume it?**

Four facts, all read, none assumed:

  1. **The session is ABSENT** -- `session-liveness.py --check`, exit 1. ⚠️ Only `ABSENT`
     licenses a resume: `UNKNOWN` (2), `AMBIGUOUS` (3) and `MALFORMED` (4) are
     **cannot-tell**, never dead. Folding them into "not live" turns an I/O error into
     permission to resume, which is the double-writer `session-liveness.py` exists to
     prevent.
  2. **The task is open work** -- `status: in_progress`, at least one `[ ]`/`[/]` box, and
     a `claude_session_id`. A terminal task is not work; a task with zero open boxes has
     nothing to resume.
  3. **The cap is not spent** -- see below.
  4. **A recorded id exists** -- never a fresh spawn while one does.

A dead session cannot hold a live operator gate, which is why the closer gate was wrong:
absence of a closer is *expected* for a dead session, not evidence of a hidden gate. The
2026-09-23 failure that gate was built for (a chat-only `pick` revived past a live
operator decision) was a **live** session with an uncaptured closer, and this rule never
fires for a live session.

Verdict tokens, first line of stdout:

  resume          ABSENT, open work, under the cap -- resume the recorded id
  capped          the cap is spent; surface the row to the operator, do not resume again
  blocked         a refusal was already recorded for this id; never retried
  live            the session is LIVE -- NOT a reopen candidate, and NOT `not-open-work`:
                  step 1 runs this on every parked row, and dropping a live row whose
                  closer is an ask/review/pick would discard a live operator gate
  not-open-work   the task is terminal, has no open boxes, or records no session id
  cannot-tell     a probe could not run, or the task file could not be read

Exit codes: 0 resume / recorded, 1 capped / not-open-work / not-recorded, 2 cannot-tell.

Cap: 3 resumes per task, recorded in
`~/.claude/state/fleet-resumes/<vault>/<task-slug>.json` -- a state store, not the task
page, so a manager restart survives it and the vault autocommit daemon is never in the
loop.

⚠️ **The cap counts only resumes that took.** `--record-resume <id>` re-checks liveness
and writes nothing unless the resumed id reads LIVE; a resume that never landed must not
arm the cap against its own retry.

⚠️ **The counter resets only on real progress.** Each resume records the open-box count
at the moment it fired. A later run whose count is *lower* means a box was ticked -- real
progress -- and the counter starts again at zero. A reopen that ticks no box is not
progress, and eleven planning passes on one row is exactly what an uncapped rule
reproduces.

⚠️ **This does not resume anything.** It decides and records; the resume itself is
`restart-worker.py`'s job, and that script refuses a headless target by design
(`headless-target`) until the headless resume guard lands. This rule is independent of
which resume mechanism runs: it removes the closer gate, it does not supply a resumer.

Usage:
  reopen-candidate.py --task "<task file>"                  decide for the recorded session
  reopen-candidate.py --task "<task file>" --session <id>   decide for an explicit id
  reopen-candidate.py --task "<task file>" --record-resume <id>
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from datetime import datetime, timezone

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(REPO, "scripts")
RESUME_CAP = 3
TERMINAL = {"completed", "aborted"}

FRONTMATTER = re.compile(r"\A---\n(.*?)\n---\n", re.DOTALL)
OPEN_BOX = re.compile(r"^[ \t]*-[ \t]*\[[ /]\]", re.MULTILINE)

EXIT = {"resume": 0, "recorded": 0, "refusal-recorded": 0, "capped": 1, "blocked": 1, "live": 1, "not-open-work": 1, "not-recorded": 1, "cannot-tell": 2}


def frontmatter(text):
    match = FRONTMATTER.match(text)
    return match.group(1) if match else ""


def scalar(block, key):
    match = re.search(rf"^{re.escape(key)}:\s*(.*)$", block, re.MULTILINE)
    return match.group(1).strip().strip("\"'") if match else ""


def open_boxes(text):
    """Open `[ ]` / `[/]` boxes anywhere in the task body."""
    return len(OPEN_BOX.findall(text))


def liveness(session_id):
    """(token, detail) from session-liveness.py. Its EXIT CODE is the contract.

    ⚠️ **Never match the printed token.** The real script writes
    `LIVE — <msg>  heartbeat age <ns>  source <src>` to STDOUT, so the first line never
    equals `LIVE`, and it writes `ABSENT — …` / `UNKNOWN — …` / `AMBIGUOUS — …` /
    `MALFORMED — …` to STDERR, so stdout is empty for every negative verdict. A
    token-equality check therefore reads CANNOT-TELL for LIVE *and* for ABSENT, which
    makes `resume` unreachable — the exact defect this function shipped with once.
    """
    try:
        proc = subprocess.run(
            [sys.executable, os.path.join(SCRIPTS, "session-liveness.py"), "--check", session_id],
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return "CANNOT-TELL", f"liveness probe could not run: {exc}"
    lines = ((proc.stdout or "").strip() or (proc.stderr or "").strip()).splitlines()
    detail = lines[0].strip() if lines else ""
    if proc.returncode == 0:
        return "LIVE", detail or "LIVE"
    if proc.returncode == 1:
        return "ABSENT", detail or "ABSENT"
    return "CANNOT-TELL", detail or f"exit {proc.returncode}, no output"


def state_path(task_path):
    task_path = os.path.abspath(task_path)
    vault = os.path.dirname(os.path.dirname(task_path))  # <vault>/25 Tasks/<file>
    name = os.path.basename(vault.rstrip(os.sep)) or "vault"
    stem = os.path.splitext(os.path.basename(task_path))[0]
    slug = re.sub(r"[^a-z0-9]+", "-", stem.lower()).strip("-")
    return os.path.expanduser(os.path.join("~/.claude/state/fleet-resumes", name, f"{slug}.json"))


def fail(verdict, message):
    """Emit a verdict on STDOUT and stop.

    ⚠️ Never `raise SystemExit("<text>")` for this: that writes the text to stderr and
    exits 1, so the caller's contract -- "the verdict token is the first line of stdout"
    -- silently yields an EMPTY stdout that reads as a crash. Caught by
    `test_unreadable_state_store_is_cannot_tell`.
    """
    print(verdict)
    print(f"  {message}")
    raise SystemExit(EXIT.get(verdict, 2))


def read_state(path):
    """A corrupt store is not an empty one: unreadable must never read as 'no resumes yet'."""
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except FileNotFoundError:
        return {"resumes": []}
    except (OSError, json.JSONDecodeError) as exc:
        fail("cannot-tell", f"state store unreadable: {exc}")
    resumes = data.get("resumes") if isinstance(data, dict) else None
    # ⚠️ Element shape is checked too, not just the container: a list holding a
    # non-object would crash on `.get` below, and an uncaught AttributeError is not
    # a verdict. A malformed store is `cannot-tell`, never a free resume.
    if not isinstance(resumes, list) or not all(isinstance(entry, dict) for entry in resumes):
        fail("cannot-tell", "state store malformed: `resumes` is not a list of objects")
    return data


def write_state(path, state):
    directory = os.path.dirname(path)
    os.makedirs(directory, exist_ok=True)
    # ⚠️ A fixed `<path>.tmp` lets two concurrent `--record-resume` runs collide on the
    # TEMP path, leaving a half-written file that every later read reports as
    # `cannot-tell`. This closes that and nothing more: the read-modify-write is still
    # unlocked, so a lost update remains possible if two writers ever overlap. The drive
    # leg is a single sequential writer, which is why that is documented rather than locked.
    handle_fd, tmp = tempfile.mkstemp(dir=directory, prefix=".fleet-resume-", suffix=".tmp")
    try:
        with os.fdopen(handle_fd, "w", encoding="utf-8") as handle:
            json.dump(state, handle, indent=2, sort_keys=True)
            handle.write("\n")
        os.replace(tmp, path)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def effective_resumes(state, current_boxes):
    """Drop the recorded resumes when a box was ticked since the first of them.

    Real progress is the only reset. A resume that ticked nothing is not progress, and
    counting it as such would let the cap be evaded by resuming forever.
    """
    resumes = state.get("resumes") or []
    if not resumes:
        return []
    first = resumes[0].get("open_boxes")
    if isinstance(first, int) and current_boxes < first:
        return []
    return resumes


def decide(task_path, session_override):
    try:
        with open(task_path, encoding="utf-8") as handle:
            text = handle.read()
    except OSError as exc:
        return "cannot-tell", [f"task unreadable: {exc}"]

    block = frontmatter(text)
    status = scalar(block, "status").lower()
    recorded = scalar(block, "claude_session_id")
    boxes = open_boxes(text)

    if status in TERMINAL:
        return "not-open-work", [f"status: {status}"]
    if status != "in_progress":
        return "not-open-work", [f"status: {status or '(none)'} -- not open work"]
    if boxes < 1:
        return "not-open-work", ["open boxes: 0 -- nothing to resume"]
    if not recorded:
        # ⚠️ An id-less task is NOT a reopen candidate, and `--session` must not make it
        # one: `record()` refuses a task with no recorded id, so returning `resume` here
        # would leave the cap unarmable and the resume unbounded.
        return "not-open-work", ["no claude_session_id recorded -- a fresh spawn, not a reopen"]
    # ⚠️ The override may only CONFIRM the recorded owner, never replace it -- `record()`
    # refuses a foreign id, so an override that disagreed would return `resume` here and
    # `not-recorded` there, and the cap could never arm.
    if session_override and session_override != recorded:
        return "not-open-work", [
            f"session {session_override[:8]} is not this task's recorded owner ({recorded[:8]})"
        ]
    session_id = recorded

    verdict, detail = liveness(session_id)
    # ⚠️ A LIVE session is its own token, never `not-open-work`. Step 1 runs this script
    # on every parked row too, and a parked row IS a live idle session with open boxes —
    # so folding it into `not-open-work` would let a caller drop a live row whose closer
    # is an ask/review/pick, which is exactly the operator gate step 4 exists to protect.
    if verdict == "LIVE":
        return "live", [f"session {session_id[:8]} is LIVE -- step 4's own probes decide this row"]
    if verdict != "ABSENT":
        return "cannot-tell", [f"liveness: {detail} -- only ABSENT licenses a resume"]

    state = read_state(state_path(task_path))
    # ⚠️ **A refusal is terminal for this session id, and without this the leg loops.**
    # `restart-worker.py` resolves its target from the registry and refuses
    # `unknown-session-id` BEFORE its hold/status/kind gates -- and the sessions this
    # rule selects are exactly the registry-absent ones, so that refusal is the normal
    # outcome, not an edge case. A refused resume never arms the cap (`--record-resume`
    # writes only on LIVE), so without this check the row returns `resume` again next
    # round and the leg re-runs the resumer and re-escalates forever.
    refused = state.get("refused") or {}
    if isinstance(refused, dict) and refused.get("session_id") == session_id:
        return "blocked", [
            f"session {session_id[:8]} was already refused: {refused.get('reason') or 'unknown'}",
            "no resumer exists for a registry-absent id -- operator action required",
        ]

    resumes = effective_resumes(state, boxes)
    if len(resumes) >= RESUME_CAP:
        last = resumes[-1].get("at", "unknown")
        first = resumes[0].get("open_boxes")
        # ⚠️ An entry missing an int `open_boxes` must render `unknown`, never `None`:
        # "open boxes: None -> 3" reads as a measurement and is not one.
        delta = f"{first} -> {boxes}" if isinstance(first, int) else f"unknown -> {boxes}"
        return "capped", [
            f"resumes: {len(resumes)}/{RESUME_CAP}",
            f"last reopen: {last}",
            f"open boxes: {delta} since the first reopen",
        ]

    return "resume", [
        f"session {session_id[:8]} ABSENT",
        f"status: in_progress, open boxes: {boxes}",
        f"resumes so far: {len(resumes)}/{RESUME_CAP}",
    ]


def record(task_path, session_id):
    """Record a resume that TOOK. A resume that never landed must not arm the cap.

    ⚠️ The id must be THIS task's recorded owner, and the task must still be open.
    A foreign id would spend this task's budget on another session's resume, and a
    terminal task has nothing left to resume -- both are refused rather than recorded.
    """
    try:
        with open(task_path, encoding="utf-8") as handle:
            text = handle.read()
    except OSError as exc:
        return "cannot-tell", [f"task unreadable: {exc}"]

    block = frontmatter(text)
    status = scalar(block, "status").lower()
    if status in TERMINAL:
        return "not-recorded", [f"status: {status} -- a terminal task accrues no resumes"]
    recorded = scalar(block, "claude_session_id")
    # ⚠️ Refuse when the task records NO id, not only when it records a different one:
    # `decide()` already treats an id-less task as `not-open-work` ("a fresh spawn, not a
    # reopen"), so recording against it would arm a cap on work that can never be resumed.
    if not recorded:
        return "not-recorded", ["no claude_session_id recorded -- nothing to record a resume against"]
    if recorded != session_id:
        return "not-recorded", [
            f"session {session_id[:8]} is not this task's recorded owner ({recorded[:8]}) -- "
            "a foreign id must not spend this task's budget"
        ]

    verdict, detail = liveness(session_id)
    if verdict != "LIVE":
        return "not-recorded", [f"liveness: {detail} -- a resume that did not land is not a resume"]

    boxes = open_boxes(text)

    path = state_path(task_path)
    state = read_state(path)
    resumes = effective_resumes(state, boxes)
    resumes.append(
        {
            "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "session_id": session_id,
            "open_boxes": boxes,
        }
    )
    state["resumes"] = resumes
    try:
        write_state(path, state)
    except OSError as exc:
        return "cannot-tell", [f"state store unwritable: {exc}"]
    return "recorded", [f"resumes: {len(resumes)}/{RESUME_CAP}", f"open boxes: {boxes}"]


def record_refusal(task_path, session_id, reason):
    """Record a refusal so the leg stops re-running a resumer that cannot succeed.

    ⚠️ This is what makes the rule terminate. The resumer refuses a registry-absent id
    at its first gate, and every session this rule selects is registry-absent, so a
    refusal is the expected outcome. Recording it turns the next round's `resume` into
    `blocked` -- surfaced once, with its reason, rather than retried forever.
    """
    path = state_path(task_path)
    state = read_state(path)
    try:
        with open(task_path, encoding="utf-8") as handle:
            block = frontmatter(handle.read())
    except OSError as exc:
        return "cannot-tell", [f"task unreadable: {exc}"]
    recorded = scalar(block, "claude_session_id")
    # ⚠️ Mirror `record()`'s ownership check: the refusal is a single slot, so a foreign
    # id would overwrite the real session's refusal, silently un-block it, and re-arm the
    # retry loop this record exists to stop.
    if recorded and recorded != session_id:
        return "cannot-tell", [
            f"session {session_id[:8]} is not this task's recorded owner ({recorded[:8]}) -- "
            "a foreign id must not overwrite this task's refusal"
        ]
    state["refused"] = {
        "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "session_id": session_id,
        "reason": reason,
    }
    try:
        write_state(path, state)
    except OSError as exc:
        return "cannot-tell", [f"state store unwritable: {exc}"]
    return "refusal-recorded", [f"session {session_id[:8]} refused: {reason}"]


def main():
    parser = argparse.ArgumentParser(description="Decide whether a dead session's open task may be reopened.")
    parser.add_argument("--task", required=True, help="path to the task file")
    parser.add_argument("--session", default="", help="session id to decide for (default: the task's recorded one)")
    parser.add_argument("--record-resume", default="", help="record a resume that took, then exit")
    parser.add_argument(
        "--record-refusal",
        nargs=2,
        metavar=("SESSION_ID", "REASON"),
        default=None,
        help="record that the resumer refused this id, so it is never retried, then exit",
    )
    args = parser.parse_args()

    if args.record_refusal:
        verdict, lines = record_refusal(args.task, *args.record_refusal)
    elif args.record_resume:
        verdict, lines = record(args.task, args.record_resume)
    else:
        verdict, lines = decide(args.task, args.session)

    print(verdict)
    for line in lines:
        print(f"  {line}")
    return EXIT.get(verdict, 2)


if __name__ == "__main__":
    sys.exit(main())
