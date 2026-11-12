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
import importlib.util
import json
import os
import re
import sys
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HERE = os.path.dirname(os.path.abspath(__file__))
DIRS = {"goal": "24 Goals", "topic": "23 Topics"}
RESUME_CAP = 3
RESUME_WINDOW = 30 * 60  # seconds

_OWNER = None


def _owner():
    """Import `scripts/owner.py` — the ONE ownership definition, and both liveness probes.

    ⚠️ **This file used to carry both probes AND the rule that combines them.** Both live in
    `owner.py` now; `main()` collects the two facts through the wrappers below and asks
    `owner.verdict_of()` for the verdict, so the definition has exactly one implementation.
    What stays here is the ACTION ladder — resume, restart, leave, open — which is this tier's
    job and nobody else's.
    """
    global _OWNER
    if _OWNER is None:
        spec = importlib.util.spec_from_file_location(
            "owner", os.path.join(HERE, "owner.py"))
        _OWNER = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(_OWNER)
    return _OWNER


def session_verdict(sid: str) -> str:
    """`LIVE` / `ABSENT` / `UNKNOWN` / `AMBIGUOUS` / `MALFORMED` — `owner.py`'s probe.

    Kept as a module-level name because `worker-keepalive.py` and the suites reach it here; the
    implementation is the resolver's, so the two tiers cannot drift apart.
    """
    return _owner().session_verdict(sid)


def cadence_state_detail(subject: str, branch: str = "goal", vault: str = "") -> tuple:
    """`(state, detail)` from ONE `manager-liveness.py --state` probe — `owner.py`'s probe.

    ⚠️ **`vault` is passed through, and it is what makes `UNARMED` mean what this file says.**
    `--state` answers `UNARMED` only once the subject's page resolves under `--vault`; without
    it the page test runs against THIS process's cwd, so a goal whose page lives in another
    vault reads as a refused, unresolvable name instead of a never-armed loop.
    """
    return _owner().loop_state(subject, branch, vault)


def cadence(subject: str, branch: str = "goal", vault: str = "") -> str:
    """The loop's state alone — `cadence_state_detail`'s first field."""
    return cadence_state_detail(subject, branch, vault)[0]


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


def session_id(fm: str) -> str:
    """The page's recorded session id, or "" for every spelling of "none recorded".

    ⚠️ **A thin delegate to `scripts/owner.py`'s `page_session_id`, which owns the fold.**
    The rule lives there because that module is the single reader of a page's session id;
    a second implementation here is exactly the drift `owner.py` was introduced to end.
    Kept as a name because `worker-keepalive.py` calls it through this module.
    """
    return _owner().page_session_id(fm)


#: The one flag predicate — see `scripts/flag_provenance.py`. Loaded by path because this
#: script runs standalone out of the plugin cache, where `scripts/` is not on `sys.path`.
_FLAG_SPEC = importlib.util.spec_from_file_location(
    "flag_provenance", os.path.join(HERE, "flag_provenance.py"))
flag_provenance = importlib.util.module_from_spec(_FLAG_SPEC)
_FLAG_SPEC.loader.exec_module(flag_provenance)

FLAG_TRUTHY = flag_provenance.FLAG_TRUTHY


def flagged(fm: str, kind: str = "goal") -> bool:
    """Delegates to `flag_provenance.flag_admits()` — the same predicate topic-spawn uses.

    ⚠️ **Provenance is read.** Until the operator's 2026-11-10 ruling (attention card
    `f8cd8b41…`) this read the flag and ignored `flag_set_by`, so a keep-alive would
    restart a manager for a subject an agent had flagged. Both scripts now call the one
    shared predicate, so a split verdict is structurally impossible rather than merely
    asserted by a test.
    """
    return flag_provenance.flag_admits(fm, kind)


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


