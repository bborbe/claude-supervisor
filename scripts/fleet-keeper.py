#!/usr/bin/env python3
"""The fleet tier's keeper: REPORT a lapsed fleet round to the operator; act only on their answer.

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
any restart cap unreachable. So this keeper **holds no kill or re-arm path, and starts nothing
on its own judgement**. Its one route to a spawn is the answer-gated resume below, which reaches
`wezterm` only through `restart-worker.py --absent`. `scripts/tests/test_fleet_keeper.py` pins
the rest of the file against every kill, resume and spawn shape, with a positive control.

⚠️ **One exception, by operator decision (2026-11-06, card `7653b7b6…`): a resume gated on
the operator's answer.** It narrows the ruling above by exactly one path, and every condition
on it is load-bearing:

  * only the **dead** card offers `resume it` — a lapsed manager is alive, and resuming a live
    session means killing it first, which is what the 2026-11-01 ruling forbids;
  * nothing runs without the answer: no timer, no staleness threshold, no autonomous branch
    reaches `resume_absent_session`, which has a single call site in `act_on_answer`;
  * `acknowledge` is not resolution, and never starts anything;
  * death is corroborated first — `session-liveness.py` ABSENT alone is not trusted (it read
    ABSENT for live sessions three times on 2026-11-06), so no registry entry may carry the
    id and the transcript must be silent `RESUME_SETTLE` seconds (`death_corroborated`);
  * once per answer, marked before it runs — an answer never becomes a loop;
  * the resume is `restart-worker.py --absent`, which refuses unless the process is gone and
    signals nothing.

⚠️ **A second exception, by operator decision (2026-11-10, task "A Stalled Fleet Loop Is
Restarted Automatically Instead of Going Unnoticed for Hours"): `restart fresh`, gated on the
operator's answer to the LAPSE card.** Measured 2026-11-10: the Fleet Manager stayed alive and
busy for 4.4 h with its context at the limit and wrote no digest; the lapse card was posted and
answered `acknowledge`, and nothing could recover it. So the lapse card for a LIVE manager now
offers `restart fresh`:

  * nothing runs without the answer — `restart_fresh_session` has a single call site, in
    `act_on_fresh_answer`, reached only from the lapse branch with the card's own item id;
  * once per card, marked in `lapse.json` (`fresh_consumed`) BEFORE it runs;
  * it calls `restart-worker.py <sid> --reason lapse --fresh-prompt /supervisor:fleet-loop`,
    which keeps every restart-worker refusal (held, busy, headless, role-undetermined) — a
    mid-turn manager is refused, never killed mid-turn — and starts a NEW session rather than
    `--resume`, because a resumed session carries the exhausted context back.

The pin is narrowed, not deleted: outside `resume_absent_session` the source still matches no
kill, resume or spawn shape, and that function may hold only the `--absent` resume.

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
deduplicated on that start time. The record is cleared on `ok` (the lapse recovered) and on a
deliberate `stopped` (it was never a lapse), so the next lapse gets a new card and a
continuing one never gets a second. A record that cannot be written or cleared is itself
carded, because either failure would silently suppress a real lapse.

⚠️ **The keeper's own liveness is a separate instrument.** Each tick rewrites
`~/.claude/state/fleet-keeper/heartbeat`. A keeper that has died leaves that file ageing while
`fleet-keepalive.py` may still read `ok`.

Usage:
  fleet-keeper.py [--ledger PATH] [--now EPOCH]
"""

from __future__ import annotations

import argparse
import datetime
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
MANAGER_LIVENESS = os.path.join(HERE, "manager-liveness.py")
ATTENTION_ASK = os.path.join(HERE, "attention-ask.py")
MANAGER_STATES = ("OK", "STALE", "STOPPED", "SLEEPING", "UNARMED", "INVALID")

