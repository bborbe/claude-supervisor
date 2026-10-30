#!/usr/bin/env python3
"""The fleet tier's worker keep-alive: did this worker's session die, and may we resume it?

The goal tier already resumes an orphan — `agents/goal-drive.md`'s auto-resume gate, on
every tick, capped by `last_auto_resume`. The fleet tier did not: `agents/fleet-drive.md`
dispositioned an absent session as `blocked` with cause `orphan` and the note *"escalate,
never nudge"*, so a worker whose session simply died was reported to the operator and left
dead. Rule 1 is *a crashed or lapsed manager or worker is restarted automatically*
(`docs/session-tiers.md` § The four tiers) — a worker that already owns its task decides
nothing new by being resumed.

⚠️ **This is not `scripts/stuck-heal.py`.** That ladder heals a *stuck* headless worker — one
whose permission channel died while the session lives — and its two rungs (resume headless,
then reopen as a tab) stay exactly as they are. This script covers the other shape: the
session is **gone**. The two are disjoint populations and neither replaces the other.

⚠️ **`scripts/restart-worker.py` is a third, different act** — it kills and resumes a LIVE,
idle session whose loaded code is stale. It refuses `busy-target` and `headless-target`, and
it never applies to a dead session, which has no pid left to kill.

The first line of stdout is the verdict, one token:

  jump          a live session — hand the operator the pane
  resume        the recorded id is ABSENT; resume it with `/supervisor:open --task`
  crash-loop    the resume cap is spent; stop resuming and escalate once
  cannot-tell   the probe could not decide, or the task records no id; report, never act

⚠️ **Never a fresh spawn while a recorded id exists.** `/supervisor:open --task` resumes the
recorded id; a second worker on one task is the double-writer failure.

⚠️ **The cap counts only resumes that took** — `--record-resume` refuses an id the liveness
instrument does not read `LIVE`, so a resume that never landed does not arm the cap against
its own retry. Store: `~/.claude/state/manager-resumes/<vault>/worker--<slug>.json`, beside
the manager caps and deliberately not on the task page.
"""

from __future__ import annotations

import argparse
import importlib.util
import os
import re
import sys
import time
from datetime import date

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TASKS_DIR = "25 Tasks"
RESUME_CAP = 3
RESUME_WINDOW = 30 * 60  # seconds
TERMINAL = {"completed", "aborted"}
#: A parked worker is not a dead one. `hold`/`blocked` is a deliberate pause, `human_review`
#: is waiting on the operator, `todo` is the operator's approval boundary, and `done` is
#: finished — resuming any of them would undo a decision rather than recover from a death.
#: `phase: done` is listed because `agents/goal-drive.md` treats it as terminal while `status`
#: may still read `in_progress`.
PARKED_STATUS = {"hold", "blocked"}
PARKED_PHASE = {"human_review", "todo", "done"}


_KEEPALIVE = None


def _keepalive():
    """`scripts/topic-keepalive.py` loaded as a module — the filename is not importable.

    Cached on purpose: the helpers (`session_verdict`, `state_path`) are the seams a test
    patches, and a fresh module per call would silently ignore every patch.
    """
    global _KEEPALIVE
    if _KEEPALIVE is None:
        path = os.path.join(REPO, "scripts", "topic-keepalive.py")
        spec = importlib.util.spec_from_file_location("topic_keepalive", path)
        _KEEPALIVE = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(_KEEPALIVE)
    return _KEEPALIVE


def state_path(vault: str, task: str) -> str:
    return _keepalive().state_path(vault, task, "worker")


def open_boxes(text: str) -> int:
    """Unchecked `- [ ]` / `- [/]` boxes — a task with none has nothing left to do."""
    n = 0
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("- [ ]") or stripped.startswith("- [/]"):
            n += 1
    return n