def flagged_path(vault: str, subject: str, branch: str = "goal") -> str:
    """Where the "this subject was flagged while running" marker lives. Pure — creates nothing.

    Keyed on the vault's real path for the same reason `resume_path` is: one vault is one
    store however it is spelled. `MANAGER_FLAGGED_STATE_DIR` relocates it for tests.
    """
    name = os.path.basename(os.path.realpath(os.path.expanduser(vault)).rstrip(os.sep)) or "vault"
    root = os.environ.get("MANAGER_FLAGGED_STATE_DIR") or "~/.claude/state/manager-flagged"
    return os.path.join(os.path.expanduser(root), name, f"{branch}--{base_slug(subject)}.flagged")


def _remember_flagged(marker: str) -> None:
    try:
        os.makedirs(os.path.dirname(marker), exist_ok=True)
        with open(marker, "w", encoding="utf-8") as fh:
            fh.write(time.strftime("%Y-%m-%dT%H:%M:%S%z") + "\n")
    except OSError:
        pass


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

    # THE OWNERSHIP READ. `claude_session_id` names WHICH session serves this subject — never
    # whether it still owns it. The verdict comes from `scripts/owner.py`, which is the single
    # implementation of `docs/session-tiers.md` § Keep-alive; this file no longer combines the
    # two signals itself. The probes stay reachable here (the wrappers above) because
    # `worker-keepalive.py` and the suites patch them at this module.
    sid = session_id(fm)
    loop, loop_detail = cadence_state_detail(a.subject, a.branch, a.vault)
    live = session_verdict(sid) if sid else _owner().NONE
    verdict, reason = _owner().verdict_of(live, loop)
    o = _owner().Ownership(verdict, reason, live, loop, sid, loop_detail)

    if o.reason == "loop-stopped":
        print("leave")
        print("cadence STOPPED — a deliberate stand-down is not a lapse")
        return 0

    if o.reason == "loop-sleeping":
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

    # ⚠️ **Unflagging IS the stand-down — but only a flag that was SET and then cleared.**
    # Nothing else reads a flag going *off*, so an operator who clears the flag keeps the
    # manager they were retiring — M1's gap in `Session Lifecycle`. The rule cannot be
    # "unflagged plus a session id": measured 2026-11-12 in `private-personal`, of 84 goal
    # pages carrying a `claude_session_id`, **64 carry no flag at all**, so that predicate
    # would stand down 83 of 84 live managers. This is a TRANSITION detector instead — an
    # admitted flag writes a marker, and a later round that finds the page no longer
    # admitted prints the stand-down. A subject never seen flagged is untouched, which is
    # what makes the rule safe against the live corpus.
    #
    # ⚠️ **The marker is NOT consumed.** This script only PRINTS the stop command; the
    # round runs it. Consuming on print would lose the signal for good if that run is
    # skipped, and the subject would never stand down. Keeping it makes the stand-down
    # retry each round — and it terminates on its own, because once `--stop` lands the loop
    # reads `STOPPED` and the `loop-stopped` arm above returns before reaching here.
    marker = flagged_path(a.vault, a.subject, a.branch)
    if flagged(fm):
        _remember_flagged(marker)
    elif sid and os.path.exists(marker):
        print("stand-down")
        print(f'python3 scripts/manager-liveness.py --stop --subject "{a.subject}" '
              f'--branch {a.branch}  (the flag was cleared — the manager is retired and '
              f'never resumed; the page keeps its status)')
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
                  f"(`flag: true` set by the operator) — rule 4 forbids opening a manager for "
                  f"it; the fleet round's open leg still opens its workers, so the work "
                  f"moves without one")
        return 0

    if o.reason == "session-undecidable":
        print("cannot-tell")
        print(f"session {sid} → {o.session}; an undecidable probe is never death")
        return 1

    if o.session == "LIVE":
        # ⚠️ `STALE` restarts even though the resolver calls it an OWNER, and that is not a
        # contradiction: ownership answers "does it hold the subject", the action answers "is
        # its loop still running". A lapsed loop is resumed in place; an `INVALID` record is
        # repaired the same way. `UNARMED`/`UNREADABLE` are `cannot-tell` and jump instead —
        # a session that never armed needs `/supervisor:goal-loop`, and restarting it would
        # kill a healthy manager on no evidence.
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
