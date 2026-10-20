#!/usr/bin/env python3
"""Model-free liveness for manager loops: tell a lapsed manager apart from a stopped one.

A manager loop is one-shot per re-arm (`ScheduleWakeup`), so a missed re-arm (after
`/compact`, a crash, or a tick that ends without re-arming) stops the loop with no
signal. Nothing a peer session can probe (`ListAgents`, sockets, `ps`, transcript
mtime) tells a lapsed manager from a healthy one between ticks, so the manager leaves
evidence on disk itself. This script writes that evidence and checks it.

Four verbs, one file each, all in the sweep-gate state dir. **`<slug>` carries the
branch — `goal--<base>` or `topic--<base>`** — because a topic and a goal may share a
name, and a flat slug let one subject's cadence record overwrite the other's:



  --arm   --subject T --branch B --interval S
                                  <slug>.cadence = "<S>\\n" and an epoch appended to
                                    <slug>.arms; clears <slug>.stopped. Run by the
                                    manager at EVERY re-arm, so the cadence file's mtime
                                    marks the last re-arm that actually happened and
                                    <slug>.arms is the history the limit is measured
                                    against. The interval is recorded, never assumed:
                                    managers change it at runtime (a 20-min idle tick
                                    was set 2026-09-22), so a fixed limit false-positives.
  --stop  --subject T --branch B   <slug>.stopped. Run by `/supervisor:stop`, so a
                                    deliberately stood-down manager is not reported
                                    as a lapse. The next --arm clears it.
  --check                          every <slug>.cadence -> OK / STALE / STOPPED.
                                    STALE = no marker AND age > PERIOD + SLACK,
                                    where PERIOD is the p90 of the manager's observed
                                    arm-to-arm gaps capped at 2.5x their median (>=3
                                    gaps), falling back to the reported interval until
                                    that history exists. The p90 absorbs an ordinary
                                    slow tick; the cap stops one historic stop from
                                    inflating the limit for ever.
                                    A re-arm after a stand-down resets the history,
                                    so an outage gap never counts as cadence.
                                    Prints one line per subject that TURNED stale (or
                                    recovered) since the last check; exits 10 if any
                                    turned stale, else 0. The notified set lives in
                                    liveness-notified.json, so one lapse notifies
                                    once, not on every poll.
  --state --subject T --branch B   ONE subject's state, as `<STATE>\t<slug>\t<detail>`.
                                    `--check` structurally cannot answer this: it prints
                                    only the subjects that TURNED stale, so STOPPED -- the
                                    state a deliberate stand-down produces -- is invisible
                                    to it by construction. A reader that must tell a
                                    stood-down manager from a lapsed one asks about the
                                    subject directly, which is what this verb is for.
                                    Exits 0 whatever the state: the state IS the answer,
                                    and a non-zero exit would make a stood-down manager
                                    read as a failure to every `if` that wraps it.

`--check` reads files only: no model, no network. A host loop (launchd) runs it and
turns exit 10 into a push.
"""
import argparse, json, os, re, sys, time

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
BRANCHES = ("goal", "topic")
SLACK = 180  # seconds: the tick's own runtime plus ordinary jitter before a re-arm lands
PERIOD_CAP = 2.5  # the p90 period estimate never exceeds this multiple of the median
ARMS_KEEP = 12  # re-arm epochs retained per topic — enough for a stable percentile
EXIT_STALE = 10


def base_slug(subject: str) -> str:
    # Same base transform as sweep-gate.py and stop-probe.py, so the files sit side by side.
    return re.sub(r"[^a-z0-9]+", "-", subject.lower()).strip("-")


def slug(subject: str, branch: str) -> str:
    """The state slug, carrying the BRANCH: `goal--<base>` / `topic--<base>`.

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


def write_atomic(p: str, text: str) -> None:
    os.makedirs(STATE_DIR, exist_ok=True)
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.replace(tmp, p)


def read_arms(s: str) -> list:
    try:
        with open(path(s, "arms"), encoding="utf-8") as fh:
            return [float(x) for x in fh.read().split()]
    except (OSError, ValueError):
        return []


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


def observed_period(s: str):
    """The p90 arm-to-arm gap, capped at PERIOD_CAP x the median; None without history.

    The MEDIAN alone is what a healthy tick costs, so a limit built from it fires on
    every ordinary slow tick: measured 2026-10-20 over the retained `.arms` history,
    5.02 events/manager-day against the old 2x rule's 1.35, and 33 of the 46 extra
    events were near-misses (inside 1.5x the median), i.e. jitter rather than lapses.

    The UNCAPPED p90 is the other failure: one historic stop stays in the retained
    window, so a subject that once sat down for hours would keep a limit ~13x its own
    median for ever. The cap bounds that while leaving the p90 to absorb jitter.
    """
    gaps = observed_gaps(s)
    if gaps is None:
        return None
    return min(percentile(gaps, 0.90), PERIOD_CAP * median(gaps))


LEGACY_EXTS = ("cadence", "arms", "stopped")
GOALS_DIR = "24 Goals"
DEFAULT_TOPICS_DIR = "23 Topics"


def detect_branch(subject: str, vault: str = ".") -> str:
    """The branch for `subject`, or `goal` when nothing resolves.

    Three sources, in order, because the caller may be a manager that has never
    resolved a page in this process:

      1. **An existing prefixed record** — a manager already running under
         `goal--<base>` or `topic--<base>` keeps its branch, whatever the page says now.
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
    print(f"branch=goal (defaulted — no page resolved for {subject!r})", file=sys.stderr)
    return "goal"