STATE_DIR = os.path.expanduser(
    os.environ.get("SUPERVISOR_FLEET_KEEPER_DIR") or "~/.claude/state/fleet-keeper"
)
HEARTBEAT = os.path.join(STATE_DIR, "heartbeat")
KEEPER_LOG = os.path.join(STATE_DIR, "keeper.log")
LAPSE = os.path.join(STATE_DIR, "lapse.json")
EPISODES = os.path.join(STATE_DIR, "episodes.json")

# The keeper's identity on the attention board. A stable string, because a launchd job has no
# Claude session to borrow one from (see `notify`).
PRODUCER_ID = "fleet-keeper"

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
    current manager. ⚠️ It is no longer only a label: since the answer-gated resume, the newest
    registration is also the session `act_on_answer` resumes. A stale newest record would be
    offered for resume — which is why the operator's answer, a corroborated death and
    `--absent`'s own refusals all stand between this read and any spawn.
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
    """Persist the lapse record. Returns whether it landed.

    ⚠️ A failure here is NOT benign, so it is never swallowed silently: without a persisted
    start time every tick begins a fresh lapse at `since=now`, the held time never reaches
    REPORT_AFTER, and a real lapse is never reported — a dead fleet read as a quiet one.
    """
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        tmp = LAPSE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(rec, fh)
        os.replace(tmp, LAPSE)
        return True
    except OSError:
        return False


def clear_lapse():
    """Forget the lapse in progress. Returns whether it is gone.

    ⚠️ Same reasoning as `save_lapse`, mirrored: a record that cannot be removed keeps its
    `reported` marker, so the NEXT lapse would read as "already reported" and never get a card.
    An absent file is success — there was nothing to forget.
    """
    try:
        os.remove(LAPSE)
    except FileNotFoundError:
        return True
    except OSError:
        return False
    return True


def day(now):
    """The UTC day of `now`, as a dedup-key bucket for cards that cannot be episodic."""
    return time.strftime("%Y%m%d", time.gmtime(now))


def unclearable(now, detail):
    """Report a lapse record that could not be cleared, and say why it matters."""
    item = notify(
        f"fleet-keeper-state-unclearable-{day(now)}",
        f"The fleet keeper cannot clear its state ({LAPSE} or {EPISODES}). An old 'already "
        f"reported' marker there would make the NEXT fleet lapse or condition look already "
        f"handled, so it would never be reported.",
        ["acknowledge"], context=detail,
    )
    report("cannot-tell", f"cannot clear lapse state — reported{card(item)}", now)
    return 1


def notify(dedup_key, payload, options, context=""):
    """Post one attention card. Returns its item id, or `None` when the post failed.

    ⚠️ **An explicit producer is mandatory here, not a nicety.** `attention-ask.py` defaults the
    producer to `$CLAUDE_CODE_SESSION_ID` and REFUSES when it is unset — and a launchd job runs
    outside any Claude session, so it is always unset. Without `--producer-id` the deployed
    keeper could post no card at all, and every lapse would be reported to nobody. The tests
    mock this function, which is how that went unseen; `NotifyArgvTest` now pins the argv.
    """
    argv = [sys.executable, ATTENTION_ASK, "post",
            "--producer-id", PRODUCER_ID, "--producer-kind", "cron",
            "--dedup-key", dedup_key, "--payload", payload]
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


def _load_episodes():
    try:
        with open(EPISODES, encoding="utf-8") as fh:
            rec = json.load(fh)
    except (OSError, ValueError):
        return {}
    return rec if isinstance(rec, dict) else {}


def _save_episodes(rec):
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        tmp = EPISODES + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(rec, fh)
        os.replace(tmp, EPISODES)
        return True
    except OSError:
        return False


#: The one answer that may start anything. Operator decision 2026-11-06 (card `7653b7b6…`):
#: "add a resume option to the keeper's card, gated on your answer". It narrows the
#: 2026-11-01 report-only ruling (card efc771fb) by exactly this one gated path.
RESUME_OPTION = "resume it"
#: The lapse card's answer-gated recovery (operator ruling 2026-11-10; see module docstring).
FRESH_OPTION = "restart fresh"
FRESH_PROMPT = "/supervisor:fleet-loop"

