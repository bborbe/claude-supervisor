#!/usr/bin/env python3
"""The fleet tier's keeper: REPORT a lapsed fleet round to the operator. It never acts on it.

`fleet-keepalive.py` answers *is the fleet round still running?* This turns a sustained "no" into
one attention card. A lapse used to be measured, announced once to a log, then suppressed
for every minute after until a human looked at a screen.

⚠️ **Report-only, by operator decision (2026-11-01, card efc771fb).** An earlier version killed
and resumed the Fleet Manager on a stale digest. On 2026-11-01 it killed the manager twice
without being able to resume it, then SIGTERMed a healthy manager it had resumed six minutes
earlier. The trigger cannot tell a dead loop from a slow round: across 10 measured rounds,
healthy arm→digest time was median 18 min, p90 38 min and max 51 min, while the keepalive
reads `stale` past 18 min. Roughly half of all healthy rounds therefore cross the trigger. No
settle window fixes that. One below ~40 min kills healthy managers, and one above it leaves
any restart cap unreachable. So this keeper **holds no kill, resume, re-arm or spawn path at
all**. `scripts/tests/test_fleet_keeper.py` pins that absence with a grep and a positive
control.

⚠️ **The trigger is `fleet-keepalive.py`'s verdict and nothing else.** The word and the exit
code must agree (`ok`/0, `stale`/10, `unknown`/2). There is no `age=`/`limit=` parse here, so no
second staleness rule. What this file adds is a *duration over that verdict*: it reports only
after `stale` has held continuously for `REPORT_AFTER` (40 min, about 58 min of digest age),
which sits above every healthy round measured. A slow round goes `ok` again first and is never
reported.

Verdicts (first line of stdout), one token:

  ok           the round is inside its limit; any lapse in progress has recovered
  stopped      the fleet loop was stood down with /supervisor:stop and is left alone
  pending      stale, but not yet for REPORT_AFTER: possibly a slow but healthy round
  reported     stale for REPORT_AFTER: the operator now has a card for this lapse
  dead         the fleet manager's session is ABSENT: reported
  cannot-tell  a probe could not decide: reported, never treated as fine

**One card per lapse.** The lapse's start time is held in `lapse.json`. A card is posted once,
deduplicated on that start time. The record is cleared only on `ok`, so the next lapse gets a
new card and a continuing one never gets a second.

⚠️ **The keeper's own liveness is a separate instrument.** Each tick rewrites
`~/.claude/state/fleet-keeper/heartbeat`. A keeper that has died leaves that file ageing while
`fleet-keepalive.py` may still read `ok`.

Usage:
  fleet-keeper.py [--ledger PATH] [--now EPOCH]
"""

from __future__ import annotations

import argparse
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
MANAGER_LIVENESS = os.path.join(HERE, "manager-liveness.py")
ATTENTION_ASK = os.path.join(HERE, "attention-ask.py")
MANAGER_STATES = ("OK", "STALE", "STOPPED", "UNARMED", "INVALID")

STATE_DIR = os.path.expanduser(
    os.environ.get("SUPERVISOR_FLEET_KEEPER_DIR") or "~/.claude/state/fleet-keeper"
)
HEARTBEAT = os.path.join(STATE_DIR, "heartbeat")
KEEPER_LOG = os.path.join(STATE_DIR, "keeper.log")
LAPSE = os.path.join(STATE_DIR, "lapse.json")

# `fleet-register.py` writes one record per fleet session here, named by session id.
REGISTER_DIR = os.path.expanduser(
    os.environ.get("SUPERVISOR_WORKER_MANAGER_DIR")
    or os.environ.get("WORKER_MANAGER_STATE_DIR")
    or "~/.claude/state/worker-manager"
)

PROBE_TIMEOUT = 30  # seconds; a hung probe must not wedge the tick

# How long `stale` must hold before it is a lapse rather than a slow round. Above the longest
# healthy round measured on 2026-11-01 (arm→digest max 51 min, p90 38 min): the keepalive turns
# `stale` at 18 min, so 40 more minutes is ~58 min of digest age.
REPORT_AFTER = 40 * 60

