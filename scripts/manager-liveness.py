#!/usr/bin/env python3
"""Model-free liveness for manager loops: tell a lapsed manager apart from a stopped one.

A manager loop is one-shot per re-arm (`ScheduleWakeup`), so a missed re-arm (after
`/compact`, a crash, or a tick that ends without re-arming) stops the loop with no
signal. Nothing a peer session can probe (`ListAgents`, sockets, `ps`, transcript
mtime) tells a lapsed manager from a healthy one between ticks, so the manager leaves
evidence on disk itself. This script writes that evidence and checks it.

Five verbs, one file each, all in the sweep-gate state dir. **`<slug>` carries the
branch — `goal--<base>`, `topic--<base>` or `fleet--<base>`** — because a topic and a goal may share a
name, and a flat slug let one subject's cadence record overwrite the other's:



  --arm   --subject T --branch B --interval S [--resume]
                                  <slug>.cadence = "<S>\\n" and an epoch appended to
                                    <slug>.arms; clears <slug>.sleeping and CONSUMES
                                    <slug>.reset. Run by the
                                    manager at EVERY re-arm, so the cadence file's mtime
                                    marks the last re-arm that actually happened and
                                    <slug>.arms is the history the limit is measured
                                    against. The interval is recorded, never assumed:
                                    managers change it at runtime (a 20-min idle tick
                                    was set 2026-09-22), so a fixed limit false-positives.
                                    ⚠️ **It NEVER clears <slug>.stopped.** An arm is a
                                    READING — every round takes one — so an arm that
                                    cleared the marker made measuring a stand-down the act
                                    that erased it: measured 2026-11-07, a manager stood
                                    down at 12:13 took its 12:51 round's reading, the
                                    marker vanished, the Fleet Manager read STALE and
                                    restarted a session that had deliberately stopped.
                                    Only --resume resumes.
  --arm --resume                    ALSO clears <slug>.stopped. This is the one verb that
                                    resumes a stood-down subject, and the two loop-start
                                    arms (`commands/goal-loop.md` step 3,
                                    `commands/topic-loop.md` step 3) pass it, so the
                                    documented `Restart: /supervisor:goal-loop "<subject>"`
                                    still comes back. A plain --arm (a round's liveness
                                    reading) must never pass it.
  --stop  --subject T --branch B   <slug>.stopped + <slug>.reset. Run by `/supervisor:stop`,
                                    so a deliberately stood-down manager is not reported
                                    as a lapse. `.reset` is the epoch an arm consumes to
                                    start a NEW cadence sample: the outage gap is not
                                    cadence and cannot be filtered at read time, which is
                                    why the reset is its own file rather than something
                                    inferred from the marker the arm must now preserve.
                                    ⚠️ `is_subject_stopped()` is the ONE reader of that
                                    marker, imported by `open-queue.py` and
                                    `adopt-orphans.py`; the manager tier reads it through
                                    `--state` / `--check`, which classify STOPPED.
  --sleep --subject T --branch B --until ISO
                                  <slug>.sleeping = the ISO instant. A date-gated
                                    subject whose remaining work is all future
                                    `defer_date`s does not re-arm until that instant, so
                                    its cadence file ages past the stale limit BY DESIGN.
                                    While the instant is in the future the subject reads
                                    SLEEPING, never STALE; once it has PASSED the marker
                                    stops suppressing anything and the normal age
                                    arithmetic runs, so a manager that failed to wake
                                    still reads STALE. The next --arm clears it.
                                    ⚠️ Distinct from --stop: a stop says "nothing left to
                                    drive" and needs a human; a sleep says "nothing can
                                    happen before this instant" and expires by itself.
  --check                          every <slug>.cadence -> OK / STALE / STOPPED.
                                    STALE = no marker AND age > PERIOD + SLACK,
                                    where PERIOD is the p90 of the manager's observed
                                    arm-to-arm gaps capped at 2.5x their median (>=3
                                    gaps), falling back to the reported interval until
                                    that history exists. The p90 absorbs an ordinary
                                    slow tick; the cap stops one historic stop from
                                    inflating the limit for ever.
                                    A CONDITIONAL FLOOR applies while the window still
                                    holds a gap armed under a shorter interval: the
                                    period is then floored at the declared one, so a
                                    manager that LENGTHENS its interval is not reported
                                    on its first healthy tick at the new cadence. It
                                    switches off once a completed gap at the declared
                                    interval comes in under half of it — the DISPROOF
                                    that the manager really ticks at a shorter cadence
                                    (goal--deploy-and-merge-gates declares 3600 and
                                    ticks at ~900; a bare floor would hide its lapse).
                                    A re-arm after a stand-down resets the history,
                                    so an outage gap never counts as cadence.
                                    Prints one line per subject that TURNED stale (or
                                    recovered) since the last check; exits 10 if any
                                    turned stale, else 0.
                                    ⚠️ A lapse is re-emitted on EVERY poll until a
                                    delivery is ACKED (`--delivered`), because the
                                    transport that carries the report is a network call
                                    that fails routinely — measured 2026-11-07: 43
                                    failed sends in one ledger, none retried. Marking
                                    the lapse reported at detection time made the next
                                    poll silent, so a send that died left nothing behind
                                    and the operator never learned the manager had
                                    lapsed. The set of owed reports lives in
                                    liveness-pending.json; the delivered set lives in
                                    liveness-notified.json and is written ONLY by the
                                    ack. One lapse still notifies once, not on every
                                    poll — "once" now means once *delivered*, not once
                                    *detected*.
  --stood-down-past MINUTES        every subject whose <slug>.stopped marker is older
                                    than MINUTES, as `<age-seconds>\t<slug>`, oldest
                                    first. The selection half of the stood-down-manager
                                    close: `--stop` writes that marker and `--arm`
                                    clears it, so the path and the timestamp format
                                    have one owner and a caller never re-derives them.
                                    ⚠️ The marker outlives the session it describes —
                                    measured 2026-11-09: 83 markers, 41 on pages still
                                    holding a `claude_session_id`, only 2 of them LIVE —
                                    so the caller must still join this to session
                                    liveness and to the page before acting.
                                    ⚠️ SLEEPING cannot appear: a sleep writes
                                    <slug>.sleeping, so a date-gated manager is excluded
                                    structurally rather than by a filter a caller must
                                    remember. Exit 0 whenever the scan completed (an
                                    empty list is a real answer); exit 2 when the state
                                    dir cannot be read, never 0 on a failed scan.
  --delivered SLUG                 ACK a lapse: move SLUG from the owed set to the
                                    delivered set. Called by the sender after the
                                    transport returns success. Carries the CROSSING
                                    time, not the ack time, so a manager that re-armed
                                    while the report was owed still starts a NEW lapse
                                    on its next STALE. Idempotent: a slug that is not
                                    owed prints `not-pending` and exits 0.
  --failed SLUG --error TEXT       Record one failed delivery attempt against SLUG
                                    (attempt count + the transport error), so the
                                    durable record names why it could not be sent. The
                                    entry stays owed, which is what makes the next poll
                                    re-attempt it.
  --state --subject T --branch B   ONE subject's state, as `<STATE>\t<slug>\t<detail>`.
                                    `SLEEPING` is a fifth state: a future `.sleeping`
                                    instant, which suppresses the STALE report a frozen
                                    cadence file would otherwise produce.
                                    `--check` structurally cannot answer this: it prints
                                    only the subjects that TURNED stale, so STOPPED -- the
                                    state a deliberate stand-down produces -- is invisible
                                    to it by construction. A reader that must tell a
                                    stood-down manager from a lapsed one asks about the
                                    subject directly, which is what this verb is for.
                                    Exits 0 for every state: the state IS the answer,
                                    and a non-zero exit would make a stood-down manager
                                    read as a failure to every `if` that wraps it.
                                    `WRONG-BRANCH` is a seventh state: no record on the
                                    probed branch but one on another, detail `record is
                                    on branch <b>` -- a probe that names the wrong tier,
                                    not a subject nothing armed.
                                    ⚠️ **Exits 3 with nothing on stdout** when the subject
                                    resolves neither a record under any branch nor a page
                                    under --vault (`24 Goals/` / `23 Goals/` / `23 Topics/`,
                                    exact name, any case): an abbreviated or mistyped name,
                                    which is not a fact about the fleet and must never read
                                    as `UNARMED` -- acting on that arms a live loop or
                                    reverses a stand-down (measured 2026-11-07: five arm
                                    instructions to managers that needed none). `--branch
                                    fleet` is exempt: the fleet manager has no page by
                                    design, so a missing fleet record is still `UNARMED`.
                                    ⚠️ `UNARMED` means NO record exists under either the
                                    prefixed or the pre-split name -- a subject nothing has
                                    ever armed, which is a fact about the fleet. `INVALID`
                                    means a record EXISTS and cannot be read -- a probe
                                    failure. Collapsing the two is how a never-armed goal
                                    reads as a measurement error (measured 2026-10-20: all
                                    of them did).
                                    ⚠️ A pre-split record is resolved rather than
                                    ignored: `migrate_legacy` deletes one once its prefixed
                                    twin is written, but a subject that never re-armed
                                    keeps it, and reading only the prefixed path reported
                                    such a subject INVALID -- including one that had armed
                                    the same day, and one that was deliberately stood down.

`--check` reads files only: no model, no network. A host loop (launchd) runs it and
turns exit 10 into a push.
"""
import argparse, json, os, re, subprocess, sys, threading, time, urllib.error, urllib.parse, urllib.request
from datetime import datetime

