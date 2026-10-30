#!/usr/bin/env python3
"""The fleet tier's keeper: turn `fleet-keepalive.py`'s verdict into an ACTION.

`fleet-keepalive.py` answers *is the fleet round still running?* and then stops — it prints
`stale` and the words "Resume the fleet manager". This is the leg that resumes. Before it
existed the fleet tier had a detector and no hands: a lapse was measured, announced once, and
then suppressed for every minute after, until a human looked at a screen.

⚠️ **The trigger is `fleet-keepalive.py`'s exit code and nothing else.** `manager-liveness.py
--state` reads `OK` throughout a lapsed round — measured 2026-10-30, `OK` for 2h30m while the
digest sat at `age=9030s limit=1080s`, because the heartbeat is written by the model-free gate
loop and a heartbeat proves the timer works, not that the round did. A second staleness rule
here would be the two-detectors-that-disagree defect this tier exists to remove, so this
script contains no `age=`/`limit=` parse of its own: it shells out to `fleet-keepalive.py` and
branches on `0` / `10` / `2`.

Verdicts (first line of stdout), one token:

  ok            the round is inside its limit — nothing to do
  restarted     a lapse was seen, the fleet manager was resumed, and the resume took
  dead          the fleet manager's session is ABSENT — reported, **never restarted**
  cannot-tell   the ledger or the liveness probe could not decide — reported, never acted on
  crash-loop    the resume cap is spent — escalated once, no further retries
  busy          the fleet manager is mid-turn or otherwise not resumable right now

⚠️ **ABSENT is not a lapse, and is never restarted.** A lapsed loop is a live session whose
model has not run; an ABSENT one is a session that is gone. A dead fleet manager may have died
for a reason a restart repeats, and there is no tier above it to escalate to, so the operator
is told instead. `UNKNOWN` / `AMBIGUOUS` / `MALFORMED` are cannot-tell and are never death.

⚠️ **It restarts, never opens.** There is exactly one fleet manager; a second would be a
double writer over every subject. The restart resumes the recorded session id through
`restart-worker.py --reason lapse` and never spawns a fresh one.

The cap is `topic-keepalive.py`'s, imported rather than re-derived: 3 resumes per subject per
30 minutes, in `~/.claude/state/manager-resumes/<vault>/fleet--fleet-manager.json`. A resume
is recorded only once `session-liveness.py --check` shows the resumed id LIVE — a resume that
never landed must not arm the cap against its own retry.

⚠️ **The keeper's own liveness is a separate instrument.** Each tick rewrites
`~/.claude/state/fleet-keeper/heartbeat`; a keeper that has died leaves that file ageing while
`fleet-keepalive.py` may still read `ok`. Without it, a dead keeper and a healthy fleet are
the same picture.

Usage:
  fleet-keeper.py [--dry-run] [--ledger PATH] [--now EPOCH]
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))

SUBJECT = "Fleet Manager"
BRANCH = "fleet"

KEEPALIVE = os.path.join(HERE, "fleet-keepalive.py")
LIVENESS = os.path.join(HERE, "session-liveness.py")
RESTART = os.path.join(HERE, "restart-worker.py")
ATTENTION_ASK = os.path.join(HERE, "attention-ask.py")

STATE_DIR = os.path.expanduser(
    os.environ.get("SUPERVISOR_FLEET_KEEPER_DIR") or "~/.claude/state/fleet-keeper"
)
HEARTBEAT = os.path.join(STATE_DIR, "heartbeat")
KEEPER_LOG = os.path.join(STATE_DIR, "keeper.log")

# `fleet-register.py` writes one record per fleet session here, named by session id.
REGISTER_DIR = os.path.expanduser(
    os.environ.get("SUPERVISOR_WORKER_MANAGER_DIR")
    or os.environ.get("WORKER_MANAGER_STATE_DIR")
    or "~/.claude/state/worker-manager"
)

PROBE_TIMEOUT = 30  # seconds; a hung probe must not wedge the tick
RESTART_TIMEOUT = 90  # wezterm spawn plus the resume handshake

VERDICT_BY_EXIT = {0: "LIVE", 1: "ABSENT", 2: "UNKNOWN", 3: "AMBIGUOUS", 4: "MALFORMED"}


def load_sibling(filename):
    """Import a sibling script by path — the repo's own convention for sharing code."""
    name = os.path.basename(filename).replace(".py", "").replace("-", "_")
    spec = importlib.util.spec_from_file_location(name, os.path.join(HERE, filename))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def log(line, now=None):
    """Append one line to the keeper's own log. Never raises: a log write must not abort a tick."""
    stamp = time.strftime(
        "%Y-%m-%dT%H:%M:%SZ", time.gmtime(now if now is not None else time.time())
    )
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        with open(KEEPER_LOG, "a", encoding="utf-8") as fh:
            fh.write(f"{stamp}\t{line}\n")
    except OSError:
        pass


