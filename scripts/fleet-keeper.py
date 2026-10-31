#!/usr/bin/env python3
"""The fleet tier's keeper: turn `fleet-keepalive.py`'s verdict into an ACTION.

`fleet-keepalive.py` answers *is the fleet round still running?* and then stops — it prints
`stale` and the words "Resume the fleet manager". This is the leg that resumes. Before it
existed the fleet tier had a detector and no hands: a lapse was measured, announced once, and
then suppressed for every minute after, until a human looked at a screen.

⚠️ **The trigger is `fleet-keepalive.py`'s verdict and nothing else.** `manager-liveness.py
--state` reads `OK` throughout a lapsed round — measured 2026-10-30, `OK` for 2h30m while the
digest sat at `age=9030s limit=1080s`, because the heartbeat is written by the model-free gate
loop and a heartbeat proves the timer works, not that the round did. A second staleness rule
here would be the two-detectors-that-disagree defect this tier exists to remove, so this
script contains no `age=`/`limit=` parse of its own. The verdict word and the exit code must
AGREE (`ok`/0, `stale`/10, `unknown`/2); a disagreement is `unknown`, never a restart.

Verdicts (first line of stdout), one token:

  ok            the round is inside its limit — nothing to do
  settling      a resume ran inside the last HOLD_OFF seconds — the manager is still coming up
  restarted     the fleet manager was resumed AND told to re-arm its loop, and it went busy
  dead          the fleet manager's session is ABSENT — reported, **never restarted**
  cannot-tell   a probe could not decide, or the re-arm could not be confirmed — reported
  crash-loop    the resume cap is spent — escalated once, no further retries
  busy          the fleet manager is mid-turn — not a lapse to act on this tick
  refused       `restart-worker.py` refused for a reason that will not clear by itself

⚠️ **ABSENT is not a lapse, and is never restarted.** A lapsed loop is a live session whose
model has not run; an ABSENT one is a session that is gone. A dead fleet manager may have died
for a reason a restart repeats, and there is no tier above it to escalate to, so the operator
is told instead. `UNKNOWN` / `AMBIGUOUS` / `MALFORMED` are cannot-tell and are never death.

⚠️ **It restarts, never opens.** There is exactly one fleet manager; a second would be a
double writer over every subject. The restart resumes the recorded session id through
`restart-worker.py --reason lapse` and never spawns a fresh one.

⚠️ **A resume is not a recovery until the loop is re-armed.** A killed session's scheduled
wakeups die with its process, and `restart-worker.py` resumes it with `/color <chip>` as its
only prompt — so a resumed fleet manager sits idle, the digest stays stale, and without the
re-arm the keeper would kill it again every tick until the cap tripped, reporting `crash-loop`
on a healthy manager. After the resume this sends `/supervisor:fleet-loop` into the pane it
just spawned, using `commands/open.md` § 3.2's recipe: wait for `Session color set to:`, send
the text, then submit with a separate `\\r`. That licence is narrow — **only the pane this tick
spawned, while it is still idle**; it is never a way to drive any other session.

⚠️ **The cap arms when the kill-and-resume RAN, not when it is proven to have landed.**
`topic-keepalive.py` counts only resumes that took, and that is right for it: it resumes a
session that is already gone, so a failed resume kills nothing. Here every resume kills first,
so a resume that does not land must count — otherwise the keeper kills a live manager every
minute with no bound. The cap's bound and its store are still `topic-keepalive.py`'s,
imported rather than re-derived: 3 resumes per subject per 30 minutes, in
`~/.claude/state/manager-resumes/<vault>/fleet--fleet-manager.json`. A HOLD_OFF window after
each resume keeps a manager that is still booting from being read as a fresh lapse.

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
import re
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

# The re-arm `restart-worker.py` names for the fleet tier, and the wait around delivering it.
REARM_COMMAND = "/supervisor:fleet-loop"
READY_MARKER = "Session color set to:"  # `commands/open.md` § 3.2: confirm before sending
READY_TIMEOUT = 90  # a resumed session replays its transcript before the colour runs
WORKING_TIMEOUT = 30  # after the submit, the session must leave `idle`
POLL_INTERVAL = 2

# After a resume ran, the next ticks must not read a manager that is still booting as a fresh
# lapse. `restart-worker.py` already refuses a BUSY target, so this only has to cover the gap
# between the spawn and the session's first turn — short enough that the 3-per-30-minute cap
# stays reachable, which a window near the round interval would not allow.
HOLD_OFF = 5 * 60

VERDICT_BY_EXIT = {0: "LIVE", 1: "ABSENT", 2: "UNKNOWN", 3: "AMBIGUOUS", 4: "MALFORMED"}
KEEPALIVE_EXIT = {"ok": 0, "stale": 10, "unknown": 2}
PANE = re.compile(r"\(pane (\d+)\)")
KILL_TOKEN = "killing-pid"  # `restart-worker.py` prints it immediately before `os.kill`

# The marker must sit in the pane's LAST lines. A resumed session renders its old transcript,
# which already carries this line from every earlier open, so a whole-screen match can pass
# before the new `/color` has run.
TAIL_LINES = 15


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
    """`(word, detail)` from `fleet-keepalive.py` — the ONLY staleness input.

    ⚠️ The word and the exit code must AGREE. Reading the word alone let a `stale` printed
    beside exit 2 drive a restart, and reading the code alone would lose the `unknown` /
    `stale` split the code is meant to carry. Any disagreement, an unrunnable keepalive, or a
    hung one is `unknown` — a probe that could not answer must not read as a healthy fleet.
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


