#!/usr/bin/env python3
"""The topic tier's keep-alive: is this goal's manager running, and may we resume it?

Two independent signals are read every time, because neither answers the other's question:

  1. **Is a session there?** The goal page's `claude_session_id`, probed with
     `session-liveness.py --check`. `LIVE` (0) / `ABSENT` (1) / `UNKNOWN` (2) /
     `AMBIGUOUS` (3) / `MALFORMED` (4). Only `ABSENT` licenses a resume: `UNKNOWN` and
     `AMBIGUOUS` are **cannot tell**, never dead.
  2. **Is its loop armed?** `manager-liveness.py --state --subject <goal> --branch goal`
     → `OK` / `STALE` / `STOPPED` / `SLEEPING` / `UNARMED` / `INVALID`. This is the signal
     a liveness probe structurally cannot give — a manager process can be alive with its
     loop unarmed after a missed re-arm. `STOPPED` is a deliberate stand-down and
     `SLEEPING` is a date-gated wait; both are left alone.
     ⚠️ `UNARMED` (no record under either name) is **not** restart-worthy and must not be
     folded into `INVALID`: a restart resumes a session that never armed a loop, so it
     cannot create the record. It reads as `jump` for a live session — the lever is
     `/supervisor:goal-loop`, which arms.
     ⚠️ **`UNREADABLE` is this file's own word, and it is not one of the five above.** It
     means the probe itself could not answer — it crashed, could not be executed, or hit
     `PROBE_TIMEOUT`. That is a fact about the instrument, not about the subject, and it
     never licenses a restart: it reads as `jump` for a live session, exactly as `UNARMED`
     does. Folding it into `INVALID` would restart a live manager on no evidence.

The first line of stdout is the verdict, one token:

  jump            a live session with an armed loop — hand the operator the pane
  open            no recorded id, and the goal carries the flag — `/supervisor:open --goal`
  open-unflagged  no recorded id and no flag — report it held; rule 4 forbids the open
  resume          recorded id, session ABSENT — resume with `/supervisor:open --goal`
  restart         live session, lapsed loop; resume it in place with `/supervisor:restart`
  leave           deliberately stopped — do nothing
  cannot-tell     the probe could not decide; report, never act
  crash-loop      the resume cap is spent; stop restarting and escalate once

⚠️ **`open` / `open-unflagged` (no recorded id) and `resume` (recorded id, session dead)
must never collapse** — the first is an open, the second a keep-alive resume. Why that
distinction, and why a recorded id is only ever resumed in place, is
`docs/session-tiers.md` § Keep-alive.

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
PROBE_TIMEOUT = 30  # seconds, on BOTH probes below; a hung liveness probe must not wedge the tick


def frontmatter(text: str) -> str:
    if not text.startswith("---"):
        return ""
    end = text.find("\n---", 3)
    return text[3:end] if end != -1 else ""


def scalar(fm: str, key: str) -> str:
    # ⚠️ `[ \t]*`, never `\s*`: `\s` matches the newline, so an EMPTY value makes the regex
    # swallow the line break and return the NEXT key's line as this key's value. A goal page
    # carrying `claude_session_id:` with no id would then read as having one — and this
    # script would resume a session that does not exist instead of reporting an open.
    m = re.search(rf"^{re.escape(key)}:[ \t]*(.*)$", fm, re.MULTILINE)
    return (m.group(1).strip().strip("\"'") if m else "")


#: Every spelling of "no id recorded", compared case-insensitively — the same set
#: `scripts/goals-declaration.py` uses for "declares nothing".
_EMPTY_IDS = frozenset({"", "[]", "null", "~"})


def session_id(fm: str) -> str:
    """The page's recorded session id, or "" for every spelling of "none recorded".

    ⚠️ **`scalar()` alone is the defect this exists for.** It returns the literal text after
    the colon, so `claude_session_id: null` arrives as the four characters `null` —
    non-empty, and a legal `session-liveness.py` argument (that guard admits any lowercase
    prefix by design, so it can accept the 12-char recovery prefix its own AMBIGUOUS verdict
    hands back). The probe therefore answers ABSENT, and ABSENT is the one verdict that
    licenses a resume. Measured 2026-11-08: `worker-keepalive.py` printed `resume` for a row
    whose id was the string `null`.

    ⚠️ **Folded here, never inside `scalar()`.** `scalar()` reads every key on the page —
    `status`, `phase`, `defer_date` — and `defer_date: null` must keep reaching
    `defer_state()` as the unreadable value that parks a row, not as "no defer".
    """
    value = scalar(fm, "claude_session_id").strip()
    return "" if value.lower() in _EMPTY_IDS else value


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


def resume_path(vault: str, subject: str, branch: str = "goal") -> str:
    """Where a subject's resume stamps live. Pure — creates nothing.

    `manager-receipt.py` reads it to prove an `executed` resume outcome, so it must be the
    same path this script writes; `MANAGER_RESUMES_STATE_DIR` relocates the store for tests.

    ⚠️ **`realpath`, so one vault is one store.** A vault reachable both through a symlink and
    by its real path has two spellings; keyed on the spelling, a probe and a `--record-resume`
    given the two names would write and read different files — an honest resume would read
    unproven and the crash-loop count would split in two.
    """
    name = os.path.basename(os.path.realpath(os.path.expanduser(vault)).rstrip(os.sep)) or "vault"
    root = os.environ.get("MANAGER_RESUMES_STATE_DIR") or "~/.claude/state/manager-resumes"
    return os.path.join(os.path.expanduser(root), name, f"{branch}--{base_slug(subject)}.json")


def state_path(vault: str, subject: str, branch: str = "goal") -> str:
    p = resume_path(vault, subject, branch)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    return p


_RECEIPT_MOD = None


def receipt_module():
    """`manager-receipt.py`, imported — the round's receipt is its file, not this one's."""
    global _RECEIPT_MOD
    if _RECEIPT_MOD is None:
        import importlib.util
        p = os.path.join(REPO, "scripts", "manager-receipt.py")
        spec = importlib.util.spec_from_file_location("manager_receipt", p)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _RECEIPT_MOD = mod
    return _RECEIPT_MOD


def record_on_receipt(topic: str, goal: str, vault: str, verdict: str, sid: str) -> None:
    """Put an owed verdict on the topic round's receipt; never let that change the verdict.

    ⚠️ **A failed write warns, it does not fail the probe.** The verdict is what the round
    acts on; turning a receipt fault into `cannot-tell` would stop a resume to protect the
    record of it. The warning goes to stderr so the round sees it, and `--check` still
    reports nothing owed for a verdict it never received — the honest bound, named.
    """
    try:
        rm = receipt_module()
        with open(os.devnull, "w") as sink:
            saved, sys.stdout = sys.stdout, sink
            try:
                rc = rm.record_verdict(topic, goal, vault, verdict, sid)
            finally:
                sys.stdout = saved
    except Exception as e:  # noqa: BLE001 — any fault here must not cost the verdict
        print(f"receipt: could not record {verdict} for {goal}: {e}", file=sys.stderr)
        return
    if rc != 0:
        print(f"receipt: {verdict} for {goal} not recorded on {topic!r} (exit {rc})",
              file=sys.stderr)


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


def load_sessions(path: str) -> list[tuple[float, str]]:
    """`[(stamp, session id), …]` — which id each recorded resume confirmed LIVE.

    Kept beside `resumes`, never inside it: `resumes` is the crash-loop cap's input and stays
    a bare list of floats, so every reader of the cap is unchanged. `manager-receipt.py`
    reads this list to prove an `executed` resume was the VERDICT's session, not merely any
    live id handed to `--record-resume`.
    """
    try:
        with open(path, encoding="utf-8") as fh:
            doc = json.load(fh)
    except (OSError, ValueError):
        return []
    rows = doc.get("sessions") if isinstance(doc, dict) else None
    out = []
    for row in rows if isinstance(rows, list) else []:
        if (isinstance(row, list) and len(row) == 2 and isinstance(row[0], (int, float))
                and isinstance(row[1], str)):
            out.append((float(row[0]), row[1]))
    return out


def record(path: str, stamps: list[float], now: float, sid: str = "") -> None:
    keep = recent(stamps, now) + [now]
    sessions = [[t, s] for t, s in load_sessions(path) if now - t < RESUME_WINDOW]
    if sid:
        sessions.append([now, sid])
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump({"resumes": keep, "sessions": sessions}, fh)
    os.replace(tmp, path)


#: `scripts/session-liveness.py`'s documented contract is the EXIT CODE, not the text:
#: `LIVE` (0) / `ABSENT` (1) / `UNKNOWN` (2) / `AMBIGUOUS` (3) / `MALFORMED` (4). Only `LIVE`
#: prints to stdout — every other verdict goes to stderr — so parsing stdout collapsed them
#: all to `UNKNOWN`.
VERDICT_BY_EXIT = {0: "LIVE", 1: "ABSENT", 2: "UNKNOWN", 3: "AMBIGUOUS", 4: "MALFORMED"}

#: `ABSENT` is believed only when the instrument also says so in words.
#: ⚠️ **Exit 1 is ABSENT's code and every Python traceback's code.** The exit-code read is
#: right, but on its own it turned a crashed instrument — an uncaught exception, a broken
#: install — into a confident `ABSENT`, and `ABSENT` is the ONE verdict that authorises a
#: resume. So a broken probe would resume a manager that is already running: the
#: double-writer this file exists to prevent, arriving through the one door the stdout scan
#: never had. `session-liveness.py`'s `--check` path prints `ABSENT — <message>` to stderr on
#: every genuine ABSENT, so the word corroborates the code before either is acted on.
#: Matched per LINE and on a word boundary, so `ABSENTEE` and a prose mention do not count.
_ABSENT_WORD = re.compile(r"ABSENT\b")


def _absent_corroborated(stderr: str) -> bool:
    return any(_ABSENT_WORD.match(line) for line in (stderr or "").splitlines())


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
    ⚠️ **`ABSENT` additionally needs the instrument's own word on stderr** — see
    `_ABSENT_WORD`. Exit 1 alone is a traceback as readily as a death, and only `ABSENT`
    licenses a resume, so an uncorroborated exit 1 is `UNKNOWN`.
    """
    try:
        probe = subprocess.run(
            [sys.executable, os.path.join(REPO, "scripts", "session-liveness.py"), "--check", sid],
            capture_output=True, text=True, timeout=PROBE_TIMEOUT,
        )
    except (OSError, subprocess.SubprocessError):
        return "UNKNOWN"
    verdict = VERDICT_BY_EXIT.get(probe.returncode, "UNKNOWN")
    if verdict == "ABSENT" and not _absent_corroborated(getattr(probe, "stderr", "")):
        return "UNKNOWN"
    return verdict