def heartbeat(now=None):
    """Rewrite the keeper's heartbeat.

    Written FIRST, before anything can fail, so a keeper that dies mid-tick is still
    distinguishable from one that never ran — the same reason `fleet-digest.py --check`
    writes its own heartbeat rather than letting the caller do it.
    """
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        tmp = HEARTBEAT + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(f"{now if now is not None else time.time():.0f}\n")
        os.replace(tmp, HEARTBEAT)
    except OSError:
        pass


def verdict(ledger=None):
    """`(word, exit_code, detail)` from `fleet-keepalive.py` — the ONLY staleness input.

    An unrunnable or hung keepalive is `unknown`, never `ok`: a probe that could not answer
    must not read as a healthy fleet.
    """
    argv = [sys.executable, KEEPALIVE]
    if ledger:
        argv += ["--ledger", ledger]
    try:
        probe = subprocess.run(argv, capture_output=True, text=True, timeout=PROBE_TIMEOUT)
    except (OSError, subprocess.SubprocessError):
        return "unknown", 2, "fleet-keepalive.py could not be run"
    lines = (probe.stdout or "").splitlines()
    word = lines[0].strip() if lines else ""
    if word not in ("ok", "stale", "unknown"):
        return "unknown", 2, f"fleet-keepalive.py answered {word!r}"
    return word, probe.returncode, " · ".join(ln.strip() for ln in lines[1:2])


def fleet_session():
    """`(session_id, vault)` for the registered fleet manager, `('AMBIGUOUS', matches)`, or None.

    ⚠️ Read from the REGISTRATION, never guessed from a name or a colour. `fleet-register.py`
    writes `{subject, branch, vault}` named by session id; `answer-path-check.py` already
    treats that record as the identity assertion, so the keeper reads the same one rather than
    inventing a second notion of "which session is the fleet manager".

    More than one match is its own answer, never a pick: a keeper that resumes one of two
    fleet managers restarts the wrong half of a double writer.
    """
    try:
        names = sorted(os.listdir(REGISTER_DIR))
    except OSError:
        return None
    matches = []
    for name in names:
        if not name.endswith(".json"):
            continue
        try:
            with open(os.path.join(REGISTER_DIR, name), encoding="utf-8") as fh:
                rec = json.load(fh)
        except (OSError, ValueError):
            continue
        if not isinstance(rec, dict):
            continue
        if (rec.get("subject") or "").strip() != SUBJECT:
            continue
        if (rec.get("branch") or "").strip() != BRANCH:
            continue
        matches.append((name[: -len(".json")], (rec.get("vault") or "").strip()))
    if len(matches) > 1:
        return ("AMBIGUOUS", matches)
    return matches[0] if matches else None


def liveness(sid):
    """`LIVE` / `ABSENT` / `UNKNOWN` / `AMBIGUOUS` / `MALFORMED`, from the EXIT CODE.

    ⚠️ The exit code, never the text: `session-liveness.py` prints `ABSENT` to **stderr** and
    exits 1, so scanning stdout collapsed every non-`LIVE` answer to `UNKNOWN` — the same
    defect `topic-keepalive.py` records at its own `session_verdict()`.
    """
    try:
        probe = subprocess.run(
            [sys.executable, LIVENESS, "--check", sid],
            capture_output=True, text=True, timeout=PROBE_TIMEOUT,
        )
    except (OSError, subprocess.SubprocessError):
        return "UNKNOWN"
    return VERDICT_BY_EXIT.get(probe.returncode, "UNKNOWN")


def restart(sid, dry=False):
    """Resume the fleet manager through `restart-worker.py --reason lapse`.

    ⚠️ `--reason lapse` is load-bearing. `restart-worker.py`'s precondition 4 refuses a restart
    when nothing would load differently — correct for a code-staleness restart, wrong here: a
    lapsed loop changed no file, and the whole point is that the loop stopped, not the code.
    Every OTHER refusal still applies: a held session, a mid-turn kill and a headless target
    are all still refused.
    """
    argv = [sys.executable, RESTART, sid, "--reason", "lapse"]
    if dry:
        argv.append("--dry-run")
    try:
        out = subprocess.run(argv, capture_output=True, text=True, timeout=RESTART_TIMEOUT)
    except (OSError, subprocess.SubprocessError) as err:
        return False, f"restart-worker.py could not be run: {err}"
    detail = " · ".join(ln.strip() for ln in (out.stdout or "").splitlines() if ln.strip())
    if out.returncode != 0:
        first = (out.stdout or "").splitlines()
        return False, f"{first[0].strip() if first else 'refused'} — {detail or (out.stderr or '').strip()}"
    return True, detail


def notify(dedup_key, payload, options, context=""):
    """Post one attention card. Returns its item id, or `None` when the post failed.

    Deduplicated on `dedup_key` so a keeper ticking every minute posts ONE card per condition
    rather than sixty. The escalation has to survive the session that raised it, and a repeat
    is noise the operator learns to ignore.
    """
    argv = [sys.executable, ATTENTION_ASK, "post", "--dedup-key", dedup_key, "--payload", payload]
    if context:
        argv += ["--context", context]
    for option in options:
        argv += ["--option", option]
    try:
        out = subprocess.run(argv, capture_output=True, text=True, timeout=PROBE_TIMEOUT)
    except (OSError, subprocess.SubprocessError):
        return None
    text = (out.stdout or "").strip()
    return text.splitlines()[-1].strip() if text and out.returncode == 0 else None


