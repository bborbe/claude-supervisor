#!/usr/bin/env python3
"""Read-only state probe for `/supervisor:stop` — the lines the stop report prints.

Four reads and nothing else. No writes, no signals, no state mutation:

  session    ~/.claude/state/worker-manager/<session-id>.json   which loop this is
  gate       `ps` + basename match                              the model-free loop,
                                                                which stop must leave
                                                                running untouched
  tick       ~/.claude/state/sweep-gate-loop/<vault>/<subject>.tick.txt
                                                                its freshness — the
                                                                load-bearing evidence
  gateledger ~/.claude/state/sweep-gate-loop/<vault>/<subject>.ledger.txt
                                                                the host's own trail
  ledger     ~/.claude/state/open-items/<session-id>.json       what must survive

**Every gate path is per-vault, and the vault comes from the session record** —
lowercased, because the vault-cli config name is lowercase (`personal`) while records
written before 2026-09-23 carry display case (`Personal`), and a strict join resolves
a directory that does not exist. The subject is the record's own, slugged by the
gate's `slug()`.

**The flat `sweep-gate/<topic>.*` tree is not where the gate writes — do not resolve gate
state there.** It was the detached `sweep-gate-ledger-loop.sh` host's state, keyed by topic
alone with no vault segment. That loop was retired when the launchd host landed (2026-09-22
22:20), and the launchd host exports `SWEEP_GATE_STATE_DIR` to write the per-vault tree
instead. The code still supports a detached loop — `GATE_SCRIPTS` names it, and the
`ps` match above reports it — but its state tree is no longer where the gate writes.

⚠️ **Corrected 2026-09-26: the flat tree is NOT dead, and this paragraph used to say it was.**
It read *"the flat tree is retired — do not read it"* and cited *"the flat tree stale for
every topic (verified 2026-09-23 21:06)"*. Re-measured: the tree is **live** —
`attention-routing.tick.txt` written 17:43, `attention-routing.{arms,cadence,json}` at
17:43–17:44 — so something still writes it. What survives from the old claim is the *kind*
of state: the flat tree carries **loop-cadence records** (`.cadence` / `.arms` / `.stopped`,
see `docs/restart-worker.md:83`), not gate state, and none of the three gate scripts resolves
it (`sweep-gate-notify-tick.sh:52` and `sweep-gate-notify-stale.sh:13` both
`BASE="$HOME/.claude/state/sweep-gate-loop"`; `sweep-gate-adhoc.py:49` likewise). Resolving
**gate** state there still finds nothing — but a `.tick.txt` in that tree is real for a live
topic, and **its writer is unidentified**. Treat the writer as open, and never let a
flat-tree absence test stand in for a gate-state check.
Reading the flat path is what made this probe report a live gate as missing.

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

STATE = os.path.expanduser(os.environ.get("STOP_PROBE_STATE_DIR", "~/.claude/state"))
INTERPRETERS = {"bash", "sh", "zsh", "dash", "python", "python3"}
GATE_SCRIPTS = {"sweep-gate.py", "sweep-gate-ledger-loop.sh"}
LAUNCHD_LABEL = "com.bborbe.sweep-gate-notify"
LAUNCHD_PLIST = os.path.expanduser(f"~/Library/LaunchAgents/{LAUNCHD_LABEL}.plist")
DEFAULT_INTERVAL = 900


def slug(topic: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", topic.lower()).strip("-")


def gate_file(vault: str, subject: str, ext: str) -> str:
    """`…/sweep-gate-loop/<vault>/<subject>.<ext>`, or "" when either is unresolved.

    The vault is lowercased — see the module docstring. Returning "" rather than a
    partial join keeps an unresolved record from rendering as a path that was looked
    at and found empty.
    """
    if not vault or not subject:
        return ""
    return os.path.join(STATE, "sweep-gate-loop", vault.lower(), f"{slug(subject)}.{ext}")


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


def heartbeat_age(path: str, now: float = None):
    """Seconds since the heartbeat's epoch field, or None when missing/unreadable."""
    try:
        with open(path) as fh:
            epoch = int(fh.read().split()[0])
    except Exception:
        return None
    return int((now if now is not None else time.time()) - epoch)


def launchd_gate(loaded: bool, age, interval: int, heartbeat_path: str = ""):
    """(present, detail) for the launchd-hosted gate — loaded AND heartbeat fresh.

    `heartbeat_path` is the resolved per-vault path, and it is load-bearing in the
    detail string: a MISSING that does not name the path it looked at is the exact
    defect this probe shipped with, where the path was wrong and the reader could not
    tell. An unresolved record is a third state, never folded into MISSING.
    """
    limit = 2 * interval
    if not loaded:
        return False, f"launchd {LAUNCHD_LABEL} not loaded"
    if not heartbeat_path:
        return False, f"launchd {LAUNCHD_LABEL} loaded, heartbeat UNRESOLVED — no subject record"
    if age is None:
        return False, f"launchd {LAUNCHD_LABEL} loaded, heartbeat MISSING {heartbeat_path}"
    if age > limit:
        return False, f"launchd {LAUNCHD_LABEL} loaded, heartbeat STALE {age}s > {limit}s"
    return True, f"launchd {LAUNCHD_LABEL} loaded, heartbeat {age}s ago (limit {limit}s)"


def file_mtime(path: str):
    """The file's mtime, or None when it cannot be stat'd.

    One stat, returned — never stat-then-stat-again at the call site. The gate
    rewrites its tick file, so a second `getmtime` on a path that vanished in
    between raises out of a probe whose whole contract is to report state without
    failing: `/supervisor:stop` would lose its entire report to a benign race.
    """
    try:
        return os.path.getmtime(path)
    except Exception:
        return None


def last_line(path: str) -> str:
    """The file's last non-blank line, or "" when it cannot be read."""
    try:
        with open(path) as fh:
            lines = [ln.rstrip("\n") for ln in fh if ln.strip()]
    except Exception:
        return ""
    return lines[-1] if lines else ""


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
    vault = st.get("vault", "")

    print(f"session  {sid[:8] or '(unknown)'}")
    print(
        f"subject  {subject or '(none recorded)'}"
        f" ({st.get('branch') or '—'}, {vault or '—'})"
    )

    rows = gate_processes()
    for pid, etime, command in rows:
        print(f"gate     pid {pid}  up {etime}  {command}")
    heartbeat = gate_file(vault, subject, "heartbeat")
    present, detail = launchd_gate(
        launchd_loaded(), heartbeat_age(heartbeat), launchd_interval(), heartbeat
    )
    if present or not rows:
        print(f"gate     {detail}")
    if not rows and not present:
        print("gate     NONE — no sweep-gate loop found; it may never have been armed")

    tick = gate_file(vault, subject, "tick.txt")
    mtime = file_mtime(tick)
    if mtime is not None:
        stamp = datetime.fromtimestamp(mtime).isoformat(timespec="seconds")
        print(f"tick     {tick}  {stamp}  ({int(time.time() - mtime)}s ago)")
    else:
        print(f"tick     ABSENT {tick}".rstrip())

    gateledger = gate_file(vault, subject, "ledger.txt")
    gmtime = file_mtime(gateledger)
    if gmtime is not None:
        print(f"gateledger {gateledger}  {last_line(gateledger)}  ({int(time.time() - gmtime)}s ago)")
    else:
        print(f"gateledger ABSENT {gateledger}".rstrip())

    ledger = os.path.join(STATE, "open-items", f"{sid}.json")
    items = read_json(ledger).get("items", [])
    opened = sum(1 for i in items if i.get("state") == "open")
    print(f"ledger   {ledger}  open {opened} of {len(items)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