def migrate_legacy(subject: str) -> None:
    """Retire a pre-split unprefixed record once its prefixed twin is written.

    ⚠️ Without this, `--check` keeps reading the flat `<base>.cadence` the old
    release wrote, and every manager that re-arms under the new name leaves it to age
    into a false STALE — measured 2026-10-20: **55** unprefixed records against **0**
    prefixed ones, so the first `--check` after the split would have reported every
    live manager as lapsed.
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


def last_arm(s: str) -> float:
    """When the subject last armed, from its cadence file's mtime; 0 when unreadable."""
    try:
        return os.path.getmtime(path(s, "cadence"))
    except OSError:
        return 0.0


def arm(subject: str, branch: str, interval: int) -> int:
    s = slug(subject, branch)
    write_atomic(path(s, "cadence"), f"{interval}\n")
    # A re-arm after a stand-down starts a NEW cadence sample. The outage gap is not
    # cadence, and it cannot be filtered at read time: the marker that records when
    # the stop happened is deleted a few lines below, so by the time `check` runs
    # there is nothing left to compare the gap against. Drop the stale history here.
    prior = [] if os.path.exists(path(s, "stopped")) else read_arms(s)
    arms = (prior + [time.time()])[-ARMS_KEEP:]
    write_atomic(path(s, "arms"), "".join(f"{v:.0f}\n" for v in arms))
    try:
        os.remove(path(s, "stopped"))
    except FileNotFoundError:
        pass
    migrate_legacy(subject)
    print(f"armed {s} interval={interval}s")
    return 0


def stop(subject: str, branch: str) -> int:
    s = slug(subject, branch)
    write_atomic(path(s, "stopped"), time.strftime("%Y-%m-%dT%H:%M:%S%z") + "\n")
    migrate_legacy(subject)
    print(f"stopped {s}")
    return 0


def classify(s: str, now: float) -> tuple:
    """(state, detail) for one topic: OK, STALE, STOPPED or INVALID."""
    if os.path.exists(path(s, "stopped")):
        return "STOPPED", "marker present"
    p = path(s, "cadence")
    try:
        with open(p, encoding="utf-8") as fh:
            interval = int(fh.read().strip())
        age = int(now - os.path.getmtime(p))
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
    detail = f"age={age}s limit={int(limit)}s {basis}"
    return ("STALE" if age > limit else "OK"), detail


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

    turned_stale = False
    still = set()
    for s in names:
        state, detail = classify(s, now)
        if state in ("STALE", "INVALID"):
            still.add(s)
            # ⚠️ An entry alone is not proof this lapse was already reported. A manager
            # may re-arm and lapse again between two polls, and then the RECOVERED
            # branch below never runs — the entry survives and every later lapse is
            # silent. An arm newer than the notification therefore starts a NEW lapse.
            # (Measured 2026-10-20: a subject read STALE for 72 min while --check
            # returned 0, and the topic had no live manager.)
            if s not in notified or notified[s] < last_arm(s):
                print(f"{state} {s} {detail}")
                turned_stale = True
                notified[s] = now
        elif s in notified:
            print(f"RECOVERED {s} {state} {detail}")
            del notified[s]
    notified = {k: v for k, v in notified.items() if k in still}
    if notified != before:
        write_notified(notified)
    return EXIT_STALE if turned_stale else 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Model-free manager-loop liveness.")
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--arm", action="store_true", help="record a re-arm (manager, every tick)")
    mode.add_argument("--stop", action="store_true", help="mark a deliberate stand-down")
    mode.add_argument("--check", action="store_true", help="report subjects that turned stale")
    mode.add_argument(
        "--state",
        action="store_true",
        help="print ONE subject's state: OK / STALE / STOPPED / INVALID",
    )
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
        help="goal | topic — auto-detected when omitted (existing record, then the vault's pages)",
    )
    ap.add_argument("--vault", default=".", help="vault ROOT, used only to auto-detect --branch")
    ap.add_argument("--interval", type=int, help="the ScheduleWakeup delay just armed, seconds")
    a = ap.parse_args()
    if a.topic_alias and not a.subject:
        a.subject = a.topic_alias
        print("--topic is deprecated; use --subject", file=sys.stderr)
    elif a.topic_alias and a.subject and a.topic_alias != a.subject:
        ap.error("--topic and --subject disagree")
    if a.check:
        return check()
    if a.state:
        if not a.subject:
            ap.error("--state requires --subject (or the deprecated --topic)")
        a.branch = a.branch or detect_branch(a.subject, a.vault)
        s = slug(a.subject, a.branch)
        state, detail = classify(s, time.time())
        print(f"{state}\t{s}\t{detail}")
        return 0
    if not a.subject:
        ap.error("--subject (or the deprecated --topic) is required with --arm/--stop")
    a.branch = a.branch or detect_branch(a.subject, a.vault)
    if a.arm:
        if not a.interval or a.interval <= 0:
            ap.error("--arm needs --interval > 0")
        return arm(a.subject, a.branch, a.interval)
    return stop(a.subject, a.branch)


if __name__ == "__main__":
    sys.exit(main())