def report(word, sentence, now=None):
    """Print the verdict pair and log it — the shape every branch below ends with."""
    print(word)
    print(sentence)
    log(f"{word} — {sentence}", now)


def main(argv):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--dry-run", action="store_true",
                    help="decide and report, restart nothing, record no resume")
    ap.add_argument("--ledger", help="the fleet digest ledger (passed through to fleet-keepalive.py)")
    ap.add_argument("--now", type=float, help="epoch seconds (tests only)")
    a = ap.parse_args(argv)

    now = a.now if a.now is not None else time.time()

    # Before anything can fail, so a keeper that dies mid-tick still proves it ran.
    heartbeat(now)

    state, _code, detail = verdict(a.ledger)

    if state == "ok":
        report("ok", detail or "the fleet round is inside its limit", now)
        return 0

    if state == "unknown":
        item = notify(
            "fleet-keeper-unknown",
            "The fleet keeper cannot read the digest ledger, so it cannot tell whether the "
            "fleet round is running. It is NOT treating that as healthy.",
            ["acknowledge"],
            context=detail,
        )
        report("cannot-tell",
               f"{detail} — reported, never treated as fine"
               + (f" (card {item})" if item else ""), now)
        return 1

    # ---- stale: something must act ----
    found = fleet_session()

    if found is None:
        item = notify(
            "fleet-keeper-no-registration",
            "The fleet round is stale and no session is registered as the fleet manager, so "
            "the keeper cannot resume anything. Nothing will restart the fleet until one is.",
            ["acknowledge"],
            context=detail,
        )
        report("cannot-tell",
               "the fleet round is stale but no `Fleet Manager` registration exists — "
               "cannot resume, reported" + (f" (card {item})" if item else ""), now)
        return 1

    if found[0] == "AMBIGUOUS":
        ids = ", ".join(sid[:8] for sid, _ in found[1])
        item = notify(
            "fleet-keeper-ambiguous-registration",
            f"The fleet round is stale and {len(found[1])} sessions claim the `Fleet Manager` "
            f"registration ({ids}). Resuming one of two would be a double writer over every "
            f"subject, so the keeper refuses to pick.",
            ["acknowledge"],
            context=detail,
        )
        report("cannot-tell",
               f"{len(found[1])} fleet registrations ({ids}) — refusing to pick one"
               + (f" (card {item})" if item else ""), now)
        return 1

    sid, vault = found
    live = liveness(sid)

    if live in ("UNKNOWN", "AMBIGUOUS", "MALFORMED"):
        report("cannot-tell", f"session {sid[:8]} → {live}; an undecidable probe is never death", now)
        return 1

    if live == "ABSENT":
        # ⚠️ A dead fleet manager is REPORTED, never restarted: it may have died for a reason a
        # restart repeats, and the fleet has no tier above it to escalate to.
        item = notify(
            "fleet-keeper-absent",
            f"The fleet manager (session {sid[:8]}) is ABSENT — the session is gone, not merely "
            f"lapsed. The keeper does not restart a dead fleet manager: it may have died for a "
            f"reason a restart repeats, and nothing above it can escalate. Start one deliberately.",
            ["acknowledge"],
            context=detail,
        )
        report("dead",
               f"fleet manager {sid[:8]} is ABSENT — reported, NOT restarted"
               + (f" (card {item})" if item else ""), now)
        return 1

    # ---- live + stale: the lapse case ----
    tk = load_sibling("topic-keepalive.py")
    path = tk.state_path(vault or "vault", SUBJECT, BRANCH)
    spent = len(tk.recent(tk.load_resumes(path), now))

    if spent >= tk.RESUME_CAP:
        item = notify(
            "fleet-keeper-crash-loop",
            f"⚠️ FLEET MANAGER CRASH-LOOP: {spent} resumes in the last 30 minutes. The keeper "
            f"has stopped restarting it — a restart is repeating a failure it cannot explain.",
            ["acknowledge"],
            context=detail,
        )
        report("crash-loop",
               f"⚠️ FLEET MANAGER CRASH-LOOP — {spent} resumes in 30 min; stop restarting"
               + (f" (card {item})" if item else ""), now)
        return 1

    ok, note = restart(sid, dry=a.dry_run)
    if not ok:
        report("busy", f"restart refused: {note}", now)
        return 1

    if a.dry_run:
        report("restarted",
               f"dry-run: would resume {sid[:8]} (attempt {spent + 1} of {tk.RESUME_CAP})", now)
        return 0

    # ⚠️ The cap counts only resumes that TOOK. A resume that never landed must not arm the
    # cap against its own retry.
    if liveness(sid) == "LIVE":
        tk.record(path, tk.load_resumes(path), now)
        report("restarted",
               f"resumed fleet manager {sid[:8]} (attempt {spent + 1} of {tk.RESUME_CAP})", now)
        return 0

    report("cannot-tell",
           f"resumed {sid[:8]} but it does not read LIVE yet — the cap is not armed against a retry",
           now)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
