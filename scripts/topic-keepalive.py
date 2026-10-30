#!/usr/bin/env python3
"""The topic tier's keep-alive: is this goal's manager running, and may we resume it?

Two independent signals are read every time, because neither answers the other's question:

  1. **Is a session there?** The goal page's `manager_session_id`, probed with
     `session-liveness.py --check`. `LIVE` (0) / `ABSENT` (1) / `UNKNOWN` (2) /
     `AMBIGUOUS` (3) / `MALFORMED` (4). Only `ABSENT` licenses a resume: `UNKNOWN` and
     `AMBIGUOUS` are **cannot tell**, never dead.
  2. **Is its loop armed?** `manager-liveness.py --state --subject <goal> --branch goal`
     → `OK` / `STALE` / `STOPPED` / `UNARMED` / `INVALID`. This is the signal a liveness
     probe structurally cannot give — a manager process can be alive with its loop unarmed
     after a missed re-arm. `STOPPED` is a deliberate stand-down and is left alone.
     ⚠️ `UNARMED` (no record under either name) is **not** restart-worthy and must not be
     folded into `INVALID`: a restart resumes a session that never armed a loop, so it
     cannot create the record. It reads as `jump` for a live session — the lever is
     `/supervisor:goal-loop`, which arms.

The first line of stdout is the verdict, one token:

  jump            a live session with an armed loop — hand the operator the pane
  open            no recorded id, and the operator flagged the goal — `/supervisor:open --goal`
  open-unflagged  no recorded id and no operator flag — report it held; rule 4 forbids the open
  resume          recorded id, session ABSENT — resume with `/supervisor:open --goal`
  restart         live session, lapsed loop; resume it in place with `/supervisor:restart`
  leave           deliberately stopped — do nothing
  cannot-tell     the probe could not decide; report, never act
  crash-loop      the resume cap is spent; stop restarting and escalate once

⚠️ **`open` and `open-unflagged` are one case split in two, and `resume` is not one of
them.** An absent `manager_session_id` means no session was ever created, so the row is an
**open** — which rule 4 makes the operator's act. A recorded id whose session has died is a
**resume**, which continues a subject that already exists. Collapsing the first into the
second is how an unflagged goal manager gets opened by a round that was told not to.

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
PROBE_TIMEOUT = 30  # seconds; a hung liveness probe must not wedge the tick


def frontmatter(text: str) -> str:
    if not text.startswith("---"):
        return ""
    end = text.find("\n---", 3)
    return text[3:end] if end != -1 else ""


def scalar(fm: str, key: str) -> str:
    # ⚠️ `[ \t]*`, never `\s*`: `\s` matches the newline, so an EMPTY value makes the regex
    # swallow the line break and return the NEXT key's line as this key's value. A goal page
    # carrying `manager_session_id:` with no id would then read as having one — and this
    # script would resume a session that does not exist instead of reporting an open.
    m = re.search(rf"^{re.escape(key)}:[ \t]*(.*)$", fm, re.MULTILINE)
    return (m.group(1).strip().strip("\"'") if m else "")


FLAG_TRUTHY = ("true", "yes", "on")


def flagged(fm: str) -> bool:
    """The same predicate as `scripts/topic-spawn.py`'s `flagged` — flag present, provenance
    not read.

    Operator ruling 2026-10-30: *"in the end it should not matter how set the flag … if flag,
    should be open if claude session limit allow it"*. `flag_set_by` is a note, not a gate.

    ⚠️ Duplicated deliberately — both scripts run standalone out of the plugin cache, so
    there is no shared module to import. A split verdict between them would open exactly
    the goal the spawn gate refuses, so `test_topic_keepalive.py` asserts the two agree.
    """
    return scalar(fm, "flag").lower() in FLAG_TRUTHY


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


#: `scripts/session-liveness.py`'s documented contract is the EXIT CODE, not the text:
#: `LIVE` (0) / `ABSENT` (1) / `UNKNOWN` (2) / `AMBIGUOUS` (3) / `MALFORMED` (4). Only `LIVE`
#: prints to stdout — every other verdict goes to stderr — so parsing stdout collapsed them
#: all to `UNKNOWN`.
VERDICT_BY_EXIT = {0: "LIVE", 1: "ABSENT", 2: "UNKNOWN", 3: "AMBIGUOUS", 4: "MALFORMED"}


def session_verdict(sid: str) -> str:
    """`LIVE` / `ABSENT` / `UNKNOWN` / `AMBIGUOUS` / `MALFORMED`, from the instrument.

    ⚠️ **Read the exit code, never the text.** This function used to scan `stdout` for the
    verdict word, which made `ABSENT` unreachable: the instrument prints `ABSENT` to **stderr**
    and exits 1, so every non-`LIVE` answer fell through to `UNKNOWN`. `ABSENT` is the only
    verdict that licenses a resume, so the entire keep-alive — the goal tier that had shipped
    for months, the topic tier and the worker tier — could never resume anything. Measured
    2026-10-30 on a killed manager: stdout `''`, stderr `'ABSENT — <id>'`, rc 1.

    An exit code outside the documented set is `UNKNOWN` — cannot-tell is never death.
    ⚠️ **An unrunnable or hung probe is `UNKNOWN` too.** Without the guard a missing
    interpreter or a stuck child raises out of this function and aborts the whole tick with a
    traceback, instead of answering `cannot-tell` for the one subject being read. Same shape as
    `scripts/crashed-managers.py`'s `session_state()`.
    """
    try:
        probe = subprocess.run(
            [sys.executable, os.path.join(REPO, "scripts", "session-liveness.py"), "--check", sid],
            capture_output=True, text=True, timeout=PROBE_TIMEOUT,
        )
    except (OSError, subprocess.SubprocessError):
        return "UNKNOWN"
    return VERDICT_BY_EXIT.get(probe.returncode, "UNKNOWN")


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
    ap.add_argument("--dir", help="override the subject directory (a vault may rename "
                                  "`24 Goals` / `23 Topics`; read it from `vault-cli config list`)")
    ap.add_argument("--record-resume", metavar="SESSION_ID",
                    help="record a resume that the liveness instrument confirmed LIVE")
    ap.add_argument("--now", type=float, help="epoch seconds (tests only)")
    a = ap.parse_args(argv)

    now = a.now if a.now is not None else time.time()
    vault = os.path.abspath(os.path.expanduser(a.vault))
    opener = "--topic" if a.branch == "topic" else "--goal"
    path = state_path(vault, a.subject, a.branch)

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
    directory = os.path.join(vault, a.dir or DIRS[a.branch])
    try:
        for entry in os.listdir(directory):
            if entry.lower() == a.subject.lower() + ".md":
                goal_page = os.path.join(directory, entry)
                break
    except OSError:
        goal_page = None
    if not goal_page:
        print("cannot-tell")
        print(f"no goal page at {directory}/{a.subject}.md")
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
        # ⚠️ No recorded id is an OPEN, not a resume — a resume continues a session that
        # already has a subject and an id. Rule 4 makes opening the operator's act, so the
        # verdict splits here instead of both arms printing `resume` and letting the agent
        # run `/supervisor:open --goal` for either. Operator ruling 2026-10-30: "we dont
        # want to auto open topic-manager, goal-manager ... but flag are fine".
        if flagged(fm):
            print("open")
            print(f'{a.subject} has no manager_session_id and the operator flagged it — '
                  f'start one with /supervisor:open {opener} "{a.subject}"')
        else:
            print("open-unflagged")
            print(f"{a.subject} has no manager_session_id and does not carry the flag "
                  f"(`flag: true`, whoever set it) — rule 4 forbids opening a manager for "
                  f"it; the fleet round's open leg still opens its workers, so the work "
                  f"moves without one")
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