#: `cadence()`'s answer when the probe could not run at all. NOT one of `manager-liveness.py`'s
#: five states, because it is not a statement about the subject: `INVALID` means the record
#: exists and cannot be read, which `main` treats as a lapsed loop and restarts a LIVE session
#: for. A crashed or timed-out child is a fact about the INSTRUMENT, so it gets its own word
#: and reads as `jump` — the same reasoning `scripts/crashed-managers.py` records for its own
#: `PROBE_FAILED`, and the same reason `session_verdict` answers `UNKNOWN` rather than `ABSENT`.
PROBE_FAILED = "UNREADABLE"


def cadence_state_detail(subject: str, branch: str = "goal") -> tuple:
    """`(state, detail)` from ONE probe, or `(PROBE_FAILED, "")` when it could not answer.

    ⚠️ **Bounded and guarded exactly like `session_verdict`, because the tick runs both.**
    `PROBE_TIMEOUT`'s claim is that a hung liveness probe cannot wedge the tick; while this
    probe had no `timeout=`, half that claim was false and a stuck `manager-liveness.py`
    blocked the keep-alive forever — worse than the abort the guard prevents, because no
    verdict is ever printed at all.

    ⚠️ **The detail is returned, not dropped.** `--state` prints `<STATE>\\t<slug>\\t<detail>`
    and the detail is the only place a reader learns WHAT a state is waiting for — for a
    sleep it carries the instant. A caller that reported `SLEEPING` without it left the
    operator with a label and no date, which is the thing the state exists to name.
    """
    try:
        probe = subprocess.run(
            [sys.executable, os.path.join(REPO, "scripts", "manager-liveness.py"),
             "--state", "--subject", subject, "--branch", branch],
            capture_output=True, text=True, timeout=PROBE_TIMEOUT,
        )
    except (OSError, subprocess.SubprocessError):
        return PROBE_FAILED, ""
    # `--state` exits 0 for every state it can answer, so a non-zero exit is the child dying.
    if probe.returncode != 0:
        return PROBE_FAILED, ""
    first = (probe.stdout.splitlines() or [""])[0]
    fields = first.split("\t")
    state = fields[0].strip()
    detail = fields[2].strip() if len(fields) > 2 else ""
    if state not in ("OK", "STALE", "STOPPED", "SLEEPING", "UNARMED", "INVALID"):
        return "INVALID", detail
    return state, detail