# This namespace is the liveness layer's OWN. `arm`, `stop` and `check` all resolve
# through it, so its reads and writes agree — it is a live namespace, not a retired
# one, and it is deliberately NOT the gate's per-vault tree. Converting it to
# `sweep-gate-loop/<vault>/` would need a --vault on all three verbs and would turn
# `check`'s single listdir into a walk of per-vault subdirs, for no defect to fix.
# (Audited 2026-09-23 alongside the stop-probe path fix; left flat on purpose.)
STATE_DIR = os.path.expanduser(
    os.environ.get("MANAGER_LIVENESS_STATE_DIR", "~/.claude/state/sweep-gate")
)
NOTIFIED = "liveness-notified.json"
# ⚠️ The OWED set is a second file, not a second field in `notified`. The two answer
# different questions — "which lapses has the operator been told about" and "which are
# still owed a report" — and collapsing them is what produced the defect this file's
# `--check` now fixes: one entry meant both, so it was written at detection time and
# the report it stood for was never sent.
PENDING = "liveness-pending.json"
# ⚠️ `fleet` is a THIRD tier, not a synonym for either of the others. The fleet loop answers
# headless workers' parked prompts exactly as a goal manager does — it runs the wrangler and
# watches the queue — but it manages no subject page, so it could not honestly register under
# `goal` or `topic`. Leaving it unregistered was not neutral: `scripts/answer-path-check.py`
# reads a session with no record as having no answer path, which refuses every headless spawn
# the Fleet Manager makes (measured 2026-10-23 — the refusal was correct for the probe and
# wrong for that session, and the fix is the record, not a bypass).
BRANCHES = ("goal", "topic", "fleet", "task")
SLACK = 180  # seconds: the tick's own runtime plus ordinary jitter before a re-arm lands
PERIOD_CAP = 2.5  # the p90 period estimate never exceeds this multiple of the median
ARMS_KEEP = 12  # re-arm epochs retained per topic — enough for a stable percentile
EXIT_STALE = 10
# `--state` for a subject that resolves no record under any branch and no page: a refusal,
# distinct from argparse's 2 so a caller can tell "unresolvable name" from "bad flags".
EXIT_UNRESOLVED = 3
# The detached sweep gate is the goal tier's sole wake source once the model cadence is
# removed, so a goal manager legitimately sits with an ageing `.cadence` between gate
# wakes. Its liveness is then the gate's own per-subject heartbeat — written model-free
# on every tick to `~/.claude/state/sweep-gate-loop/<vault>/<base>.heartbeat` — not the
# model round that used to refresh `.cadence`. `--state` reads it so a gate-driven
# manager is `OK` rather than `STALE`; a STALE reading is what makes
# `topic-keepalive.py` restart a healthy manager. Scope is the `goal--` branch only:
# the topic and fleet tiers still arm their own cadence.
GATE_LOOP_STATE_DIR = os.path.expanduser(
    os.environ.get("MANAGER_GATE_LOOP_STATE_DIR", "~/.claude/state/sweep-gate-loop")
)
GATE_ALIVE = 1800  # seconds: the loop is alive if ANY subject heartbeat is this fresh
GATE_SUBJECT_FRESH = 7200  # seconds: a subject's own heartbeat this old is not being ticked


def base_slug(subject: str) -> str:
    # Same base transform as sweep-gate.py and stop-probe.py, so the files sit side by side.
    return re.sub(r"[^a-z0-9]+", "-", subject.lower()).strip("-")


def slug(subject: str, branch: str) -> str:
    """The state slug, carrying the BRANCH: `goal--<base>` / `topic--<base>` / `fleet--<base>`.

    ⚠️ The prefix is not decoration. A topic and a goal may share a name — that is
    exactly why `docs/subject-resolution.md` requires exact-basename matching — so a
    flat slug let one subject's cadence record overwrite the other's in this one
    directory. The branch is the only thing that tells the two apart, and the
    directory can stay flat.
    """
    if branch not in BRANCHES:
        raise ValueError(f"branch must be one of {BRANCHES}, got {branch!r}")
    return f"{branch}--{base_slug(subject)}"



def path(name: str, ext: str) -> str:
    return os.path.join(STATE_DIR, f"{name}.{ext}")


def legacy_base(s: str):
    """The unprefixed base of a branch-prefixed slug, or None when it has no prefix.

    Records written before the goal/topic split live under the bare base name.
    `migrate_legacy` deletes one once its prefixed twin is written, but a subject
    that never re-armed keeps it — and a reader that looks only at the prefixed
    path reports that subject INVALID, i.e. as a probe failure rather than as the
    lapse (or deliberate stand-down) the record actually describes.
    """
    return s.split("--", 1)[1] if "--" in s else None


def resolve(s: str, ext: str):
    """(path, is_legacy) — the prefixed record, else its pre-split twin.

    Falls back to the prefixed path when neither exists, so the caller's open()
    raises FileNotFoundError and can tell "no record at all" from "unreadable".
    """
    prefixed = path(s, ext)
    if os.path.exists(prefixed):
        return prefixed, False
    base = legacy_base(s)
    if base:
        legacy = path(base, ext)
        if os.path.exists(legacy):
            return legacy, True
    return prefixed, False


def stopped_marker(subject: str, branch: str):
    """The marker file that stands this subject down, or None — THE one marker read.

    `open-queue.py` (the goal and topic tiers) and `adopt-orphans.py` call this rather
    than re-deriving the path: a second copy would drift from `stop()`'s writer, and the
    drift is silent in the worst direction — the fleet round would keep reopening a
    subject the operator had deliberately ended.

    ⚠️ **Existence, not the timestamp.** Later stops rewrite the marker's content, so the
    file existing is the load-bearing fact; `--arm --resume` is the one verb that clears
    it. The RESOLVED path is returned, never a re-composed `<branch>--<slug>`: a legacy
    pre-split record (`<slug>.stopped`, no branch prefix) counts too, through `resolve()`,
    and a caller that re-derived the name would report a file that does not exist.

    A branch outside `BRANCHES` raises rather than answering None: the caller named a tier
    that does not exist, and a silent "not stopped" would hide that bug behind a reopened
    row.
    """
    marker, _legacy = resolve(slug(subject, branch), "stopped")
    return marker if os.path.exists(marker) else None


def is_subject_stopped(subject: str, branch: str) -> bool:
    """Is `stopped_marker()` non-None? The boolean form every consumer reads."""
    return stopped_marker(subject, branch) is not None


def write_atomic(p: str, text: str) -> None:
    os.makedirs(STATE_DIR, exist_ok=True)
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.replace(tmp, p)


def parse_arm_lines(text: str) -> list:
    """(epoch, declared interval or None, is_tail) per line of a `<slug>.arms` ring.

    Three formats are on disk and all keep working: the original single column, the
    `<epoch> <interval>` written since per-arm recording, and `<epoch> <interval> tail`
    written by the round's closing beat before the marker moved to `<slug>.tail`. That
    third column is now READ-ONLY legacy — `arm()` no longer writes it, because a
    pre-upgrade session rebuilds the ring from two-tuples and erases it. A line
    carrying no interval is LEGACY and reads as None rather than a guess — a guessed
    value would either floor a subject that never lengthened its interval or hide a
    real lapse.

    `is_tail` is what lets `observed_period` measure ROUND-to-round. A tail beat is the
    same round's closing write, so the gap ending on one is intra-round and must stay
    out of the period. The two cannot be told apart by gap size — a tail sits wherever
    the round happened to end — so the marker is the only honest discriminator.
    """
    out = []
    for line in text.splitlines():
        parts = line.split()
        if not parts:
            continue
        try:
            epoch = float(parts[0])
        except ValueError:
            continue
        interval = None
        if len(parts) > 1:
            try:
                interval = int(float(parts[1]))
            except ValueError:
                interval = None
        out.append((epoch, interval, len(parts) > 2 and parts[2] == "tail"))
    return out