def fleet_session():
    """`(session_id, vault)` for the registered fleet manager, `('AMBIGUOUS', matches)`, or None.

    ⚠️ Read from the REGISTRATION, never guessed from a name or a colour. `fleet-register.py`
    writes `{subject, branch, vault}` named by session id; `answer-path-check.py` already
    treats that record as the identity assertion, so the keeper reads the same one rather than
    inventing a second notion of "which session is the fleet manager".

    ⚠️ **Nothing deletes a registration**, so every fleet manager ever replaced leaves one
    behind. Read raw, that turns the first replacement into a permanent `AMBIGUOUS` and the
    keeper never acts again. So OLDER claimants whose session reads `ABSENT` are dropped — a
    gone session cannot be the writer a second resume would collide with. Only a dead claimant
    is dropped: `UNKNOWN` keeps its place, because cannot-tell is never death.

    ⚠️ **The NEWEST registration is never routed around.** If it reads `ABSENT`, it is
    returned so the `ABSENT` branch reports the death. Falling back to an older claimant that
    still reads `LIVE` would resurrect a replaced manager in some forgotten tab and leave the
    real one's death unreported.
    """
    try:
        names = os.listdir(REGISTER_DIR)
    except OSError:
        return None
    matches = []
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
        if (rec.get("subject") or "").strip() != SUBJECT:
            continue
        if (rec.get("branch") or "").strip() != BRANCH:
            continue
        matches.append((mtime, name[: -len(".json")], (rec.get("vault") or "").strip()))
    if not matches:
        return None
    matches.sort(reverse=True)  # newest registration first
    newest, older = matches[0], matches[1:]
    if not older or liveness(newest[1]) == "ABSENT":
        return newest[1], newest[2]
    rivals = [m for m in older if liveness(m[1]) != "ABSENT"]
    if not rivals:
        return newest[1], newest[2]
    return ("AMBIGUOUS", [(sid, vault) for _, sid, vault in [newest, *rivals]])


