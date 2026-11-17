#!/usr/bin/env python3
"""Re-arm the exit observers of live manager panes whose writer has died, and card each one.

**Why this is a separate job and not a step in `fleet-keeper.py`.** The keeper is pinned by
`test_fleet_keeper.NoKillPathTest` to hold **no** kill, resume, re-arm or spawn path — an
operator decision of 2026-11-06 (card `7653b7b6`), whose single exception is
`resume_absent_session`, reachable only from `act_on_answer`. Adding an `observe` spawn there
was tried on 2026-11-17 and the pin caught it, correctly. So the re-arm lives here.

**The card is filed before the re-arm, and the re-arm runs only when the card exists.** The
other order hides the death: re-arming returns the pane to `covered`, so a card that failed to
reach the board would leave no trace that the writer ever died — which is the failure this
whole change exists to end.

**This job watches its own death.** A watcher nobody watches is the recursion the parent task
exists to break, so every tick rewrites `~/.claude/state/session-exit/keeper-heartbeat`, and
`exit-record-coverage.py` — which a round already runs — reads that file and reports it when it
goes stale. One existing reader, no new instrument.

Usage:
  exit-observer-keeper.py [--now EPOCH] [--dry-run]
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))

COVERAGE = os.path.join(HERE, "exit-record-coverage.py")
RECORD = os.path.join(HERE, "session-exit-record.py")
ATTENTION_ASK = os.path.join(HERE, "attention-ask.py")

# ⚠️ The SAME override the store owner reads (`session-exit-record.py` `state_dir()`), not a
# second name for it: a keeper pointed at a different directory would watch an empty store and
# report every pane healthy forever.
STATE_DIR = os.path.expanduser(
    os.environ.get("SUPERVISOR_SESSION_EXIT_DIR") or "~/.claude/state/session-exit")
HEARTBEAT = os.path.join(STATE_DIR, "keeper-heartbeat")

# A stable producer identity: a launchd tick has no Claude session to borrow one from.
PRODUCER_ID = "exit-observer-keeper"
SWEEP_TIMEOUT = 180  # the sweep measured ~24 s on an idle host; a busy one must not wedge the tick


def beat(now):
    """Rewrite the liveness file `exit-record-coverage.py` reads. Never raises."""
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        tmp = HEARTBEAT + ".tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            handle.write(f"{int(now)}\n")
        os.replace(tmp, HEARTBEAT)
    except OSError:
        pass


def silent(now):
    """`[{pane, name, session_id, line}]` for manager panes whose writer has gone silent.

    `None` means the sweep could not be run or parsed — a different fault from "nothing is
    wrong", and never reported as one.
    """
    try:
        out = subprocess.run([sys.executable, COVERAGE, "--json"], capture_output=True,
                             text=True, timeout=SWEEP_TIMEOUT)
        doc = json.loads(out.stdout)
    except (subprocess.TimeoutExpired, ValueError, OSError):
        return None
    return [row for row in doc.get("unrecorded", [])
            if "silent exit recorder" in row.get("line", "")]


def identity(pane):
    """`(pid, session, last_beat_epoch)` from the pane's newest live spawn and its log mtime.

    `last_beat_epoch` identifies THIS death: two deaths of the same pane have different final
    heartbeat times, so a card keyed on it is one per death and a re-post inside one death
    collapses onto the card already on the board.
    """
    path = os.path.join(STATE_DIR, f"pane-{pane}.log")
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            lines = [ln.rstrip("\n") for ln in handle if ln.strip()]
        beat_at = int(os.stat(path).st_mtime)
    except OSError:
        return None
    for line in reversed(lines):
        parts = dict(part.split("=", 1) for part in line.split("\t") if "=" in part)
        if parts.get("kind") != "spawn" or parts.get("writer") != "trap":
            continue
        pid = parts.get("pid", "")
        if pid.isdigit() and _alive(int(pid)):
            return int(pid), parts.get("session", "") or "", beat_at
    return None


def _alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def card(row, beat_at, now):
    """File one `ack` card for this death. Returns the item id, or `None` when it did not land."""
    pane = str(row.get("pane", ""))
    payload = (
        f"The exit observer for {row.get('name', '?')} (pane {pane}, session "
        f"{str(row.get('session_id', ''))[:8]}) has died: the pane's last heartbeat is stale, so "
        f"the session runs with no writer and its next death would leave no record. "
        f"The keeper re-arms it once this card exists."
    )
    try:
        out = subprocess.run(
            [sys.executable, ATTENTION_ASK, "post",
             "--dedup-key", f"exit-observer-silent-{pane}-{beat_at}",
             "--payload", payload,
             "--mechanism", "ack",
             "--producer-id", PRODUCER_ID,
             "--producer-kind", "cron"],
            capture_output=True, text=True, timeout=SWEEP_TIMEOUT)
    except (subprocess.TimeoutExpired, OSError):
        return None
    if out.returncode != 0:
        return None
    return (out.stdout or "").strip() or "posted"


def rearm(pane, pid, session):
    """Start a fresh observer for a live pane — the same call the spawn line makes.

    Returns whether it actually ran. A failure is reported by the caller, never swallowed: the
    card is already on the board, so a silent re-arm failure would leave the pane unrecorded
    with a card that says it was re-armed.
    """
    try:
        out = subprocess.run([sys.executable, RECORD, "observe", "--pane", str(pane), "--pid", str(pid),
                              "--session", session], capture_output=True, timeout=SWEEP_TIMEOUT)
    except (subprocess.TimeoutExpired, OSError):
        return False
    return out.returncode == 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--now", type=float, help="epoch seconds (tests only)")
    ap.add_argument("--dry-run", action="store_true", help="report what would happen, act on nothing")
    args = ap.parse_args(argv)
    now = args.now if args.now is not None else time.time()

    if not args.dry_run:
        beat(now)

    rows = silent(now)
    if rows is None:
        print("exit-observer-keeper: coverage sweep failed — cannot tell whether any observer is "
              "dead; not reporting that as fine", file=sys.stderr)
        return 1
    if not rows:
        print("exit-observer-keeper: every manager pane has a live writer")
        return 0

    rc = 0
    for row in rows:
        pane = str(row.get("pane", ""))
        ident = identity(pane)
        if ident is None:
            print(f"exit-observer-keeper: pane {pane} has no live spawn line — not this job's fault",
                  file=sys.stderr)
            continue
        pid, session, beat_at = ident
        if args.dry_run:
            print(f"exit-observer-keeper: would card and re-arm pane {pane} (pid {pid})")
            continue
        item = card(row, beat_at, now)
        if not item:
            # Deliberately no re-arm: see the module docstring.
            print(f"exit-observer-keeper: pane {pane} card did not land — leaving the pane "
                  f"unrecorded rather than hiding the death", file=sys.stderr)
            rc = 1
            continue
        if not rearm(pane, pid, session):
            print(f"exit-observer-keeper: pane {pane} carded ({item}) but the RE-ARM FAILED — it "
                  f"stays unrecorded", file=sys.stderr)
            rc = 1
            continue
        print(f"exit-observer-keeper: pane {pane} carded ({item}) and re-armed (pid {pid})")
    return rc


if __name__ == "__main__":
    sys.exit(main())