def cadence(subject: str, branch: str = "goal") -> str:
    """The loop's state alone — `cadence_state_detail`'s first field."""
    return cadence_state_detail(subject, branch)[0]


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
    ap.add_argument("--receipt", metavar="TOPIC",
                    help="the topic round's receipt subject: a `resume` / `restart` verdict "
                         "is also recorded there as owed (`manager-receipt.py "
                         "--record-verdict`), so `--check` flags it until the round records "
                         "an outcome")
    ap.add_argument("--now", type=float, help="epoch seconds (tests only)")
    a = ap.parse_args(argv)
    if a.receipt and a.branch != "goal":
        ap.error("--receipt records a goal manager's verdict; it needs --branch goal")

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
        record(path, load_resumes(path), now, a.record_resume)
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
    # `claude_session_id` is the only field a goal page records its manager in; the
    # retired-key fallback was removed once both vaults were migrated.
    sid = scalar(fm, "claude_session_id")

    loop, loop_detail = cadence_state_detail(a.subject, a.branch)

    if loop == "STOPPED":
        print("leave")
        print("cadence STOPPED — a deliberate stand-down is not a lapse")
        return 0

    if loop == "SLEEPING":
        # A date-gated subject is not re-arming until its named instant, by design. Left
        # alone here, and NOT a lapse: restarting it would spend a full-context round to
        # rediscover that nothing is due yet. ⚠️ The fallback wake needs no branch of its
        # own — once the instant PASSES the marker stops suppressing anything,
        # `manager-liveness.py` reports STALE, and the normal restart path below resumes
        # the session, which arms and clears the marker.
        #
        # ⚠️ **The reason names the INSTANT, not just the state.** `--state`'s detail is
        # `until <ISO> (<n>s remaining)`; dropping it left a reader with a label and no
        # date, so the one fact that makes a sleep actionable was the one the line omitted.
        print("leave")
        print(f"cadence SLEEPING — waiting on its own defer_date, not a lapse"
              + (f" ({loop_detail})" if loop_detail else ""))
        return 0

    if not sid:
        # ⚠️ No recorded id is an OPEN, not a resume — a resume continues a session that
        # already has a subject and an id. Rule 4 makes opening the operator's act, so the
        # verdict splits here instead of both arms printing `resume` and letting the agent
        # run `/supervisor:open --goal` for either. Operator ruling 2026-10-30: "we dont
        # want to auto open topic-manager, goal-manager ... but flag are fine".
        if flagged(fm):
            print("open")
            print(f'{a.subject} has no session id and it carries the flag — '
                  f'start one with /supervisor:open {opener} "{a.subject}"')
        else:
            print("open-unflagged")
            print(f"{a.subject} has no session id and does not carry the flag "
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
            if a.receipt:
                record_on_receipt(a.receipt, a.subject, vault, "restart", sid)
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
    if a.receipt:
        record_on_receipt(a.receipt, a.subject, vault, "resume", sid)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