def tail_store_path(s: str) -> str:
    """`<slug>.tail` — the tail-marker store, the `<slug>.arms` ring's sibling."""
    return path(s, "tail")


def read_tail_epochs(s: str) -> set:
    """Epoch keys marked as tail beats, one per line, formatted like the ring column."""
    try:
        with open(tail_store_path(s), encoding="utf-8") as fh:
            return {line.strip() for line in fh if line.strip()}
    except OSError:
        return set()


def write_tail_epochs(s: str, epochs) -> None:
    """Rewrite the store; an empty set removes the file rather than leaving it blank."""
    p = tail_store_path(s)
    if not epochs:
        try:
            os.remove(p)
        except OSError:
            pass
        return
    write_atomic(p, "".join(f"{e}\n" for e in sorted(epochs)))


def read_arm_pairs(s: str) -> list:
    """(epoch, interval-or-None, is_tail) from the ring, marker from its own store.

    The tail marker lives in `<slug>.tail`, NOT in a third column of the ring: a
    session still running a pre-upgrade copy of this script rebuilds the ring from
    two-tuples and writes `{epoch} {interval}`, which erases a column marker on every
    arm it takes. A separate file is the one home that writer never touches.

    A third column is still honoured when present. Rings written before the move carry
    `tail` there, and dropping it would silently turn every one of those tail beats
    into a head — collapsing the median and manufacturing the false STALE the marker
    exists to prevent.
    """
    p, _ = resolve(s, "arms")
    try:
        with open(p, encoding="utf-8") as fh:
            text = fh.read()
    except OSError:
        return []
    stored = read_tail_epochs(s)
    return [
        (epoch, interval, column_tail or f"{epoch:.0f}" in stored)
        for epoch, interval, column_tail in parse_arm_lines(text)
    ]


def read_arms(s: str) -> list:
    """Epochs only — the shape `observed_gaps` and `manager-receipt.py` consume."""
    return [epoch for epoch, _, _ in read_arm_pairs(s)]


def declared_interval(s: str):
    """The interval the manager last reported to `--arm`, or None when unreadable."""
    p, _ = resolve(s, "cadence")
    try:
        with open(p, encoding="utf-8") as fh:
            return int(fh.read().strip())
    except (OSError, ValueError):
        return None


def observed_gaps(s: str):
    """Sorted arm-to-arm gaps in seconds, or None when there is too little history."""
    vals = read_arms(s)[-ARMS_KEEP:]
    if len(vals) < 4:
        return None
    gaps = sorted(b - a for a, b in zip(vals, vals[1:]))
    if len(gaps) < 3:
        return None
    return gaps


def median(xs):
    """True median of an already-sorted list.

    `xs[len(xs) // 2]` is the UPPER median on an even count, and it is not a cosmetic
    difference: it is what `PERIOD_CAP` is measured against, so a subject with 10 gaps
    got a cap 2.5x an over-estimate — measured 2026-10-20, one subject's limit came out
    at 4478 s against a 3590 s cap.
    """
    n = len(xs)
    mid = n // 2
    return xs[mid] if n % 2 else (xs[mid - 1] + xs[mid]) / 2


def observed_interval(s: str):
    """Median arm-to-arm gap in seconds, or None when there is too little history."""
    gaps = observed_gaps(s)
    return None if gaps is None else median(gaps)


def percentile(xs, p: float):
    """Linear-interpolated percentile over an already-sorted list."""
    k = (len(xs) - 1) * p
    lo = int(k)
    hi = min(lo + 1, len(xs) - 1)
    return xs[lo] + (xs[hi] - xs[lo]) * (k - lo)


def period_floor_applies(spans, declared: int) -> bool:
    """Whether the CONDITIONAL FLOOR applies to this window — see `observed_period`.

    `spans` is (gap seconds, interval recorded on the arm that started it). The floor
    needs both a RISE and the absence of a DISPROOF:

      - **Rise, recorded** — some gap was armed under a recorded interval SHORTER than
        the one declared now. This is the precise case and it catches a transition on
        its first tick, when only one arm carries the new interval.
      - **Rise, by gap** — the window's p75 gap already reaches half the declared
        interval. A LEGACY window carries no interval at all, so the recorded test
        cannot see anything in it: every one of the 118 `.arms` files on this machine
        is single-column today, and without this limb the floor could not fire on the
        subject that motivated it. The p75 is what separates the two shapes — on the
        measured histories the right-skewed subject's p75 is 3642 s against a declared
        3600, while `goal--deploy-and-merge-gates` (declared 3600, really ticking at
        ~900) has a p75 of 994 s, and one historic stop cannot lift a p75 the way it
        lifts a max.
      - **Disproof** — a completed gap armed under the declared interval that came in
        under half of it. That proves the manager does not actually tick at the
        interval it declares, so the floor would only hide a lapse.

    A manager ticking steadily at the interval it declares is unaffected either way:
    its p75 is about `declared`, so the floored period and the uncapped one agree.
    """
    recorded_rise = any(iv is not None and iv < declared for _, iv in spans)
    gaps = sorted(gap for gap, _ in spans)
    gap_rise = percentile(gaps, 0.75) >= declared / 2
    if not (recorded_rise or gap_rise):
        return False
    return not any(iv == declared and gap < declared / 2 for gap, iv in spans)


def observed_period(s: str):
    """The p90 ROUND-to-round gap, capped at PERIOD_CAP x the median; None without history.

    Head beats only: a tail beat is the same round's closing write, and counting it
    would make the population bimodal (see the filter below).

    The MEDIAN alone is what a healthy tick costs, so a limit built from it fires on
    every ordinary slow tick: measured 2026-10-20 over the retained `.arms` history,
    5.02 events/manager-day against the old 2x rule's 1.35, and 33 of the 46 extra
    events were near-misses (inside 1.5x the median), i.e. jitter rather than lapses.

    The UNCAPPED p90 is the other failure: one historic stop stays in the retained
    window, so a subject that once sat down for hours would keep a limit ~13x its own
    median for ever. The cap bounds that while leaving the p90 to absorb jitter.

    The CONDITIONAL FLOOR covers the third failure. A manager that LENGTHENS its
    interval (900 s -> 3600 s) keeps the old short gaps in its 12-arm window, so the
    cap is taken from a cadence it no longer runs and the limit lands BELOW the
    interval it just declared — every healthy tick then reads STALE until the window
    rolls. While `period_floor_applies()` holds, the period is floored at the declared
    interval and the cap base is `max(median, declared)`.

    It is conditional, and both conditions are load-bearing. Without the rise test the
    floor would lift `goal--deploy-and-merge-gates-become-standing-allow-rules` — which
    declares 3600 and really ticks at ~900 — from a 1740 s limit to 3780 s and hide its
    lapse for an hour. Without the DISPROOF test a rise followed by a manager that
    keeps ticking at the old cadence would stay floored for the eleven ticks it takes
    the old gaps to age out. Together they bound the floor to the transition itself.
    """
    # ROUND-to-round, never beat-to-beat. Since the tail step a round writes a head
    # beat and a closing tail beat, so the full population alternates `D` and `I-D`.
    # The MEDIAN then collapses to the smaller of the two, which puts the 2.5x cap
    # BELOW the round-to-round gap it has to cover. Measured on a 900 s interval with
    # 120 s rounds: median 120, cap 300, p90 780 -> period 300, limit 480, against a
    # 900 s gap — a false STALE on every quiet round, which is exactly the defect the
    # tail beat exists to remove. A tail beat is the SAME round's closing write, so
    # its gap is intra-round and stays out of the population entirely.
    #
    # A ring with too few heads returns None rather than falling back to the mixed
    # population: None means "no observed cadence", and every caller then uses the
    # DECLARED interval plus SLACK — the safe direction, and the honest answer for a
    # ring that has not yet accumulated four complete rounds.
    heads = [(e, iv) for e, iv, tail in read_arm_pairs(s)[-ARMS_KEEP:] if not tail]
    if len(heads) < 4:
        return None
    pairs = heads
    spans = [(b[0] - a[0], a[1]) for a, b in zip(pairs, pairs[1:])]
    if len(spans) < 3:
        return None
    gaps = sorted(gap for gap, _ in spans)
    p90 = percentile(gaps, 0.90)
    med = median(gaps)
    declared = declared_interval(s)
    if declared and period_floor_applies(spans, declared):
        return max(declared, min(p90, PERIOD_CAP * max(med, declared)))
    return min(p90, PERIOD_CAP * med)