#: How long the dead session's transcript must have been silent before a resume may run.
#: The third limb of the death corroboration below.
RESUME_SETTLE = 300

SESSIONS_DIR = os.path.expanduser(
    os.environ.get("SUPERVISOR_SESSIONS_DIR") or "~/.claude/sessions"
)
PROJECTS_DIR = os.path.expanduser(
    os.environ.get("SUPERVISOR_PROJECTS_DIR") or "~/.claude/projects"
)


def answer_of(item):
    """`(option, answered_epoch)` for card `item` — `(None, None)` when unanswered or unreadable.

    `answered_epoch` is the STORE's answer time (`poll --answered-at`), or `None` when the
    store gives none. The wait bound in `act_on_answer` runs from it, so a keeper that was
    down for hours does not hand an hours-old answer a fresh window.
    """
    if not item:
        return None, None
    try:
        out = subprocess.run([sys.executable, ATTENTION_ASK, "poll", item, "--answered-at"],
                             capture_output=True, text=True, timeout=PROBE_TIMEOUT)
    except (OSError, subprocess.SubprocessError):
        return None, None
    option, answered = None, None
    for line in (out.stdout or "").splitlines():
        if line.startswith("ANSWERED: option:"):
            option = line.split(":", 2)[2].strip()
        elif line.startswith("ANSWERED_AT:"):
            answered = parse_rfc3339(line.split(":", 1)[1].strip())
            if answered is None:
                log(f"unparseable ANSWERED_AT for card {item}: {line!r} — the resume wait is "
                    f"bounded from first sight instead")
    return option, answered


_RFC3339 = re.compile(r"(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(?:\.(\d+))?(Z|[+-]\d\d:\d\d)")


def parse_rfc3339(stamp):
    """Epoch seconds for an RFC 3339 time, or `None`. Any fraction length, any Python ≥ 3.7.

    ⚠️ Not `datetime.fromisoformat` alone: the launchd job runs `/usr/bin/python3` (3.9.6),
    which accepts only 3- or 6-digit fractions, while the store writes Go's `RFC3339Nano`,
    which drops trailing zeros (1–9 digits). Measured 2026-11-06: `…54.5883+00:00` and a
    9-digit fraction both raise there, and the bound would silently fall back to first sight.
    The fraction is normalised to exactly 6 digits first.
    """
    m = _RFC3339.fullmatch(stamp or "")
    if not m:
        return None
    base, frac, zone = m.groups()
    frac = ((frac or "") + "000000")[:6]
    zone = "+00:00" if zone == "Z" else zone
    try:
        return datetime.datetime.fromisoformat(f"{base}.{frac}{zone}").timestamp()
    except ValueError:
        return None


#: How long an answered `resume it` may wait on a too-fresh transcript before it is consumed
#: unrun. Without a bound the answer stays armed and fires whenever the reads later agree —
#: `RESUME_SETTLE` would become the trigger, which is exactly the autonomous resume the
#: operator's answer-gating forbids.
RESUME_DEFER_MAX = 1800

#: How long either restart-worker call may take before the keeper reports an UNKNOWN outcome.
#: ⚠️ 120 s was too short: `restart-worker.py` resolves the session's role through
#: `fleet-board.vault_index()`, which walks every vault's topic/goal/task pages and reads each
#: task's frontmatter — measured 2026-11-12 at **170.6 s**, on its own, before the kill or the
#: spawn. Every restart therefore timed out, and because the hang sits BEFORE the kill it
#: aborted harmlessly while never actually recovering anything. Operator ruling 2026-11-12:
#: raise the budget above the index cost rather than optimise the index here.
RESTART_WORKER_TIMEOUT = 400