VERDICT_BY_EXIT = {0: "LIVE", 1: "ABSENT", 2: "UNKNOWN", 3: "AMBIGUOUS", 4: "MALFORMED"}
KEEPALIVE_EXIT = {"ok": 0, "stale": 10, "unknown": 2}


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
    """Rewrite the keeper's heartbeat. Written FIRST, so a keeper that dies mid-tick is still
    distinguishable from one that never ran."""
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        tmp = HEARTBEAT + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(f"{now if now is not None else time.time():.0f}\n")
        os.replace(tmp, HEARTBEAT)
    except OSError:
        pass


def verdict(ledger=None):
    """`(word, detail)` from `fleet-keepalive.py`, the ONLY staleness input.

    ⚠️ The word and the exit code must AGREE. Any disagreement, an unrunnable keepalive or a
    hung one is `unknown`: a probe that could not answer must not read as a healthy fleet.
    """
    argv = [sys.executable, KEEPALIVE]
    if ledger:
        argv += ["--ledger", ledger]
    try:
        probe = subprocess.run(argv, capture_output=True, text=True, timeout=PROBE_TIMEOUT)
    except (OSError, subprocess.SubprocessError):
        return "unknown", "fleet-keepalive.py could not be run"
    lines = (probe.stdout or "").splitlines()
    word = lines[0].strip() if lines else ""
    if KEEPALIVE_EXIT.get(word) != probe.returncode:
        return "unknown", f"fleet-keepalive.py answered {word!r} with exit {probe.returncode}"
    return word, " · ".join(ln.strip() for ln in lines[1:2])