# ⚠️ `stopped` is NOT here, and its absence is the fix this list is part of. Migration
# used to DELETE a pre-split stand-down, so the first `--arm` after the upgrade erased a
# deliberate stop through the legacy path even once `arm()` stopped touching the marker —
# the same defect one surface out. A legacy `.stopped` is MIGRATED (renamed to its
# prefixed twin, see `migrate_legacy`) and never removed.
LEGACY_EXTS = ("cadence", "arms", "reset")
GOALS_DIR = "24 Goals"
DEFAULT_TOPICS_DIR = "23 Topics"
# The page folders `--state` accepts as proof a subject exists. `private-personal` /
# `seibert-*` keep goals in `24 Goals`, the `24 Tasks` vaults in `23 Goals` — the same pair
# `scripts/close-worker.py` resolves.
PAGE_DIRS = (
    ("goal", "24 Goals"),
    ("goal", "23 Goals"),
    ("topic", DEFAULT_TOPICS_DIR),
    ("task", "25 Tasks"),
    ("task", "24 Tasks"),
    ("task", "tasks"),
)
# The task folders `detect_branch()` probes. Same pair as `owner.py`'s `SUBJECT_DIRS["task"]`.
TASK_DIRS = ("25 Tasks", "24 Tasks", "tasks")
# What counts as "a record exists on this branch" — the files `--arm` / `--stop` /
# `--sleep` write. `.arms` alone is history, never written without a `.cadence`.
RECORD_EXTS = ("cadence", "stopped", "sleeping")


def record_branches(subject: str) -> list:
    """Every branch holding a prefixed record for `subject`, in BRANCHES order."""
    base = base_slug(subject)
    return [
        b for b in BRANCHES
        if any(os.path.exists(path(f"{b}--{base}", ext)) for ext in RECORD_EXTS)
    ]


def page_kinds(subject: str, vault: str) -> set:
    """EVERY tier with a `<vault>/<page dir>/<subject>.md` (exact name, any case).

    Exact basename, never a substring — `docs/subject-resolution.md` § The page test: a
    topic's member goals are named after it, so a wildcard resolves one label in both
    folders. Case-insensitive for the same reason that doc uses `-iname`.

    ⚠️ **A SET, not the first match.** Goal and topic pages are named independently, so one
    name routinely has a page in both folders — and the tier question below is "does THIS
    subject exist on the branch I probed", which a goal-first first-match cannot answer.
    """
    root = os.path.abspath(os.path.expanduser(vault))
    want = subject.lower() + ".md"
    found = set()
    for kind, folder in PAGE_DIRS:
        try:
            if any(e.lower() == want for e in os.listdir(os.path.join(root, folder))):
                found.add(kind)
        except OSError:
            continue
    return found


def page_kind(subject: str, vault: str):
    """The first tier `page_kinds` reports, in `PAGE_DIRS` order — or None."""
    kinds = page_kinds(subject, vault)
    for kind, _ in PAGE_DIRS:
        if kind in kinds:
            return kind
    return None


def state(subject: str, branch, vault: str, now: float) -> int:
    """`--state`: print `<STATE>\\t<slug>\\t<detail>` and exit 0, or refuse with EXIT_UNRESOLVED.

    `UNARMED` is a claim about a SUBJECT — "nothing ever armed it" — so it is printed only
    once the subject is known to exist. Two answers used to collapse into it:

      * **A record on another branch** → `WRONG-BRANCH`. Measured 2026-11-07: a goal-armed,
        stood-down subject probed with `--branch topic` read `UNARMED`, while `--branch goal`
        read `STOPPED`. The record was right; the probe named the wrong tier.
      * **No record anywhere and no page** → a refusal on stderr, exit 3, nothing on stdout.
        An abbreviated label (`"No Session Sits Idle"`) slugs to a different base, finds no
        record, and read `UNARMED` for a manager that was armed and `OK` under its full name.

    `--branch fleet` skips the page test: the fleet manager manages no page by design.

    ⚠️ **A record on another branch is only a wrong-branch probe when the subject's own page
    says that branch is its tier.** Goal and topic pages are named independently, so two
    different subjects routinely share a name — a never-armed goal whose name an armed topic
    also uses must still read `UNARMED`, not `WRONG-BRANCH`, or `commands/steps/round-liveness.md`
    never arms it. `page_kind`'s KIND is what decides: no page anywhere is the one case where
    the other branch's record is the only evidence the subject exists at all.
    """
    branch = branch or detect_branch(subject, vault)
    s = slug(subject, branch)
    st, detail = classify(s, now)
    if st == "UNARMED" and branch != "fleet":
        kinds = page_kinds(subject, vault)
        elsewhere = [b for b in record_branches(subject) if b != branch]
        # ⚠️ **A page ON THE PROBED BRANCH settles it: the subject exists here and was never
        # armed.** Another tier holding a same-named page AND a record is a DIFFERENT subject
        # that happens to share the name — reporting WRONG-BRANCH for it would leave this
        # one's loop unarmed forever, which is the failure the tier check exists to prevent.
        if branch in kinds:
            elsewhere = []
        tier = (sorted(kinds - {branch}) or elsewhere or [None])[0]
        if elsewhere and tier in elsewhere:
            st, detail = "WRONG-BRANCH", f"record is on branch {tier}" + (
                f" (also {', '.join(b for b in elsewhere if b != tier)})"
                if len(elsewhere) > 1 else ""
            )
        elif not kinds and not elsewhere:
            root = os.path.abspath(os.path.expanduser(vault))
            print(
                f"manager-liveness: --state refused: {subject!r} resolves no record under any "
                f"branch and no page under {root} ('24 Goals/', '23 Goals/', '23 Topics/', "
                "'25 Tasks/', '24 Tasks/') — an "
                "abbreviated or mistyped subject is not an unarmed one; pass the full page name "
                "and --vault <vault root>",
                file=sys.stderr,
            )
            return EXIT_UNRESOLVED
    print(f"{st}\t{s}\t{detail}")
    return 0


def detect_branch(subject: str, vault: str = ".") -> str:
    """The branch for `subject`, or `goal` when nothing resolves.

    Three sources, in order, because the caller may be a manager that has never
    resolved a page in this process:

      1. **An existing prefixed record** — a manager already running under
         `goal--<base>`, `topic--<base>` or `fleet--<base>` keeps its branch, whatever the
         page says now. `fleet` is checked like the others because the Fleet Manager arms a
         loop and manages no page: it has no other way to be resolved.
         ⚠️ The order is goal → topic → fleet, so a goal or topic literally named
         "Fleet Manager" shadows the fleet record. The arm path is unaffected — every arm
         passes `--branch fleet` explicitly — but a branch-less call (`commands/stop.md`
         runs `--stop --topic <subject>` with no branch) would mark the wrong record. The
         goal-vs-topic ambiguity of the same shape predates this branch.
      2. **The vault's pages** — `24 Goals/<subject>.md` is a goal; the topics folder
         holding `<subject>.md` is a topic.
      3. **`goal`** — the default, announced on stderr so a wrong guess is visible
         rather than silent. It is the common case: a goal manager is the tier that
         arms a loop.
    """
    base = base_slug(subject)
    for branch in BRANCHES:
        if os.path.exists(path(f"{branch}--{base}", "cadence")) or os.path.exists(
            path(f"{branch}--{base}", "stopped")
        ):
            return branch
    root = os.path.abspath(os.path.expanduser(vault))
    for name in (subject + ".md",):
        if os.path.isfile(os.path.join(root, GOALS_DIR, name)):
            return "goal"
    topics = os.path.join(root, DEFAULT_TOPICS_DIR)
    try:
        if any(e.lower() == subject.lower() + ".md" for e in os.listdir(topics)):
            return "topic"
    except OSError:
        pass
    # A task page is proof the subject exists, and `task` is a tier since `/supervisor:stop`
    # gained its by-name form. Resolved AFTER goal and topic, so a task and a goal sharing a
    # title still prefers the goal — the ambiguity that predates this tier.
    for task_dir in TASK_DIRS:
        try:
            if any(e.lower() == subject.lower() + ".md" for e in os.listdir(os.path.join(root, task_dir))):
                return "task"
        except OSError:
            pass
    print(f"branch=goal (defaulted — no page resolved for {subject!r})", file=sys.stderr)
    return "goal"