def death_corroborated(sid, now):
    """`(ok, why, retryable)` — `ok` only when every independent read agrees the session is gone.

    `session-liveness.py` ABSENT is the branch's own precondition, and on its own it is NOT
    enough: it published ABSENT for live sessions three times on 2026-11-06. Two more reads
    must agree, and the first is the strong one — a live session's registry file exists:

      1. no `~/.claude/sessions/*.json` carries the id;
      2. the session's transcript has been silent for at least `RESUME_SETTLE` seconds.

    `retryable` says whether a miss may be waited out. A registry entry that carries the id
    means the session is ALIVE, and an unmeasurable transcript never gets better — both are
    final, and the answer is consumed. An unreadable or half-written registry file and a
    too-fresh transcript are transient and may be waited out, within `RESUME_DEFER_MAX`.
    """
    try:
        names = os.listdir(SESSIONS_DIR)
    except OSError:
        return False, f"the registry at {SESSIONS_DIR} could not be read", True
    for name in names:
        if not name.endswith(".json"):
            continue
        try:
            with open(os.path.join(SESSIONS_DIR, name), encoding="utf-8") as fh:
                rec = json.load(fh)
        except (OSError, ValueError, RecursionError):
            # Half-written — possibly this very session mid-rewrite. Never proof of absence.
            return False, f"registry entry {name} could not be parsed", True
        if isinstance(rec, dict) and rec.get("sessionId") == sid:
            return False, f"registry entry {name} still carries the session — it is alive", False
    newest = None
    try:
        for project in os.listdir(PROJECTS_DIR):
            path = os.path.join(PROJECTS_DIR, project, f"{sid}.jsonl")
            if os.path.isfile(path):
                mtime = os.path.getmtime(path)
                newest = mtime if newest is None else max(newest, mtime)
    except OSError:
        return False, f"the transcripts under {PROJECTS_DIR} could not be read", True
    if newest is None:
        return False, "no transcript found for the session, so its silence cannot be measured", False
    quiet = now - newest
    if quiet < RESUME_SETTLE:
        return False, f"the transcript was written {int(quiet)}s ago (< {RESUME_SETTLE}s)", True
    return True, f"no registry entry, transcript silent {int(quiet)}s", False


def resume_absent_session(sid):
    """Resume a DEAD fleet manager. The only resume this file holds; returns `(ok, detail)`.

    Reached from exactly one place — `act_on_answer`, behind the operator's `resume it` and a
    corroborated death. `--absent` signals nothing and refuses while any registry entry or
    recent pane record names the session, so a mistaken call cannot kill a live process — but
    it CAN start a second copy of a live conversation, which is why the gates above it exist.
    """
    script = os.path.join(HERE, "restart-worker.py")
    try:
        out = subprocess.run([sys.executable, script, sid, "--absent"],
                             capture_output=True, text=True, timeout=RESTART_WORKER_TIMEOUT)
    except subprocess.TimeoutExpired:
        return False, (f"outcome UNKNOWN — restart-worker did not finish within "
                       f"{RESTART_WORKER_TIMEOUT} s, so a pane may have been spawned; check for "
                       f"one before starting it again")
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"could not run restart-worker, nothing was spawned: {exc}"
    lines = [ln for ln in (out.stdout or "").splitlines() if ln.strip()]
    stderr = (out.stderr or "").strip()
    if out.returncode == 0:
        return True, lines[-1] if lines else "resumed"
    # A traceback has an empty stdout: report stderr's last line, never index an empty list.
    reason = lines[0] if lines else (stderr.splitlines()[-1] if stderr else "no output")
    return False, f"exit {out.returncode}: {reason}"


