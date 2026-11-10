#!/usr/bin/env python3
"""The fleet tier's worker keep-alive: did this worker's session die, and may we resume it?

Which workers the fleet round keeps, and which fall to a goal manager's auto-resume gate, is
`docs/session-tiers.md` § Keep-alive. Before this script, `agents/fleet-drive.md` reported a
worker whose session simply died to the operator and left it dead.

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

⚠️ **`/supervisor:open --task` resumes the recorded id in place** (`docs/session-tiers.md`
§ Keep-alive).

⚠️ **The cap counts close→resume cycles, not a time window.** It used to count resumes inside
a 30-minute window, so a loop that closed and resumed every ~40 minutes never tripped it while
a healthy burst of resumes did. The count now resets only on **progress** — an open box ticked,
a new `# Progress` entry, or a new commit on the task's `repo:` branch — so three cycles with
no progress stop the loop however far apart they are.

⚠️ **The cap counts only resumes that took** — `--record-resume` refuses an id the liveness
instrument does not read `LIVE`, so a resume that never landed does not arm the cap against
its own retry. Store: `~/.claude/state/manager-resumes/<vault>/worker--<slug>.json`, beside
the manager caps and deliberately not on the task page.

⚠️ **A crash loop posts ONE attention card** (`dedup_key` `crash-loop:<slug>`), so a re-run of
the fleet leg does not post a second.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HERE = os.path.dirname(os.path.abspath(__file__))


def _load(name, filename):
    """Import a sibling script by path — the filenames carry hyphens."""
    spec = importlib.util.spec_from_file_location(name, os.path.join(HERE, filename))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


#: `defer_date` is an instant, not a date — see `defer-date.py`.
defer_date = _load("defer_date", "defer-date.py")

TASKS_DIR = "25 Tasks"
RESUME_CAP = 3
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


_OWNER = None


def _owner():
    """`scripts/owner.py` loaded as a module — the ONE ownership definition.

    ⚠️ **This file must not decide ownership itself.** Its predecessor, `goal_manager_live()`,
    read a goal page's `claude_session_id` and the session's liveness verdict and nothing else,
    so a goal manager stood down with `/supervisor:stop` — session `LIVE`, loop `STOPPED` —
    still counted as an owner, and the worker it "owned" was kept by nobody: the fleet leg
    deferred to the goal tier, and the goal round is exactly what stopped.
    """
    global _OWNER
    if _OWNER is None:
        spec = importlib.util.spec_from_file_location(
            "owner", os.path.join(REPO, "scripts", "owner.py"))
        _OWNER = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(_OWNER)
    return _OWNER


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


def _progress_section(text: str) -> str:
    """The body of the `# Progress` section, or "" when the page has none."""
    out = []
    inside = False
    for line in text.splitlines():
        if line.startswith("# "):
            inside = line.strip().lower() == "# progress"
            continue
        if inside:
            out.append(line)
    return "\n".join(out).strip()


def progress_signature(text: str) -> tuple:
    """(open boxes, hash of the `# Progress` section) — the file-side progress signal."""
    return (open_boxes(text),
            hashlib.sha256(_progress_section(text).encode("utf-8")).hexdigest())


def load_state(path: str) -> dict:
    """The worker resume store: `{"resumes": [epoch...], "progress": {...}}`.

    ⚠️ A legacy store holds only `{"resumes": [...]}` and is read with `progress: None`, so
    a task already in a crash loop keeps counting until it makes progress — the honest
    reading, not a silent reset.
    """
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {"resumes": [], "progress": None}
    if not isinstance(data, dict):
        return {"resumes": [], "progress": None}
    resumes = data.get("resumes")
    if not isinstance(resumes, list):
        resumes = []
    progress = data.get("progress")
    if not isinstance(progress, dict):
        progress = None
    return {"resumes": [float(x) for x in resumes if isinstance(x, (int, float))],
            "progress": progress}


def save_state(path: str, state: dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(state, fh)
    os.replace(tmp, path)


def repo_head(fm: str) -> str:
    """HEAD sha of the task's `repo:` checkout, or "" when it names none / cannot be read.

    ⚠️ The commit signal requires a **local** checkout path (`~` expanded). A URL, a bare
    repo name, or an absent path returns "" and the commit signal is inert for that task.
    """
    repo = _keepalive().scalar(fm, "repo")
    if not repo:
        return ""
    try:
        return subprocess.check_output(
            ["git", "-C", os.path.expanduser(repo), "rev-parse", "HEAD"],
            stderr=subprocess.DEVNULL, text=True).strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def made_progress(prev: dict, signature: tuple, head: str) -> bool:
    """Did the task advance since the last recorded resume?

    Progress is an open box ticked, a new `# Progress` entry, or a new commit on the task's
    branch. A first record (no `prev`) is NOT progress — it only sets the baseline, so a task
    that has never been resumed does not start with a free reset.
    """
    if not prev:
        return False
    if signature[0] < prev.get("open_boxes", signature[0]):
        return True
    if signature[1] != prev.get("progress_hash", ""):
        return True
    if head and head != prev.get("head_sha", ""):
        return True
    return False


def post_card(task: str, spent: int) -> None:
    """Post ONE attention card for a crash loop (`dedup_key` `crash-loop:<slug>`).

    Best-effort: a missing `attention-ask.py` must not turn a crash-loop verdict into a
    traceback, so the failure is swallowed and the verdict still stands.
    """
    slug = _keepalive().base_slug(task)
    script = os.path.join(HERE, "attention-ask.py")
    if not os.path.exists(script):
        return
    try:
        subprocess.run(
            [sys.executable, script, "post",
             "--dedup-key", f"crash-loop:{slug}",
             "--payload", (f"WORKER CRASH-LOOP: {task} — {spent} resumes with no progress. "
                           f"Resume anyway?"),
             "--option", "keep stopped", "--option", "resume anyway",
             "--recommend", "keep stopped", "--interrupt-class", "pick"],
            check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30)
    except (OSError, subprocess.SubprocessError):
        pass


def read_task(vault: str, dirname: str, task: str):
    """`(path, text)` for the task page, or `(None, None)` when it is absent."""
    directory = os.path.join(vault, dirname or TASKS_DIR)
    try:
        for entry in os.listdir(directory):
            if entry.lower() == task.lower() + ".md":
                path = os.path.join(directory, entry)
                with open(path, encoding="utf-8") as fh:
                    return path, fh.read()
    except OSError:
        pass
    return None, None


def goal_owners(fm: str, vault: str, goals_dir: str) -> list:
    """The `Ownership` of every goal this task declares, resolved by `scripts/owner.py`.

    ⚠️ **This is the mutual exclusion with `agents/goal-drive.md`'s orphan gate, and it is a
    mechanism rather than a note on purpose.** That gate resumes the same dead-worker
    population on the goal tier's tick, and both caps record only AFTER a resume the
    instrument confirms LIVE — so a check-then-act race lets two resumes land before either
    cap arms. Deferring here means the fleet leg never races at all: a worker whose goal has a
    live, ARMED manager is that manager's to resume.

    ⚠️ **An owner is a session `LIVE` with its loop `OK` or `STALE` — never a live session
    alone.** `docs/session-tiers.md` § Keep-alive owns that definition and `scripts/owner.py`
    implements it; this function only collects the pages.

    Returns every goal's answer rather than the first: `main()` needs to see an owner anywhere
    in the list before it may defer, and a cannot-tell anywhere in it before it may hand the
    worker to the fleet leg.
    """
    k = _keepalive()
    o = _owner()
    # `goals:` is a YAML list, so `scalar` cannot read it. Scope the scan to that key's own
    # block: a wikilink in ANY other frontmatter field (a `themes:` or a stray `related:`)
    # would otherwise match a page with a live manager and silently skip a dead worker.
    block = re.search(r"^goals:[ \t]*(.*(?:\n[ \t-].*)*)", fm, re.MULTILINE)
    if not block:
        return []
    found = []
    for name in re.findall(r"\[\[([^\]|]+)", block.group(1)):
        subject = name.strip()
        page = os.path.join(vault, goals_dir, subject + ".md")
        try:
            with open(page, encoding="utf-8") as fh:
                gfm = k.frontmatter(fh.read())
        except OSError:
            continue
        # `claude_session_id` is the only field a goal page records its manager in; the
        # retired-key fallback was removed once both vaults were migrated.
        sid = k.session_id(gfm)
        loop, detail = k.cadence_state_detail(subject, "goal")
        live = k.session_verdict(sid) if sid else o.NONE
        verdict, reason = o.verdict_of(live, loop)
        found.append(o.Ownership(verdict, reason, live, loop, sid, detail))
    return found


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
        task_file, text = read_task(vault, a.dir, a.task)
        state = load_state(path)
        if task_file is None:
            # No page to measure progress against: count the resume with no progress signal.
            state["resumes"] = state["resumes"] + [now]
            save_state(path, state)
            print("recorded")
            return 0
        fm = k.frontmatter(text)
        signature = progress_signature(text)
        head = repo_head(fm)
        if made_progress(state.get("progress"), signature, head):
            state["resumes"] = [now]
        else:
            state["resumes"] = state["resumes"] + [now]
        state["progress"] = {"open_boxes": signature[0], "progress_hash": signature[1],
                             "head_sha": head, "recorded_at": now}
        save_state(path, state)
        print("recorded")
        return 0

    task_file, text = read_task(vault, a.dir, a.task)
    if not task_file:
        directory = os.path.join(vault, a.dir or TASKS_DIR)
        print("cannot-tell")
        print(f"no task page at {directory}/{a.task}.md")
        return 1
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
    # The same instant the resume window is measured at, so an injected `--now`
    # (the suite's clock) moves the defer guard with it.
    state, reason = defer_date.defer_state(
        k.scalar(fm, "defer_date"), datetime.fromtimestamp(now).astimezone()
    )
    if state == "invalid":
        # Not "due now": an unreadable defer parks the row rather than earning a resume.
        print("jump")
        print(f"{reason} — a defer this script cannot read is not an invitation to resume")
        return 0
    if state == "deferred":
        print("jump")
        print(f"task is deferred to {defer_date.display(k.scalar(fm, 'defer_date'))}; "
              "a snooze is not a death")
        return 0
    if open_boxes(text) == 0:
        print("jump")
        print("no open boxes; the worker has nothing left to drive")
        return 0

    owners = goal_owners(fm, vault, a.goals_dir)
    # An OWNER anywhere in the task's goals defers the row: that goal's manager runs the round
    # that resumes it. Checked before the cannot-tell pass, so one resolvable owner is not
    # masked by another goal whose probe failed.
    for own in owners:
        if own.is_owner:
            print("jump")
            print(f"goal manager {own.session_id} is an owner ({own.describe()}); "
                  f"the goal tier resumes it")
            return 0
    # ⚠️ A goal whose ownership could not be decided is NOT "no owner". `not-owner` is what
    # licenses this leg to resume; answering it on a read that failed is the second writer the
    # whole keep-alive exists to prevent — here, a fleet resume of a worker whose goal manager
    # is merely unmeasurable.
    for own in owners:
        if own.is_cannot_tell:
            print("cannot-tell")
            print(f"goal manager {own.session_id or '(none recorded)'} → {own.verdict} "
                  f"({own.reason}: {own.describe()}); an undecidable owner read never hands the "
                  f"worker to the fleet leg")
            return 1

    sid = k.session_id(fm)
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

    # ABSENT — the only verdict that licenses a resume. The count is the close→resume cycles
    # since the last progress, NOT resumes inside a time window: a loop that closes and
    # resumes every ~40 minutes with no progress must still stop. Progress since the last
    # record resets the count here too — a worker that advanced and then died must not be
    # refused for the cycles it ran before it advanced.
    state = load_state(path)
    if state.get("progress") is None:
        # Legacy store (written before the progress marker existed): it has no baseline, so
        # its old resumes cannot be read as a live crash loop. Fall back to the retired
        # 30-minute window; the next --record-resume writes the marker and migrates it.
        spent = len([t for t in state["resumes"] if now - t < k.RESUME_WINDOW])
    elif made_progress(state["progress"], progress_signature(text), repo_head(fm)):
        spent = 0
    else:
        spent = len(state["resumes"])
    if spent >= RESUME_CAP:
        print("crash-loop")
        print(f"⚠️ WORKER CRASH-LOOP: {a.task} — {spent} resumes with no progress; stop resuming")
        post_card(a.task, spent)
        return 1
    print("resume")
    print(f'session {sid} is ABSENT; resume with /supervisor:open --task "{a.task}" '
          f'— attempt {spent + 1} of {RESUME_CAP}')
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