def migrate_legacy(subject: str, s: str = None) -> None:
    """Retire a pre-split unprefixed record once its prefixed twin is written.

    ⚠️ Without this, `--check` keeps reading the flat `<base>.cadence` the old
    release wrote, and every manager that re-arms under the new name leaves it to age
    into a false STALE — measured 2026-10-20: **55** unprefixed records against **0**
    prefixed ones, so the first `--check` after the split would have reported every
    live manager as lapsed.

    ⚠️ **A legacy `.stopped` is RENAMED, never removed.** Deleting it was the same
    defect the arm path fixes, one surface out: a manager stood down before the split
    would have had its marker destroyed by the first arm after the upgrade, and read
    STALE for a stop that was still in force. The rename keeps the stand-down, retires
    the unprefixed file, and leaves it clearable by `--arm --resume` like any other.
    """
    base = base_slug(subject)
    removed = []
    for ext in LEGACY_EXTS:
        legacy = path(base, ext)
        if os.path.exists(legacy):
            try:
                os.remove(legacy)
                removed.append(ext)
            except OSError:
                pass
    if removed:
        print(f"migrated legacy {base} ({', '.join(removed)})", file=sys.stderr)
    legacy_stop = path(base, "stopped")
    if s and os.path.exists(legacy_stop):
        prefixed_stop = path(s, "stopped")
        try:
            if os.path.exists(prefixed_stop):
                os.remove(legacy_stop)
            else:
                os.rename(legacy_stop, prefixed_stop)
            print(f"migrated legacy {base}.stopped -> {s}.stopped", file=sys.stderr)
        except OSError:
            pass


def read_notified() -> dict:
    """slug -> the epoch at which that lapse was reported.

    A plain list is the shape older releases wrote; those entries load at the
    file's own mtime (see below), so the upgrade reports no subject twice and a
    manager that re-arms afterwards still re-notifies on its next lapse.
    """
    try:
        with open(os.path.join(STATE_DIR, NOTIFIED), encoding="utf-8") as fh:
            raw = json.load(fh)
    except (OSError, ValueError):
        return {}
    if isinstance(raw, dict):
        out = {}
        for k, v in raw.items():
            try:
                out[str(k)] = float(v)
            except (TypeError, ValueError):
                out[str(k)] = 0.0
        return out
    # Legacy list: stamp each entry with the file's own mtime rather than 0. At 0 the
    # upgrade would re-announce every subject that was already known-lapsed — a
    # notification burst on the first tick after deploy. A subject that armed after
    # that write still re-notifies, which is the lapse this change exists to catch.
    try:
        legacy_epoch = os.path.getmtime(os.path.join(STATE_DIR, NOTIFIED))
    except OSError:
        legacy_epoch = 0.0
    return {str(k): legacy_epoch for k in raw}


def write_notified(notified: dict) -> None:
    write_atomic(
        os.path.join(STATE_DIR, NOTIFIED),
        json.dumps({k: notified[k] for k in sorted(notified)}) + "\n",
    )


def iso(epoch: float) -> str:
    """An epoch as the UTC ISO-8601 second the ledger and the sender both speak."""
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch))


def read_pending() -> dict:
    """slug -> the lapse owed a report: {detected_at, detail, attempts, last_error, emits}.

    ⚠️ `detected_at` is the CROSSING time, and it is what the sender puts in the report
    and what `--delivered` moves into `notified`. A poll timestamp would differ between
    the failed attempt and the retry, so the same lapse would look like two.

    ⚠️ `emits` counts how many times this crossing has been PRINTED, so the first
    emission can be told apart from a retry of it. It is not `attempts`, which counts
    failed DELIVERIES — a poll that prints and succeeds still increments `emits`.
    """
    try:
        with open(os.path.join(STATE_DIR, PENDING), encoding="utf-8") as fh:
            raw = json.load(fh)
    except (OSError, ValueError):
        return {}
    if not isinstance(raw, dict):
        return {}
    out = {}
    for k, v in raw.items():
        if not isinstance(v, dict):
            continue
        try:
            detected = float(v.get("detected_at", 0.0))
        except (TypeError, ValueError):
            detected = 0.0
        try:
            attempts = int(v.get("attempts", 0) or 0)
        except (TypeError, ValueError):
            attempts = 0
        try:
            emits = int(v.get("emits", 0) or 0)
        except (TypeError, ValueError):
            emits = 0
        out[str(k)] = {
            "detected_at": detected,
            "detail": str(v.get("detail", "")),
            "attempts": attempts,
            "last_error": v.get("last_error"),
            "emits": emits,
            # Producer identity, carried through every rewrite: a normalising reader
            # that dropped these would make the record indistinguishable from one
            # written by hand, which is the property they exist to give it.
            "pid": v.get("pid"),
            "row_ts": v.get("row_ts"),
        }
    return out


def write_pending(pending: dict) -> None:
    write_atomic(
        os.path.join(STATE_DIR, PENDING),
        json.dumps({k: pending[k] for k in sorted(pending)}) + "\n",
    )


def delivered(s: str) -> int:
    """Ack ONE lapse: it moves from owed to reported. Written ONLY by the sender.

    The ack is the one event that proves the transport carried the report, so it is the
    only thing allowed to stop `--check` re-emitting the lapse. It stores the CROSSING
    time rather than now: a manager that re-armed while the report was owed has a newer
    arm than the crossing, so `notified[s] < last_arm(s)` still starts a new lapse.
    """
    pending = read_pending()
    entry = pending.pop(s, None)
    if entry is None:
        # Idempotent by design: the sender acks every slug it was told about, and a slug
        # can be superseded between the check and the ack.
        print(f"not-pending {s}")
        return 0
    notified = read_notified()
    notified[s] = entry["detected_at"]
    write_pending(pending)
    write_notified(notified)
    print(f"delivered {s}")
    return 0


def failed(s: str, error: str, pid: str = None, row_ts: str = None) -> int:
    """Record ONE failed delivery attempt. The entry STAYS owed — that is the retry.

    ⚠️ `pid` and `row_ts` are the PRODUCER's identity: the sender's own process id and
    the timestamp of the ledger row it wrote in the same run. They exist so a durable
    record can be shown to have been written by a real failed send rather than by hand —
    a hand-written file reproduces every other field exactly.
    """
    pending = read_pending()
    entry = pending.get(s)
    if entry is None:
        print(f"not-pending {s}")
        return 0
    entry["attempts"] = int(entry.get("attempts", 0)) + 1
    entry["last_error"] = (error or "")[:500]
    if pid:
        entry["pid"] = str(pid)[:40]
    if row_ts:
        entry["row_ts"] = str(row_ts)[:40]
    write_pending(pending)
    print(f"failed {s} attempts={entry['attempts']}")
    return 0


def last_arm(s: str) -> float:
    """When the subject last armed, from its cadence file's mtime; 0 when unreadable."""
    p, _ = resolve(s, "cadence")
    try:
        return os.path.getmtime(p)
    except OSError:
        return 0.0


def read_sleep(s: str):
    """`(epoch, text)` for `<slug>.sleeping`, or `(None, None)`.

    ⚠️ **An unreadable or empty marker reads as ABSENT, never as a future instant.**
    This module fails toward ARMED everywhere else, and the same asymmetry applies here:
    an unreadable sleep must not suppress a real `STALE` report, because a lapsed manager
    hidden behind a corrupt file is exactly the loss the liveness layer exists to catch.
    """
    p, _ = resolve(s, "sleeping")
    try:
        with open(p, encoding="utf-8") as fh:
            text = fh.read().strip()
    except OSError:
        return None, None
    if not text:
        return None, None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp(), text
    except ValueError:
        return None, None


def sleep(subject: str, branch: str, until: str) -> int:
    """Record a sleep until `until` — a date-gated subject waiting on its own clock.

    Distinct from `--stop` on purpose. A stand-down says "nothing left to drive"; a sleep
    says "nothing can happen before this instant". They render differently to every reader
    (SLEEPING vs STOPPED), and a sleep expires by itself while a stop needs a human.
    """
    s = slug(subject, branch)
    try:
        datetime.fromisoformat(until.replace("Z", "+00:00"))
    except ValueError:
        print(f"manager-liveness: --until is not an ISO 8601 instant: {until}", file=sys.stderr)
        return 2
    write_atomic(path(s, "sleeping"), until + "\n")
    migrate_legacy(subject, s)
    print(f"sleeping {s} until {until}")
    return 0



# The reach thread, joined at exit so a short-lived CLI cannot drop the write.
_REACH_THREAD = None


def join_reach_thread(timeout=20):
    """Wait for the reach emission to finish, bounded. Never raises."""
    if _REACH_THREAD is not None and _REACH_THREAD.is_alive():
        _REACH_THREAD.join(timeout=timeout)


