#!/usr/bin/env python3
"""Read-only state probe for `/supervisor:stop` — the lines the stop report prints.

Three reads and nothing else. No writes, no signals, no state mutation:

  session  ~/.claude/state/worker-manager/<session-id>.json   which loop this is
  gate     `ps` + basename match                              the model-free loop,
                                                              which stop must leave
                                                              running untouched
  tick     ~/.claude/state/sweep-gate/<topic>.tick.txt        its freshness — the
                                                              load-bearing evidence
  ledger   ~/.claude/state/open-items/<session-id>.json       what must survive

**The gate probe matches the script ARGUMENT's basename, never a substring of the
command line.** `pgrep -f sweep-gate` reads false positives off any process whose
argv merely *mentions* the path, and a manager's spawn prompt quotes it — measured
2026-09-22, the substring probe returned three pids for one loop, two of them the
worker sessions it had spawned from a prompt naming the script. The basename match
also fails in the safe direction: a path containing a space mis-splits and the probe
under-reports rather than inventing a loop.

The gate loop is armed detached (`setsid nohup …`, so `ppid 1`) and is never
signalled from here — see `docs/fleet-surface.md` § Session end.

**The gate may also be hosted by launchd** (`com.bborbe.sweep-gate-notify`, a
`StartInterval` job). Such a job has no standing pid between ticks, so the `ps`
match alone reports it absent while it is healthy. It counts as present when the
job is loaded (`launchctl print` exits 0) AND its heartbeat is fresh (≤ 2× its
`StartInterval`). Both are required: loaded-but-stale is a wedged job, and a fresh
heartbeat outlives a `bootout` by up to two intervals.

The tick-file slug is the gate's own — copied from `sweep-gate.py`'s `slug()` so the
two agree on the filename rather than on a second guess at it.
"""
import argparse, json, os, plistlib, re, subprocess, sys, time
from datetime import datetime

STATE = os.path.expanduser("~/.claude/state")
INTERPRETERS = {"bash", "sh", "zsh", "dash", "python", "python3"}
GATE_SCRIPTS = {"sweep-gate.py", "sweep-gate-ledger-loop.sh"}
LAUNCHD_LABEL = "com.bborbe.sweep-gate-notify"
LAUNCHD_PLIST = os.path.expanduser(f"~/Library/LaunchAgents/{LAUNCHD_LABEL}.plist")
HEARTBEAT = os.path.join(STATE, "sweep-gate-loop", "manager-layer.heartbeat")
DEFAULT_INTERVAL = 900


def slug(topic: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", topic.lower()).strip("-")


def read_json(path: str) -> dict:
    try:
        with open(path) as fh:
            return json.load(fh)
    except Exception:
        return {}


def gate_processes() -> list:
    """(pid, etime, command) for every live sweep-gate loop, or []."""
    try:
        out = subprocess.run(
            ["ps", "-eo", "pid=,etime=,command="],
            capture_output=True, text=True, timeout=15,
        ).stdout
    except Exception:
        return []
    rows = []
    for line in out.splitlines():
        parts = line.split(None, 2)
        if len(parts) < 3:
            continue
        pid, etime, command = parts
        tokens = command.split()
        if len(tokens) < 2:
            continue
        if os.path.basename(tokens[0]) not in INTERPRETERS:
            continue
        if os.path.basename(tokens[1]) not in GATE_SCRIPTS:
            continue
        rows.append((pid, etime, command))
    return rows


def launchd_loaded(label: str = LAUNCHD_LABEL) -> bool:
    try:
        return subprocess.run(
            ["launchctl", "print", f"gui/{os.getuid()}/{label}"],
            capture_output=True, timeout=15,
        ).returncode == 0
    except Exception:
        return False


def launchd_interval(plist: str = LAUNCHD_PLIST) -> int:
    try:
        with open(plist, "rb") as fh:
            return int(plistlib.load(fh).get("StartInterval") or DEFAULT_INTERVAL)
    except Exception:
        return DEFAULT_INTERVAL


def heartbeat_age(path: str = HEARTBEAT, now: float = None):
    """Seconds since the heartbeat's epoch field, or None when missing/unreadable."""
    try:
        with open(path) as fh:
            epoch = int(fh.read().split()[0])
    except Exception:
        return None
    return int((now if now is not None else time.time()) - epoch)


def launchd_gate(loaded: bool, age, interval: int):
    """(present, detail) for the launchd-hosted gate — loaded AND heartbeat fresh."""
    limit = 2 * interval
    if not loaded:
        return False, f"launchd {LAUNCHD_LABEL} not loaded"
    if age is None:
        return False, f"launchd {LAUNCHD_LABEL} loaded, heartbeat MISSING {HEARTBEAT}"
    if age > limit:
        return False, f"launchd {LAUNCHD_LABEL} loaded, heartbeat STALE {age}s > {limit}s"
    return True, f"launchd {LAUNCHD_LABEL} loaded, heartbeat {age}s ago (limit {limit}s)"


def main() -> int:
    ap = argparse.ArgumentParser(description="Read-only state probe for /supervisor:stop.")
    ap.add_argument(
        "--session",
        default=os.environ.get("CLAUDE_CODE_SESSION_ID", ""),
        help="session id (default: $CLAUDE_CODE_SESSION_ID)",
    )
    args = ap.parse_args()
    sid = args.session

    st = read_json(os.path.join(STATE, "worker-manager", f"{sid}.json")) if sid else {}
    subject = st.get("subject", "")

    print(f"session  {sid[:8] or '(unknown)'}")
    print(
        f"subject  {subject or '(none recorded)'}"
        f" ({st.get('branch') or '—'}, {st.get('vault') or '—'})"
    )

    rows = gate_processes()
    for pid, etime, command in rows:
        print(f"gate     pid {pid}  up {etime}  {command}")
    present, detail = launchd_gate(launchd_loaded(), heartbeat_age(), launchd_interval())
    if present or not rows:
        print(f"gate     {detail}")
    if not rows and not present:
        print("gate     NONE — no sweep-gate loop found; it may never have been armed")

    tick = os.path.join(STATE, "sweep-gate", f"{slug(subject)}.tick.txt") if subject else ""
    if tick and os.path.exists(tick):
        stamp = datetime.fromtimestamp(os.path.getmtime(tick)).isoformat(timespec="seconds")
        age = int(datetime.now().timestamp() - os.path.getmtime(tick))
        print(f"tick     {tick}  {stamp}  ({age}s ago)")
    else:
        print(f"tick     ABSENT {tick}".rstrip())

    ledger = os.path.join(STATE, "open-items", f"{sid}.json")
    items = read_json(ledger).get("items", [])
    opened = sum(1 for i in items if i.get("state") == "open")
    print(f"ledger   {ledger}  open {opened} of {len(items)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