def restart(sid, dry=False):
    """`(outcome, note, pane)` from `restart-worker.py --reason lapse`.

    `outcome` is `ran` (killed and resumed), `busy` (the session is mid-turn — transient),
    `refused` (a refusal that will not clear by itself), or `failed` (the script could not run,
    hung, or failed AFTER its kill — the session may already be gone).

    ⚠️ `--reason lapse` is load-bearing. `restart-worker.py`'s precondition 4 refuses a restart
    when nothing would load differently — correct for a code-staleness restart, wrong here: a
    lapsed loop changed no file, and the whole point is that the loop stopped, not the code.
    Every OTHER refusal still applies.
    """
    argv = [sys.executable, RESTART, sid, "--reason", "lapse"]
    if dry:
        argv.append("--dry-run")
    try:
        out = subprocess.run(argv, capture_output=True, text=True, timeout=RESTART_TIMEOUT)
    except (OSError, subprocess.SubprocessError) as err:
        # A timeout may land after `os.kill`, so this is `failed`, never `busy`.
        return "failed", f"restart-worker.py did not complete: {err}", None
    lines = [ln.strip() for ln in (out.stdout or "").splitlines() if ln.strip()]
    detail = " · ".join(lines) or (out.stderr or "").strip()
    if out.returncode == 0:
        found = PANE.search(out.stdout or "")
        return "ran", detail, (found.group(1) if found else None)
    # ⚠️ Classified by `restart-worker.py`'s `killing-pid` token, never by the prose of the
    # refusal: a refusal printed after the header has the header as its first line, so a
    # first-line read cannot tell a pre-kill refusal from a post-kill failure.
    if any(ln.startswith(KILL_TOKEN) for ln in lines):
        return "failed", detail, None
    if "busy-target" in lines:
        return "busy", detail, None
    return "refused", detail, None


def wezterm(args):
    """`(rc, stdout)` from one `wezterm cli` call. Never raises."""
    try:
        out = subprocess.run(
            ["wezterm", "cli", *args], capture_output=True, text=True, timeout=PROBE_TIMEOUT
        )
    except (OSError, subprocess.SubprocessError):
        return 1, ""
    return out.returncode, out.stdout or ""


def registry_records(sid):
    """Every registry record for `sid` as a list of dicts, or `None` when unreadable.

    Read through `restart-worker.py`'s own reader — the list accessor, so two records for one
    session id stay two (the dict accessor would collapse them, and a kill + resume briefly
    leaves exactly that: the killed process's record beside the new one).
    """
    try:
        entries = load_sibling("restart-worker.py").registry_entries()
    except Exception:
        return None
    if entries is None:
        return None
    return [rec for _, rec in entries if isinstance(rec, dict) and rec.get("sessionId") == sid]


def registry_pid(sid):
    """The pid the registry holds for `sid` right now, or `None` when there is not exactly one."""
    recs = registry_records(sid) or []
    pids = [r.get("pid") for r in recs if isinstance(r.get("pid"), int)]
    return pids[0] if len(pids) == 1 else None


def new_status(sid, old_pid):
    """The status of the RESUMED process — the record for `sid` whose pid is not `old_pid`.

    `''` when there is no such record yet, or more than one. ⚠️ Keyed on the pid, never on the
    session id alone: the killed process's record can still be on disk reading `busy`, and a
    status read without the pid filter would take that as the new session working.
    """
    recs = [r for r in (registry_records(sid) or [])
            if isinstance(r.get("pid"), int) and r.get("pid") != old_pid]
    if len(recs) != 1:
        return ""
    return (recs[0].get("status") or "").strip().lower()


def tail(text, n=TAIL_LINES):
    return "\n".join([ln for ln in text.splitlines() if ln.strip()][-n:])