def emit_wrapper_reach():
    """Emit this session's `wrapper` answer-reach, best-effort, beside the arm.

    ⚠️ **Folded into `--arm` on purpose.** The wrapper leg is a Bash `curl` plus a
    recorder call, and across days of measurement managers ran the tool call and not
    these two commands — 4 fresh `mcp` reaches against 1 fresh `wrapper` reach on
    2026-11-13. An instruction the fleet reliably does not follow is not a gate it can
    pass, so the arm emits it instead: one command, no extra step to skip.

    ⚠️ **This certifies a WEAKER claim than the manual curl did, and that is a deliberate
    choice.** `scripts/answer-reach.py`'s docstring says the wrapper leg exists to prove the
    session's *answer* command reached the wrapper, and the permission classifier gates that
    command. Folding the call in here makes the gated command `manager-liveness.py --arm` —
    which must be allowed every round for the loop to run at all. So a reach now says "this
    arm could POST to the answer route", not "this manager's own answer command is allowed".
    It still separates a session whose wrapper is unreachable from one whose is not, and it
    still writes nothing when the classifier denies the arm. Operators chose this over an
    instruction the fleet reliably did not follow (4 fresh `mcp` reaches against 1 fresh
    `wrapper` reach, 2026-11-13).

    ⚠️ **Best-effort, never fatal.** An arm is the liveness reading and must succeed
    even when the wrapper is down, so every failure here is reported and swallowed.
    A missing session id, an unreachable wrapper and a non-404/409 status all leave
    the arm intact and simply write no reach — which is the correct signal for the
    probe, since this session then has not shown it can reach the route.
    """
    session_id = os.environ.get("CLAUDE_CODE_SESSION_ID", "")
    if not session_id:
        return
    raw = os.environ.get("SUPERVISOR_WRAPPER_URL", "http://localhost:18090")
    # ⚠️ An ORIGIN, with no path. `config.mjs` resolves the same variable to `url.origin`,
    # so a value carrying a path would make this call hit `<path>/api/1.0/...` and get a
    # route-level 404 — which `answer-reach.py` accepts as a valid reach, certifying a
    # route never reached. `rstrip("/")` does not enforce that; `urlsplit` does.
    parts = urllib.parse.urlsplit(raw)
    base = f"{parts.scheme}://{parts.netloc}" if parts.scheme and parts.netloc else ""
    if not base:
        print(f"reach: SUPERVISOR_WRAPPER_URL {raw!r} is not an origin — no wrapper reach written", file=sys.stderr)
        return
    body = b'{"request_id": "answer-path-reach-sentinel", "behavior": "allow"}'
    try:
        # ⚠️ Built INSIDE the try: a malformed value raises ValueError here, and an
        # uncaught traceback from a background thread is not "reported and swallowed".
        request = urllib.request.Request(
            f"{base}/api/1.0/sessions/{urllib.parse.quote(session_id, safe='')}/answer",
            data=body,
            headers={"content-type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=10) as response:
            status = response.status
    except urllib.error.HTTPError as err:
        status = err.code
    except (urllib.error.URLError, OSError, ValueError) as err:
        print(f"reach: wrapper call failed ({err}) — no wrapper reach written", file=sys.stderr)
        return
    recorder = os.path.join(os.path.dirname(os.path.abspath(__file__)), "answer-reach.py")
    try:
        subprocess.run(
            [sys.executable, recorder, "--session-id", session_id, "--status", str(status)],
            check=False, capture_output=True, timeout=20,
        )
    except (OSError, subprocess.SubprocessError) as err:
        print(f"reach: recorder could not run ({err}) — no wrapper reach written", file=sys.stderr)


def arm(subject: str, branch: str, interval: int, resume: bool = False,
        beat: str = "head") -> int:
    """Record a re-arm. **Never** resumes a stood-down subject unless `resume` is set.

    ⚠️ **The arm does not touch `<slug>.stopped`, and that is the whole point.** An arm
    runs at EVERY round — it is the liveness reading, not a decision about the subject —
    so an arm that cleared the marker made taking a measurement destroy the evidence
    that the subject had deliberately stopped. Measured 2026-11-07: stood down at 12:13,
    read at 12:51, marker gone, the Fleet Manager read STALE and restarted the session at
    13:14. Only `--resume` — the explicit "this subject is being restarted" verb, passed
    by the two loop-start arms — clears it.
    """
    s = slug(subject, branch)
    write_atomic(path(s, "cadence"), f"{interval}\n")
    # ⚠️ **Fire-and-forget, in a daemon thread.** `arm()` is the liveness READING: the
    # epochs it records are what `observed_period` measures, so a network call on this
    # path would lengthen every arm-to-arm gap by its own latency and drift the period
    # it is measuring. Measured 2026-11-13: an inline call moved an observed period from
    # 2250s to 2251s and broke a test that asserts exact seconds. The reach is
    # best-effort and must never delay or fail the reading.
    global _REACH_THREAD
    _REACH_THREAD = threading.Thread(target=emit_wrapper_reach, daemon=True)
    _REACH_THREAD.start()
    # A re-arm after a stand-down starts a NEW cadence sample: the outage gap is not
    # cadence. That cannot be inferred from the marker any more — the arm preserves it —
    # so `--stop` leaves a separate `.reset` epoch and this is the one arm that consumes
    # it. Consumed, not merely read: the SECOND arm after a stop must keep its history,
    # or `observed_period` would never return and every subject would sit on its
    # reported interval for ever.
    # The ring is rebuilt from (epoch, interval) PAIRS, never from `read_arms()`: that
    # returns epochs only, so rebuilding from it would strip every earlier line's
    # interval and the conditional floor in `observed_period()` could never fire on a
    # live subject. A legacy line keeps its single-column shape — inventing an interval
    # for it would floor a subject that never lengthened anything.
    reset_path, _ = resolve(s, "reset")
    has_reset = os.path.exists(reset_path)
    prior = [] if has_reset else read_arm_pairs(s)
    now = time.time()
    # `beat` arrives as a string ("head" / "tail"); store a bool so every entry in the
    # ring carries the same shape and `if b` cannot read "head" as marked.
    arms = (prior + [(now, interval, beat == "tail")])[-ARMS_KEEP:]
    # The ring keeps TWO columns: the tail marker moved to `<slug>.tail`, because a
    # session still running a pre-upgrade copy rebuilds this ring from two-tuples and
    # erases a third-column marker on every arm it takes. A legacy line (no interval)
    # keeps its single-column shape.
    #
    # The store is rebuilt from `arms`' own beat flags, so it stays bounded by
    # ARMS_KEEP — and those epochs SURVIVE the two-tuple rebuild, which is what makes
    # the marker outlive it. `read_arm_pairs` already ORs a stored marker into the
    # flag, so this is the union of store-marked and legacy column-marked arms,
    # pruned to the ring. Seeding from the store alone would be wrong: this writer
    # drops the legacy third column, so a pre-move marker not carried over here would
    # be erased by the very arm meant to preserve it.
    tail_keys = {f"{epoch:.0f}" for epoch, _, b in arms if b}
    if beat == "tail":
        tail_keys.add(f"{now:.0f}")
    write_atomic(path(s, "arms"), "".join(
        f"{epoch:.0f}\n" if iv is None else f"{epoch:.0f} {iv}\n"
        for epoch, iv, _ in arms
    ))
    write_tail_epochs(s, tail_keys)
    # BEFORE the reset is consumed, so a pre-split `.reset` is migrated (and reported)
    # rather than silently deleted by the consumption a line below.
    migrate_legacy(subject, s)
    if has_reset:
        try:
            os.remove(reset_path)
        except OSError:
            pass
    if resume:
        stop_path, _ = resolve(s, "stopped")
        try:
            os.remove(stop_path)
        except FileNotFoundError:
            pass
    # An arm ENDS a sleep: the manager woke up (or a human woke it), so the marker that
    # suppressed the STALE report must not outlive the state it described.
    try:
        os.remove(path(s, "sleeping"))
    except FileNotFoundError:
        pass
    print(f"armed {s} interval={interval}s" + (" resumed" if resume else ""))
    return 0


def stop(subject: str, branch: str) -> int:
    s = slug(subject, branch)
    write_atomic(path(s, "stopped"), time.strftime("%Y-%m-%dT%H:%M:%S%z") + "\n")
    # The epoch an arm consumes to start a fresh cadence sample. Written here rather
    # than derived from the marker's mtime because the marker now OUTLIVES the arm, so
    # "when did this subject stop" and "has the history been reset" are two different
    # questions and the second must be answered once, not once per arm.
    write_atomic(path(s, "reset"), f"{time.time():.0f}\n")
    migrate_legacy(subject, s)
    print(f"stopped {s}")
    return 0


def stood_down_past(minutes: float, now: float | None = None) -> int:
    """Every subject whose stand-down is older than MINUTES: `<age-seconds>\\t<slug>`.

    The selection half of the stood-down-manager close. `--stop` writes the marker and
    `--arm` clears it, so this script already owns both the path and the timestamp
    format; a caller that globbed the state dir itself would re-derive both, and drift
    the moment either changed.

    ⚠️ **The marker's existence is not the population.** Measured 2026-11-09: 83
    `<slug>.stopped` markers in the state dir, 41 of them on pages still carrying a
    `claude_session_id`, and only **2** whose session the registry read LIVE — the
    marker outlives the session it describes. This verb answers "how long has this
    subject read STOPPED", which is the half the close leg needs; the caller must still
    join it to session liveness and to the page before acting.

    ⚠️ **`SLEEPING` never appears here, and that is the point.** A sleep writes
    `<slug>.sleeping`, not `<slug>.stopped`, so a date-gated manager cannot be selected
    however long it waits — the exclusion is structural, not a filter a caller must
    remember to apply.

    Order: oldest first, so a leg that acts on a prefix acts on the longest-standing
    stand-downs. Exit 0 whenever the scan completed — an empty list is a real answer.
    Exit 2 when the state dir cannot be read; never 0 on a failed scan.
    """
    now = time.time() if now is None else now
    bound = minutes * 60
    try:
        names = os.listdir(STATE_DIR)
    except OSError as e:
        print(f"state dir unreadable: {e}", file=sys.stderr)
        return 2
    rows = []
    for name in names:
        if not name.endswith(".stopped"):
            continue
        s = name[: -len(".stopped")]
        try:
            with open(os.path.join(STATE_DIR, name), encoding="utf-8") as fh:
                text = fh.read().strip()
            when = datetime.strptime(text, "%Y-%m-%dT%H:%M:%S%z").timestamp()
        except (OSError, ValueError):
            # An unreadable marker is not a close candidate: report it, never guess an age.
            print(f"unreadable stopped marker: {name}", file=sys.stderr)
            continue
        age = now - when
        if age > bound:
            rows.append((age, s))
    rows.sort(reverse=True)
    for age, s in rows:
        print(f"{int(age)}\t{s}")
    return 0


def gate_watched(s: str, now: float):
    """`(subject_age, loop_alive)` from the detached gate's heartbeats, or `(None, None)`.

    The detached sweep gate writes one `<base>.heartbeat` per subject on every tick,
    model-free. A goal manager has no model cadence any more, so its `.cadence` ages
    between gate wakes; the gate heartbeat is the live signal. `loop_alive` says whether
    ANY subject was ticked within `GATE_ALIVE` (a dead loop), and `subject_age` says how
    long ago THIS subject was ticked (a subject dropped from enumeration, whose old
    heartbeat would otherwise read as watched forever). The two are separate so the
    caller can tell "the gate is down" from "the gate is up but not watching this
    subject": the first must not restart-storm the tier, the second is a real lapse.

    ⚠️ **Known limitation: the match is by bare filename across every vault directory.**
    The liveness namespace is deliberately flat (see `STATE_DIR`'s note), and `classify`
    has no vault to scope by, so a same-named goal in another vault can keep this
    subject's heartbeat fresh for up to `GATE_SUBJECT_FRESH`. Bounded, not new.
    """
    base = s.split("--", 1)[1] if "--" in s else s
    subject_hb = None
    newest = None
    try:
        for vdir in os.listdir(GATE_LOOP_STATE_DIR):
            d = os.path.join(GATE_LOOP_STATE_DIR, vdir)
            if not os.path.isdir(d):
                continue
            for f in os.listdir(d):
                if not f.endswith(".heartbeat"):
                    continue
                try:
                    m = os.path.getmtime(os.path.join(d, f))
                except OSError:
                    continue
                newest = m if newest is None else max(newest, m)
                if f == f"{base}.heartbeat":
                    subject_hb = m if subject_hb is None else max(subject_hb, m)
    except OSError:
        return (None, None)
    if subject_hb is None or newest is None:
        return (None, None)
    return (int(now - subject_hb), (now - newest) <= GATE_ALIVE)


def classify(s: str, now: float) -> tuple:
    """(state, detail) for one subject: OK, STALE, STOPPED, SLEEPING, UNARMED or INVALID.

    ⚠️ `UNARMED` and `INVALID` are different answers and must never be collapsed.
    `INVALID` means a record EXISTS and cannot be read — a probe failure, the
    manager's state is unknown. `UNARMED` means there is no record under either
    the prefixed or the pre-split name — no manager has ever armed this subject,
    which is a fact about the fleet, not a failure to measure it. Measured
    2026-10-20: every never-armed goal read `INVALID`, so a reader could not tell
    "needs a manager started" from "cannot tell".
    """
    stopped_path, stopped_legacy = resolve(s, "stopped")
    if os.path.exists(stopped_path):
        return "STOPPED", (
            "marker present (legacy record)" if stopped_legacy else "marker present"
        )
    # SLEEPING, checked BEFORE the cadence age. A date-gated subject does not re-arm
    # until its instant, so its cadence file ages far past the stale limit by design —
    # measuring it as a lapse is the false STALE this state exists to remove. Once the
    # instant has PASSED the marker stops suppressing anything and the normal age
    # arithmetic runs, so a manager that failed to wake still reads STALE.
    until, until_text = read_sleep(s)
    if until is not None and now < until:
        return "SLEEPING", f"until {until_text} ({int(until - now)}s remaining)"
    # A sleep marker that has EXPIRED (only `--arm` removes it) must stay STALE: the gate
    # cannot see a `defer_date` arrive (`manager-predispatch.digest_of` carries no
    # defer_date), so `topic-keepalive.py` recovers the manager off the STALE reading. The
    # gate-driven OK fallback below must not suppress that, or a date-gated goal manager
    # sleeps for ever.
    # Keyed on the FILE's existence, not the parsed instant: an unparseable `.sleeping`
    # marker reads as absent from `read_sleep`, and keying on the parse would let the
    # gate-driven fallback return OK for a date-gated goal the gate cannot wake. The file
    # existing is the fact that a sleep was recorded; its contents are only how long.
    sleep_path, _ = resolve(s, "sleeping")
    sleep_pending = os.path.exists(sleep_path)
    p, legacy = resolve(s, "cadence")
    try:
        with open(p, encoding="utf-8") as fh:
            interval = int(fh.read().strip())
        age = int(now - os.path.getmtime(p))
    except FileNotFoundError:
        return "UNARMED", "no cadence file under either name"
    except (OSError, ValueError) as exc:
        return "INVALID", f"unreadable cadence file ({exc})"
    # Measure against the manager's OBSERVED period, never the delay it reports. A
    # tick costs real time on top of the delay, so a limit built from the delay alone
    # sits below the true period and fires on a live manager every cycle. Measured
    # 2026-09-23: reported 300 s, real arm-to-arm 17 min — five false pushes in 32 min.
    #
    # ONE period, not two. A two-period limit reports a lapse only after the manager
    # has missed its next re-arm AND that whole cycle has elapsed again: the live
    # ledger shows every production report landing at 1860-2040 s against a 900-960 s
    # cadence, and a planted-lapse probe took 681 s on a 300 s cadence. One period
    # plus SLACK reports the first missed re-arm instead.
    period = observed_period(s)
    if period:
        limit, basis = period + SLACK, f"period={int(period)}s(observed)"
    else:
        limit, basis = interval + SLACK, f"period={interval}s(reported)"
    if legacy:
        # Say so: a pre-split record is a manager that has not re-armed since the
        # split, and the age is measured against a file nothing will refresh.
        basis += " legacy-record"
    detail = f"age={age}s limit={int(limit)}s {basis}"
    if age > limit:
        # Goal tier only: the detached sweep gate is its sole wake source, so an ageing
        # `.cadence` between gate wakes is expected, not a lapse. A fresh gate heartbeat
        # means the manager is watched; without it, STALE stands. An EXPIRED sleep marker
        # is the exception (see above): it stays STALE so `topic-keepalive.py` recovers it.
        if s.startswith("goal--") and not sleep_pending:
            subject_age, loop_alive = gate_watched(s, now)
            if loop_alive and subject_age is not None and subject_age <= GATE_SUBJECT_FRESH:
                return "OK", detail + f" gate-driven (gate tick {subject_age}s ago)"
            if loop_alive is False:
                # The gate is down: the manager cannot be woken, but restarting it will not
                # help, and reporting STALE here turns one dead gate into an uncapped
                # restart storm across the tier. The gate's own stale watcher alerts on the
                # outage; this reading names it without acting on it.
                return "OK", detail + " gate-down (no gate tick in the window)"
        return "STALE", detail
    return "OK", detail


def emit_lapse(state: str, subject: str, detail: str, entry: dict) -> None:
    """Print ONE emission of an owed lapse, marking a retry of an earlier one.

    ⚠️ The FIRST emission of a crossing is the firing; every later one is a RETRY of
    that same firing and carries `retry=1`. Both are printed and both exit 10 — the
    sender must keep retrying — but a consumer counting FIRINGS now gets one per
    episode instead of one per poll, which is the whole difference between a failure
    rate and a poll count.

    ⚠️ `retry=1` is a TRAILING token, never a prefix. The sender matches
    `^(STALE|INVALID|RECOVERED-PENDING) ` on line start and reads the subject from
    field 2 (`manager-liveness-notify-tick.sh:61-62`); a `RETRY STALE …` spelling
    would satisfy every test here and silently drop every retry from delivery.

    One home for both call sites: the STALE branch and the RECOVERED-PENDING branch
    print the same line shape, and a marker added to only one of them would leave the
    other inflating the count it exists to fix.
    """
    entry["emits"] = int(entry.get("emits", 0)) + 1
    retry = " retry=1" if entry["emits"] > 1 else ""
    print(f"{state} {subject} {detail} crossing={iso(entry['detected_at'])}{retry}")


def check(now: float = None) -> int:
    now = time.time() if now is None else now
    try:
        names = sorted(f[: -len(".cadence")] for f in os.listdir(STATE_DIR) if f.endswith(".cadence"))
    except FileNotFoundError:
        names = []
    # ⚠️ A pre-split record is superseded the moment its prefixed twin exists. Without
    # this skip, every manager that migrated leaves its old flat record behind and
    # `--check` reports it STALE forever while the manager is healthy — the same false
    # alarm `migrate_legacy` prevents on the write side, kept here as the read-side guard
    # for a record an older release already wrote and nothing will ever re-arm.
    prefixed_bases = {
        n.split("--", 1)[1] for n in names if "--" in n
    }
    names = [n for n in names if "--" in n or n not in prefixed_bases]
    notified = read_notified()
    before = dict(notified)
    pending = read_pending()
    before_pending = {k: dict(v) for k, v in pending.items()}

    turned_stale = False
    still = set()
    for s in names:
        state, detail = classify(s, now)
        if state in ("STALE", "INVALID"):
            still.add(s)
            # ⚠️ A pending entry recorded BEFORE the subject's last re-arm describes a
            # lapse the manager has already come back from; the newer lapse supersedes
            # it. Without this, a subject that armed while its report was owed would
            # keep re-announcing the old crossing.
            if s in pending and pending[s]["detected_at"] < last_arm(s):
                del pending[s]
            if s not in pending and (s not in notified or notified[s] < last_arm(s)):
                pending[s] = {
                    "detected_at": now,
                    "detail": detail,
                    "attempts": 0,
                    "last_error": None,
                    "emits": 0,
                }
            if s in pending:
                # ⚠️ Re-emitted on EVERY poll until a delivery is acked. This is the
                # retry: the transport is a network call that fails routinely, and a
                # single failed send used to mark the lapse reported for ever.
                emit_lapse(state, s, detail, pending[s])
                turned_stale = True
        elif s in pending:
            # The manager came back before anyone was told. The lapse still happened and
            # the operator is still owed the report, so it stays owed — but saying STALE
            # would be a lie, so it is named for what it is.
            emit_lapse("RECOVERED-PENDING", s, detail, pending[s])
            turned_stale = True
        elif s in notified:
            print(f"RECOVERED {s} {state} {detail}")
            del notified[s]
    notified = {k: v for k, v in notified.items() if k in still}
    if notified != before:
        write_notified(notified)
    if pending != before_pending:
        write_pending(pending)
    return EXIT_STALE if turned_stale else 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Model-free manager-loop liveness.")
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--arm", action="store_true", help="record a re-arm (manager, every tick)")
    mode.add_argument("--stop", action="store_true", help="mark a deliberate stand-down")
    mode.add_argument("--check", action="store_true", help="report subjects that turned stale")
    mode.add_argument(
        "--stood-down-past",
        metavar="MINUTES",
        type=float,
        help="every subject whose <slug>.stopped marker is older than MINUTES -> "
             "`<age-seconds>\\t<slug>`, oldest first. The selection half of the "
             "stood-down-manager close; a sleep writes .sleeping, so SLEEPING is "
             "excluded structurally. Exit 0 whenever the scan completed, 2 when the "
             "state dir cannot be read",
    )
    mode.add_argument(
        "--sleep",
        action="store_true",
        help="record a sleep until --until (a date-gated subject, waiting on its own clock)",
    )
    mode.add_argument(
        "--state",
        action="store_true",
        help="print ONE subject's state: OK / STALE / STOPPED / SLEEPING / UNARMED / INVALID / "
             "WRONG-BRANCH; exits 3 (stdout empty) for a subject with no record and no page",
    )
    mode.add_argument(
        "--delivered",
        metavar="SLUG",
        help="ack a lapse as DELIVERED — only this stops --check re-emitting it",
    )
    mode.add_argument(
        "--failed",
        metavar="SLUG",
        help="record one failed delivery attempt for SLUG (needs --error)",
    )
    ap.add_argument("--error", help="the transport error --failed records")
    ap.add_argument("--pid", help="--failed: the sender's pid, so the record names its producer")
    ap.add_argument("--row-ts", dest="row_ts", help="--failed: the ledger row's timestamp")
    ap.add_argument("--subject", help="the goal or topic name")
    ap.add_argument(
        "--topic",
        dest="topic_alias",
        help="DEPRECATED alias for --subject, kept so an armed loop's wakeup prompt "
             "from a pre-0.187 release keeps working",
    )
    ap.add_argument(
        "--branch",
        choices=BRANCHES,
        help="goal | topic | fleet | task — auto-detected when omitted (existing record, then the vault's pages)",
    )
    ap.add_argument(
        "--vault",
        default=".",
        help="vault ROOT: auto-detects --branch, and --state's page test for a subject with no record",
    )
    ap.add_argument(
        "--resume",
        action="store_true",
        help="with --arm: ALSO clear a stand-down marker. The one verb that resumes a "
             "stood-down subject; a round's ordinary liveness arm must never pass it",
    )
    ap.add_argument(
        "--beat",
        choices=("head", "tail"),
        default="head",
        help="with --arm: `head` (the default, the round's opening write) or `tail` "
             "(the round's closing write). Only the period reads the marker: a tail "
             "beat's gap is intra-round and is excluded from observed_period()",
    )
    ap.add_argument("--interval", type=int, help="the ScheduleWakeup delay just armed, seconds")
    ap.add_argument(
        "--until",
        help="the ISO 8601 instant a sleep ends (with --sleep) — the earliest defer_date",
    )
    a = ap.parse_args()
    if a.topic_alias and not a.subject:
        a.subject = a.topic_alias
        print("--topic is deprecated; use --subject", file=sys.stderr)
    elif a.topic_alias and a.subject and a.topic_alias != a.subject:
        ap.error("--topic and --subject disagree")
    if a.stood_down_past is not None:
        if a.stood_down_past <= 0:
            ap.error("--stood-down-past needs MINUTES > 0")
        return stood_down_past(a.stood_down_past)
    if a.check:
        return check()
    if a.delivered:
        return delivered(a.delivered)
    if a.failed:
        if not a.error:
            ap.error("--failed needs --error (the transport error it records)")
        return failed(a.failed, a.error, pid=a.pid, row_ts=a.row_ts)
    if a.state:
        if not a.subject:
            ap.error("--state requires --subject (or the deprecated --topic)")
        return state(a.subject, a.branch, a.vault, time.time())
    if not a.subject:
        ap.error("--subject (or the deprecated --topic) is required with --arm/--stop/--sleep")
    a.branch = a.branch or detect_branch(a.subject, a.vault)
    if a.arm:
        if not a.interval or a.interval <= 0:
            ap.error("--arm needs --interval > 0")
        code = arm(a.subject, a.branch, a.interval, resume=a.resume, beat=a.beat)
        # Bounded join so the reach lands before the CLI exits, without the network call
        # sitting on the arm's own path.
        join_reach_thread()
        return code
    if a.resume:
        # Resuming is an ARM, never a stop: the flag clears a marker, so silently
        # ignoring it on `--stop` would leave the operator believing they had resumed.
        ap.error("--resume only applies with --arm")
    if a.sleep:
        if not a.until:
            ap.error("--sleep needs --until <ISO 8601 instant>")
        return sleep(a.subject, a.branch, a.until)
    return stop(a.subject, a.branch)


if __name__ == "__main__":
    sys.exit(main())