def act_on_answer(sid, condition, item, now):
    """Run the resume once for this card, only on `resume it`, only on a corroborated death.

    ⚠️ **Every answer is used at most once.** The `consumed_item` marker is written BEFORE the
    resume runs, so a tick that dies mid-resume never becomes a second resume. A final
    corroboration miss (the session is alive, or its transcript cannot be measured) consumes
    the answer UNRUN, and so does a transient miss that outlasts `RESUME_DEFER_MAX` — an
    answer that could fire hours later, when the reads happen to agree, would be the timer the
    operator's answer-gating forbids. A consumed answer is told to the operator on a card, once.
    """
    episodes = _load_episodes()
    ep = episodes.get(condition) or {}
    if ep.get("consumed_item") == item:
        return None
    option, answered = answer_of(item)
    if option != RESUME_OPTION:
        return None
    # The bound runs from the STORE's answer time when it has one, else from first sight.
    seen = ep.get("answer_seen_at")
    if ep.get("answer_seen_item") != item or not isinstance(seen, (int, float)):
        seen = answered if isinstance(answered, (int, float)) else now
        ep["answer_seen_at"], ep["answer_seen_item"] = seen, item
    ok, why, retryable = death_corroborated(sid, now)
    if not ok and retryable and now - seen < RESUME_DEFER_MAX:
        log(f"resume answered for {sid[:8]}, waiting — {why}", now)
        first_notice = ep.get("held_noticed") != item
        ep["held_noticed"] = item
        episodes[condition] = ep
        # Posted only once the marker that suppresses the NEXT post is on disk: an unwritable
        # record would otherwise repost the held card every tick, for the whole wait.
        if _save_episodes(episodes) and first_notice:
            notify(f"fleet-keeper-resume-held-{item}",
                   f"You answered '{RESUME_OPTION}' for the fleet manager (session {sid[:8]}). "
                   f"Not run yet: {why}. The keeper waits at most {RESUME_DEFER_MAX // 60} min from "
                   f"your answer for that to clear, then drops it unrun.", ["acknowledge"])
        return None
    ep["consumed_item"] = item
    episodes[condition] = ep
    if not _save_episodes(episodes):
        log(f"cannot persist the answer marker for {sid[:8]}; refusing to resume", now)
        return None
    if not ok:
        reason = why if not retryable else f"{why}, still after {int(now - seen)}s"
        log(f"resume answer for {sid[:8]} consumed UNRUN — {reason}", now)
        notify(f"fleet-keeper-resume-dropped-{item}",
               f"Your '{RESUME_OPTION}' for the fleet manager (session {sid[:8]}) was NOT run and "
               f"is now used up: {reason}. If it is still dead, the next card asks again.",
               ["acknowledge"])
        return False
    done, detail = resume_absent_session(sid)
    log(f"resume of {sid[:8]} on answer {item}: {'ok' if done else 'FAILED'} — {detail}", now)
    notify(f"fleet-keeper-resumed-{item}",
           (f"Resumed the fleet manager (session {sid[:8]}) on your answer: {detail}. It must "
            f"re-arm its loop (/supervisor:fleet-loop) — a dead session's jobs died with it.")
           if done else
           (f"The resume you asked for did NOT complete for session {sid[:8]}: {detail}. "
            f"Nothing was killed."),
           ["acknowledge"])
    return done


