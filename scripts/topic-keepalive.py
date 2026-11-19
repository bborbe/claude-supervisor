#!/usr/bin/env python3
"""The topic tier's keep-alive: is this goal's manager running, and may we resume it?

Two independent signals are read every time, because neither answers the other's question:

  1. **Is a session there?** The goal page's `claude_session_id`, probed with
     `session-liveness.py --check`. `LIVE` (0) / `ABSENT` (1) / `UNKNOWN` (2) /
     `AMBIGUOUS` (3) / `MALFORMED` (4). Only `ABSENT` licenses a resume: `UNKNOWN` and
     `AMBIGUOUS` are **cannot tell**, never dead.
  2. **Is its loop armed?** `manager-liveness.py --state --subject <goal> --branch goal`
     → `OK` / `WORKING` / `LAPSED` / `IDLE` / `STALE` / `STOPPED` / `SLEEPING` / `UNARMED` / `INVALID`. This is the
     signal a liveness probe structurally cannot give — a manager process can be alive with
     its loop unarmed after a missed re-arm. `STOPPED` is a deliberate stand-down and
     `SLEEPING` is a date-gated wait; both are left alone.
     ⚠️ **The record carries a WORK SIGNAL alongside round cadence, and `IDLE` is its
     verdict.** `--state`'s detail carries `work=<seconds>s` — the age of the manager's own
     transcript, resolved from the session id `--arm` records — so `OK` means a round inside
     the limit, `WORKING` means work but no round (alive, not lapsed: never restarted), while
     `IDLE` means armed and gate-watched but neither: the gate tick alone no longer reads
     `OK`. `IDLE` is **not** restart-worthy — the gate is watching and will wake it, so a
     restart spends a full-context round on a manager that is merely idle — and it is not
     `leave` either: the slot exists and is producing nothing, which the operator must see.
     It reads as `jump`, the same as `OK`, so the row renders with its real state word.
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

  jump            a live session with an armed loop (`OK`, `WORKING`, `LAPSED` or `IDLE`) — hand the operator the pane
  open            no recorded id, and the goal carries the flag — `/supervisor:open --goal`
  open-unflagged  no recorded id and no flag — report it held; rule 4 forbids the open
  resume          recorded id, session ABSENT — resume with `/supervisor:open --goal`
  restart         live session, lapsed loop; resume it in place with `/supervisor:restart`
  leave           deliberately stopped — do nothing
  cannot-tell     the probe could not decide; report, never act
  crash-loop      the resume cap is spent; stop restarting and escalate once
  claimed         another writer holds this session's resume claim; this writer skips
  claim-taken     `--claim-resume` won the claim; act, then record with `--record-resume`

⚠️ **`claimed` is the double-writer guard, and it is the only check that covers a RACE.**
The live check refuses a session already running, and the cap refuses a spent retry budget;
neither refuses two writers that both read `ABSENT` (or `cannot-tell`) for one
`claude_session_id` before the first resume registers. The guard is split across the two
roles on purpose: the deciding probe only READS the claim (`claim_state`, never a write), and
the acting step takes it with `--claim-resume`, an `O_EXCL` create, so exactly one writer
wins. `--record-resume` releases it once the session reads LIVE, and `CLAIM_TTL` expires it,
so a resume that never landed cannot block its own retry.

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
import hashlib
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


#: A claim outlives one resume attempt, not two. Past it the claim is stale and must not
#: block a retry — otherwise a resume that never landed becomes a permanent block.
CLAIM_TTL = RESUME_WINDOW  # seconds


def _claim_token(sid: str) -> str:
    """The session id as a filename component, never as a path.

    ⚠️ A `claude_session_id` is page content, so it is untrusted input: `../../settings` would
    otherwise escape the state directory. Anything outside the safe set is hashed, so the name
    stays stable and collision-resistant without ever carrying a separator.
    """
    if re.fullmatch(r"[A-Za-z0-9._-]{1,128}", sid or ""):
        return sid
    return "sha256-" + hashlib.sha256((sid or "").encode("utf-8")).hexdigest()[:16]


def claim_path(vault: str, subject: str, sid: str, branch: str = "goal") -> str:
    """Where a session's resume claim lives. Beside the ledger, so one seam redirects both.

    ⚠️ Derived from `state_path` and not from `resume_path` on purpose: `state_path` is the
    seam the tests redirect, so a claim written during a test can never reach the real store.
    """
    return f"{state_path(vault, subject, branch)}.{_claim_token(sid)}.claim"


def claim_state(vault: str, subject: str, sid: str, now: float,
                branch: str = "goal") -> str | None:
    """Read-only: is a FRESH claim held for this session? The reason, or `None`.

    ⚠️ **Never writes.** A stale claim reports as absent and is left for the actor's
    `--claim-resume` to clear — a deciding probe must not mutate state, or the fleet leg's
    documented "decides, never acts" contract stops being true and a probe could block the
    resume it was merely inspecting.
    """
    p = claim_path(vault, subject, sid, branch)
    try:
        age = now - os.path.getmtime(p)
    except OSError:
        return None
    if age >= CLAIM_TTL:
        return None
    return f"another writer claimed it {int(max(age, 0))}s ago"


def take_claim(vault: str, subject: str, sid: str, now: float,
               branch: str = "goal") -> str | None:
    """Claim the right to resume `sid`. `None` on a win, else the reason it was refused.

    `O_EXCL` is the whole guard: the create is atomic, so of two writers racing on one
    `claude_session_id` exactly one wins. The live check and the resume cap each refuse
    BEFORE this point, and neither covers the race that matters — both legs reading `ABSENT`
    or `cannot-tell` for the same goal manager before the first resume registers.
    """
    p = claim_path(vault, subject, sid, branch)
    try:
        fd = os.open(p, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
    except FileExistsError:
        try:
            age = now - os.path.getmtime(p)
        except OSError:
            return "a claim exists and its age cannot be read"
        if age < CLAIM_TTL:
            return f"another writer claimed it {int(max(age, 0))}s ago"
        # Expired. ⚠️ `rename`, not `unlink`: rename is atomic and exactly one writer can take
        # the stale slot. Unlinking would let two writers both clear it — the second deleting
        # the claim the first had just created — and then both would resume.
        stale = f"{p}.stale.{os.getpid()}.{int(now)}"
        try:
            os.rename(p, stale)
        except OSError:
            return "a concurrent writer reclaimed the expired claim"
        try:
            os.unlink(stale)
        except OSError:
            pass
        try:
            fd = os.open(p, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except FileExistsError:
            return "a concurrent writer re-claimed it"
    os.close(fd)
    return None


def release_claim(vault: str, subject: str, sid: str, branch: str = "goal") -> None:
    """Drop the claim for a session that now reads LIVE — the resume it guarded landed."""
    try:
        os.unlink(claim_path(vault, subject, sid, branch))
    except OSError:
        pass


def flagged_path(vault: str, subject: str, branch: str = "goal") -> str:
    """Where the "this subject was flagged while running" marker lives. Pure — creates nothing.

    Keyed on the vault's real path for the same reason `resume_path` is: one vault is one
    store however it is spelled. `MANAGER_FLAGGED_STATE_DIR` relocates it for tests.
    """
    name = os.path.basename(os.path.realpath(os.path.expanduser(vault)).rstrip(os.sep)) or "vault"
    root = os.environ.get("MANAGER_FLAGGED_STATE_DIR") or "~/.claude/state/manager-flagged"
    return os.path.join(os.path.expanduser(root), name, f"{branch}--{base_slug(subject)}.flagged")


def _remember_flagged(marker: str) -> None:
    """Record that this subject was flagged while running.

    ⚠️ **A write fault warns and does not fail the probe** — the verdict is what the round
    acts on, and a receipt-style fault must not cost it. But it must not be silent either:
    without the marker the subject can never be stood down, so an unwritable state dir
    would turn the whole retire path into a no-op that reports nothing at all.
    """
    try:
        os.makedirs(os.path.dirname(marker), exist_ok=True)
        with open(marker, "w", encoding="utf-8") as fh:
            fh.write(time.strftime("%Y-%m-%dT%H:%M:%S%z") + "\n")
    except OSError as exc:
        print(f"flagged marker not written ({marker}): {exc}", file=sys.stderr)


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
    ap.add_argument("--claim-resume", metavar="SESSION_ID",
                    help="claim the right to resume a session BEFORE acting on it; refuses "
                         "when another writer already holds a fresh claim, so two rounds "
                         "that both decided `resume` cannot both act")
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
        release_claim(vault, a.subject, a.record_resume, a.branch)
        print("recorded")
        return 0

    # The ACTOR's half of the double-writer guard: take the claim, then act. The deciding
    # probe only reads it (`claim_state`), so a read-only check never blocks a real resume.
    if a.claim_resume:
        refusal = take_claim(vault, a.subject, a.claim_resume, now, a.branch)
        if refusal:
            print("claimed")
            print(f"⚠️ {a.subject} — a resume of {a.claim_resume} is already claimed "
                  f"({refusal}); this writer skips")
            return 1
        print("claim-taken")
        print(f"claim taken for {a.claim_resume}; act, then record it with --record-resume")
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
        # ⚠️ Deliberately NOT recorded on `--receipt`. `manager-receipt.py`'s owed-verdict
        # machinery exists for verdicts whose execution is otherwise unprovable, and it
        # refuses a verdict outside `OWING_VERDICTS`; adding `stand-down` there would make
        # every round owe an outcome it is not wired to pay, and an unpaid debt STOPS the
        # round. A stand-down needs none: its execution is proved by the next tick reading
        # the loop `STOPPED` and taking the `loop-stopped` arm.
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
        # The session reads LIVE — any claim a previous resume took has served its purpose,
        # and `--record-resume` releases it. A probe never writes, so nothing is unlinked here.
        if loop in ("STALE", "INVALID"):
            refusal = claim_state(vault, a.subject, sid, now, a.branch)
            if refusal:
                print("claimed")
                print(f"⚠️ {a.subject} — a restart of {sid} is already claimed "
                      f"({refusal}); this writer skips")
                return 1
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
    refusal = claim_state(vault, a.subject, sid, now, a.branch)
    if refusal:
        print("claimed")
        print(f"⚠️ {a.subject} — a resume of {sid} is already claimed ({refusal}); "
              f"this writer skips")
        return 1
    print("resume")
    print(f'session {sid} is ABSENT (cadence {loop}); resume with '
          f'/supervisor:open {opener} "{a.subject}" — attempt {spent + 1} of {RESUME_CAP}')
    if a.receipt:
        record_on_receipt(a.receipt, a.subject, vault, "resume", sid)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
