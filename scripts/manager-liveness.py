#!/usr/bin/env python3
"""Model-free liveness for manager loops: tell a lapsed manager apart from a stopped one.

A manager loop is one-shot per re-arm (`ScheduleWakeup`), so a missed re-arm (after
`/compact`, a crash, or a tick that ends without re-arming) stops the loop with no
signal. Nothing a peer session can probe (`ListAgents`, sockets, `ps`, transcript
mtime) tells a lapsed manager from a healthy one between ticks, so the manager leaves
evidence on disk itself. This script writes that evidence and checks it.

Three verbs, one file each, all in the sweep-gate state dir:

  --arm   --topic T --interval S   <slug>.cadence = "<S>\\n" and an epoch appended to
                                    <slug>.arms; clears <slug>.stopped. Run by the
                                    manager at EVERY re-arm, so the cadence file's mtime
                                    marks the last re-arm that actually happened and
                                    <slug>.arms is the history the limit is measured
                                    against. The interval is recorded, never assumed:
                                    managers change it at runtime (a 20-min idle tick
                                    was set 2026-09-22), so a fixed limit false-positives.
  --stop  --topic T                <slug>.stopped. Run by `/supervisor:stop`, so a
                                    deliberately stood-down manager is not reported
                                    as a lapse. The next --arm clears it.
  --check                          every <slug>.cadence -> OK / STALE / STOPPED.
                                    STALE = no marker AND age > 2 x PERIOD + SLACK,
                                    where PERIOD is the manager's observed median
                                    arm-to-arm gap (>=3 gaps), falling back to the
                                    reported interval until that history exists.
                                    Prints one line per topic that TURNED stale (or
                                    recovered) since the last check; exits 10 if any
                                    turned stale, else 0. The notified set lives in
                                    liveness-notified.json, so one lapse notifies
                                    once, not on every poll.

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
SLACK = 60  # seconds: covers the tick's own runtime before the re-arm lands
ARMS_KEEP = 12  # re-arm epochs retained per topic — enough for a stable median
EXIT_STALE = 10


def slug(topic: str) -> str:
    # Same slug as sweep-gate.py and stop-probe.py, so the files sit side by side.
    return re.sub(r"[^a-z0-9]+", "-", topic.lower()).strip("-")


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
    """Median arm-to-arm gap in seconds, or None when there is too little history.

    Gaps that span a deliberate stand-down are dropped — the loop was not running,
    so such a gap measures the stop, not the cadence.
    """
    vals = read_arms(s)[-ARMS_KEEP:]
    if len(vals) < 4:
        return None
    stop_at = None
    if os.path.exists(path(s, "stopped")):
        try:
            stop_at = os.path.getmtime(path(s, "stopped"))
        except OSError:
            stop_at = None
    gaps = sorted(
        b - a
        for a, b in zip(vals, vals[1:])
        if not (stop_at is not None and a < stop_at < b)
    )
    if len(gaps) < 3:
        return None
    return gaps[len(gaps) // 2]


def arm(topic: str, interval: int) -> int:
    s = slug(topic)
    write_atomic(path(s, "cadence"), f"{interval}\n")
    arms = (read_arms(s) + [time.time()])[-ARMS_KEEP:]
    write_atomic(path(s, "arms"), "".join(f"{v:.0f}\n" for v in arms))
    try:
        os.remove(path(s, "stopped"))
    except FileNotFoundError:
        pass
    print(f"armed {s} interval={interval}s")
    return 0


def stop(topic: str) -> int:
    s = slug(topic)
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
    mode.add_argument("--check", action="store_true", help="report topics that turned stale")
    ap.add_argument("--topic")
    ap.add_argument("--interval", type=int, help="the ScheduleWakeup delay just armed, seconds")
    a = ap.parse_args()
    if a.check:
        return check()
    if not a.topic:
        ap.error("--topic is required with --arm/--stop")
    if a.arm:
        if not a.interval or a.interval <= 0:
            ap.error("--arm needs --interval > 0")
        return arm(a.topic, a.interval)
    return stop(a.topic)


if __name__ == "__main__":
    sys.exit(main())