def restart_fresh_session(sid):
    """Kill a LAPSED fleet manager and start a fresh one. The only kill this file holds.

    Reached from exactly one place — `act_on_fresh_answer`, behind the operator's
    `restart fresh` answer to that lapse's own card. Returns `(ok, detail)`.

    `--allow-busy-lapse` (operator ruling 2026-11-12): a lapsed manager is very often still
    BUSY — the 4.4 h stall on 2026-11-10 was a live manager answering messages with its
    context exhausted and no digest, and `busy-target` refuses exactly that shape. Measured
    2026-11-12: 30 of 30 direct attempts refused `busy-target` because the manager runs
    fleet rounds continuously and never presents `idle`. The operator's answer on this card
    is the judgement that separates a lapse from a slow round, so the blanket refusal would
    override a decision the human has already made. The flag is confined to
    `--reason lapse --fresh-prompt` by restart-worker's own argument check, so it cannot
    widen any other restart.
    """
    script = os.path.join(HERE, "restart-worker.py")
    try:
        out = subprocess.run([sys.executable, script, sid, "--reason", "lapse",
                              "--fresh-prompt", FRESH_PROMPT, "--allow-busy-lapse"],
                             capture_output=True, text=True, timeout=RESTART_WORKER_TIMEOUT)
    except subprocess.TimeoutExpired:
        return False, (f"outcome UNKNOWN — restart-worker did not finish within "
                       f"{RESTART_WORKER_TIMEOUT} s, so the manager may be stopped and a pane "
                       f"spawned; check before answering again")
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"could not run restart-worker, nothing was signalled: {exc}"
    lines = [ln for ln in (out.stdout or "").splitlines() if ln.strip()]
    stderr = (out.stderr or "").strip()
    if out.returncode == 0:
        return True, lines[-1].strip() if lines else "restarted fresh"
    # ⚠️ The ERROR line, never `lines[0]`. restart-worker prints a header first and its
    # `❌ error:` / `   killing-pid` lines after it, so `lines[0]` is always the header and
    # reporting it hides the cause completely. Measured 2026-11-11: the keeper logged
    # "exit 1: 🔄 restart-worker · session ac24db61 · pid 21437 · 'Fleet Manager'" for a
    # failure whose real cause was a bare `wezterm` missing from launchd's PATH — the header
    # was reported as the finding and the actual error sat one line lower.
    errors = [ln for ln in lines if ln.lstrip().startswith("❌")]
    reason = (errors[-1] if errors
              else lines[-1] if lines
              else (stderr.splitlines()[-1] if stderr else "no output"))
    return False, f"exit {out.returncode}: {reason.strip()}"


def act_on_fresh_answer(sid, lapse, now):
    """Run `restart fresh` once for this lapse card, only on that answer. Returns None when
    nothing ran, else the executor's ok flag."""
    item = lapse.get("reported")
    if not item or not sid or lapse.get("fresh_consumed") == item:
        return None
    option, _answered = answer_of(item)
    if option != FRESH_OPTION:
        return None
    lapse["fresh_consumed"] = item
    if not save_lapse(lapse):
        log(f"cannot persist the fresh-restart marker for {sid[:8]}; refusing to restart", now)
        return None
    done, detail = restart_fresh_session(sid)
    first = lapse.setdefault("fresh_first_try", now)
    if not done and "busy-target" in detail and now - first < RESUME_DEFER_MAX:
        # A mid-turn manager is refused, never killed. Re-arm the SAME answer for the next tick
        # rather than spend it, bounded like the resume's wait so it can never fire hours later.
        lapse["fresh_consumed"] = None
        save_lapse(lapse)
        log(f"fresh restart of {sid[:8]} waiting — manager mid-turn ({int(now - first)}s of "
            f"{RESUME_DEFER_MAX}s)", now)
        return None
    log(f"fresh restart of {sid[:8]} on answer {item}: {'ok' if done else 'FAILED'} — {detail}", now)
    notify(f"fleet-keeper-fresh-{item}",
           (f"Restarted the fleet manager fresh on your answer (old session {sid[:8]}): {detail}. "
            f"The new session's first turn arms /supervisor:fleet-loop.")
           if done else
           (f"The fresh restart you asked for did NOT complete for session {sid[:8]}: {detail}. "
            f"If it was refused as busy, the manager was mid-turn and was not killed; the answer "
            f"is used up — answer the next lapse card again."),
           ["acknowledge"])
    return done