def stand_down():
    """`'STOPPED'` when the fleet loop was stood down on purpose, `'running'` otherwise, `None`
    when that cannot be read.

    `/supervisor:stop` writes `<slug>.stopped` so a watcher reads the silence as a stop, not a
    lapse (`docs/fleet-surface.md` § Session end). Only the `STOPPED` token is acted on;
    `manager-liveness.py`'s own heartbeat-based `OK`/`STALE` is ignored.
    """
    try:
        probe = subprocess.run(
            [sys.executable, MANAGER_LIVENESS, "--state", "--subject", SUBJECT, "--branch", BRANCH],
            capture_output=True, text=True, timeout=PROBE_TIMEOUT,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    first = (probe.stdout or "").splitlines()
    token = first[0].split("\t")[0].strip() if first else ""
    if probe.returncode != 0 or token not in MANAGER_STATES:
        return None
    return "STOPPED" if token == "STOPPED" else "running"


def liveness(sid):
    """`LIVE` / `ABSENT` / `UNKNOWN` / `AMBIGUOUS` / `MALFORMED`, from the EXIT CODE.

    ⚠️ The exit code, never the text: `session-liveness.py` prints `ABSENT` to stderr and exits 1.
    """
    try:
        probe = subprocess.run(
            [sys.executable, LIVENESS, "--check", sid],
            capture_output=True, text=True, timeout=PROBE_TIMEOUT,
        )
    except (OSError, subprocess.SubprocessError):
        return "UNKNOWN"
    return VERDICT_BY_EXIT.get(probe.returncode, "UNKNOWN")


def fleet_session():
    """The NEWEST registered fleet manager's session id, or `None`.

    Read from the `fleet-register.py` registration, the same record `answer-path-check.py`
    treats as the identity assertion. Nothing deletes a registration, so the newest one is the
    current manager. This keeper never acts on the session, so a stale older record cannot
    cause harm; it only names who the card is about.
    """
    try:
        names = os.listdir(REGISTER_DIR)
    except OSError:
        return None
    newest = None
    for name in names:
        if not name.endswith(".json"):
            continue
        path = os.path.join(REGISTER_DIR, name)
        try:
            with open(path, encoding="utf-8") as fh:
                rec = json.load(fh)
            mtime = os.path.getmtime(path)
        except (OSError, ValueError):
            continue
        if not isinstance(rec, dict):
            continue
        if (rec.get("subject") or "").strip() != SUBJECT or (rec.get("branch") or "").strip() != BRANCH:
            continue
        if newest is None or mtime > newest[0]:
            newest = (mtime, name[: -len(".json")])
    return newest[1] if newest else None


def load_lapse():
    """`{"since": epoch, "reported": item_id-or-None}` for the lapse in progress, or `None`."""
    try:
        with open(LAPSE, encoding="utf-8") as fh:
            rec = json.load(fh)
    except (OSError, ValueError):
        return None
    if not isinstance(rec, dict) or not isinstance(rec.get("since"), (int, float)):
        return None
    return rec


def save_lapse(rec):
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        tmp = LAPSE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(rec, fh)
        os.replace(tmp, LAPSE)
    except OSError:
        pass


def clear_lapse():
    try:
        os.remove(LAPSE)
    except OSError:
        pass


def notify(dedup_key, payload, options, context=""):
    """Post one attention card. Returns its item id, or `None` when the post failed."""
    argv = [sys.executable, ATTENTION_ASK, "post", "--dedup-key", dedup_key, "--payload", payload]
    if context:
        argv += ["--context", context]
    for option in options:
        argv += ["--option", option]
    try:
        out = subprocess.run(argv, capture_output=True, text=True, timeout=PROBE_TIMEOUT)
    except (OSError, subprocess.SubprocessError):
        return None
    for line in (out.stdout or "").splitlines():
        if line.startswith("ITEM_ID:"):
            return line.split(":", 1)[1].strip() or None
    return None


def report(word, sentence, now=None):
    """Print the verdict pair and log it, the shape every branch ends with."""
    print(word)
    print(sentence)
    log(f"{word} — {sentence}", now)


def card(item):
    return f" (card {item})" if item else ""


def main(argv):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--ledger", help="the fleet digest ledger (passed through to fleet-keepalive.py)")
    ap.add_argument("--now", type=float, help="epoch seconds (tests only)")
    a = ap.parse_args(argv)

    now = a.now if a.now is not None else time.time()
    heartbeat(now)

    state, detail = verdict(a.ledger)

    if state == "ok":
        recovered = load_lapse()
        clear_lapse()
        tail = (f"; the lapse since {time.strftime('%H:%M:%SZ', time.gmtime(recovered['since']))} "
                f"recovered") if recovered else ""
        report("ok", (detail or "the fleet round is inside its limit") + tail, now)
        return 0

    if state == "unknown":
        item = notify(
            "fleet-keeper-unknown",
            "The fleet keeper cannot read the digest ledger, so it cannot tell whether the "
            "fleet round is running. It is NOT treating that as healthy.",
            ["acknowledge"], context=detail,
        )
        report("cannot-tell", f"{detail} — reported, never treated as fine{card(item)}", now)
        return 1

    # ---- stale ----
    stood = stand_down()
    if stood == "STOPPED":
        clear_lapse()
        report("stopped", "the fleet loop was stood down with /supervisor:stop — a deliberate "
                          "stop, not a lapse; left alone until it is re-armed", now)
        return 0

    sid = fleet_session()
    if sid and liveness(sid) == "ABSENT":
        item = notify(
            "fleet-keeper-absent",
            f"The fleet manager (session {sid[:8]}) is ABSENT — the session is gone, not merely "
            f"lapsed. Nothing supervises the fleet until one is started. The keeper never "
            f"starts or restarts one itself.",
            ["acknowledge"], context=detail,
        )
        report("dead", f"fleet manager {sid[:8]} is ABSENT — reported{card(item)}", now)
        return 1

    lapse = load_lapse()
    if lapse is None:
        lapse = {"since": now, "reported": None}
        save_lapse(lapse)

    held = now - lapse["since"]
    if held < REPORT_AFTER:
        report("pending",
               f"stale for {int(held)}s of the {REPORT_AFTER}s that make a lapse — possibly a "
               f"slow but healthy round; not reported yet", now)
        return 0

    if lapse.get("reported"):
        report("reported",
               f"lapse since {time.strftime('%H:%M:%SZ', time.gmtime(lapse['since']))} already "
               f"reported{card(lapse['reported'])}; nothing more until it recovers", now)
        return 1

    who = f"session {sid[:8]}" if sid else "no registered fleet manager"
    item = notify(
        f"fleet-keeper-lapse-{int(lapse['since'])}",
        f"The fleet round has been stale for {int(held // 60)} min ({who}). That is past the "
        f"longest healthy round measured, so this is a lapse, not a slow round. The keeper does "
        f"not restart the fleet manager: check its tab and re-arm its loop "
        f"(/supervisor:fleet-loop), or restart it deliberately.",
        ["acknowledge"], context=detail,
    )
    if stood is None:
        item_note = " (stop state unreadable)"
    else:
        item_note = ""
    if item:
        lapse["reported"] = item
        save_lapse(lapse)
    report("reported",
           f"stale for {int(held)}s — lapse reported for {who}{card(item)}{item_note}", now)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