def rearm(pane, sid, old_pid, sleep=time.sleep, clock=time.monotonic):
    """Re-arm the resumed fleet manager's loop. Returns `(ok, note)`.

    `commands/open.md` § 3.2's recipe, applied to a pane THIS tick just spawned:
      1. wait — bounded — until the session is READY: `Session color set to:` in the pane's
         last lines AND the new process (pid ≠ the killed one) reading `idle`. Either alone is
         not enough: the marker can be old transcript a resume re-renders, and `idle` can be
         read before the `/color` turn has started;
      2. send the command text, then submit with a separate `\\r` — a trailing `\\r` on the
         text is not enough;
      3. confirm the NEW process went `busy` after the submit. Because step 1 already saw it
         `idle` after `/color`, that `busy` is the re-arm's turn, not the colour's.
    """
    if not pane:
        return False, "restart-worker.py named no pane, so there is nowhere to send the re-arm"

    deadline = clock() + READY_TIMEOUT
    while True:
        rc, text = wezterm(["get-text", "--pane-id", pane])
        if rc == 0 and READY_MARKER in tail(text) and new_status(sid, old_pid) == "idle":
            break
        if clock() >= deadline:
            return False, (f"pane {pane} did not reach ready ({READY_MARKER!r} in its last "
                           f"{TAIL_LINES} lines and the resumed process idle) within {READY_TIMEOUT}s")
        sleep(POLL_INTERVAL)

    rc, _ = wezterm(["send-text", "--pane-id", pane, "--no-paste", REARM_COMMAND])
    if rc != 0:
        return False, f"send-text of {REARM_COMMAND} into pane {pane} failed"
    sleep(1)
    rc, _ = wezterm(["send-text", "--pane-id", pane, "--no-paste", "\r"])
    if rc != 0:
        return False, f"submit into pane {pane} failed"

    deadline = clock() + WORKING_TIMEOUT
    while True:
        status = new_status(sid, old_pid)
        if status and status != "idle":
            return True, f"{REARM_COMMAND} submitted into pane {pane}; resumed process is {status}"
        if clock() >= deadline:
            return False, (f"{REARM_COMMAND} sent into pane {pane} but the resumed process still "
                           f"reads {status or 'unreadable'} after {WORKING_TIMEOUT}s")
        sleep(POLL_INTERVAL)


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


def card(item):
    return f" (card {item})" if item else ""