def raise_once(condition, payload, now, detail="", options=("acknowledge",)):
    """Post ONE card per episode of `condition`. Returns the item id (existing or new), or None.

    ⚠️ Two failure modes this exists to close, both measured against the store's own source
    (`attention-controller` `pkg/attention-store-impl.go`):

    1. **Flooding.** The store collapses a re-push only "while the matching item is still open"
       AND "its producer is still live". A launchd tick is a fresh process every minute, so
       relying on the store to dedup a card re-posted every tick is relying on producer-liveness
       semantics this file cannot see. The keeper therefore posts once per episode itself.
    2. **Swallowing.** `suppressAnsweredTwins` drops — and `closeRedundantReraises` closes — an
       open card whose `askIdentity` (producer + dedup key + payload) matches an ANSWERED one.
       A constant key and payload would make the operator's `acknowledge` on the first episode
       silently swallow every later one. The key therefore carries the episode's start time, so
       no two episodes ever share an identity.

    An episode ends on `ok` (`end_episodes`). A post that fails is retried on the next tick,
    under the same episode key, so the board still ends up with exactly one card.
    """
    episodes = _load_episodes()
    ep = episodes.get(condition)
    since = ep["since"] if ep and isinstance(ep.get("since"), (int, float)) else now
    # Re-raised once per day of the episode, exactly as the lapse card is: a card expires after
    # its 24 h default, and an ABSENT fleet manager with no successor is the case that outlives
    # it. Days of the EPISODE, not calendar days, so the day-0 card overlaps the day-1 card
    # instead of leaving a gap. (A record without `day` reads as day 0, so an upgrade does not
    # repost.)
    ep_day = int((now - since) // 86400)
    if ep and ep.get("item") and ep.get("day", 0) == ep_day:
        return ep["item"]
    item = notify(f"fleet-keeper-{condition}-{int(since)}-d{ep_day}", payload, list(options),
                  context=detail)
    episodes[condition] = {**(ep or {}), "since": since, "item": item, "day": ep_day}
    if not _save_episodes(episodes):
        notify(f"fleet-keeper-state-unwritable-{day(now)}",
               f"The fleet keeper cannot write {EPISODES}, so it cannot remember which conditions "
               f"it already reported; it may repost them.", ["acknowledge"], context=detail)
    return item


def end_episodes():
    """The fleet is healthy: every open condition has ended. Returns whether the record is gone.

    ⚠️ A record that survives keeps its `item`, and `raise_once` returns early on that — so the
    next episode of the same condition would get NO card. Same failure class as `clear_lapse`,
    and handled the same way by the caller.
    """
    try:
        os.remove(EPISODES)
    except FileNotFoundError:
        return True
    except OSError:
        return False
    return True


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
        if not clear_lapse() or not end_episodes():
            return unclearable(now, detail)
        tail =(f"; the lapse since {time.strftime('%H:%M:%SZ', time.gmtime(recovered['since']))} "
                f"recovered") if recovered else ""
        report("ok", (detail or "the fleet round is inside its limit") + tail, now)
        return 0

    if state == "unknown":
        item = raise_once("unknown", "The fleet keeper cannot read the digest ledger, so it cannot tell whether the "
            "fleet round is running. It is NOT treating that as healthy.", now, detail)
        report("cannot-tell", f"{detail} — reported, never treated as fine{card(item)}", now)
        return 1

    # ---- stale ----
    stood = stand_down()
    if stood == "STOPPED":
        if not clear_lapse() or not end_episodes():
            return unclearable(now, detail)
        report("stopped", "the fleet loop was stood down with /supervisor:stop — a deliberate "
                          "stop, not a lapse; left alone until it is re-armed", now)
        return 0

    sid = fleet_session()
    live = liveness(sid) if sid else None
    if live in ("UNKNOWN", "AMBIGUOUS", "MALFORMED"):
        # An undecidable probe is never death — but it IS a fault, independent of round speed,
        # so it is carded at once rather than folded into the 40-minute lapse timer. The lapse
        # timing below still runs, so a genuine lapse behind it is reported too.
        raise_once("liveness-unknown", f"The fleet round is stale and the liveness probe for the fleet manager (session "
            f"{sid[:8]}) answers {live}, so the keeper cannot tell whether the session is alive.", now, detail)
        log(f"liveness {live} for {sid[:8]} — carded; lapse timing continues", now)
    if live == "ABSENT":
        condition = f"absent-{sid[:8]}"
        # This card carries an action, so an unreadable stop state is said ON it: a manager
        # stood down on purpose and then closed is ABSENT too, and must not be resumed blind.
        stop_caveat = (" ⚠️ The keeper could not read whether the fleet was stood down on "
                       "purpose — if it was (/supervisor:stop), do not resume it."
                       if stood is None else "")
        item = raise_once(condition, f"The fleet manager (session {sid[:8]}) is ABSENT — the session is gone, not merely "
            f"lapsed. Nothing supervises the fleet until one is started. Answer "
            f"'{RESUME_OPTION}' to have the keeper resume it once, after it confirms the death; it "
            f"never starts or restarts one on its own.{stop_caveat}", now, detail,
            options=(RESUME_OPTION, "acknowledge"))
        if item:
            act_on_answer(sid, condition, item, now)
        report("dead", f"fleet manager {sid[:8]} is ABSENT — reported{card(item)}", now)
        return 1

    lapse = load_lapse()
    if lapse is None or lapse["since"] > now:
        # A start time in the future is clock skew; restart the timer rather than hold a
        # negative age that would keep the lapse `pending` for as long as the skew lasts.
        lapse = {"since": now, "reported": None}
        if not save_lapse(lapse):
            item = notify(
                f"fleet-keeper-state-unwritable-{day(now)}",
                f"The fleet keeper cannot write {LAPSE}, so it cannot time a lapse: every tick "
                f"would start a fresh 40-minute timer and a real lapse would never be reported. "
                f"The fleet round is stale right now.",
                ["acknowledge"], context=detail,
            )
            report("cannot-tell", f"cannot persist lapse state — reported{card(item)}", now)
            return 1

    held = now - lapse["since"]
    if held < REPORT_AFTER:
        report("pending",
               f"stale for {int(held)}s of the {REPORT_AFTER}s that make a lapse — possibly a "
               f"slow but healthy round; not reported yet", now)
        return 0

    lapse_day = int(held // 86400)
    if lapse.get("reported") and lapse.get("reported_day", 0) == lapse_day:
        if live == "LIVE":
            act_on_fresh_answer(sid, lapse, now)
        report("reported",
               f"lapse since {time.strftime('%H:%M:%SZ', time.gmtime(lapse['since']))} already "
               f"reported{card(lapse['reported'])}; nothing more until it recovers", now)
        return 1

    who = f"session {sid[:8]}" if sid else "no registered fleet manager"
    fresh = bool(sid) and live == "LIVE"
    # An unreadable stop state is said ON THE CARD, not only in the log: the operator reading the
    # card is the one who must know this might be a deliberate stop the keeper could not see.
    caveat = (" ⚠️ The keeper could not read whether the fleet was stood down on purpose, so "
              "this may be a deliberate /supervisor:stop." if stood is None else "")
    item = notify(
        f"fleet-keeper-lapse-{int(lapse['since'])}-d{lapse_day}",
        f"The fleet round has been stale for {int(held // 60)} min ({who}). That is past the "
        f"longest healthy round measured, so this is a lapse, not a slow round. "
        + (f"Answer '{FRESH_OPTION}' to have the keeper stop it and start a NEW fleet manager "
           f"(fresh context, first turn /supervisor:fleet-loop) once; it never does so on its own. "
           if fresh else
           "Check its tab and re-arm its loop (/supervisor:fleet-loop), or restart it deliberately. ")
        + caveat.lstrip(),
        [FRESH_OPTION, "acknowledge"] if fresh else ["acknowledge"], context=detail,
    )
    note = " (stop state unreadable)" if stood is None else ""
    if item:
        lapse["reported"] = item
        lapse["reported_day"] = lapse_day
        if not save_lapse(lapse):
            # The card is up; only the "already reported" marker failed to persist. The next tick
            # re-posts under the same dedup key, so the board still holds one card. Logged so a
            # persistently unwritable state is visible rather than inferred.
            note += " (reported-marker not persisted; next tick re-posts under the same key)"
    report("reported", f"stale for {int(held)}s — lapse reported for {who}{card(item)}{note}", now)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
