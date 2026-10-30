#!/usr/bin/env python3
"""The topic tier's keep-alive: is this subject's manager running, and may we resume it?

Serves **both** branches. `--branch goal` (the default) keeps a goal manager alive and reads
`24 Goals`; `--branch topic` keeps a topic manager alive and reads `23 Topics`. The two differ
only in which directory the subject page comes from and which cadence file is read — the
field, the verdicts and the cap are identical, so a second script would have been a copy.

Two independent signals are read every time, because neither answers the other's question:

  1. **Is a session there?** The subject page's `manager_session_id`, probed with
     `session-liveness.py --check`. `LIVE` (0) / `ABSENT` (1) / `UNKNOWN` (2) /
     `AMBIGUOUS` (3) / `MALFORMED` (4). Only `ABSENT` licenses a resume: `UNKNOWN` and
     `AMBIGUOUS` are **cannot tell**, never dead.
  2. **Is its loop armed?** `manager-liveness.py --state --subject <subject> --branch goal|topic`
     → `OK` / `STALE` / `STOPPED` / `UNARMED` / `INVALID`. This is the signal a liveness
     probe structurally cannot give — a manager process can be alive with its loop unarmed
     after a missed re-arm. `STOPPED` is a deliberate stand-down and is left alone.
     ⚠️ `UNARMED` (no record under either name) is **not** restart-worthy and must not be
     folded into `INVALID`: a restart resumes a session that never armed a loop, so it
     cannot create the record. It reads as `jump` for a live session — the lever is
     `/supervisor:goal-loop` (or `/supervisor:topic-loop`), which arms.

The first line of stdout is the verdict, one token:

  jump          a live session with an armed loop — hand the operator the pane
  resume        no session; start one with `/supervisor:open --goal`
  restart       live session, lapsed loop; resume it in place with `/supervisor:restart`
  leave         deliberately stopped — do nothing
  cannot-tell   the probe could not decide; report, never act
  crash-loop    the resume cap is spent; stop restarting and escalate once

⚠️ **Never a fresh spawn while a recorded id exists.** A second manager on one goal is the
double-writer failure `commands/open.md` § Step 2C exists to prevent. The resume is the
only route, and `restart` resumes in place.

⚠️ **The cap counts only resumes that took.** A resume is recorded once
`session-liveness.py --check` shows the resumed id live; a resume that never landed must
not arm the cap against its own retry. `--record-resume <id>` is that write.

Cap: 3 resumes per goal per 30 minutes, recorded in
`~/.claude/state/manager-resumes/<vault>/<slug>.json` — a state store, not the goal page,
so a manager restart survives it and the autocommit daemon is never in the loop.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DIRS = {"goal": "24 Goals", "topic": "23 Topics"}
RESUME_CAP = 3
RESUME_WINDOW = 30 * 60  # seconds


def frontmatter(text: str) -> str:
    if not text.startswith("---"):
        return ""
    end = text.find("\n---", 3)
    return text[3:end] if end != -1 else ""


def scalar(fm: str, key: str) -> str:
    m = re.search(rf"^{re.escape(key)}:\s*(.*)$", fm, re.MULTILINE)
    return (m.group(1).strip().strip("\"'") if m else "")


def base_slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")


def state_path(vault: str, subject: str, branch: str = "goal") -> str:
    name = os.path.basename(vault.rstrip(os.sep)) or "vault"
    directory = os.path.expanduser(os.path.join("~/.claude/state/manager-resumes", name))
    os.makedirs(directory, exist_ok=True)
    return os.path.join(directory, f"{branch}--{base_slug(subject)}.json")


def load_resumes(path: str) -> list[float]:
    try:
        with open(path, encoding="utf-8") as fh:
            doc = json.load(fh)
    except (OSError, ValueError):
        return []
    stamps = doc.get("resumes") if isinstance(doc, dict) else None
    return [float(s) for s in stamps if isinstance(s, (int, float))] if isinstance(stamps, list) else []


def recent(stamps: list[float], now: float) -> list[float]:
    return [s for s in stamps if now - s < RESUME_WINDOW]


def record(path: str, stamps: list[float], now: float) -> None:
    keep = recent(stamps, now) + [now]
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump({"resumes": keep}, fh)
    os.replace(tmp, path)


def session_verdict(sid: str) -> str:
    """`LIVE` / `ABSENT` / `UNKNOWN` / `AMBIGUOUS` / `MALFORMED`, from the instrument."""
    probe = subprocess.run(
        [sys.executable, os.path.join(REPO, "scripts", "session-liveness.py"), "--check", sid],
        capture_output=True, text=True,
    )
    for word in ("LIVE", "ABSENT", "UNKNOWN", "AMBIGUOUS", "MALFORMED"):
        if probe.stdout.startswith(word):
            return word
    return "UNKNOWN"  # an unparseable answer is cannot-tell, never death


def cadence(subject: str, branch: str = "goal") -> str:
    probe = subprocess.run(
        [sys.executable, os.path.join(REPO, "scripts", "manager-liveness.py"),
         "--state", "--subject", subject, "--branch", branch],
        capture_output=True, text=True,
    )
    first = (probe.stdout.splitlines() or [""])[0]
    state = first.split("\t")[0].strip()
    return state if state in ("OK", "STALE", "STOPPED", "UNARMED", "INVALID") else "INVALID"


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--vault", required=True, help="the vault ROOT path")
    ap.add_argument("--subject", "--goal", dest="subject", required=True,
                    help="the goal or topic name; --goal is kept as an alias")
    ap.add_argument("--branch", choices=("goal", "topic"), default="goal",
                    help="which tier's subject page and cadence to read (default: goal)")
    ap.add_argument("--record-resume", metavar="SESSION_ID",
                    help="record a resume that the liveness instrument confirmed LIVE")
    ap.add_argument("--now", type=float, help="epoch seconds (tests only)")
    a = ap.parse_args(argv)

    now = a.now if a.now is not None else time.time()
    vault = os.path.abspath(os.path.expanduser(a.vault))
    path = state_path(vault, a.subject, a.branch)
    opener = "--topic" if a.branch == "topic" else "--goal"

    # A confirmed resume is recorded and nothing else — the caller has already acted.
    if a.record_resume:
        if session_verdict(a.record_resume) != "LIVE":
            print("cannot-tell")
            print(f"resume {a.record_resume} is not LIVE; the cap is not armed against a retry")
            return 1
        record(path, load_resumes(path), now)
        print("recorded")
        return 0

    goal_page = None
    directory = os.path.join(vault, DIRS[a.branch])
    try:
        for entry in os.listdir(directory):
            if entry.lower() == a.subject.lower() + ".md":
                goal_page = os.path.join(directory, entry)
                break
    except OSError:
        goal_page = None
    if not goal_page:
        print("cannot-tell")
        print(f"no {a.branch} page at {directory}/{a.subject}.md")
        return 1
    with open(goal_page, encoding="utf-8") as fh:
        fm = frontmatter(fh.read())
    sid = scalar(fm, "manager_session_id")

    loop = cadence(a.subject, a.branch)

    if loop == "STOPPED":
        print("leave")
        print("cadence STOPPED — a deliberate stand-down is not a lapse")
        return 0

    if not sid:
        print("resume")
        print(f"no manager_session_id recorded — start one with /supervisor:open {opener}")
        return 0

    live = session_verdict(sid)
    if live in ("UNKNOWN", "AMBIGUOUS", "MALFORMED"):
        print("cannot-tell")
        print(f"session {sid} → {live}; an undecidable probe is never death")
        return 1

    if live == "LIVE":
        if loop in ("STALE", "INVALID"):
            print("restart")
            print(f"/supervisor:restart {sid}  (cadence {loop})")
        else:
            print("jump")
            print(f"session {sid} is live and its loop is {loop}")
        return 0

    # ABSENT — the only verdict that licenses a resume.
    spent = len(recent(load_resumes(path), now))
    if spent >= RESUME_CAP:
        print("crash-loop")
        print(f"⚠️ MANAGER CRASH-LOOP: {a.subject} — {spent} resumes in 30 min; stop restarting")
        return 1
    print("resume")
    print(f'session {sid} is ABSENT (cadence {loop}); resume with '
          f'/supervisor:open {opener} "{a.subject}" — attempt {spent + 1} of {RESUME_CAP}')
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