def main(argv):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--dry-run", action="store_true",
                    help="decide and report; restart nothing, re-arm nothing, record no resume")
    ap.add_argument("--ledger", help="the fleet digest ledger (passed through to fleet-keepalive.py)")
    ap.add_argument("--now", type=float, help="epoch seconds (tests only)")
    a = ap.parse_args(argv)

    now = a.now if a.now is not None else time.time()

    # Before anything can fail, so a keeper that dies mid-tick still proves it ran.
    heartbeat(now)

    state, detail = verdict(a.ledger)

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
        report("cannot-tell", f"{detail} — reported, never treated as fine{card(item)}", now)
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
               f"cannot resume, reported{card(item)}", now)
        return 1

    if found[0] == "AMBIGUOUS":
        ids = ", ".join(sid[:8] for sid, _ in found[1])
        item = notify(
            "fleet-keeper-ambiguous-registration",
            f"The fleet round is stale and {len(found[1])} live sessions claim the "
            f"`Fleet Manager` registration ({ids}). Resuming one of two would be a double "
            f"writer over every subject, so the keeper refuses to pick.",
            ["acknowledge"],
            context=detail,
        )
        report("cannot-tell",
               f"{len(found[1])} live fleet registrations ({ids}) — refusing to pick one"
               f"{card(item)}", now)
        return 1

    sid, vault = found
    live = liveness(sid)

    if live in ("UNKNOWN", "AMBIGUOUS", "MALFORMED"):
        item = notify(
            "fleet-keeper-liveness-unknown",
            f"The fleet round is stale and the liveness probe for the fleet manager "
            f"(session {sid[:8]}) answers {live}. The keeper will not act on a probe that "
            f"cannot decide — and nothing else will restart the fleet while it stays this way.",
            ["acknowledge"],
            context=detail,
        )
        report("cannot-tell",
               f"session {sid[:8]} → {live}; an undecidable probe is never death{card(item)}", now)
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
        report("dead", f"fleet manager {sid[:8]} is ABSENT — reported, NOT restarted{card(item)}", now)
        return 1

    # ---- live + stale: the lapse case ----
    tk = load_sibling("topic-keepalive.py")
    path = tk.state_path(vault or "vault", SUBJECT, BRANCH)
    stamps = tk.recent(tk.load_resumes(path), now)

    if stamps and now - max(stamps) < HOLD_OFF:
        report("settling",
               f"a resume ran {int(now - max(stamps))}s ago — the fleet manager is still coming "
               f"up; not re-read as a lapse until {HOLD_OFF}s have passed", now)
        return 0

    if len(stamps) >= tk.RESUME_CAP:
        item = notify(
            "fleet-keeper-crash-loop",
            f"⚠️ FLEET MANAGER CRASH-LOOP: {len(stamps)} resumes in the last 30 minutes. The "
            f"keeper has stopped restarting it — a restart is repeating a failure it cannot "
            f"explain.",
            ["acknowledge"],
            context=detail,
        )
        report("crash-loop",
               f"⚠️ FLEET MANAGER CRASH-LOOP — {len(stamps)} resumes in 30 min; stop restarting"
               f"{card(item)}", now)
        return 1

    attempt = f"attempt {len(stamps) + 1} of {tk.RESUME_CAP}"

    if a.dry_run:
        outcome, note, _pane = restart(sid, dry=True)
        if outcome != "ran":
            report(outcome, f"dry-run: restart-worker.py would refuse — {note}", now)
            return 1
        report("restarted", f"dry-run: would resume {sid[:8]} and send {REARM_COMMAND} ({attempt})", now)
        return 0

    # Read BEFORE the kill — afterwards the registry briefly holds two records for this id, and
    # the re-arm tells the resumed process from the killed one by this pid.
    old_pid = registry_pid(sid)
    outcome, note, pane = restart(sid)

    if outcome == "busy":
        report("busy", f"fleet manager {sid[:8]} is mid-turn — {note}", now)
        return 1

    if outcome == "refused":
        item = notify(
            "fleet-keeper-restart-refused",
            f"The fleet round is stale and restart-worker.py refused to resume the fleet "
            f"manager (session {sid[:8]}). That refusal will not clear by itself.",
            ["acknowledge"],
            context=note,
        )
        report("refused", f"restart refused: {note}{card(item)}", now)
        return 1

    # From here the kill may already have happened, so the cap is armed whatever follows —
    # a resume that does not land must count, or the keeper kills with no bound.
    tk.record(path, tk.load_resumes(path), now)

    if outcome == "failed":
        item = notify(
            "fleet-keeper-restart-failed",
            f"The keeper tried to resume the fleet manager (session {sid[:8]}) and the restart "
            f"failed partway. The old process may already be gone.",
            ["acknowledge"],
            context=note,
        )
        report("cannot-tell", f"restart failed after it may have killed {sid[:8]}: {note}{card(item)}", now)
        return 1

    ok, rearm_note = rearm(pane, sid, old_pid)
    if not ok:
        item = notify(
            "fleet-keeper-rearm-unconfirmed",
            f"The keeper resumed the fleet manager (session {sid[:8]}) but could not confirm "
            f"its loop re-armed. A resumed manager with no armed loop stays idle and the round "
            f"stays stale. Send {REARM_COMMAND} in its pane.",
            ["acknowledge"],
            context=rearm_note,
        )
        report("cannot-tell",
               f"resumed {sid[:8]} ({attempt}) but the re-arm is unconfirmed: {rearm_note}{card(item)}",
               now)
        return 1

    report("restarted", f"resumed fleet manager {sid[:8]} and re-armed its loop ({attempt}) — {rearm_note}", now)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