def goal_manager_live(fm: str, vault: str, goals_dir: str) -> str:
    """The id of a LIVE goal manager that already owns this task, else "".

    ⚠️ **This is the mutual exclusion with `agents/goal-drive.md`'s orphan gate, and it is a
    mechanism rather than a note on purpose.** That gate resumes the same dead-worker
    population on the goal tier's tick, and both caps record only AFTER a resume the
    instrument confirms LIVE — so a check-then-act race lets two resumes land before either
    cap arms. Deferring here means the fleet leg never races at all: a worker whose goal has a
    live manager is that manager's to resume.
    """
    k = _keepalive()
    # `goals:` is a YAML list, so `scalar` cannot read it — scan the frontmatter for the
    # wikilinks instead, which works for both the list and the single-inline forms.
    for name in re.findall(r"\[\[([^\]|]+)", fm):
        page = os.path.join(vault, goals_dir, name.strip() + ".md")
        try:
            with open(page, encoding="utf-8") as fh:
                gfm = k.frontmatter(fh.read())
        except OSError:
            continue
        sid = k.scalar(gfm, "manager_session_id")
        if sid and k.session_verdict(sid) == "LIVE":
            return sid
    return ""


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--vault", required=True, help="the vault ROOT path")
    ap.add_argument("--task", required=True)
    ap.add_argument("--dir", help="override the task directory (a vault may rename `25 Tasks`; "
                                  "read it from `vault-cli config list`)")
    ap.add_argument("--goals-dir", default="24 Goals",
                    help="where the goal pages live, for the ownership check")
    ap.add_argument("--record-resume", metavar="SESSION_ID",
                    help="record a resume that the liveness instrument confirmed LIVE")
    ap.add_argument("--now", type=float, help="epoch seconds (tests only)")
    a = ap.parse_args(argv)

    k = _keepalive()
    now = a.now if a.now is not None else time.time()
    vault = os.path.abspath(os.path.expanduser(a.vault))
    path = state_path(vault, a.task)

    if a.record_resume:
        if k.session_verdict(a.record_resume) != "LIVE":
            print("cannot-tell")
            print(f"resume {a.record_resume} is not LIVE; the cap is not armed against a retry")
            return 1
        k.record(path, k.load_resumes(path), now)
        print("recorded")
        return 0

    task_file = None
    directory = os.path.join(vault, a.dir or TASKS_DIR)
    try:
        for entry in os.listdir(directory):
            if entry.lower() == a.task.lower() + ".md":
                task_file = os.path.join(directory, entry)
                break
    except OSError:
        task_file = None
    if not task_file:
        print("cannot-tell")
        print(f"no task page at {directory}/{a.task}.md")
        return 1
    with open(task_file, encoding="utf-8") as fh:
        text = fh.read()
    fm = k.frontmatter(text)

    status = k.scalar(fm, "status")
    if status in TERMINAL:
        print("jump")
        print(f"task is {status}; nothing to resume")
        return 0
    phase = k.scalar(fm, "phase")
    if status in PARKED_STATUS or phase in PARKED_PHASE:
        print("jump")
        print(f"task is parked (status {status}, phase {phase}); a park is not a death")
        return 0
    defer = k.scalar(fm, "defer_date")[:10]
    if defer and defer > date.today().isoformat():
        print("jump")
        print(f"task is deferred to {defer}; a snooze is not a death")
        return 0
    if open_boxes(text) == 0:
        print("jump")
        print("no open boxes; the worker has nothing left to drive")
        return 0

    owner = goal_manager_live(fm, vault, a.goals_dir)
    if owner:
        print("jump")
        print(f"goal manager {owner} is live and owns this task; the goal tier resumes it")
        return 0

    sid = k.scalar(fm, "claude_session_id")
    if not sid:
        print("cannot-tell")
        print("no claude_session_id recorded — nothing to resume; report the row")
        return 1

    live = k.session_verdict(sid)
    if live in ("UNKNOWN", "AMBIGUOUS", "MALFORMED"):
        print("cannot-tell")
        print(f"session {sid} → {live}; an undecidable probe is never death")
        return 1
    if live == "LIVE":
        print("jump")
        print(f"session {sid} is live")
        return 0

    # ABSENT — the only verdict that licenses a resume.
    spent = len(k.recent(k.load_resumes(path), now))
    if spent >= RESUME_CAP:
        print("crash-loop")
        print(f"⚠️ WORKER CRASH-LOOP: {a.task} — {spent} resumes in 30 min; stop resuming")
        return 1
    print("resume")
    print(f'session {sid} is ABSENT; resume with /supervisor:open --task "{a.task}" '
          f'— attempt {spent + 1} of {RESUME_CAP}')
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
