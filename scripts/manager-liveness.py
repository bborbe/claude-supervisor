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
                                    STALE = no marker AND age > 2 x PERIOD + SLACK,
                                    where PERIOD is the manager's observed median
                                    arm-to-arm gap (>=3 gaps), falling back to the
                                    reported interval until that history exists.
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
SLACK = 60  # seconds: covers the tick's own runtime before the re-arm lands
ARMS_KEEP = 12  # re-arm epochs retained per topic — enough for a stable median
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


def observed_interval(s: str):
    """Median arm-to-arm gap in seconds, or None when there is too little history."""
    vals = read_arms(s)[-ARMS_KEEP:]
    if len(vals) < 4:
        return None
    gaps = sorted(b - a for a, b in zip(vals, vals[1:]))
    if len(gaps) < 3:
        return None
    return gaps[len(gaps) // 2]


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
    print(f"armed {s} interval={interval}s")
    return 0


def stop(subject: str, branch: str) -> int:
    s = slug(subject, branch)
    write_atomic(path(s, "stopped"), time.strftime("%Y-%m-%dT%H:%M:%S%z") + "\n")
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
    observed = observed_interval(s)
    if observed:
        limit, basis = 2 * observed + SLACK, f"period={int(observed)}s(observed)"
    else:
        limit, basis = 2 * interval + SLACK, f"period={interval}s(reported)"
    detail = f"age={age}s limit={limit}s {basis}"
    return ("STALE" if age > limit else "OK"), detail


def check(now: float = None) -> int:
    now = time.time() if now is None else now
    try:
        names = sorted(f[: -len(".cadence")] for f in os.listdir(STATE_DIR) if f.endswith(".cadence"))
    except FileNotFoundError:
        names = []
    npath = os.path.join(STATE_DIR, NOTIFIED)
    try:
        with open(npath, encoding="utf-8") as fh:
            notified = set(json.load(fh))
    except (OSError, ValueError):
        notified = set()

    turned_stale = False
    still = set()
    for s in names:
        state, detail = classify(s, now)
        if state in ("STALE", "INVALID"):
            still.add(s)
            if s not in notified:
                print(f"{state} {s} {detail}")
                turned_stale = True
        elif s in notified:
            print(f"RECOVERED {s} {state} {detail}")
    if still != notified:
        write_atomic(npath, json.dumps(sorted(still)) + "\n")
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
    ap.add_argument("--branch", choices=BRANCHES, help="goal | topic — the tier that owns the subject")
    ap.add_argument("--interval", type=int, help="the ScheduleWakeup delay just armed, seconds")
    a = ap.parse_args()
    if a.check:
        return check()
    if a.state:
        if not a.subject or not a.branch:
            ap.error("--state requires --subject and --branch")
        s = slug(a.subject, a.branch)
        state, detail = classify(s, time.time())
        print(f"{state}\t{s}\t{detail}")
        return 0
    if not a.subject or not a.branch:
        ap.error("--subject and --branch are required with --arm/--stop")
    if a.arm:
        if not a.interval or a.interval <= 0:
            ap.error("--arm needs --interval > 0")
        return arm(a.subject, a.branch, a.interval)
    return stop(a.subject, a.branch)


if __name__ == "__main__":
    sys.exit(main())
